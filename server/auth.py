"""API 认证与多用户身份（v0.15.0，设计见 AUTH_DESIGN.md）。

模型：每人一把 API Key（pak- 前缀 + 32 位 hex），HTTP Bearer 认证；
服务端中间件把凭据解析为身份（username + role）并注入请求上下文，
成为 usage.current_user 等 contextvars 的唯一可信来源。

凭据双轨（v0.15.0）：
- API Key（pak-）：CLI 签发，适合脚本/服务调用，可限期、可轮换、可吊销
- 登录令牌（pat-）：账号密码登录（POST /api/auth/login）签发的短期令牌，
  默认 12 小时过期，适合浏览器端人工使用；密码只存 PBKDF2 加盐散列

角色：user（聊天/自决审批）→ approver（决议任何审批）→ admin（用户管理）。

存储：SQLite（STATE_DB 同库），只存凭据的散列，吊销不删除（留审计）。
AUTH_ENABLED=false（默认）= 开放模式，行为与认证引入前完全一致。
"""
import hashlib
import hmac
import ipaddress
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from . import config

ROLES = ("user", "approver", "admin")
_lock = threading.Lock()

# 受信反向代理来源 IP 白名单（v0.17.4 安全审计 F2）：身份头来源校验。
# 留空 = 不信任任何身份头（fail-closed）；命中白名单才允许 SSO 身份头生效。
_allowed_proxy_nets: list = []
if config.AUTH_PROXY_ALLOWED_IPS:
    try:
        _allowed_proxy_nets = [
            ipaddress.ip_network(tok.strip(), strict=False)
            for tok in config.AUTH_PROXY_ALLOWED_IPS.split(",") if tok.strip()]
    except ValueError:
        _allowed_proxy_nets = []  # 非法白名单配置 → 按未配置处理（fail-closed）

# 密码散列参数（PBKDF2-HMAC-SHA256；格式 pbkdf2$<迭代数>$<盐hex>$<散列hex>）
_PBKDF2_ITERATIONS = 200_000
MIN_PASSWORD_LEN = 8

# 登录失败限流（v0.17.2）：内存级按用户名计数，连续失败达阈值锁定一段时间。
# 防的是在线爆破账号密码；PBKDF2 已大幅拉高单次尝试成本，此限流再兜一层。
# 返回仍与成功同路径的模糊响应（None），不泄露「锁定中」以外的信息（防枚举）。
LOGIN_MAX_FAILS = 5         # 连续失败达到该次数 → 锁定
LOGIN_LOCK_SECONDS = 30     # 锁定时长（秒），到期自动解锁（计数清零）
_LOGIN_FAILS_MAX = 10_000   # 限流表容量上限（v0.17.3）：防随机用户名灌爆进程内存
_login_fails: dict[str, tuple[int, float]] = {}  # username -> (失败计数, 锁定截止时间)


def enabled() -> bool:
    return config.AUTH_ENABLED


