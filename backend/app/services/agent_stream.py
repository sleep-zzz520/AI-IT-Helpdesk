"""SSE 正常结束、异常和客户端断开均关闭生成器，释放准入资源。"""
import threading

import anyio
from fastapi.responses import StreamingResponse


class GuardedStreamingResponse(StreamingResponse):
    def __init__(self, iterator, cleanup, **kwargs):
        self._iterator = iter(iterator)
        self._cleanup = cleanup
        self._iteration_lock = threading.Lock()
        super().__init__(self._iterate(), **kwargs)

    def _iterate(self):
        while True:
            with self._iteration_lock:
                try:
                    item = next(self._iterator)
                except StopIteration:
                    return
            yield item

    def _close(self):
        # 正在执行的同步节点不能被线程取消。等该次 next 返回后才回收槽位，
        # 避免断线已释放预算、后台工具却还在执行。
        with self._iteration_lock:
            try:
                close = getattr(self._iterator, "close", None)
                if close is not None:
                    close()
            finally:
                self._cleanup()

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self._close)
