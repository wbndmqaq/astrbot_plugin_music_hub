"""插件运行日志环形缓冲：挂到插件 logger 上，供 WebUI「运行日志」页轮询。"""

from __future__ import annotations

import itertools
import logging
from collections import deque

_MAX_ENTRIES = 500
_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_DATEFMT = "%H:%M:%S"


class LogBuffer(logging.Handler):
    def __init__(self, capacity: int = _MAX_ENTRIES):
        super().__init__(level=logging.INFO)
        self.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
        self._buf: deque[dict] = deque(maxlen=capacity)
        # emit 会在任意线程被调用（logging.Handler 语义）。seq 必须单调不丢号，
        # 否则 WebUI 按 seq 过滤会永久跳过中间条目。itertools.count 的 next()
        # 在 CPython 是 C 层原子操作，读 _last_seq 只是取一个 int 属性，都不需要锁。
        self._seqs = itertools.count(1)
        self._last_seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001 - logging.Handler 里抛异常会反噬宿主，这里只吞
            return
        seq = next(self._seqs)
        self._last_seq = seq
        self._buf.append({"seq": seq, "level": record.levelname, "msg": msg[-2000:]})

    def after(self, seq: int, limit: int = 200) -> tuple[list[dict], int]:
        """返回 seq 之后的条目与「本次已推进到的 seq」。

        超过 limit 时返回**最早**的 limit 条并只推进到该条 —— 若返回最后 limit 条
        却把 seq 推到最新，客户端下次拉取会永久跳过中间条目（seq 单调前进，
        被跳过的区间再也不会出现）。
        """
        items = [e for e in self._buf if e["seq"] > seq]
        if len(items) > limit:
            return items[:limit], items[limit - 1]["seq"]
        return items, self._last_seq

    def latest(self, limit: int = 200) -> tuple[list[dict], int]:
        items = list(self._buf)[-limit:]
        return items, self._last_seq
