"""编排层：管理多个相互独立的监控、报告与配置。

单独成层的原因：会话不持有报告，报告不持有状态，配置不关心监控生命周期，
而"停止时产出报告""新建/移除监控时维护会话集合""当前查看哪个监控"
都属于跨这些组件的业务语义。

多监控模型：每个"监控"是一个独立槽位（一个 MonitorSession），
可以在新建后保持空白，稍后填入链接再开始采集；各槽位互不影响，
"切换"只是切换查看，采集在后台继续。所有会话共享同一个 B站 客户端，
限流冷却因此对所有监控同时生效。
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

from . import report as report_module
from . import reports as report_store
from .collector import BiliClient, CollectError
from .session import (
    DEFAULT_INTERVAL,
    MonitorSession,
    clamp_interval,
    empty_snapshot,
    prepare_snapshot,
)
from .settings import Settings

# 会话内报告的标记前缀，报告页用它在两类来源间区分
LIVE_PREFIX = "live:"


class MonitorController:
    MAX_MONITORS = 5
    # 监控移除后仍保留的报告份数，供回退查看
    MAX_ARCHIVED = 5

    def __init__(self, client: BiliClient, settings: Settings, report_dir: str) -> None:
        self._client = client
        self._settings = settings
        self._report_dir = report_dir
        self._monitors: dict[str, MonitorSession] = {}
        self._order: list[str] = []
        self._active_id: str | None = None
        self._next_id = 1
        self._reports: dict[str, dict[str, Any]] = {}
        self._documents: dict[str, dict[str, Any]] = {}
        # 已移除但仍保留报告的监控 id（最近若干份），键与 _reports 一致
        self._orphans: list[str] = []
        # 新建监控时继承的采样周期，避免无监控时改周期被丢弃
        self._interval = DEFAULT_INTERVAL

    # ---------------------------------------------------------------- 查询

    def state(
        self, since: int | None = None, monitor_id: str | None = None
    ) -> dict[str, Any]:
        """整体状态：指定（或当前）监控的会话快照平铺在顶层，附全部监控概要。"""
        session = (
            self._monitors.get(monitor_id) if monitor_id else self._active_session()
        )
        monitor_key = monitor_id or self._active_id
        payload: dict[str, Any] = (
            session.snapshot(since) if session else empty_snapshot(self._interval)
        )
        payload["active_monitor"] = monitor_key
        payload["monitors"] = self._monitor_summaries()
        payload["report"] = self._report_for(monitor_key)
        payload["report_dir"] = self._report_dir
        payload["settings"] = self._settings.as_dict()
        payload["cooldown_remaining"] = int(round(self._client.cooldown_remaining()))
        return payload

    def report_document(self, monitor_id: str | None = None) -> dict[str, Any] | None:
        """完整报告对象，供下载接口序列化。

        指定 monitor_id 时直接按该 id 取，监控已移除也能取到；
        不指定时取当前监控，没有则回退最近一份已移除监控的报告。
        """
        if monitor_id:
            return self._documents.get(monitor_id)

        session = self._active_session()
        key = session.monitor_id if session else None
        if key and key in self._documents:
            return self._documents[key]
        if self._orphans:
            return self._documents.get(self._orphans[-1])
        return None

    def _report_for(self, monitor_key: str | None) -> dict[str, Any] | None:
        """当前监控的报告摘要；该监控没有时回退到最近一份已移除监控的报告。"""
        if monitor_key and monitor_key in self._reports:
            return self._reports[monitor_key]
        if self._orphans:
            archived = self._reports.get(self._orphans[-1])
            if archived:
                return dict(archived, archived=True)
        return None

    def settings(self) -> dict[str, Any]:
        return self._settings.as_dict()

    # ---------------------------------------------------------------- 操作

    def create(self) -> dict[str, Any]:
        """新建一个空白监控并切换过去。"""
        if len(self._monitors) >= self.MAX_MONITORS:
            raise CollectError(
                f"最多同时保留 {self.MAX_MONITORS} 个监控，请先移除部分监控"
            )
        monitor_id = "m%d" % self._next_id
        self._next_id += 1
        session = MonitorSession(self._client)
        session.monitor_id = monitor_id
        if self._interval != DEFAULT_INTERVAL:
            session.set_interval(self._interval)
        self._monitors[monitor_id] = session
        self._order.append(monitor_id)
        self._active_id = monitor_id
        return self.state()

    def start(
        self,
        monitor_id: Any = None,
        source: Any = None,
        interval: Any = None,
        align_clock: Any = False,
    ) -> dict[str, Any]:
        """启动（或重新启动）指定监控：绑定链接并开始采集。"""
        session = self._session_for(monitor_id)
        if session is None:
            # 列表为空时直接自动新建，用户不必先手动创建
            if not self._order:
                self.create()
                session = self._active_session()
            else:
                raise CollectError("监控不存在，请先新建监控")

        if interval is None:
            # 未指定周期时沿用该监控当前周期，避免调用方漏传就报错
            interval = session.interval if session else self._interval

        # 校验与首次探测在改状态之前完成，失败时监控保持原样
        prepared = prepare_snapshot(self._client, str(source or ""), interval)

        previous_report = None
        if session.is_running:
            # 先停掉当前采集并结算报告，再以新参数重新开始
            stopped = session.stop()
            previous_report = self._build_report(session, stopped)

        session.begin(prepared, bool(align_clock))
        self._active_id = session.monitor_id
        payload = self.state()
        payload["previous_report"] = previous_report
        return payload

    def stop(self, monitor_id: str | None = None) -> dict[str, Any]:
        session = self._session_for(monitor_id)
        stopped_report = None
        if session and session.is_running:
            snapshot = session.stop()
            stopped_report = self._build_report(session, snapshot)

        payload = self.state()
        # 停止的可能不是当前监控，这种情况下回报视图仍应是它自己的报告
        if stopped_report is not None:
            payload["report"] = stopped_report
        return payload

    def stop_all(self) -> None:
        """退出前结算所有运行中的监控。

        关闭窗口时若只停当前监控，其余会话的样本会随进程消失，
        自动保存也就落不下报告。
        """
        for monitor_id in list(self._order):
            session = self._monitors.get(monitor_id)
            if session is None or not session.is_running:
                continue
            try:
                self._build_report(session, session.stop())
            except Exception:  # 单个监控结算失败不应阻止进程退出
                continue

    def remove(self, monitor_id: str | None = None) -> dict[str, Any]:
        key = monitor_id or self._active_id
        session = self._monitors.get(key) if key else None
        if session is None or key is None:
            raise CollectError("监控不存在")

        if session.is_running:
            snapshot = session.stop()
            # 移除运行中的监控时保留其报告，供查看与下载
            self._build_report(session, snapshot)

        # 无论运行与否，只要该监控有报告就登记为可回退的归档报告
        if key in self._reports and key not in self._orphans:
            self._orphans.append(key)
            del self._orphans[:-self.MAX_ARCHIVED]

        del self._monitors[key]
        self._order.remove(key)
        if self._active_id == key:
            self._active_id = self._order[0] if self._order else None

        # 只保留仍在归档窗口内的报告文档，否则反复增删监控会让内存只增不减
        keep = set(self._monitors) | set(self._orphans)
        for stale in [item for item in self._documents if item not in keep]:
            self._documents.pop(stale, None)
            self._reports.pop(stale, None)
        return self.state()

    def select(self, monitor_id: str | None = None) -> dict[str, Any]:
        key = monitor_id or (self._order[0] if self._order else None)
        if not key or key not in self._monitors:
            raise CollectError("监控不存在")
        self._active_id = key
        return self.state()

    def set_interval(self, interval: Any) -> dict[str, Any]:
        session = self._active_session()
        if session:
            self._interval = session.set_interval(interval)["interval"]
        else:
            # 没有监控时也要记住周期，供新建监控时继承
            self._interval = clamp_interval(interval)
        return self.state()

    def update_settings(
        self,
        auto_save: Any = None,
        auto_save_dir: Any = None,
        align_clock: Any = None,
        import_dirs: Any = None,
    ) -> dict[str, Any]:
        enable = None if auto_save is None else bool(auto_save)
        directory = None if auto_save_dir is None else str(auto_save_dir)
        align = None if align_clock is None else bool(align_clock)
        dirs = None
        if import_dirs is not None:
            dirs = [str(item) for item in import_dirs] if isinstance(import_dirs, list) else []
        return self._settings.update(enable, directory, align, dirs)

    # ---------------------------------------------------------------- 报告页

    def reports_page(self) -> dict[str, Any]:
        """报告页数据：先同步自动导入目录，再返回列表与目录配置。"""
        synced = self._sync_import_dirs()
        return {
            "saved": report_store.list_saved(self._report_dir),
            "live": self._live_reports(),
            "report_dir": self._report_dir,
            "import_dirs": self._effective_import_dirs(),
            "synced": len(synced),
        }

    def _effective_import_dirs(self) -> list[dict[str, Any]]:
        """自动导入目录：仓库固定包含，自动保存目录自动纳入，其余为用户添加。"""
        repo = os.path.abspath(self._report_dir)
        result: list[dict[str, Any]] = [
            {"path": self._report_dir, "name": "仓库目录", "fixed": True, "source": "repo"}
        ]
        seen = {repo.lower()}

        auto_dir = self._settings.auto_save_dir()
        if auto_dir:
            key = os.path.abspath(auto_dir).lower()
            if key not in seen:
                seen.add(key)
                result.append(
                    {
                        "path": auto_dir,
                        "name": "自动保存目录",
                        "fixed": True,
                        "source": "auto_save",
                    }
                )

        for item in self._settings.import_dirs():
            key = os.path.abspath(item).lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(
                {
                    "path": item,
                    "name": os.path.basename(item.rstrip("\\/")) or item,
                    "fixed": False,
                    "source": "user",
                }
            )
        return result

    def _sync_import_dirs(self) -> list[dict[str, Any]]:
        """把自动导入目录里仓库尚未收录的报告复制进来（内容重复的会跳过）。"""
        targets = [
            item["path"]
            for item in self._effective_import_dirs()
            if os.path.isdir(item["path"])
        ]
        if not targets:
            return []
        saved, _ = report_store.import_paths(self._report_dir, targets)
        return saved

    def import_reports(self, paths: Any) -> dict[str, Any]:
        if not paths:
            raise CollectError("没有选择任何文件或目录")
        imported, skipped = report_store.import_paths(self._report_dir, paths)
        return {
            "imported": imported,
            "skipped": skipped,
            "saved": report_store.list_saved(self._report_dir),
        }

    def export_reports(
        self, names: Any = None, live_ids: Any = None, target_dir: Any = None
    ) -> dict[str, Any]:
        """导出报告。指定目录时写入磁盘，未指定时返回链接由浏览器逐个下载。"""
        name_list = [str(item) for item in (names or [])]
        live_list = [str(item) for item in (live_ids or [])]
        if not name_list and not live_list:
            raise CollectError("请先选择要导出的报告")

        target = str(target_dir).strip() if target_dir else ""

        # 导出到仓库目录只会把文件复制成 -2 副本，直接拦掉
        if target and os.path.abspath(target).lower() == os.path.abspath(self._report_dir).lower():
            raise CollectError("导出目录不能是 reports 仓库本身，仓库内的报告无需再导出")

        if not target:
            links: list[dict[str, str]] = []
            for name in name_list:
                links.append({"name": name, "url": "/api/report?name=" + quote(name)})
            for monitor_id in live_list:
                document = self._documents.get(monitor_id)
                if not document:
                    continue
                links.append(
                    {
                        "name": report_module.filename(document),
                        "url": "/api/report?monitor_id=" + quote(monitor_id),
                    }
                )
            return {"mode": "browser", "count": len(links), "links": links}

        written: list[str] = []
        if name_list:
            written.extend(report_store.export(self._report_dir, name_list, target))
        for monitor_id in live_list:
            document = self._documents.get(monitor_id)
            if document:
                written.append(report_store.write_document(target, document))

        return {
            "mode": "disk",
            "count": len(written),
            "exported": written,
            "target_dir": target,
        }

    def report_file(self, name: Any) -> dict[str, Any]:
        """按仓库文件名读取报告，供下载接口使用。"""
        return report_store.load_saved(self._report_dir, str(name))

    def delete_report(self, name: Any) -> dict[str, Any]:
        if not name:
            raise CollectError("未指定要删除的报告")
        report_store.remove(self._report_dir, str(name))
        return {"saved": report_store.list_saved(self._report_dir)}

    def save_live_reports(self) -> dict[str, Any]:
        """把会话内尚未落盘的报告写入仓库目录。"""
        existing = {
            report_store.signature_of(document)
            for document in report_store.load_all(self._report_dir)
        }

        written: list[str] = []
        for document in list(self._documents.values()):
            signature = report_store.signature_of(document)
            if signature in existing:
                continue
            written.append(report_store.write_document(self._report_dir, document))
            existing.add(signature)

        return {
            "saved_count": len(written),
            "paths": written,
            "saved": report_store.list_saved(self._report_dir),
            "live": self._live_reports(),
        }

    def report_detail(self, key: Any) -> dict[str, Any]:
        text = str(key or "")
        if text.startswith(LIVE_PREFIX):
            document = self._documents.get(text[len(LIVE_PREFIX):])
            if not document:
                raise CollectError("该会话报告不存在或已过期")
            return document
        return report_store.load_saved(self._report_dir, text)

    def _live_reports(self) -> list[dict[str, Any]]:
        """会话内未保存的报告，含已移除监控保留的归档报告。

        已经落到 reports/ 里的不再算「未保存」，否则同一份报告会出现两次。
        """
        saved_signatures = {
            report_store.signature_of(document)
            for document in report_store.load_all(self._report_dir)
        }

        result: list[dict[str, Any]] = []
        seen: set[str] = set()

        def collect(monitor_id: str, archived: bool) -> None:
            document = self._documents.get(monitor_id)
            if not document:
                return
            if report_store.signature_of(document) in saved_signatures:
                return
            summary = report_store.summarize(document, LIVE_PREFIX + monitor_id, "live")
            summary["monitor_id"] = monitor_id
            summary["archived"] = archived
            result.append(summary)

        for monitor_id in self._order:
            seen.add(monitor_id)
            collect(monitor_id, False)

        for monitor_id in self._orphans:
            if monitor_id in seen:
                continue
            collect(monitor_id, True)

        result.sort(key=lambda item: item["generated_at"], reverse=True)
        return result

    # ---------------------------------------------------------------- 内部

    def _active_session(self) -> MonitorSession | None:
        return self._monitors.get(self._active_id) if self._active_id else None

    def _session_for(self, monitor_id: str | None) -> MonitorSession | None:
        if monitor_id:
            return self._monitors.get(monitor_id)
        return self._active_session()

    def _monitor_summaries(self) -> list[dict[str, Any]]:
        summaries = []
        for monitor_id in self._order:
            session = self._monitors[monitor_id]
            snap = session.snapshot()
            video = snap.get("video") or {}
            configured = bool(video)
            summaries.append(
                {
                    "id": monitor_id,
                    "configured": configured,
                    "bvid": video.get("bvid") or "",
                    "title": video.get("title") or "未配置监控",
                    "cover": video.get("cover") or "",
                    "url": video.get("url") or "",
                    "running": snap["running"],
                    "sample_count": snap["sample_count"],
                    "last_error": snap["last_error"],
                }
            )
        return summaries

    def _build_report(
        self, session: MonitorSession, snapshot: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """有样本才产出报告。默认开启自动保存，未指定目录时落到仓库目录。"""
        snapshot = snapshot or session.snapshot()
        if not snapshot.get("sample_count"):
            return None

        document = report_module.build(session.context())
        self._documents[session.monitor_id] = document

        summary: dict[str, Any] = {
            "name": report_module.filename(document),
            "generated_at": document["generated_at"],
            "sample_count": document["monitor"]["sample_count"],
            "duration_seconds": document["monitor"]["duration_seconds"],
            "saved_path": None,
        }

        if self._settings.auto_save_enabled():
            # 目录留空表示用仓库目录，这样报告生成后立刻能在「查看报告」里看到
            directory = self._settings.auto_save_dir() or self._report_dir
            try:
                summary["saved_path"] = report_module.write(directory, document)
            except OSError as exc:
                summary["save_error"] = str(exc)

        self._reports[session.monitor_id] = summary
        return summary
