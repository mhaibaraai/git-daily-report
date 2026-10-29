"""报告文件的进程间互斥，避免网页与 CLI 同时写入。"""

from contextlib import contextmanager
from pathlib import Path

import fcntl


@contextmanager
def report_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("此报告正被其他操作使用，请稍后重试") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
