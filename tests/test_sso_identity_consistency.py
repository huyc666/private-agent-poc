"""SSO 身份一致性红测试（v0.17.39，spec: docs/specs/security-hardening-v0.17.39.md）。

来源：11-01 架构审核报告 §5（P1）「SSO 部门身份在 HTTP 与 Agent 工具执行层
不一致」的「必补验收」4 条 + 用户补充的三凭据（SSO 头 / Bearer / Cookie）
优先级共存一致性。

AC1 调岗全口径生效：本地 HR + SSO Finance → 执行层 current_department/role
    取请求身份；提示词包片段按 Finance 过滤（HTTP 层 resolve_identity 同值）。
AC2 撤权即时性：本地表仍 HR，SSO Finance → HR 部门白名单 kb/包片段立即
    不可见（无需等本地表同步）。
AC3 SSO 空部门 fail-closed + 三路径对比：已配置部门头且头值为空 →
    department=""（不回退本地）；Bearer → 本地表部门；开放模式既有契约。
AC4 并发隔离：两并发对话（Finance / hr）身份互不串用（事件驱动交错）。
AC5 凭据优先级一致性：SSO 头+Cookie → SSO 生效（不混用）；SSO 用户不存在
    +Cookie → 401 不回退；Bearer 失效+Cookie → Cookie（v0.17.37 迁移行为）；
    非白名单来源 SSO 头+Cookie → 头忽略；chat 端点把同一份身份传入执行层。
AC6 执行层禁查表：user_by_name 爆炸时对话内角色/部门仍取请求身份。
"""
import asyncio
from types import SimpleNamespace

import pytest
import yaml

from server import agent, auth, config, packs, usage
from server.state import STATE


@pytest.fixture(autouse=True)
def _restore_state():
    """STATE 单例与 agent 内存态测试间复位（沿用 test_agent_llm 先例）。"""
    saved = (STATE.llm, STATE.agent, STATE.checkpointer, STATE.checkpointer_cm)
    saved_locks = dict(agent._session_locks)
    saved_cache = dict(agent._compact_cache)
    yield
    (STATE.llm, STATE.agent, STATE.checkpointer, STATE.checkpointer_cm) = saved
    agent._session_locks.clear()
    agent._session_locks.update(saved_locks)
    agent._compact_cache.clear()
    agent._compact_cache.update(saved_cache)


@pytest.fixture
def api(client_factory):
    with client_factory() as c:
        yield c


def _collect_stream(agen):
    async def _run():
        return [ev async for ev in agen]
    return asyncio.run(_run())


