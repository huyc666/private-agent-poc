"""Docker 环境集成测试（Langfuse 观测栈 / PostgreSQL checkpointer / 代码沙箱）。

默认跳过；设 RUN_DOCKER_TESTS=1 并启动对应栈后运行：
    # 1) Langfuse（本机惯例 3100 端口，预置密钥 pk-lf-local/sk-lf-local）
    $env:LANGFUSE_WEB_PORT="3100"; docker compose -f docker/docker-compose.langfuse.yml up -d
    # 2) PostgreSQL checkpointer
    docker compose -f docker/docker-compose.postgres.yml up -d
    # 3) 代码沙箱（令牌须与下方 SBX_TOKEN 一致）
    $env:SANDBOX_AUTH_TOKEN="it-sbx-token"; docker compose -f docker/docker-compose.sandbox.yml up -d --build
    # 运行
    $env:RUN_DOCKER_TESTS="1"; & .venv\\Scripts\\python.exe -B -m pytest tests/test_integration_docker.py -q
"""
import asyncio
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

import pytest

from server import agent, config, custom_tools, telemetry, tools_builtin
from server.state import STATE

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DOCKER_TESTS") != "1",
    reason="Docker 集成测试：设 RUN_DOCKER_TESTS=1 并启动对应 docker 栈后运行",
)

LANGFUSE_HOST = "http://localhost:3100"
PG_URL = "postgresql://postgres:postgres@127.0.0.1:5432/agent_poc"
SBX_URL = "http://127.0.0.1:9001"
SBX_TOKEN = "it-sbx-token"


# ---------------------------------------------------------------- Langfuse 观测

@pytest.fixture
def lf_live(monkeypatch):
    """连真实本地 Langfuse（3100）：env 打开 + telemetry 模块级缓存复位。"""
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_HOST", LANGFUSE_HOST)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-local")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-local")
    monkeypatch.setattr(telemetry, "_client", None)
    monkeypatch.setattr(telemetry, "_next_retry", 0.0)
    client = telemetry.get_client()
    if client is None:
        pytest.fail("Langfuse 连接失败：先启动 docker/docker-compose.langfuse.yml（3100 端口）")
    yield client
    telemetry.flush()


def test_langfuse_auth_and_callbacks(lf_live):
    """真实栈：auth_check 通过 + LangChain CallbackHandler 可装配。"""
    assert lf_live.auth_check() is True
    assert telemetry.langchain_callbacks(), "回调装配失败"


def test_langfuse_trace_roundtrip(lf_live):
    """埋点端到端：trace_span → set_trace_io → flush → 公开 API 可查到该 trace。"""
    marker = f"it-span-{uuid.uuid4().hex[:8]}"
    with telemetry.trace_span(marker, input={"q": "ping"}) as obs:
        assert obs is not None
        trace_id = obs.trace_id
        telemetry.set_trace_io(input={"q": "ping"}, output={"a": "pong"})
    telemetry.flush()

    auth = base64.b64encode(b"pk-lf-local:sk-lf-local").decode()
    req = urllib.request.Request(
        f"{LANGFUSE_HOST}/api/public/traces/{trace_id}",
        headers={"Authorization": f"Basic {auth}"})
    data = None
    for _ in range(15):                     # flush 后索引有延迟，轮询最多 15s
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            time.sleep(1)
    assert data and data.get("id") == trace_id
    assert data.get("output") == {"a": "pong"}       # set_trace_io 已写回 trace


# ---------------------------------------------------------------- PostgreSQL

