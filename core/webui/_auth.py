"""WebUI 鉴权：密码登录 → HMAC 会话令牌（HttpOnly Cookie）+ 可撤销会话表。

- 密码来自插件配置 ``webui.password``，constant-time 比较
- 令牌 = base64(payload).base64(hmac-sha256)，payload 含会话 ID 与过期时间
- 会话表（内存）：jti → {user, created, last_seen}，支持按会话吊销
- 登录限速：单 IP 5 次失败 / 5 分钟 → 锁 5 分钟
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from astrbot.api import logger

COOKIE_NAME = "mh_session"
SESSION_TTL = 12 * 3600
RATE_WINDOW = 300
RATE_MAX_FAILS = 5
RATE_BLOCK = 300

_SECRET_PATH = "webui_secret.key"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


class AuthManager:
    def __init__(self, data_dir, password_getter):
        self._data_dir = data_dir
        self._password_getter = password_getter  # () -> str
        self._secret = self._load_secret()
        self.sessions: dict[str, dict] = {}
        self._fails: dict[str, list[float]] = {}
        self._blocked: dict[str, float] = {}

    def _load_secret(self) -> bytes:
        path = self._data_dir / _SECRET_PATH
        try:
            data = path.read_text("utf-8").strip()
            if data:
                return data.encode()
        except OSError:
            pass
        secret = secrets.token_hex(32)
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(secret, "utf-8")
            # HMAC 密钥可伪造管理员会话（同机任何用户读到即可自行签发 jti），
            # 因此只允许属主读写
            path.chmod(0o600)
        except OSError as e:
            # 静默失败会让每次重载都生成新密钥 → 所有会话立即失效且无线索
            logger.warning(f"[music_hub] WebUI 密钥写入失败（{path}）：{e}；会话将在重启后失效")
        return secret.encode()

    # ──────────── 密码 ────────────
    def check_password(self, password: str) -> bool:
        """constant-time 比较。**用 UTF-8 字节比而不是 str 比**：
        hmac.compare_digest 对含非 ASCII 的 str 直接抛 TypeError，
        用户若把密码设成中文会永远登不上（抛异常变500 / 被吞变 False）。"""
        expect = self._password_getter()
        if not expect:
            return False
        try:
            return hmac.compare_digest(str(password or "").encode("utf-8"), str(expect).encode("utf-8"))
        except (TypeError, ValueError, UnicodeError):
            return False

    # ──────────── 限速 ────────────
    # 全局失败上限：只按单 IP 计数时，轮换源 IP 就能无限次试密码
    RATE_MAX_FAILS_TOTAL = 30
    MAX_SESSIONS = 64
    MAX_SESSIONS_PER_IP = 5

    def _ip_fails(self, ip: str) -> list[float]:
        self._gc_fails()
        # gc 之后再取：刚 setdefault 出来的空列表若被 gc 当成过期清掉，
        # 下面 return self._fails[ip] 会 KeyError
        return self._fails.setdefault(ip, [])

    def _gc_fails(self) -> None:
        now = time.time()
        for ip in [ip for ip, ts in self._fails.items() if ts and now - ts[-1] > RATE_WINDOW]:
            self._fails.pop(ip, None)
        for ip in [ip for ip, until in self._blocked.items() if until <= now]:
            self._blocked.pop(ip, None)

    def login_blocked(self, ip: str) -> int:
        self._gc_fails()
        until = self._blocked.get(ip, 0)
        return int(until - time.time()) if until > time.time() else 0

    def record_fail(self, ip: str) -> None:
        now = time.time()
        fails = [t for t in self._ip_fails(ip) if now - t < RATE_WINDOW]
        fails.append(now)
        self._fails[ip] = fails
        total = sum(len(v) for v in self._fails.values())
        if len(fails) >= RATE_MAX_FAILS or total >= self.RATE_MAX_FAILS_TOTAL:
            self._blocked[ip] = now + RATE_BLOCK
            self._fails.pop(ip, None)

    def record_success(self, ip: str) -> None:
        self._fails.pop(ip, None)

    # ──────────── 会话 ────────────
    def create_session(self, username: str = "admin", ip: str = "") -> str:
        self._gc_sessions()
        # 会话表只在内存里，上限必须硬性兜底：否则反复登录能把它撑到无界
        while len(self.sessions) >= self.MAX_SESSIONS:
            oldest = min(self.sessions, key=lambda j: self.sessions[j].get("created", 0), default=None)
            if oldest is None:
                break
            self.sessions.pop(oldest, None)
        if ip:
            same_ip = sum(1 for r in self.sessions.values() if r.get("ip") == ip)
            if same_ip >= self.MAX_SESSIONS_PER_IP:
                self.sessions.pop(
                    min(
                        (j for j, r in self.sessions.items() if r.get("ip") == ip),
                        key=lambda j: self.sessions[j].get("created", 0),
                    ),
                    None,
                )
        jti = secrets.token_urlsafe(16)
        now = int(time.time())
        payload = {"jti": jti, "sub": username, "iat": now, "exp": now + SESSION_TTL}
        body = _b64(json.dumps(payload, separators=(",", ":")).encode())
        sig = _b64(hmac.new(self._secret, body.encode(), hashlib.sha256).digest())
        self.sessions[jti] = {"user": username, "created": now, "last_seen": now, "ip": ip}
        return f"{body}.{sig}"

    def verify(self, token: str) -> dict | None:
        """校验令牌；有效返回 payload（并顺带 touch last_seen）。

        整段包异常：token 来自用户 cookie，畸形输入（非 ASCII 签名、非法 base64、
        长度不符）会让 compare_digest / b64decode 抛 TypeError/ValueError，
        穿透到请求层变成 500 而不是 401。
        """
        if not token or "." not in token:
            return None
        try:
            body, _, sig = token.partition(".")
            expect = _b64(hmac.new(self._secret, body.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(sig, expect):
                return None
            payload = json.loads(_unb64(body))
        except (TypeError, ValueError, UnicodeError):
            return None
        if not isinstance(payload, dict):
            return None
        jti = payload.get("jti", "")
        if payload.get("exp", 0) < time.time() or jti not in self.sessions:
            self.sessions.pop(jti, None)
            return None
        row = self.sessions[jti]
        if time.time() - row.get("last_seen", 0) > 60:
            row["last_seen"] = int(time.time())
        return payload

    def revoke(self, jti: str) -> bool:
        return self.sessions.pop(jti, None) is not None

    def revoke_others(self, keep_jti: str) -> int:
        removed = [j for j in self.sessions if j != keep_jti]
        for j in removed:
            self.sessions.pop(j, None)
        return len(removed)

    def list_sessions(self, current_jti: str = "") -> list[dict]:
        out = []
        for jti, row in self.sessions.items():
            out.append(
                {
                    "id": jti[:8],
                    "current": jti == current_jti,
                    "created": row.get("created", 0),
                    "lastSeen": int(row.get("last_seen", 0)),
                    "age": int(time.time() - row.get("created", 0)),
                }
            )
        return sorted(out, key=lambda r: -r["created"])

    def _gc_sessions(self) -> None:
        now = time.time()
        for jti in [j for j, r in self.sessions.items() if now - r.get("last_seen", 0) > SESSION_TTL]:
            self.sessions.pop(jti, None)
