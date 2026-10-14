"""telemetry：Langfuse 观测接入——启用开关、连接冷却重试、回调装配、埋点降级。
全部离线（不真实连接观测服务；客户端构造与回调模块均打桩）。"""
import sys
import time
import types
from contextlib import contextmanager

import pytest

from server import telemetry

try:
    import langfuse as _langfuse_pkg      # 已装则可打桩客户端构造
except ImportError:
    _langfuse_pkg = None


@pytest.fixture(autouse=True)
def _restore_globals():
    """模块全局态（客户端与冷却时间戳）测试间复位。"""
    client, nxt = telemetry._client, telemetry._next_retry
    yield
    telemetry._client, telemetry._next_retry = client, nxt


# ---------------------------------------------------------------- 启用开关

def test_enabled_env_switch(monkeypatch):
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    assert telemetry.enabled() is True
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    assert telemetry.enabled() is False
    monkeypatch.delenv("LANGFUSE_ENABLED")
    assert telemetry.enabled() is False           # 默认关闭，零侵入


# ---------------------------------------------------------------- get_client

def test_get_client_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("LANGFUSE_ENABLED", "false")
    telemetry._client = None
    telemetry._next_retry = 0.0
    assert telemetry.get_client() is None
    assert telemetry._next_retry == 0.0           # 未启用不探测


@pytest.mark.skipif(_langfuse_pkg is None, reason="langfuse 未安装")
def test_get_client_success_cached(monkeypatch):
    """连接成功 → 全局缓存，后续请求直接复用（不再探测）。"""
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    telemetry._client = None
    telemetry._next_retry = 0.0
    calls = {"auth": 0}

    class _Ok:
        def __init__(self, **kw):
            pass

        def auth_check(self):
            calls["auth"] += 1
            return True

    monkeypatch.setattr(_langfuse_pkg, "Langfuse", _Ok)
    c1 = telemetry.get_client()
    assert c1 is not None
    c2 = telemetry.get_client()
    assert c2 is c1 and calls["auth"] == 1         # 缓存命中


@pytest.mark.skipif(_langfuse_pkg is None, reason="langfuse 未安装")
def test_get_client_auth_fail_enters_cooldown(monkeypatch):
    """认证失败 → 进入冷却期；冷却期内再调用直接短路（不再探测）。"""
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    telemetry._client = None
    telemetry._next_retry = 0.0
    probes = {"n": 0}

    class _AuthFail:
        def __init__(self, **kw):
            probes["n"] += 1

        def auth_check(self):
            return False

    monkeypatch.setattr(_langfuse_pkg, "Langfuse", _AuthFail)
    assert telemetry.get_client() is None
    assert telemetry._next_retry > 0.0            # 冷却已登记
    assert telemetry.get_client() is None         # 冷却期内不再探测
    assert probes["n"] == 1


@pytest.mark.skipif(_langfuse_pkg is None, reason="langfuse 未安装")
def test_get_client_exception_enters_cooldown(monkeypatch):
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    telemetry._client = None
    telemetry._next_retry = 0.0

    class _Boom:
        def __init__(self, **kw):
            raise ConnectionError("refused")

    monkeypatch.setattr(_langfuse_pkg, "Langfuse", _Boom)
    assert telemetry.get_client() is None
    assert telemetry._next_retry > 0.0


def test_get_client_uninstalled_degrades(monkeypatch):
    """langfuse 包缺失 → 异常被吞、进入冷却，主链路不受影响。"""
    if _langfuse_pkg is not None:
        pytest.skip("本环境已安装 langfuse，ImportError 路径由打桩用例覆盖")
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    telemetry._client = None
    telemetry._next_retry = 0.0
    assert telemetry.get_client() is None
    assert telemetry._next_retry > 0.0


# ---------------------------------------------------------------- 回调装配

def test_langchain_callbacks_empty_without_client():
    telemetry._client = None
    assert telemetry.langchain_callbacks() == []


def test_langchain_callbacks_with_fake_handler(monkeypatch):
    telemetry._client = object()                   # 有客户端 → 走回调装配
    fake = types.ModuleType("langfuse.langchain")

    class CallbackHandler:
        pass

    fake.CallbackHandler = CallbackHandler
    if "langfuse" not in sys.modules:
        monkeypatch.setitem(sys.modules, "langfuse", types.ModuleType("langfuse"))
    monkeypatch.setitem(sys.modules, "langfuse.langchain", fake)
    handlers = telemetry.langchain_callbacks()
    assert len(handlers) == 1 and isinstance(handlers[0], CallbackHandler)


def test_langchain_callbacks_import_failure_degrades(monkeypatch):
    """回调模块导入失败 → 空列表，按无观测运行。"""
    telemetry._client = object()
    if _langfuse_pkg is not None:
        monkeypatch.setitem(sys.modules, "langfuse.langchain", None)  # None → ImportError
    assert telemetry.langchain_callbacks() == []


# ---------------------------------------------------------------- 手动埋点

def test_trace_span_noop_without_client():
    telemetry._client = None
    with telemetry.trace_span("x") as obs:
        assert obs is None                          # 业务照常执行


def test_trace_span_yields_observation():
    calls = []

    class _Cli:
        @contextmanager
        def start_as_current_observation(self, name, as_type, input):
            calls.append((name, as_type, input))
            yield {"id": 1}

    telemetry._client = _Cli()
    with telemetry.trace_span("span1", input={"a": 1}) as obs:
        assert obs == {"id": 1}
    assert calls == [("span1", "span", {"a": 1})]


def test_trace_span_exception_degrades():
    """埋点抛异常 → yield None，业务代码不受影响。"""
    class _Cli:
        @contextmanager
        def start_as_current_observation(self, **kw):
            raise RuntimeError("boom")
            yield                                     # pragma: no cover

    telemetry._client = _Cli()
    with telemetry.trace_span("s") as obs:
        assert obs is None


# ---------------------------------------------------------------- trace IO 与冲刷

def test_set_trace_io_and_flush_without_client():
    telemetry._client = None
    telemetry.set_trace_io(input={"i": 1})            # 无客户端不炸
    telemetry.flush()                                 # 无客户端不炸


def test_set_trace_io_and_flush_with_client():
    recorded = {}

    class _Cli:
        def set_current_trace_io(self, input=None, output=None):
            recorded.update({"input": input, "output": output})

        def flush(self):
            recorded["flushed"] = True

    telemetry._client = _Cli()
    telemetry.set_trace_io(input={"i": 1}, output={"o": 2})
    assert recorded == {"input": {"i": 1}, "output": {"o": 2}}
    telemetry.flush()
    assert recorded["flushed"] is True


def test_set_trace_io_and_flush_swallows_errors():
    class _Cli:
        def set_current_trace_io(self, **kw):
            raise RuntimeError("x")

        def flush(self):
            raise RuntimeError("x")

    telemetry._client = _Cli()
    telemetry.set_trace_io(input={})                  # 异常被吞
    telemetry.flush()                                  # 异常被吞
