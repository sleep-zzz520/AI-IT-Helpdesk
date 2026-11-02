"""Same-host lifetime guard: stop every backend before collecting immutable generations."""
import fcntl
from contextlib import contextmanager
from pathlib import Path

from app.config import settings


@contextmanager
def service_guard(*, exclusive=False):
    path = Path(settings.KB_VECTOR_DIR) / '.maintenance.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('Maintenance lock must not be a symlink')
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('知识库维护需要停止所有后端服务；服务启动也不能与维护并发') from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
