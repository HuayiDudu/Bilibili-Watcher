"""配置层：自动保存、自动导入目录与整点采样，持久化到 config.json。

只负责读写配置，不涉及报告生成与 HTTP。
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

CONFIG_NAME = "config.json"

_DEFAULTS: dict[str, Any] = {
    # 默认开启：报告生成后自动落到仓库目录，无需手动干预
    "auto_save": True,
    "auto_save_dir": "",
    "align_clock": False,
    "import_dirs": [],
}


class Settings:
    def __init__(self, base_dir: str) -> None:
        self._path = os.path.join(base_dir, CONFIG_NAME)
        self._lock = threading.RLock()
        self._data = self._load()

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data, import_dirs=list(self._data["import_dirs"]))

    def update(
        self,
        auto_save: bool | None = None,
        auto_save_dir: str | None = None,
        align_clock: bool | None = None,
        import_dirs: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if auto_save is not None:
                self._data["auto_save"] = bool(auto_save)
            if auto_save_dir is not None:
                self._data["auto_save_dir"] = str(auto_save_dir).strip()
            if align_clock is not None:
                self._data["align_clock"] = bool(align_clock)
            if import_dirs is not None:
                self._data["import_dirs"] = _clean_dirs(import_dirs)
            self._save()
            return dict(self._data, import_dirs=list(self._data["import_dirs"]))

    def auto_save_dir(self) -> str | None:
        with self._lock:
            directory = self._data["auto_save_dir"]
            return directory or None

    def auto_save_enabled(self) -> bool:
        with self._lock:
            return bool(self._data["auto_save"])

    def import_dirs(self) -> list[str]:
        with self._lock:
            return list(self._data["import_dirs"])

    # ---------------------------------------------------------------- 内部

    def _load(self) -> dict[str, Any]:
        data = dict(_DEFAULTS)
        data["import_dirs"] = []
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return data

        if isinstance(raw, dict):
            # 只覆盖配置里真正出现过的键，缺省的沿用默认值
            if "auto_save" in raw:
                data["auto_save"] = bool(raw["auto_save"])
            directory = raw.get("auto_save_dir")
            data["auto_save_dir"] = directory.strip() if isinstance(directory, str) else ""
            if "align_clock" in raw:
                data["align_clock"] = bool(raw["align_clock"])
            data["import_dirs"] = _clean_dirs(raw.get("import_dirs"))
        return data

    def _save(self) -> None:
        try:
            with open(self._path, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2)
        except OSError:
            pass


def _clean_dirs(value: Any) -> list[str]:
    """去掉空项、去重并保持顺序。"""
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result
