from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as tmp:
            tmp.write(content)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


def atomic_write_json(path: Path, data: Any) -> None:
    def default(value: Any) -> Any:
        if hasattr(value, "value"):
            return value.value
        if isinstance(value, Path):
            return str(value)
        if is_dataclass(value):
            return asdict(value)
        raise TypeError(f"Cannot serialize {type(value)!r}")

    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False, default=default) + "\n")


def atomic_copy(src: Path, dst: Path) -> None:
    ensure_dir(dst.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=dst.parent)
    os.close(fd)
    try:
        shutil.copyfile(src, tmp_name)
        os.replace(tmp_name, dst)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


def read_text_if_exists(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def safe_cmd_path(p: Path | str) -> str:
    s = str(p)
    if os.name == "nt" and os.path.exists("X:\\"):
        s = s.replace("R:\\Đồ án", "X:").replace("R:/Đồ án", "X:")
    return s


def safe_cmd_path_obj(p: Path | str) -> Path:
    return Path(safe_cmd_path(p))
