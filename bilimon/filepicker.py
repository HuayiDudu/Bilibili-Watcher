"""目录浏览：给内置文件选择器提供列表数据。

只读，不修改任何文件，也不判断内容是否是可导入的报告。
"""

from __future__ import annotations

import os
from typing import Any

from .collector import CollectError

# 浏览时略过这些目录，它们在文件选择场景里没有意义
SKIP_DIRS = {
    "$recycle.bin",
    "system volume information",
    "node_modules",
    ".git",
    "__pycache__",
}


def list_directory(
    path: str | None = None,
    mode: str = "any",
    exts: list[str] | None = None,
) -> dict[str, Any]:
    """列出目录内容，供统一的文件/目录选择器使用。

    mode 决定可选目标：dirs 只列目录，files 只列匹配的文件（目录仍用于导航），
    any 两者都列。exts 为空时不限制扩展名。path 为空时返回「此电脑」的盘符列表。
    """
    target = (path or "").strip()
    if not target:
        return _root_view()

    target = os.path.abspath(target)

    if not os.path.isdir(target):
        raise CollectError("目录不存在或无法访问")

    parent = os.path.dirname(target)
    if parent == target:
        parent = ""  # 已经在盘符根，上一级回到「此电脑」

    normalized_mode = mode if mode in ("any", "dirs", "files") else "any"
    allow_files = normalized_mode != "dirs"
    allowed = {
        ("." + item.strip().lstrip(".")).lower()
        for item in (exts or [])
        if item.strip()
    }

    dirs: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    try:
        with os.scandir(target) as entries:
            for entry in entries:
                name = entry.name
                if name.startswith(".") or name.lower() in SKIP_DIRS:
                    continue
                try:
                    if entry.is_dir():
                        dirs.append({"name": name, "path": entry.path})
                    elif allow_files and (
                        not allowed or os.path.splitext(name)[1].lower() in allowed
                    ):
                        files.append(
                            {
                                "name": name,
                                "path": entry.path,
                                "size": entry.stat().st_size,
                            }
                        )
                except OSError:
                    continue
    except PermissionError as exc:
        raise CollectError(f"没有访问权限：{target}") from exc
    except OSError as exc:
        raise CollectError(f"读取目录失败：{exc}") from exc

    dirs.sort(key=lambda item: item["name"].lower())
    files.sort(key=lambda item: item["name"].lower())

    return {
        "path": target,
        "parent": parent,
        "isRoot": False,
        "mode": normalized_mode,
        "drives": _drives(),
        "dirs": dirs,
        "files": files,
    }


def _root_view() -> dict[str, Any]:
    """「此电脑」视图：把盘符当作可进入的目录列出。"""
    dirs = []
    for letter in _drives():
        name = letter.rstrip("\\/")
        dirs.append({"name": f"{name} 盘", "path": letter, "drive": True})

    return {
        "path": "",
        "parent": "",
        "isRoot": True,
        "mode": "any",
        "drives": _drives(),
        "dirs": dirs,
        "files": [],
    }


def _drives() -> list[str]:
    """可用盘符，用于在浏览时切换磁盘。"""
    try:
        return list(os.listdrives())
    except AttributeError:  # 非 Windows 或旧版本 Python
        return []
