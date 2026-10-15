from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from agent.util import PlanError


def atomic_json(path: Path, payload: dict, *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False, default=str) + "\n"
    if immutable and path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != json.loads(text):
            raise PlanError(f"不可覆寫已凍結紀錄：{path}")
        return
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def execution_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / ".daily.lock"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise PlanError(f"已有執行鎖 {path}；先確認沒有其他程序，再依排錯文件清除過期鎖") from exc
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)
