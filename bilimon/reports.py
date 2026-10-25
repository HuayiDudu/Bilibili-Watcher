"""报告仓库：管理 reports/ 目录下的报告文件。

只做文件读写与结构校验，不涉及 HTTP、会话状态与界面。
导入统一按 monitor_{id}_{bvid}_{time}.json 命名，重名追加 -2、-3。
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Any

from .report import filename as canonical_filename

REPORT_TYPE = "bilibili_video_monitor"

# 记录「仓库内文件名 → 来源目录」，用于按目录筛选
ORIGIN_FILE = ".origins.json"


def validate(document: Any) -> str | None:
    """结构校验，返回错误描述；None 表示可用。"""
    if not isinstance(document, dict):
        return "不是 JSON 对象"
    if document.get("report_type") != REPORT_TYPE:
        return "不是本工具生成的报告"
    if not isinstance(document.get("video"), dict) or not document["video"]:
        return "缺少 video 段"
    if not isinstance(document.get("samples"), list):
        return "缺少 samples 段"
    return None


def signature_of(document: dict[str, Any]) -> tuple[str, str]:
    """报告内容签名：同一视频、同一生成时刻视为同一份报告。"""
    video = document.get("video") or {}
    return (video.get("bvid") or "", document.get("generated_at") or "")


def summarize(document: dict[str, Any], name: str, source: str, path: str | None = None) -> dict[str, Any]:
    """报告摘要，供列表展示。"""
    video = document.get("video") or {}
    monitor = document.get("monitor") or {}
    samples = document.get("samples") or []
    return {
        "name": name,
        "source": source,
        "path": path,
        "bvid": video.get("bvid") or "",
        "title": video.get("title") or "",
        "owner": video.get("owner") or "",
        "pubdate": video.get("pubdate") or 0,
        "cover": video.get("cover") or "",
        "monitor_id": monitor.get("id") or "",
        "generated_at": document.get("generated_at") or "",
        "sample_count": monitor.get("sample_count") or len(samples),
        "duration_seconds": monitor.get("duration_seconds") or 0,
        "interval": monitor.get("interval_seconds"),
        "size": os.path.getsize(path) if path and os.path.isfile(path) else 0,
    }


def list_saved(report_dir: str) -> list[dict[str, Any]]:
    """仓库内全部合法报告，按生成时间倒序。"""
    if not os.path.isdir(report_dir):
        return []

    origins = load_origins(report_dir)
    entries: list[dict[str, Any]] = []
    for name in os.listdir(report_dir):
        if not name.lower().endswith(".json") or name == ORIGIN_FILE:
            continue
        path = os.path.join(report_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError):
            continue
        if validate(document):
            continue
        entry = summarize(document, name, "file", path)
        entry["origin"] = origins.get(name, "")
        entries.append(entry)

    entries.sort(key=lambda item: item["generated_at"], reverse=True)
    return entries


def load_origins(report_dir: str) -> dict[str, str]:
    """读取来源索引；文件缺失或损坏时返回空表。"""
    path = os.path.join(report_dir, ORIGIN_FILE)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_origins(report_dir: str, origins: dict[str, str]) -> None:
    path = os.path.join(report_dir, ORIGIN_FILE)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(origins, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass


def resolve_path(report_dir: str, name: str) -> str:
    """校验文件名并返回绝对路径，阻止路径穿越。"""
    if not name or os.path.basename(name) != name:
        raise ValueError("非法文件名")
    path = os.path.join(report_dir, name)
    if not os.path.isfile(path):
        raise ValueError("报告不存在")
    return path


def load_saved(report_dir: str, name: str) -> dict[str, Any]:
    with open(resolve_path(report_dir, name), "r", encoding="utf-8") as handle:
        document = json.load(handle)
    error = validate(document)
    if error:
        raise ValueError(error)
    return document


def _unique_name(report_dir: str, document: dict[str, Any]) -> str:
    base = canonical_filename(document)
    stem, ext = os.path.splitext(base)
    candidate = base
    index = 2
    while os.path.exists(os.path.join(report_dir, candidate)):
        candidate = f"{stem}-{index}{ext}"
        index += 1
    return candidate


def import_paths(report_dir: str, paths: list[str]) -> tuple[list[dict], list[dict]]:
    """导入文件或目录中的报告，返回 (成功, 跳过)。

    目录只扫描其下的 .json（不递归），文件则逐个校验。
    两种重复会被跳过：源文件已在仓库目录内、仓库里已有内容相同的报告。
    """
    os.makedirs(report_dir, exist_ok=True)
    root = os.path.abspath(report_dir)
    existing = {signature_of(item) for item in load_all(report_dir)}
    origins = load_origins(report_dir)

    # 记住每个候选文件来自哪个目录，导入后写进来源索引
    candidates: list[tuple[str, str]] = []
    for raw in paths:
        path = os.path.abspath(str(raw))
        if os.path.isdir(path):
            for name in sorted(os.listdir(path)):
                if name.lower().endswith(".json"):
                    candidates.append((os.path.join(path, name), path))
        elif os.path.isfile(path):
            candidates.append((path, os.path.dirname(path)))

    saved: list[dict] = []
    skipped: list[dict] = []
    for path, origin in candidates:
        name = os.path.basename(path)

        # 源文件就在仓库里，再导入只会复制出一个 -2
        if os.path.dirname(path).lower() == root.lower():
            skipped.append({"name": name, "reason": "已在 reports 目录中"})
            continue

        try:
            with open(path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError):
            skipped.append({"name": name, "reason": "读取或解析失败"})
            continue

        error = validate(document)
        if error:
            skipped.append({"name": name, "reason": error})
            continue

        signature = signature_of(document)
        if signature in existing:
            skipped.append({"name": name, "reason": "仓库中已有相同报告"})
            continue

        target_name = _unique_name(report_dir, document)
        target = os.path.join(report_dir, target_name)
        temp = target + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
        os.replace(temp, target)
        existing.add(signature)

        if origin.lower() != root.lower():
            origins[target_name] = origin

        entry = summarize(document, target_name, "file", target)
        entry["origin"] = origins.get(target_name, "")
        saved.append(entry)

    if saved:
        _save_origins(report_dir, origins)

    return saved, skipped


def load_all(report_dir: str) -> list[dict[str, Any]]:
    """仓库内全部合法报告文档，供去重时取签名。"""
    documents: list[dict[str, Any]] = []
    if not os.path.isdir(report_dir):
        return documents
    for name in os.listdir(report_dir):
        if not name.lower().endswith(".json") or name == ORIGIN_FILE:
            continue
        try:
            with open(os.path.join(report_dir, name), "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError):
            continue
        if validate(document) is None:
            documents.append(document)
    return documents


def export(report_dir: str, names: list[str], target_dir: str) -> list[str]:
    """把仓库内的报告复制到目标目录，重名自动追加序号。"""
    os.makedirs(target_dir, exist_ok=True)
    written: list[str] = []
    for name in names:
        source = resolve_path(report_dir, name)
        destination = os.path.join(target_dir, name)
        if os.path.exists(destination):
            stem, ext = os.path.splitext(name)
            index = 2
            while os.path.exists(os.path.join(target_dir, f"{stem}-{index}{ext}")):
                index += 1
            destination = os.path.join(target_dir, f"{stem}-{index}{ext}")
        shutil.copy2(source, destination)
        written.append(destination)
    return written


def remove(report_dir: str, name: str) -> None:
    os.remove(resolve_path(report_dir, name))
    origins = load_origins(report_dir)
    if name in origins:
        origins.pop(name)
        _save_origins(report_dir, origins)


def write_document(target_dir: str, document: dict[str, Any]) -> str:
    """把内存中的报告直接写到目标目录（用于导出会话内未保存的报告）。"""
    os.makedirs(target_dir, exist_ok=True)
    name = canonical_filename(document)
    path = os.path.join(target_dir, name)
    if os.path.exists(path):
        stem, ext = os.path.splitext(name)
        index = 2
        while os.path.exists(os.path.join(target_dir, f"{stem}-{index}{ext}")):
            index += 1
        path = os.path.join(target_dir, f"{stem}-{index}{ext}")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
    return path

