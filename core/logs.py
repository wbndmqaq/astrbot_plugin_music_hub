"""插件运行日志环形缓冲：挂到插件 logger 上，供 WebUI「运行日志」页轮询。"""

from __future__ import annotations

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
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001 - logging.Handler 里抛异常会反噬宿主，这里只吞
            return
        self._seq += 1
        self._buf.append({"seq": self._seq, "level": record.levelname, "msg": msg[-2000:]})

    def after(self, seq: int, limit: int = 200) -> tuple[list[dict], int]:
        """返回 seq 之后的条目（最多 limit 条）与最新 seq。"""
        items = [e for e in self._buf if e["seq"] > seq]
        return items[-limit:], self._seq

    def latest(self, limit: int = 200) -> tuple[list[dict], int]:
        items = list(self._buf)[-limit:]
        return items, self._seq
