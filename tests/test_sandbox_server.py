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
