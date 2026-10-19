"""插件运行日志环形缓冲：挂到插件 logger 上，供 WebUI「运行日志」页轮询。"""

from __future__ import annotations

import itertools
import logging
import re
import threading
from collections import deque

_MAX_ENTRIES = 500

# /api/logs 会把缓冲区全量下发给登录会话：上游 httpx 异常、扫码凭证处理等日志行
# 可能携带登录态材料（musickey、cookie 串、QR ticket），下发前统一掩码。
# "? 容忍 JSON 形态（"musickey":"Q_H_L_..." 键与冒号隔着引号）：纯 [=:] 口径
# 对最常见的 f"resp: {resp_json}" 打日志形态恰好失效，凭证原样进面板
_SENSITIVE_RE = re.compile(
    r"((?:musickey|quantum|cookie|ticket|token|music_u)\s*\"?\s*[=:]\s*\"?)[^\s&;\"']+",
    re.IGNORECASE,
)


def _mask(msg: str) -> str:
    return _SENSITIVE_RE.sub(lambda m: m.group(1) + "***", msg)


class LogBuffer(logging.Handler):
    def __init__(self, capacity: int = _MAX_ENTRIES):
        super().__init__(level=logging.INFO)
        self._buf: deque[dict] = deque(maxlen=capacity)
        # emit 可来自任意线程（logging.Handler 语义），deque 边迭代边 append 会抛
        # RuntimeError（WebUI 偶发 500），读写统一过这把锁。seq 必须单调不丢号，
        # 否则 WebUI 按 seq 过滤会永久跳过中间条目；itertools.count 的 next()
        # 在 CPython 是 C 层原子操作，读 _last_seq 只是取一个 int 属性，无需锁。
        self._buf_lock = threading.Lock()
        self._seqs = itertools.count(1)
        self._last_seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        # 存结构化字段而非 format() 后的整行：时间戳要参与前端「清屏」过滤，
        # 级别要单独渲染徽标，拼在 msg 里会导致双显且没法按时间过滤
        try:
            msg = _mask(record.getMessage())
        except Exception:  # noqa: BLE001 - logging.Handler 里抛异常会反噬宿主，这里只吞
            return
        # seq 的分配与 _last_seq 推进必须和 append 同一把锁：emit 可能来自任意线程，
        # 锁外交错时后完成的低 seq 会把 _last_seq 往回拨，WebUI 按 seq 轮询就会重复拉取
        with self._buf_lock:
            seq = next(self._seqs)
            self._last_seq = seq
            self._buf.append(
                {"seq": seq, "ts": int(record.created), "level": record.levelname, "msg": msg[-2000:]}
            )

    def after(self, seq: int, limit: int = 200) -> tuple[list[dict], int]:
        """返回 seq 之后的条目与「本次已推进到的 seq」。

        超过 limit 时返回**最早**的 limit 条并只推进到该条 —— 若返回最后 limit 条
        却把 seq 推到最新，客户端下次拉取会永久跳过中间条目（seq 单调前进，
        被跳过的区间再也不会出现）。
        """
        limit = max(1, int(limit))  # 无下界防御时 limit=0 会 IndexError
        with self._buf_lock:
            items = [e for e in self._buf if e["seq"] > seq]
            last = self._last_seq
        if len(items) > limit:
            return items[:limit], items[limit - 1]["seq"]
        return items, last

    def latest(self, limit: int = 200) -> tuple[list[dict], int]:
        limit = max(1, int(limit))  # 与 after() 同防御：limit=0 时 [-0:] 会返回整个缓冲区
        with self._buf_lock:
            items = list(self._buf)[-limit:]
            last = self._last_seq
        return items, last
