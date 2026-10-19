"""WebUI 鉴权：密码登录 → HMAC 会话令牌（HttpOnly Cookie）+ 可撤销会话表。

- 密码来自插件配置 ``webui.password``，constant-time 比较
- 令牌 = base64(payload).base64(hmac-sha256)，payload 含会话 ID 与过期时间
- 会话表（内存）：jti → {user, created, last_seen}，支持按会话吊销
- 登录限速：单 IP 5 次失败 / 5 分钟 → 锁 5 分钟（锁定中的 IP 一律 429，正确密码
  等锁过期）。全局窗口失败总数超限 → 全局锁，**只作「分布式爆破进行中」的记号、
  不拒绝任何请求**：直接拒绝会把「未认证者发 30 个错误密码」变成「管理员 5 分钟
  登不上面板」的永久封锁杠杆；命中时正确密码照常登录（并清锁），错误猜测的代价
  由闸门内的 FAIL_DELAY 体现。
  **单 IP 锁定只改响应码、拦不住全速并发提交**，真正的吞吐限制在传输层：失败尝试在
  ``auth.gate`` 全局闸门内串行排队并各付 FAIL_DELAY 秒（见 _api_auth），
  把猜测速率压到常数级；正确密码不加延迟。
- 来源 IP：直连取 TCP 对端；仅当对端命中 ``webui.trustedProxy`` 时才采信
  X-Forwarded-For（见 client_ip）——登录限速、会话 per-IP 上限与 IP 变化告警
  共用这一口径，反代/NAT 下不统一口径会坍缩成代理 IP 单桶。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import subprocess
import time
from functools import lru_cache

from astrbot.api import logger

COOKIE_NAME = "mh_session"
# 会话有效期的缺省小时数：实际值经 AuthManager 的 session_hours_getter 现读
# 配置（webui.sessionHours），改配置对后续登录/重签即时生效
DEFAULT_SESSION_HOURS = 12
RATE_WINDOW = 300
RATE_MAX_FAILS = 5
RATE_BLOCK = 300
# 失败尝试在全局闸门内必须消耗的墙钟时间（秒）：所有错误猜测被迫串行，
# 爆破吞吐上限 ≈ 1/FAIL_DELAY 次每秒，与攻击者并发数无关
FAIL_DELAY = 0.75

_SECRET_PATH = "webui_secret.key"

# 口令落盘格式：pbkdf2_sha256$<iter>$<salt_hex>$<dk_hex>
# 配置文件里的明文密码 = 同机任意读者直接拿到面板密码（面板可导出三平台登录态、
# 改全部配置、向已登记会话投递消息），因此落盘只存 PBKDF2 哈希；
# 旧明文值由 main.initialize 首次迁移，改密接口直接写哈希。
# 迭代数取 OWASP 2023 对 PBKDF2-HMAC-SHA256 的建议（600k）；旧哈希自带迭代数
# 仍可校验，无需迁移
_PBKDF2_ITERATIONS = 600_000
_HASH_PREFIX = "pbkdf2_sha256"
# verify 可接受的迭代数上界：哈希来自配置文件，被篡改成超大迭代数时每次登录
# 校验会跑数分钟 PBKDF2（虽已移到线程池不卡事件循环，仍可把闸门占满制造成本）
_PBKDF2_MAX_VERIFY_ITERATIONS = 2_000_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{_HASH_PREFIX}${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


async def hash_password_async(password: str) -> str:
    """PBKDF2 单次约 50-200ms 纯 CPU，绝不能跑在事件循环上（阻塞整个 AstrBot）。"""
    return await asyncio.to_thread(hash_password, password)


def is_password_hash(value: str) -> bool:
    return str(value or "").startswith(_HASH_PREFIX + "$")


def verify_password(stored: str, candidate: str) -> bool:
    """stored 为哈希格式时按 PBKDF2 校验；旧明文值回退 constant-time 字节比较
    （调用方比对成功后应尽快把明文迁移成哈希，见 main.initialize）。"""
    stored = str(stored or "")
    candidate_b = str(candidate or "").encode("utf-8")
    if is_password_hash(stored):
        try:
            _, iters, salt_hex, dk_hex = stored.split("$")
            n = int(iters)
            if not (1 <= n <= _PBKDF2_MAX_VERIFY_ITERATIONS):
                return False
            dk = hashlib.pbkdf2_hmac("sha256", candidate_b, bytes.fromhex(salt_hex), n)
            return hmac.compare_digest(dk.hex(), dk_hex)
        except (TypeError, ValueError):
            return False
    try:
        return hmac.compare_digest(candidate_b, stored.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        return False


async def verify_password_async(stored: str, candidate: str) -> bool:
    """verify_password 的线程池版本：登录/改密热路径必须用这个（见 hash_password_async）。"""
    return await asyncio.to_thread(verify_password, stored, candidate)


_INITIAL_PWD_FILE = "webui_initial_password.txt"


def write_initial_password_file(data_dir, password: str) -> None:
    """首启随机口令的告知文件：配置只存哈希，AstrBot 配置面板无从「查看」口令，
    这是用户得知初始口令的唯一途径。权限与密钥文件同口径收紧（同机其他用户
    不可读）；改密成功后由 AuthManager.clear_initial_password_file 自动删除。"""
    from ..api.http import harden_sensitive_file

    path = data_dir / _INITIAL_PWD_FILE
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{password}\n\n这是 WebUI 的初始登录密码（首次启动自动生成）。登录面板后请在"
            "「我的会话 → 修改密码」修改，修改后本文件自动删除；不再需要时也可手动删除。\n",
            "utf-8",
        )
    except OSError as e:
        logger.warning(f"[music_hub] WebUI 初始密码文件写入失败（{path}）：{e}")
        return
    # 明文口令是面板的全部权限（可导出三平台登录态）。ACL 收紧失败时同机任意
    # 用户可读——与密钥文件同一取舍：宁可让用户从服务端日志拿口令，不留弱保护
    # 的明文副本
    if not harden_sensitive_file(path):
        try:
            path.unlink()
        except OSError:
            pass
        logger.warning(
            "[music_hub] WebUI 初始密码文件 ACL 收紧失败，已删除该文件（防同机其他用户读取）。"
            "初始密码见 AstrBot 日志中本插件首启日志，或在「插件配置 → webui.password」"
            "填入新明文密码并重载插件完成设置"
        )


# ──────────── 来源 IP ────────────
@lru_cache(maxsize=8)
def _trusted_proxy_nets(raw: str) -> tuple:
    """trustedProxy 配置（IP/CIDR，逗号分隔）→ 网络元组。非法项忽略：解析失败按
    「不信任」处理比按「全信任」失败安全。lru_cache：配置取值极少变化，逐请求
    重复解析 CIDR 是无谓开销。"""
    nets = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            # 裸 IP 自动视为 /32（IPv6 为 /128）；host bits 置位由 strict=False 归一
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            pass
    return tuple(nets)


def client_ip(remote: str, forwarded: str = "", trusted_proxy: str = "") -> str:
    """客户端来源 IP 的统一口径（登录限速 / 会话 per-IP 上限 / IP 变化告警共用）。

    ``remote`` 是 TCP 对端地址：反代/NAT 场景下所有访问者共享同一地址，单 IP
    限速与会话上限会坍缩成一个桶（少量失败就封掉全部管理员）。仅当 remote 命中
    trusted_proxy（``webui.trustedProxy``）时才取 X-Forwarded-For 的第一个有效项
    （逐项校验、跳过畸形项，取不到回落 remote）；默认空配置不信任任何代理头——
    XFF 是请求头、客户端可伪造，直连场景采信它等于限速形同虚设。
    """
    remote = str(remote or "").strip()
    nets = _trusted_proxy_nets(trusted_proxy)
    if not remote or not nets:
        return remote
    try:
        peer = ipaddress.ip_address(remote)
    except ValueError:
        # remote 非 IP 字面量（测试桩 / 特殊传输）：无法判定可信代理，保守回落
        return remote
    if not any(peer in net for net in nets):
        return remote
    # rightmost-untrusted：反代通常把客户端自带的 XFF 前置拼接（X-Forwarded-For:
    # 伪造, 真实IP），取最左项等于攻击者可控——每猜一轮换一个假 IP，单 IP 锁与
    # 会话上限永不触发。从右往左跳过可信代理链上的节点，取第一个不可信项；
    # 全部可信/畸形时回落 TCP 对端
    for part in reversed([p.strip() for p in str(forwarded or "").split(",")]):
        if not part:
            continue
        try:
            ip = ipaddress.ip_address(part)
        except ValueError:
            continue
        if any(ip in net for net in nets):
            continue
        return str(ip)
    return remote


class GateBusy(RuntimeError):
    """闸门排队已满（登录/改密防自放大 DoS 的快速拒绝）。"""


class BoundedGate:
    """登录/改密的密码校验全局闸门：串行 + 排队上限。

    纯 asyncio.Lock 是无限容量 FIFO——未认证攻击者持续并发提交失败登录时，
    每个持锁睡 FAIL_DELAY 秒，会把管理员的真实登录/改密压到队尾且等待无上界。
    排队数达上限直接抛 GateBusy（调用方回 429）。注意闸门满时管理员的正确
    登录同样吃 429（要等攻击窗口过去重试）——这是防自放大 DoS 的有意取舍，
    换来的是攻击者自身吞吐也被压到 FAIL_DELAY 倒数级别。
    """

    def __init__(self, max_waiters: int = 8):
        self._lock = asyncio.Lock()
        self._max_waiters = max_waiters
        self._waiting = 0

    async def __aenter__(self):
        # 单线程事件循环：检查与自增之间无 await，计数不会超卖
        if self._waiting >= self._max_waiters:
            raise GateBusy()
        self._waiting += 1
        try:
            await self._lock.acquire()
        except BaseException:
            self._waiting -= 1
            raise
        # 已进入临界区：不再占排队名额（临界区内同时只有 1 个，吞吐上限不变）
        self._waiting -= 1
        return self

    async def __aexit__(self, *exc):
        self._lock.release()
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _restrict_acl(path) -> bool:
    """限制密钥文件访问面。POSIX 走 chmod 0o600；Windows 的 chmod 只切只读位、
    ACL 原样保留（同机其他用户仍可读），需 icacls 收紧。返回是否收紧成功——
    失败时调用方应删除密钥文件退回内存密钥：密钥可伪造管理员会话，
    留一份「同机任意用户可读」的密钥文件比会话不跨重启更危险。"""
    try:
        path.chmod(0o600)
    except OSError:
        pass
    if os.name != "nt":
        return True
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if not user:
        # 取不到用户名时 icacls 无法定向授权，按收紧失败处理
        logger.warning("[music_hub] WebUI 密钥 ACL 收紧失败（Windows）：无法确定当前用户名")
        return False
    try:
        r = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        if r.returncode != 0:
            logger.warning(f"[music_hub] WebUI 密钥 ACL 收紧失败（Windows）：icacls 退出码 {r.returncode}")
            return False
        return True
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(f"[music_hub] WebUI 密钥 ACL 收紧失败（Windows）：{e}")
        return False


class AuthManager:
    def __init__(self, data_dir, password_getter, trusted_proxy_getter=lambda: "", session_hours_getter=None):
        self._data_dir = data_dir
        self._password_getter = password_getter  # () -> str
        # () -> str，webui.trustedProxy：惰性读取，配置改了无需重建 AuthManager
        self._trusted_proxy_getter = trusted_proxy_getter
        # () -> int，webui.sessionHours：同上惰性读取；未注入按缺省 12h
        self._session_hours_getter = session_hours_getter or (lambda: DEFAULT_SESSION_HOURS)
        self._secret = self._load_secret()
        self.sessions: dict[str, dict] = {}
        self._fails: dict[str, list[float]] = {}
        self._blocked: dict[str, float] = {}
        self._global_block_until: float = 0.0
        # 登录/改密的密码校验全局闸门：失败尝试持锁睡 FAIL_DELAY，
        # 所有猜测串行化（见模块 docstring「登录限速」一段）；排队有上限，
        # 防并发失败提交把管理员真实登录压到队尾（GateBusy → 429 快速拒绝）
        self.gate = BoundedGate()

    def client_ip(self, remote: str, forwarded: str = "") -> str:
        """本实例口径的客户端来源 IP（trustedProxy 取自构造时注入的配置 getter）。"""
        return client_ip(remote, forwarded, self._trusted_proxy_getter())

    def _session_ttl(self) -> int:
        """会话有效期（秒）。配置值异常时回落缺省——TTL 取错只影响会话长短。"""
        try:
            hours = int(self._session_hours_getter())
        except (TypeError, ValueError):
            hours = DEFAULT_SESSION_HOURS
        return max(1, min(hours, 24 * 30)) * 3600

    def _load_secret(self) -> bytes:
        path = self._data_dir / _SECRET_PATH
        try:
            data = path.read_text("utf-8").strip()
            if data:
                # 已存在的密钥文件不复检/不收紧是永久窗口：旧版本创建的或权限被
                # 改动过的宽 ACL 文件，同机读到即可自签任意管理员会话。best-effort
                # 复收紧（对齐「弱保护不如无文件」的取舍，失败删文件退内存密钥）
                if not _restrict_acl(path):
                    try:
                        path.unlink()
                        logger.warning(
                            "[music_hub] 已删除弱保护的既有密钥文件，改用内存密钥（会话重启后失效）"
                        )
                    except OSError:
                        logger.warning("[music_hub] 既有密钥文件 ACL 收紧失败且无法删除，请手动检查其权限")
                        return data.encode()
                return data.encode()
        except OSError:
            pass
        secret = secrets.token_hex(32)
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            # 以 0o600 直接建文件（而非默认权限创建后再收紧），消除权限窗口；
            # Windows 上 mode 只切只读位，ACL 仍靠下方 icacls 收紧
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(secret)
            # HMAC 密钥可伪造管理员会话（同机任何用户读到即可自行签发 jti），
            # 因此只允许属主读写；收紧失败就删文件退内存密钥，不留弱保护副本
            if not _restrict_acl(path):
                try:
                    path.unlink()
                except OSError:
                    pass
                logger.warning("[music_hub] 已删除弱保护的密钥文件，改用内存密钥（会话重启后失效）")
                return secret.encode()
        except OSError as e:
            # 静默失败会让每次重载都生成新密钥 → 所有会话立即失效且无线索
            logger.warning(f"[music_hub] WebUI 密钥写入失败（{path}）：{e}；会话将在重启后失效")
        return secret.encode()

    # ──────────── 密码 ────────────
    def password_stored(self) -> str:
        """当前落盘凭证原文（PBKDF2 哈希或旧明文）。登录/改密热路径先取它再进
        verify_password_async：取值与校验分离，避免线程池里反复调 getter。"""
        return str(self._password_getter() or "")

    def check_password(self, password: str) -> bool:
        """校验口令。**用 UTF-8 字节比而不是 str 比**：
        hmac.compare_digest 对含非 ASCII 的 str 直接抛 TypeError，
        用户若把密码设成中文会永远登不上（抛异常变500 / 被吞变 False）。
        落盘值为 PBKDF2 哈希（新）或明文（旧配置，由 main.initialize 迁移）。

        注意：本方法是同步实现，含 PBKDF2 纯 CPU——只允许测试与非请求路径调用，
        登录/改密 handler 一律走 verify_password_async（线程池）。"""
        expect = self.password_stored()
        if not expect:
            return False
        return verify_password(expect, str(password or ""))

    def _pw_fingerprint(self, stored_credential: str) -> str:
        """当前口令凭证的指纹（HMAC-SHA256 前 16 hex，密钥复用会话签名密钥）。

        写进会话 payload 做绑定：verify 时与当前凭证指纹比对，任何路径改密
        （AstrBot 主面板 / 手改配置文件）都自动失效全部旧会话——此前只有面板内
        改密会 revoke_others，被窃会话能活满 12h。

        用 HMAC 而非无盐 sha256：cookie 单独泄漏时拿不到服务端密钥，无法离线
        高速验证弱口令猜测；而能读到密钥的攻击者本就可伪造任意会话，无额外损失。
        （无盐 sha256 截断仅 64 bit，且改密后仍可离线爆破出旧密码本身。）"""
        return hmac.new(
            self._secret, str(stored_credential or "").encode("utf-8"), hashlib.sha256
        ).hexdigest()[:16]

    # ──────────── 限速 ────────────
    # 全局失败上限：只按单 IP 计数时，轮换源 IP 就能无限次试密码
    RATE_MAX_FAILS_TOTAL = 30
    # _fails 总条目上限：每个新来源 IP 占一个键，海量伪造 IP 可在窗口内撑大内存
    FAILS_MAX = 10_000
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
        """单 IP 锁剩余秒数（登录/改密的前置 429 依据）。

        只看单 IP 锁、不看全局锁：全局锁若也在此拒绝请求，未认证者每 5 分钟
        打 30 个错误密码就能让正确密码永远吃 429——管理员被永久挡在面板外。
        全局锁命中时登录照常走完整校验（见模块 docstring「登录限速」），错误
        猜测的代价由闸门内的 FAIL_DELAY 体现。
        """
        self._gc_fails()
        now = time.time()
        until = self._blocked.get(ip, 0)
        return int(until - now) if until > now else 0

    def record_fail(self, ip: str) -> None:
        now = time.time()
        self._gc_fails()
        # 总量兜底：超限后不再记新条目、直接按触发全局锁处理。保守取舍：未记录
        # 的来源攒不出单 IP 锁，其猜测只受闸门 FAIL_DELAY 约束；已在记的 IP 照常
        # 累计，正常用户不受影响。不这样兜底就得让 _fails 无界增长
        if ip not in self._fails and len(self._fails) >= self.FAILS_MAX:
            self._global_block_until = now + RATE_BLOCK
            return
        fails = [t for t in self._ip_fails(ip) if now - t < RATE_WINDOW]
        fails.append(now)
        self._fails[ip] = fails
        total = sum(len(v) for v in self._fails.values())
        if len(fails) >= RATE_MAX_FAILS:
            self._blocked[ip] = now + RATE_BLOCK
            self._fails.pop(ip, None)
        if total >= self.RATE_MAX_FAILS_TOTAL:
            # 全局锁不用于拒绝（见 login_blocked）：这里只留「分布式爆破进行中」
            # 的可观测记号，成功登录会清掉
            self._global_block_until = now + RATE_BLOCK

    def record_success(self, ip: str) -> None:
        # 正确密码同时清全局锁：能登进来的只可能是管理员本人，锁继续留着只会
        # 让后续日志口径混乱
        self._fails.pop(ip, None)
        self._global_block_until = 0.0

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
        payload = {
            "jti": jti,
            "sub": username,
            "iat": now,
            "exp": now + self._session_ttl(),
            # 密码指纹：verify 时与当前密码比对，改密即全路径吊销
            "pwf": self._pw_fingerprint(self._password_getter()),
        }
        self.sessions[jti] = {"user": username, "created": now, "last_seen": now, "ip": ip}
        return self._sign(payload)

    def _sign(self, payload: dict) -> str:
        body = _b64(json.dumps(payload, separators=(",", ":")).encode())
        sig = _b64(hmac.new(self._secret, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def reissue_session(self, jti: str) -> str:
        """为既有会话原地重签令牌（改密成功后续用当前会话），返回新 token。

        会话 payload 内嵌密码指纹：新哈希写入配置的瞬间，当前 token 的旧指纹必然
        失配——只 revoke_others 的话，管理员自己也会被下一个请求（运行日志页
        2 秒轮询）401 登出。沿用原 jti：会话表行原样保留（面板上的会话 ID/年龄/
        IP 告警口径不变），仅按当前密码指纹重算签名并刷新过期时间；其他会话的
        吊销由调用方 revoke_others(本 jti) 负责。会话不存在（无 jti / 已被摘除）
        返回空串，调用方不下发 cookie。
        """
        row = self.sessions.get(jti)
        if row is None:
            return ""
        now = int(time.time())
        payload = {
            "jti": jti,
            "sub": row.get("user", "admin"),
            "iat": now,
            "exp": now + self._session_ttl(),
            "pwf": self._pw_fingerprint(self._password_getter()),
        }
        row["last_seen"] = now
        return self._sign(payload)

    def verify(self, token: str, ip: str = "") -> dict | None:
        """校验令牌；有效返回 payload（并顺带 touch last_seen）。

        ip 传入时做软校验：与会话签发 IP 不一致只记一次告警不拒绝——
        移动网络/出口池换 IP 是正常行为，硬拒会频繁踢人；但告警能暴露
        cookie 被窃后在异 IP 重放的情形。

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
        # 密码指纹比对：不一致说明登录后密码被改过（任意路径，含 AstrBot 主面板）
        # ——拒绝该会话，被窃的旧 cookie 不能活满 TTL。只拒绝、不从表里摘行：
        # 改密 handler 会为当前 jti 原地重签（reissue_session），响应送达前在途的
        # 旧 token 若在此把行摘掉，重签出的同 jti 新令牌会跟着失效
        if payload.get("pwf") != self._pw_fingerprint(self._password_getter()):
            return None
        if ip and row.get("ip") and row["ip"] != ip and not row.get("ip_warned"):
            row["ip_warned"] = True
            logger.warning(
                f"[music_hub] WebUI 会话来源 IP 变化（{row['ip']} → {ip}）："
                "若非本人操作请立即在「我的会话」吊销并改密"
            )
        if time.time() - row.get("last_seen", 0) > 60:
            row["last_seen"] = int(time.time())
        return payload

    def revoke(self, jti: str) -> bool:
        return self.sessions.pop(jti, None) is not None

    def clear_initial_password_file(self) -> None:
        """改密成功后删除初始口令告知文件（best-effort，文件不存在也无碍）。"""
        try:
            (self._data_dir / _INITIAL_PWD_FILE).unlink(missing_ok=True)
        except OSError:
            pass

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
                    # 该会话发生过来源 IP 变化：cookie 被窃后在异 IP 重放的服务端
                    # 侧只留一条 warning 日志，面板不可见——把标记下发，管理员在
                    # 「我的会话」能直接看到异常会话
                    "ipChanged": bool(row.get("ip_warned")),
                }
            )
        return sorted(out, key=lambda r: -r["created"])

    def _gc_sessions(self) -> None:
        now = time.time()
        for jti in [j for j, r in self.sessions.items() if now - r.get("last_seen", 0) > self._session_ttl()]:
            self.sessions.pop(jti, None)