def _db_path() -> Path:
    p = Path(config.STATE_DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path()))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_users (
            username TEXT PRIMARY KEY,
            key_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            created REAL NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0
        )""")
    # v0.14.2 Key 过期迁移：expires_at 为 NULL 表示永不过期
    cols = {r[1] for r in conn.execute("PRAGMA table_info(api_users)")}
    if "expires_at" not in cols:
        conn.execute("ALTER TABLE api_users ADD COLUMN expires_at REAL")
    # v0.15.0 账号密码登录迁移：password_hash 为 NULL 表示该用户禁用密码登录
    if "password_hash" not in cols:
        conn.execute("ALTER TABLE api_users ADD COLUMN password_hash TEXT")
    # 登录令牌（pat-）：只存 sha256；过期即失效
    conn.execute("""
        CREATE TABLE IF NOT EXISTS auth_tokens (
            token_hash TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created REAL NOT NULL,
            expires_at REAL NOT NULL
        )""")


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_key() -> str:
    """pak- = private-agent-key，前缀便于日志识别与防误提交。"""
    return "pak-" + secrets.token_hex(16)


def add_user(username: str, role: str = "user", days: int = 0) -> str | None:
    """创建用户并返回一次性明文 Key（只在此时可见）；用户名已存在或角色非法返回 None。
    days>0 时 Key 在指定天数后过期（expires_at）；0 = 永不过期。"""
    username = username.strip()
    if not username or len(username) > 64 or role not in ROLES:
        return None
    key = generate_key()
    expires = time.time() + days * 86400 if days > 0 else None
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            if conn.execute("SELECT 1 FROM api_users WHERE username = ?",
                            (username,)).fetchone():
                return None
            conn.execute(
                "INSERT INTO api_users (username, key_hash, role, created, expires_at)"
                " VALUES (?,?,?,?,?)", (username, _hash(key), role, time.time(), expires))
            conn.commit()
            return key
        finally:
            conn.close()


def rotate_user(username: str, days: int = 0) -> str | None:
    """轮换 Key：为已存在的用户签发新 Key，旧 Key 立即失效（key_hash 被覆盖）。
    同时清除吊销状态、按 days 重设有效期。用户不存在返回 None。"""
    username = username.strip()
    key = generate_key()
    expires = time.time() + days * 86400 if days > 0 else None
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.execute(
                "UPDATE api_users SET key_hash = ?, revoked = 0, expires_at = ?"
                " WHERE username = ?",
                (_hash(key), expires, username))
            conn.commit()
            return key if cur.rowcount > 0 else None
        finally:
            conn.close()


def _hash_password(password: str, salt: bytes | None = None,
                   iterations: int = _PBKDF2_ITERATIONS) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt_hex, hash_hex = stored.split("$", 3)
        if scheme != "pbkdf2":
            return False
        expect = _hash_password(password, bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(expect, stored)
    except (ValueError, TypeError):
        return False


def set_password(username: str, password: str) -> bool:
    """设置/重置用户密码（只存 PBKDF2 加盐散列）。
    用户不存在或密码短于 MIN_PASSWORD_LEN 返回 False。"""
    if len(password or "") < MIN_PASSWORD_LEN:
        return False
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.execute(
                "UPDATE api_users SET password_hash = ? WHERE username = ?",
                (_hash_password(password), username.strip()))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def login(username: str, password: str) -> dict | None:
    """账号密码登录：验证通过则签发 pat- 令牌（有效期 AUTH_TOKEN_TTL_HOURS）。
    返回 {token, expires_at, identity:{username, role}}；失败（用户不存在/
    已吊销/未设密码/密码错误/限流锁定中）一律返回 None，不区分原因（防用户名枚举）。
    v0.17.2：连续失败 LOGIN_MAX_FAILS 次锁定 LOGIN_LOCK_SECONDS（内存级，
    单进程/单副本生效；多副本部署应由前置网关承担统一限流）。"""
    username = (username or "").strip()
    if not username or not password:
        return None
    with _lock:
        now = time.time()
        # 限流表容量控制（v0.17.3）：攻击者可用海量随机用户名刷失败记录，
        # 若无上限内存会无限增长。先清已解锁的旧条目；极端情况（超上限且
        # 均为活跃条目）整体清空保底——限流临时降级，PBKDF2 仍兜底暴力破解。
        if len(_login_fails) >= _LOGIN_FAILS_MAX:
            stale = [u for u, (_, until) in _login_fails.items() if until <= now]
            for u in stale:
                del _login_fails[u]
            if len(_login_fails) >= _LOGIN_FAILS_MAX:
                _login_fails.clear()
        fails, lock_until = _login_fails.get(username, (0, 0.0))
        if lock_until > now:
            # 锁定中：仍返回模糊 None（与密码错误不可区分），仅记日志供排障
            print(f"[auth] 登录限流锁定中，拒绝: {username}"
                  f"（{int(lock_until - now)}s 后解锁）")
            return None
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT username, role, password_hash FROM api_users"
                " WHERE username = ? AND revoked = 0", (username,)).fetchone()
            if not row or not row["password_hash"] \
                    or not _verify_password(password, row["password_hash"]):
                fails += 1
                if fails >= LOGIN_MAX_FAILS:
                    _login_fails[username] = (0, now + LOGIN_LOCK_SECONDS)
                    print(f"[auth] 登录连续失败 {LOGIN_MAX_FAILS} 次，"
                          f"锁定 {username} {LOGIN_LOCK_SECONDS}s"
                          "（锁定期内该账号登录一律拒绝）")
                else:
                    _login_fails[username] = (fails, lock_until)
                return None
            _login_fails.pop(username, None)  # 登录成功：清除失败计数
            token = "pat-" + secrets.token_hex(16)
            now = time.time()
            expires = now + config.AUTH_TOKEN_TTL_HOURS * 3600
            conn.execute("DELETE FROM auth_tokens WHERE expires_at <= ?", (now,))
            conn.execute(
                "INSERT INTO auth_tokens (token_hash, username, created, expires_at)"
                " VALUES (?,?,?,?)", (_hash(token), row["username"], now, expires))
            conn.commit()
            return {"token": token, "expires_at": expires,
                    "identity": {"username": row["username"], "role": row["role"]}}
        finally:
            conn.close()


def resolve_token(token: str) -> dict | None:
    """pat- 登录令牌 → 身份；无效/过期/用户已吊销返回 None。"""
    if not token or not token.startswith("pat-"):
        return None
    with _lock:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            _ensure_schema(conn)
            now = time.time()
            row = conn.execute(
                "SELECT u.username, u.role FROM auth_tokens t"
                " JOIN api_users u ON u.username = t.username"
                " WHERE t.token_hash = ? AND t.expires_at > ? AND u.revoked = 0",
                (_hash(token), now)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def resolve_credential(token: str) -> dict | None:
    """按前缀分发：pak- 走 API Key，pat- 走登录令牌。"""
    if token.startswith("pat-"):
        return resolve_token(token)
    return resolve_key(token)


def user_by_name(username: str) -> dict | None:
    """按用户名取身份（未吊销）；不存在返回 None。"""
    username = (username or "").strip()
    if not username:
        return None
    with _lock:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT username, role FROM api_users"
                " WHERE username = ? AND revoked = 0", (username,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def proxy_ip_allowed(remote_host: str | None) -> bool:
    """身份头来源校验（v0.17.4 安全审计 F2）：仅当请求来源 IP 命中
    AUTH_PROXY_ALLOWED_IPS 受信代理白名单时，中间件才允许身份头生效。
    未配置白名单 / 来源不命中 → False（fail-closed：任何人直连 Agent
    端口都无法伪造身份头冒充他人）。"""
    if not _allowed_proxy_nets or not remote_host:
        return False
    try:
        ip = ipaddress.ip_address(remote_host)
    except ValueError:
        return False
    return any(ip in net for net in _allowed_proxy_nets)


def resolve_identity(bearer_token: str, header_user: str = "") -> dict | None:
    """SSO 钩子（v0.16.0）：身份解析的唯一入口，企业接入时替换/扩展此函数。

    优先级：
    1. 反向代理身份头（配置了 AUTH_IDENTITY_HEADER 且请求带该头）：
       认证由上游企业网关/IdP（oauth2-proxy、Keycloak 等）完成，
       本层只信任头中的用户名，角色仍查本地用户表（认证在外、授权在内）。
       用户不存在或已吊销 → None（fail-closed）。
       ⚠️ v0.17.4（安全审计 F2）：身份头是否被信任由 main.auth_middleware
       依据 proxy_ip_allowed() 判定（来源须命中 AUTH_PROXY_ALLOWED_IPS 白名单），
       本函数只负责把已获信任的头映射到本地用户——直连端口伪造头不会到达此处。
    2. Bearer 凭据（pak- API Key / pat- 登录令牌）。

    扩展其他身份源（OIDC JWT 等）时在此加分支即可，下游角色判断、
    资源级 ACL、审批审计、token 计量全部不受影响。
    """
    if header_user:
        return user_by_name(header_user)
    if bearer_token:
        return resolve_credential(bearer_token)
    return None


def logout(token: str) -> bool:
    """注销：删除登录令牌（对 pak- Key 无效，Key 请用吊销/轮换）。"""
    if not token or not token.startswith("pat-"):
        return False
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.execute("DELETE FROM auth_tokens WHERE token_hash = ?",
                               (_hash(token),))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def revoke_user(username: str) -> bool:
    """吊销用户（保留记录，中间件立即拒绝其 Key；同时清除其登录令牌）。"""
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            cur = conn.execute(
                "UPDATE api_users SET revoked = 1 WHERE username = ? AND revoked = 0",
                (username,))
            conn.execute("DELETE FROM auth_tokens WHERE username = ?", (username,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def list_users() -> list[dict]:
    with _lock:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            _ensure_schema(conn)
            rows = conn.execute(
                "SELECT username, role, created, revoked, expires_at,"
                " (password_hash IS NOT NULL) AS has_password"
                " FROM api_users ORDER BY created").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def resolve_key(key: str) -> dict | None:
    """Bearer Key → 身份 {username, role}；无效/已吊销/已过期返回 None。"""
    if not key or not key.startswith("pak-"):
        return None
    with _lock:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        try:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT username, role FROM api_users"
                " WHERE key_hash = ? AND revoked = 0"
                " AND (expires_at IS NULL OR expires_at > ?)",
                (_hash(key), time.time())).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def bootstrap_admin() -> None:
    """认证模式下用户表为空时，按 AUTH_BOOTSTRAP_ADMIN 创建首个 admin 并打印一次性 Key。"""
    if not enabled() or not config.AUTH_BOOTSTRAP_ADMIN:
        return
    with _lock:
        conn = _connect()
        try:
            _ensure_schema(conn)
            n = conn.execute("SELECT COUNT(*) FROM api_users").fetchone()[0]
        finally:
            conn.close()
    if n > 0:
        return
    key = add_user(config.AUTH_BOOTSTRAP_ADMIN, role="admin")
    if key:
        print(f"[auth] 已创建引导管理员「{config.AUTH_BOOTSTRAP_ADMIN}」，"
              f"一次性 API Key（请立即保存，不再显示）: {key}")
