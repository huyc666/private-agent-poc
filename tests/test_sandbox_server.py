"""沙箱服务（sandbox/server.py）：共享令牌 fail-closed 鉴权、子进程环境脱敏、
/run 与 /run_tool 端点校验。经 importlib 独立加载（sandbox/ 非包），
子进程执行真实 Python（-I 隔离模式），不依赖 Docker。"""
import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

_SB_PATH = Path(__file__).resolve().parent.parent / "sandbox" / "server.py"
_spec = importlib.util.spec_from_file_location("sbx_server_under_test", _SB_PATH)
sbx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sbx)


# ---------------------------------------------------------------- check_token

def test_check_token_configured_mismatch(monkeypatch):
    monkeypatch.setattr(sbx, "SANDBOX_AUTH_TOKEN", "shared-secret")
    with pytest.raises(HTTPException) as ei:
        sbx.check_token("wrong-token")
    assert ei.value.status_code == 401


def test_check_token_configured_match(monkeypatch):
    monkeypatch.setattr(sbx, "SANDBOX_AUTH_TOKEN", "shared-secret")
    assert sbx.check_token("shared-secret") is None


def test_check_token_unconfigured_fail_closed(monkeypatch):
    """未配置令牌 → 默认拒绝（503）；仅 SANDBOX_OPEN_MODE=true 显式放行。"""
    monkeypatch.setattr(sbx, "SANDBOX_AUTH_TOKEN", "")
    monkeypatch.setattr(sbx, "SANDBOX_OPEN_MODE", False)
    with pytest.raises(HTTPException) as ei:
        sbx.check_token("")
    assert ei.value.status_code == 503
    monkeypatch.setattr(sbx, "SANDBOX_OPEN_MODE", True)
    assert sbx.check_token("") is None


# ---------------------------------------------------------------- 子进程环境脱敏

