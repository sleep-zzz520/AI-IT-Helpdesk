"""单机非阻塞准入：同会话互斥、总执行数有上限，锁随进程退出自动释放。"""
import fcntl
import hashlib
import threading
from pathlib import Path

from fastapi import HTTPException

from app.config import settings


class Lease:
    def __init__(self, files):
        self.files = files
        self._guard = threading.Lock()

    def release(self):
        with self._guard:
            for handle in self.files:
                handle.close()
            self.files.clear()


def _try_lock(name: str):
    root = Path(settings.AGENT_LOCK_DIR)
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / name).open("a+b")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def acquire_conversation(tenant_id: int, conv_id: int) -> Lease:
    # 不删除锁文件：unlink 后其他进程可能锁到新 inode，导致互斥失效。
    key = hashlib.sha256(f"{tenant_id}:{conv_id}".encode()).hexdigest()
    handle = _try_lock(f"conversation-{key}.lock")
    if handle is None:
        raise HTTPException(409, "该会话已有请求正在处理，请等待完成后重试", headers={"Retry-After": "2"})
    return Lease([handle])


def acquire_slot() -> Lease:
    # ponytail: 固定数量锁槽保证同机多进程有界执行，无等待队列；多主机不能共用此保证。
    for index in range(settings.AGENT_MAX_CONCURRENT):
        handle = _try_lock(f"slot-{index}.lock")
        if handle is not None:
            return Lease([handle])
    raise HTTPException(503, "Agent 当前繁忙，请稍后重试", headers={"Retry-After": "2"})


def acquire_ingress() -> Lease:
    for index in range(settings.AGENT_MAX_INFLIGHT_HTTP):
        handle = _try_lock(f"ingress-{index}.lock")
        if handle is not None:
            return Lease([handle])
    raise HTTPException(503, "请求量超过当前容量，请稍后重试", headers={"Retry-After": "2"})
