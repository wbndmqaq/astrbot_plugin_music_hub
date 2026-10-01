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
        except OSError:
            pass
        return secret.encode()

    # ──────────── 密码 ────────────
    def check_password(self, password: str) -> bool:
        expect = self._password_getter()
        if not expect:
            return False
        return hmac.compare_digest(str(password or ""), str(expect))

    # ──────────── 限速 ────────────
    def _ip_fails(self, ip: str) -> list[float]:
        return self._fails.setdefault(ip, [])

    def login_blocked(self, ip: str) -> int:
        until = self._blocked.get(ip, 0)
        if until > time.time():
            return int(until - time.time())
        if until:
            self._blocked.pop(ip, None)
        return 0

    def record_fail(self, ip: str) -> None:
        now = time.time()
        fails = [t for t in self._ip_fails(ip) if now - t < RATE_WINDOW]
        fails.append(now)
        self._fails[ip] = fails
        if len(fails) >= RATE_MAX_FAILS:
            self._blocked[ip] = now + RATE_BLOCK
            self._fails.pop(ip, None)

    def record_success(self, ip: str) -> None:
        self._fails.pop(ip, None)

    # ──────────── 会话 ────────────
    def create_session(self, username: str = "admin") -> str:
        jti = secrets.token_urlsafe(16)
        now = int(time.time())
        payload = {"jti": jti, "sub": username, "iat": now, "exp": now + SESSION_TTL}
        body = _b64(json.dumps(payload, separators=(",", ":")).encode())
        sig = _b64(hmac.new(self._secret, body.encode(), hashlib.sha256).digest())
        token = f"{body}.{sig}"
        self.sessions[jti] = {"user": username, "created": now, "last_seen": now}
        self._gc_sessions()
        return token

    def verify(self, token: str) -> dict | None:
        """校验令牌；有效返回 payload（并顺带 touch last_seen）。"""
        if not token or "." not in token:
            return None
        body, _, sig = token.partition(".")
        expect = _b64(hmac.new(self._secret, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expect):
            return None
        try:
            payload = json.loads(_unb64(body))
        except ValueError:
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