def _hdr(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


_KB_PACKS = [
    {"name": "hr-pack", "knowledge_bases": ["kb-hr"],
     "allowed_roles": [], "allowed_departments": ["hr"]},
    {"name": "fin-pack", "knowledge_bases": ["kb-fin"],
     "allowed_roles": [], "allowed_departments": ["finance"]},
]


# ------------------------------------------------------------- 执行层探针

class _ProbeAgent:
    """执行层探针：在图执行现场（astream_events 内，与工具/提示词同一上下文）
    调用 probe() 记录当时的身份判定口径——验证执行层看到的是请求身份本身。"""

    def __init__(self, probe):
        self.probe = probe
        self.records: list = []

    async def astream_events(self, payload, config=None, version="v2"):
        self.records.append(self.probe())
        return
        yield

    async def aget_state(self, cfg):
        return SimpleNamespace(tasks=[])


def _run_with_probe(probe, *, sid="s-id1", user_id="u1", identity=None):
    """以给定请求身份驱动一轮 stream_reply，返回探针记录列表。"""
    STATE.agent = _ProbeAgent(probe)
    agent._rebuild_graph()                    # 对齐签名，避免热重载噪音
    _collect_stream(agent.stream_reply(
        [{"role": "user", "content": "探针"}], session_id=sid,
        user_id=user_id, run_id="", identity=identity))
    return STATE.agent.records


def _mk_pack(name: str, departments: list[str]):
    """落一个带 prompt.md 的部门白名单包（沿用 test_kb_permissions 模式）。"""
    manifest = {"name": name, "version": "0.1.0",
                "description": f"{name} 包", "core_version": ">=0.1.0",
                "tools": ["tools/greet.py"], "prompt": "prompt.md",
                "allowed_departments": departments}
    d = config.PACKS_DIR / name
    (d / "tools").mkdir(parents=True, exist_ok=True)
    (d / "pack.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    (d / "prompt.md").write_text(f"{name} 的领域提示词片段", encoding="utf-8")
    (d / "tools" / "greet.py").write_text(
        'def greet(name: str) -> str:\n    """问候"""\n    return f"hi {name}"\n',
        encoding="utf-8")
    packs.invalidate_scan_cache()


# ------------------------------------------------------------- AC1 调岗全口径


def test_ac1_sso_department_unified_across_layers(monkeypatch):
    """AC1（报告验收 1）：本地表部门 hr、SSO 部门头 finance——HTTP 层
    resolve_identity 与执行层（current_department/current_role/提示词包片段）
    必须同取 finance。"""
    monkeypatch.setattr(config, "MOCK_LLM", False)
    monkeypatch.setattr(config, "AUTH_IDENTITY_DEPARTMENT_HEADER", "X-Dept")
    STATE.llm = None
    assert auth.add_user("u1", role="user", department="hr")

    # HTTP 层口径（既有行为，对照基准）
    ident = auth.resolve_identity("", "u1", "finance")
    assert ident["department"] == "finance" and ident["role"] == "user"

    _mk_pack("hr-pack", ["hr"])
    _mk_pack("fin-pack", ["finance"])
    identity = usage.Identity(username="u1", role="user",
                              department="finance", auth_source="sso")

    def probe():
        return {
            "department": auth.current_department(),
            "role": auth.current_role(),
            "fragments": packs.prompt_fragments(
                role=auth.current_role(), department=auth.current_department()),
        }

    (rec,) = _run_with_probe(probe, identity=identity)
    # 执行层与 HTTP 层同口径：SSO finance，而非本地表 hr
    assert rec["department"] == "finance"
    assert rec["role"] == "user"
    assert "fin-pack 的领域提示词片段" in rec["fragments"]
    assert "hr-pack 的领域提示词片段" not in rec["fragments"]


# ------------------------------------------------------------- AC2 撤权即时性


def test_ac2_revocation_immediate_without_local_sync(monkeypatch):
    """AC2（报告验收 2）：本地表仍 hr（尚未同步调岗），SSO 头 finance——
    执行层的知识库定位判定（kb_allowed，与原文端点同一函数）对 hr 库立即
    拒绝、对 finance 库放行。"""
    monkeypatch.setattr(config, "MOCK_LLM", False)
    STATE.llm = None
    assert auth.add_user("u2", role="user", department="hr")
    identity = usage.Identity(username="u2", role="user",
                              department="finance", auth_source="sso")

    def probe():
        from server.main import kb_allowed   # 原文端点同一判定函数
        dept = auth.current_department()
        return (kb_allowed("kb-hr", "user", dept, _KB_PACKS),
                kb_allowed("kb-fin", "user", dept, _KB_PACKS))

    (hr_ok, fin_ok), = _run_with_probe(probe, sid="s-id2", user_id="u2",
                                       identity=identity)
    assert hr_ok is False, "调岗后 hr 库须立即不可访问（不等本地表同步）"
    assert fin_ok is True


# ------------------------------------------------------------- AC3 空部门 fail-closed


def test_ac3_empty_sso_department_failclosed(monkeypatch):
    """AC3（报告验收 3）：已配置部门头且 SSO 头部门为空 → department=""
    （IdP 权威，不回退本地表 hr），部门白名单 fail-closed；Bearer 路径 →
    本地表部门；开放模式 → 既有放行契约。"""
    monkeypatch.setattr(config, "MOCK_LLM", False)
    monkeypatch.setattr(config, "AUTH_IDENTITY_DEPARTMENT_HEADER", "X-Dept")
    STATE.llm = None
    key = auth.add_user("u3", role="user", department="hr")
    assert key

    # SSO 头部门为空：fail-closed ""（红：现实现回退本地表 "hr"）
    ident = auth.resolve_identity("", "u3", "")
    assert ident["department"] == ""
    # Bearer 路径：无 SSO 声明 → 本地表部门（三路径对比）
    assert auth.resolve_identity(key)["department"] == "hr"

    identity = usage.Identity(username="u3", role="user",
                              department="", auth_source="sso")

    def probe():
        from server.main import kb_allowed
        dept = auth.current_department()
        return dept, kb_allowed("kb-hr", "user", dept, _KB_PACKS)

    (dept, hr_ok), = _run_with_probe(probe, sid="s-id3", user_id="u3",
                                     identity=identity)
    assert dept == ""
    assert hr_ok is False, "空部门不命中任何部门白名单（fail-closed）"

    # 开放模式契约：Identity("anonymous","admin","","open")
    def probe_open():
        return auth.current_role(), auth.current_department()

    open_identity = usage.Identity(username="anonymous", role="admin",
                                   department="", auth_source="open")
    (role, dept), = _run_with_probe(probe_open, sid="s-id3b",
                                    user_id="anonymous",
                                    identity=open_identity)
    assert role == "admin" and dept == ""


# ------------------------------------------------------------- AC4 并发隔离


def test_ac4_concurrent_identity_isolation(monkeypatch):
    """AC4（报告验收 4）：两并发对话身份互不串用——A：本地 hr/SSO finance；
    B：本地 finance/SSO hr。事件驱动确定性交错（各自记录两次，零 sleep）。"""
    monkeypatch.setattr(config, "MOCK_LLM", False)
    STATE.llm = None
    assert auth.add_user("ua", role="user", department="hr")
    assert auth.add_user("ub", role="user", department="finance")

    ev_a, ev_b = asyncio.Event(), asyncio.Event()
    recs: dict[str, list[str]] = {"s-ca": [], "s-cb": []}

    class _RaceAgent:
        async def astream_events(self, payload, config=None, version="v2"):
            sid = config["configurable"]["thread_id"]
            mine, other = (ev_a, ev_b) if sid == "s-ca" else (ev_b, ev_a)
            recs[sid].append(auth.current_department())
            mine.set()                       # 双方都已读取一次后才放行
            await other.wait()
            recs[sid].append(auth.current_department())
            return
            yield

        async def aget_state(self, cfg):
            return SimpleNamespace(tasks=[])

    STATE.agent = _RaceAgent()
    agent._rebuild_graph()

    id_a = usage.Identity(username="ua", role="user",
                          department="finance", auth_source="sso")
    id_b = usage.Identity(username="ub", role="user",
                          department="hr", auth_source="sso")

    async def _both():
        await asyncio.gather(
            _drain("s-ca", "ua", id_a), _drain("s-cb", "ub", id_b))

    async def _drain(sid, uid, ident):
        async for _ in agent.stream_reply(
                [{"role": "user", "content": "并发"}], session_id=sid,
                user_id=uid, run_id="", identity=ident):
            pass

    asyncio.run(_both())
    assert recs["s-ca"] == ["finance", "finance"], recs
    assert recs["s-cb"] == ["hr", "hr"], recs


# ------------------------------------------------------------- AC5 凭据优先级一致性


def _mk_password_user(username, department=""):
    assert auth.add_user(username, role="user", department=department)
    assert auth.set_password(username, "password123")


def _login_cookie(api, username):
    r = api.post("/api/auth/login",
                 json={"username": username, "password": "password123"})
    assert r.status_code == 200


def test_ac5_credential_precedence(api, monkeypatch):
    """AC5（用户补充关注点）：三凭据共存的解析优先级与一致性——
    ①SSO 头+有效 Cookie → SSO 身份整体生效（username/department 同取 SSO，
      不得出现「名取 SSO、部门取 Cookie」的跨身份混用）；
    ②SSO 头用户不存在+有效 Cookie → 401，不回退 Cookie；
    ③Bearer 失效+Cookie 有效 → Cookie 身份（v0.17.37 迁移行为固化）；
    ④非白名单来源 SSO 头+Cookie → 头忽略、Cookie 生效。"""
    _mk_password_user("ua", department="finance")
    _mk_password_user("ub", department="hr")
    monkeypatch.setattr(config, "AUTH_IDENTITY_HEADER", "X-User")
    monkeypatch.setattr(config, "AUTH_IDENTITY_DEPARTMENT_HEADER", "X-Dept")

    # ①SSO 头（受信）+ Cookie(ub) → 整体取 SSO 的 ua/finance
    monkeypatch.setattr(auth, "proxy_ip_allowed", lambda host: True)
    _login_cookie(api, "ub")
    r = api.get("/api/health", headers={"X-User": "ua", "X-Dept": "finance"})
    ident = r.json()["identity"]
    assert ident["username"] == "ua" and ident["department"] == "finance"

    # ②SSO 头用户不存在 + Cookie(ub) → 401（fail-closed 不回退）
    r = api.get("/api/sessions", headers={"X-User": "ghost"})
    assert r.status_code == 401

    # ③Bearer 失效 + Cookie(ub) → Cookie 身份生效
    r = api.get("/api/health", headers={"Authorization": "Bearer pak-expired"})
    assert r.json()["identity"]["username"] == "ub"

    # ④非白名单来源 SSO 头 + Cookie(ub) → 头忽略、Cookie 生效
    monkeypatch.setattr(auth, "proxy_ip_allowed", lambda host: False)
    r = api.get("/api/health", headers={"X-User": "ua", "X-Dept": "finance"})
    assert r.json()["identity"]["username"] == "ub"


def test_ac5_chat_passes_request_identity(api, monkeypatch):
    """AC5⑤：chat 端点把中间件解析的同一份身份（不可变快照）传入执行层——
    SSO 请求的 auth_source/role/department 与 request.state.user 同源一致。"""
    _mk_password_user("uc", department="hr")
    monkeypatch.setattr(config, "AUTH_IDENTITY_HEADER", "X-User")
    monkeypatch.setattr(config, "AUTH_IDENTITY_DEPARTMENT_HEADER", "X-Dept")
    monkeypatch.setattr(auth, "proxy_ip_allowed", lambda host: True)

    captured: dict = {}

    async def fake_stream(messages, session_id="default", user_id="anonymous",
                          run_id="", **kw):
        captured.update(kw)
        captured["user_id"] = user_id
        yield {"type": "done", "usage": {}}

    monkeypatch.setattr(agent, "stream_reply", fake_stream)
    r = api.post("/api/chat", headers={"X-User": "uc", "X-Dept": "finance"},
                 json={"session_id": "s-ci", "message": "hi"})
    assert r.status_code == 200
    ident = captured.get("identity")
    assert ident is not None, "chat 端点须把请求身份传入 stream_reply"
    assert (ident.username, ident.role, ident.department, ident.auth_source) \
        == ("uc", "user", "finance", "sso")


def test_ac5_open_mode_identity_username_aligned(api, monkeypatch):
    """AC5⑥（评审 minor-1 修复）：开放模式 chat——identity.username 与请求体
    自报 user_id 对齐（不再固定 "anonymous"），role/department/auth_source
    维持旧版全局开放契约。"""
    monkeypatch.setattr(config, "AUTH_ENABLED", False)

    captured: dict = {}

    async def fake_stream(messages, session_id="default", user_id="anonymous",
                          run_id="", **kw):
        captured.update(kw)
        yield {"type": "done", "usage": {}}

    monkeypatch.setattr(agent, "stream_reply", fake_stream)
    r = api.post("/api/chat",
                 json={"session_id": "s-open", "message": "hi",
                       "user_id": "visitor"})
    assert r.status_code == 200
    ident = captured.get("identity")
    assert ident is not None
    assert (ident.username, ident.role, ident.department, ident.auth_source) \
        == ("visitor", "admin", "", "open")


# ------------------------------------------------------------- AC6 执行层禁查表


def test_ac6_execution_layer_never_requeries(monkeypatch):
    """AC6（修复建议 2）：执行层禁止按用户名重查重建——user_by_name 爆炸时，
    对话内 current_role/current_department 仍返回请求身份值。"""
    monkeypatch.setattr(config, "MOCK_LLM", False)
    STATE.llm = None
    assert auth.add_user("u6", role="user", department="hr")

    def _boom(username):
        raise RuntimeError("执行层不得重查用户表")

    monkeypatch.setattr(auth, "user_by_name", _boom)
    identity = usage.Identity(username="u6", role="user",
                              department="finance", auth_source="sso")

    def probe():
        return auth.current_department(), auth.current_role()

    (rec,) = _run_with_probe(probe, sid="s-id6", user_id="u6",
                             identity=identity)
    assert rec == ("finance", "user")