def test_postgres_checkpointer_roundtrip(monkeypatch):
    """真实 postgres：_make_checkpointer 建表 + checkpoint 写读回路 + 关闭。
    Windows 上 psycopg 异步与默认 ProactorEventLoop 不兼容（agent.py 的
    fail-fast 提示同源），本用例改用 SelectorEventLoop 执行。"""
    pytest.importorskip("langgraph.checkpoint.postgres.aio")
    monkeypatch.setattr(config, "CHECKPOINTER", "postgres")
    monkeypatch.setattr(config, "DATABASE_URL", PG_URL)
    from langgraph.checkpoint.base import empty_checkpoint

    loop = (asyncio.SelectorEventLoop() if sys.platform == "win32"
            else asyncio.new_event_loop())

    async def _inner():
        await agent._make_checkpointer()
        assert STATE.checkpointer is not None
        cfg = {"configurable": {"thread_id": f"it-pg-{uuid.uuid4().hex[:8]}",
                                 "checkpoint_ns": ""}}
        ck = empty_checkpoint()
        await STATE.checkpointer.aput(cfg, ck, {"source": "input"}, {})
        got = await STATE.checkpointer.aget_tuple(cfg)
        assert got is not None and got.checkpoint["id"] == ck["id"]
        await agent.close_checkpointer()

    try:
        loop.run_until_complete(_inner())
    finally:
        loop.close()
        STATE.checkpointer = None
        STATE.checkpointer_cm = None


# ---------------------------------------------------------------- 代码沙箱

@pytest.fixture
def sbx_live(monkeypatch):
    """Agent 侧配置真实沙箱（9001）与共享令牌，并用一次真实执行探活。"""
    monkeypatch.setattr(config, "SANDBOX_URL", SBX_URL)
    monkeypatch.setattr(config, "SANDBOX_AUTH_TOKEN", SBX_TOKEN)
    out = tools_builtin.run_python_code("print('ping')")
    assert "ping" in out, ("沙箱不可达：先 $env:SANDBOX_AUTH_TOKEN="
                           "'it-sbx-token'; docker compose -f "
                           "docker/docker-compose.sandbox.yml up -d --build")
    return tools_builtin


def test_sandbox_run_basic(sbx_live):
    """真实执行：print(1+1) → 2。"""
    out = sbx_live.run_python_code("print(1+1)")
    assert "2" in out


def test_sandbox_egress_blocked(sbx_live):
    """出网封锁：容器内访问外网失败（iptables 规则真实生效）。"""
    code = ("import urllib.request\n"
            "try:\n"
            "    urllib.request.urlopen('https://www.baidu.com', timeout=4)\n"
            "    print('NET_OK')\n"
            "except Exception as e:\n"
            "    print('NET_BLOCKED', type(e).__name__)\n")
    out = sbx_live.run_python_code(code)
    assert "NET_BLOCKED" in out and "NET_OK" not in out


def test_sandbox_child_env_stripped(sbx_live):
    """子进程环境剔除鉴权令牌：沙箱内代码读不到 SANDBOX_AUTH_TOKEN。"""
    out = sbx_live.run_python_code(
        "import os\nprint('TKN=' + repr(os.environ.get('SANDBOX_AUTH_TOKEN')))")
    assert "TKN=None" in out


def test_sandbox_rejects_wrong_token(monkeypatch):
    """共享令牌校验：错误令牌 → 401（HTTPError，区别于不可达的 URLError）。"""
    with urllib.request.urlopen(SBX_URL + "/health", timeout=3) as r:
        assert r.status == 200               # 前置：栈在线，断言才有意义
    monkeypatch.setattr(config, "SANDBOX_URL", SBX_URL)
    monkeypatch.setattr(config, "SANDBOX_AUTH_TOKEN", "wrong-token")
    out = tools_builtin.run_python_code("print(1)")
    assert "HTTPError" in out                # 401 被拒


def test_sandbox_run_tool_code_direct(sbx_live):
    """/run_tool 源码直传（专属工具路径，v0.17.12）：隔离子进程执行 + __RESULT__ 协议。"""
    src = "def it_add(a: int, b: int = 1):\n    return a + b\n"
    out = custom_tools._sandbox_run_tool("it_add", {"a": 41, "b": 1}, code=src)
    assert out == "42"


def test_sandbox_run_tool_pack(sbx_live):
    """/run_tool 领域包路径：packs 只读卷内的 core-utils calculator 真实执行。"""
    out = custom_tools._sandbox_run_tool(
        "calculator", {"expression": "6 * 7"}, pack="core-utils")
    assert "42" in out