def test_run_isolated_strips_auth_env(monkeypatch):
    """子进程环境必须剔除沙箱自身的鉴权配置——不可信代码不能读走令牌伪造调用方。"""
    monkeypatch.setenv("SANDBOX_AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("SANDBOX_OPEN_MODE", "true")
    code = ("import os\n"
            "print('TOK=' + ('present' if 'SANDBOX_AUTH_TOKEN' in os.environ else 'stripped'))\n"
            "print('OPEN=' + ('present' if 'SANDBOX_OPEN_MODE' in os.environ else 'stripped'))\n")
    res = sbx._run_isolated({"code": code})
    assert res["returncode"] == 0
    assert "TOK=stripped" in res["output"]
    assert "OPEN=stripped" in res["output"]


def test_run_isolated_extra_env(monkeypatch):
    monkeypatch.delenv("PA_SB_TEST_EXTRA", raising=False)
    res = sbx._run_isolated(
        {"code": "import os\nprint(os.environ['PA_SB_TEST_EXTRA'])\n"},
        extra_env={"PA_SB_TEST_EXTRA": "injected"})
    assert "injected" in res["output"]


# ---------------------------------------------------------------- /run 端点

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(sbx, "SANDBOX_AUTH_TOKEN", "shared-secret")
    return TestClient(sbx.app)


def test_run_ok(client):
    r = client.post("/run", json={"code": "print(1 + 1)"},
                    headers={"X-Sandbox-Token": "shared-secret"})
    assert r.status_code == 200
    assert "2" in r.json()["output"]


def test_run_unauthorized(client):
    assert client.post("/run", json={"code": "print(1)"}).status_code == 401
    assert client.post("/run", json={"code": "print(1)"},
                       headers={"X-Sandbox-Token": "bad"}).status_code == 401


def test_run_rejects_oversized_code(client):
    r = client.post("/run", json={"code": "x = 1\n" * 5000},
                    headers={"X-Sandbox-Token": "shared-secret"})
    assert "代码过长" in r.json()["error"]


# ---------------------------------------------------------------- /run_tool 端点

_ADD_TWO = "def add_two(a: int) -> str:\n    '''加二'''\n    return str(a + 2)\n"


def test_run_tool_code_direct(client):
    """源码直传（专属工具场景）：与 /run 同等隔离执行，用后即删临时目录。"""
    r = client.post("/run_tool",
                    json={"name": "add_two", "code": _ADD_TWO, "args": {"a": 3}},
                    headers={"X-Sandbox-Token": "shared-secret"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "result": "5"}


def test_run_tool_code_pack_mutex(client):
    r = client.post("/run_tool",
                    json={"name": "add_two", "code": _ADD_TWO,
                          "pack": "some-pack", "args": {}},
                    headers={"X-Sandbox-Token": "shared-secret"})
    assert "不能同时指定" in r.json()["error"]


def test_run_tool_bad_name(client):
    r = client.post("/run_tool", json={"name": "9Bad!", "args": {}},
                    headers={"X-Sandbox-Token": "shared-secret"})
    assert "非法工具名" in r.json()["error"]


def test_run_tool_args_too_big(client):
    r = client.post("/run_tool", json={"name": "add_two", "args": {"a": "x" * 9000}},
                    headers={"X-Sandbox-Token": "shared-secret"})
    assert "参数过大" in r.json()["error"]


def test_run_tool_bad_env_key(client):
    r = client.post("/run_tool",
                    json={"name": "add_two", "args": {}, "env": {"9bad": "x"}},
                    headers={"X-Sandbox-Token": "shared-secret"})
    assert "非法环境变量名" in r.json()["error"]


def test_run_tool_missing_in_tools_dir(client):
    """无 code 无 pack：从 TOOLS_DIR 取，文件不存在 → 明确报错。"""
    r = client.post("/run_tool",
                    json={"name": "no_such_tool_xyz", "args": {}},
                    headers={"X-Sandbox-Token": "shared-secret"})
    assert "不存在" in r.json()["error"]


# ---------------------------------------------------------------- 错误详情外泄收敛

def test_run_isolated_internal_error_no_exception_leak(monkeypatch):
    """编码规范批 3：沙箱内部意外异常的原文（可能含服务器绝对路径）不得随
    error 字段回传调用方（会一路透传给模型/用户），只回固定归类文案。"""
    def _boom(*a, **kw):
        raise RuntimeError(r"E:\deploy\private-agent-poc\sandbox boom")
    monkeypatch.setattr(sbx.subprocess, "run", _boom)
    res = sbx._run_isolated({"code": "print(1)"})
    assert res["error"] == "沙箱内部错误（已终止，详情见服务端日志）"
    assert "boom" not in res["error"]
    assert "E:\\" not in res["error"]


# ---- B 批次：拒绝路径与边界分支（覆盖率补齐，不堆正向用例）----

def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_run_isolated_timeout(monkeypatch):
    """执行超时 → 归类文案，不把异常原文外抛。"""
    def _timeout(*a, **kw):
        raise sbx.subprocess.TimeoutExpired("py", 1)
    monkeypatch.setattr(sbx.subprocess, "run", _timeout)
    assert "执行超时" in sbx._run_isolated({"code": "print(1)"})["error"]


def test_run_isolated_unlink_failure_ignored(monkeypatch):
    """临时文件删除失败（Windows 占用）→ 忽略，结果照常返回。"""
    monkeypatch.setattr(sbx.os, "unlink", lambda p: (_ for _ in ()).throw(OSError("busy")))
    res = sbx._run_isolated({"code": "print('ok')"})
    assert res["returncode"] == 0 and "ok" in res["output"]


def test_run_tool_code_too_long(client):
    r = client.post("/run_tool",
                    json={"name": "add_two", "code": "x" * 20_001, "args": {}},
                    headers={"X-Sandbox-Token": "shared-secret"})
    assert "代码过长" in r.json()["error"]


def test_run_tool_args_unserializable():
    """JSON 入参理论上可序列化；直接调用兜底分支，拒绝且不落临时目录。"""
    res = sbx.run_tool(sbx.ToolRequest(name="add_two", args={"o": object()}))
    assert "参数无法序列化" in res["error"]


def test_run_tool_env_limit_and_value_truncation(client):
    ok = client.post("/run_tool",
                     json={"name": "add_two", "code": _ADD_TWO,
                           "args": {"a": 1},
                           "env": {"OK_VAR": "y" * 5000}},
                     headers={"X-Sandbox-Token": "shared-secret"})
    assert ok.status_code == 200 and ok.json()["result"] == "3"

    many = {f"K{i}": "v" for i in range(51)}
    r = client.post("/run_tool",
                    json={"name": "add_two", "args": {}, "env": many},
                    headers={"X-Sandbox-Token": "shared-secret"})
    assert "环境变量过多" in r.json()["error"]


def test_run_tool_bad_pack_name_and_missing_tool(client):
    r = client.post("/run_tool",
                    json={"name": "add_two", "pack": "Bad Pack", "args": {}},
                    headers={"X-Sandbox-Token": "shared-secret"})
    assert "非法领域包名" in r.json()["error"]

    r = client.post("/run_tool",
                    json={"name": "no_such_tool", "pack": "demo", "args": {}},
                    headers={"X-Sandbox-Token": "shared-secret"})
    assert "不存在" in r.json()["error"]


def test_run_tool_source_write_failure(monkeypatch):
    """直传源码落盘失败 → 明确报错且清理临时目录（不残留未审批代码）。"""
    def _boom(*a, **kw):
        raise OSError("MARKER disk full")
    monkeypatch.setattr(sbx, "open", _boom, raising=False)
    res = sbx.run_tool(sbx.ToolRequest(name="add_two", code=_ADD_TWO, args={"a": 1}))
    assert res["error"] == "工具源码落盘失败"
    assert "MARKER" not in res["error"]


def test_run_tool_cleanup_failure_ignored(monkeypatch, client):
    """临时目录清理抛异常（Windows 句柄未释放）→ 忽略，仍返回执行结果。"""
    def _boom(*a, **kw):
        raise OSError("busy")
    monkeypatch.setattr(sbx.shutil, "rmtree", _boom)
    r = client.post("/run_tool",
                    json={"name": "add_two", "code": _ADD_TWO, "args": {"a": 2}},
                    headers={"X-Sandbox-Token": "shared-secret"})
    assert r.json()["result"] == "4"


def test_run_tool_run_error_passthrough_and_bad_result(monkeypatch):
    """执行层 error 原样返回；结果标记行不是合法 JSON → 归类为未返回结果。"""
    monkeypatch.setattr(sbx, "_run_isolated",
                        lambda *a, **kw: {"error": "沙箱内部错误（已终止，详情见服务端日志）"})
    assert "沙箱内部错误" in sbx.run_tool(
        sbx.ToolRequest(name="add_two", code=_ADD_TWO, args={}))["error"]

    monkeypatch.setattr(sbx, "_run_isolated",
                        lambda *a, **kw: {"output": "__RESULT__not-json\n"})
    assert "工具未返回结果" in sbx.run_tool(
        sbx.ToolRequest(name="add_two", code=_ADD_TWO, args={}))["error"]

    monkeypatch.setattr(sbx, "_run_isolated",
                        lambda *a, **kw: {"output": "plain output, no marker"})
    assert "工具未返回结果" in sbx.run_tool(
        sbx.ToolRequest(name="add_two", code=_ADD_TWO, args={}))["error"]
