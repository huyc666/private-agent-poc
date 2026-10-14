"""测试环境隔离。

关键约定：server.config 在 import 时读取环境变量，因此必须在任何 server
模块导入**之前**把全部路径类变量指到临时目录——本文件在 pytest 收集阶段
最先被加载，在模块顶层完成设置（绝不触碰项目运行数据 state.db / uploads/
outputs/ skills/ packs/ custom_tools/ .env）。

运行期隔离：config 的路径值（STATE_DB / *_DIR / TOOL_WORKSPACE 等）都在
函数调用时读取，autouse fixture 对每个测试再打补丁到 pytest 管理的
tmp_path，实现测试间零串扰。
"""
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---- import 前的全局环境（会话级兜底；dotenv 不会覆盖已存在的环境变量）----
_TMP = Path(tempfile.mkdtemp(prefix="pa_poc_tests_"))
_ENV_DEFAULTS = {
    "STATE_DB": str(_TMP / "state.db"),
    "USAGE_DB": str(_TMP / "usage.db"),
    "MOCK_LLM": "true",            # 不依赖任何 GPU/模型
    "AUTH_ENABLED": "true",        # 默认测认证路径；开放模式按用例单独关
    "MCP_ENABLED": "false",         # 不拉 MCP 子进程
    "TOOL_WORKSPACE": str(_TMP / "workspace"),
    "SKILLS_DIR": str(_TMP / "skills"),
    "PERSONAL_SKILLS_DIR": str(_TMP / "skills_personal"),
    "CUSTOM_TOOLS_DIR": str(_TMP / "custom_tools"),
    "PERSONAL_CUSTOM_TOOLS_DIR": str(_TMP / "custom_tools_personal"),
    "PACKS_DIR": str(_TMP / "packs"),
    "LANGFUSE_ENABLED": "false",   # 观测默认关闭
    "SANDBOX_URL": "",              # 沙箱不可达 → auto 模式回退 local / fail-closed
    "PACK_SIGNING_KEY": "",
    "AUTH_BOOTSTRAP_ADMIN": "",
}
for _k, _v in _ENV_DEFAULTS.items():
    os.environ[_k] = _v

import pytest  # noqa: E402  （须在 sys.path/env 设置之后）

from server import auth, config, custom_tools, packs, skills, usage  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """每个测试独立的状态库与目录（config 值在调用时读取，可安全打补丁）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(config, "STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setattr(config, "USAGE_DB", str(tmp_path / "usage.db"))
    monkeypatch.setattr(config, "TOOL_WORKSPACE", ws)
    monkeypatch.setattr(config, "UPLOADS_DIR", ws / "uploads")
    monkeypatch.setattr(config, "OUTPUTS_DIR", ws / "outputs")
    monkeypatch.setattr(config, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(config, "PERSONAL_SKILLS_DIR", tmp_path / "skills_personal")
    monkeypatch.setattr(config, "CUSTOM_TOOLS_DIR", tmp_path / "custom_tools")
    monkeypatch.setattr(config, "PERSONAL_CUSTOM_TOOLS_DIR", tmp_path / "custom_tools_personal")
    monkeypatch.setattr(config, "PACKS_DIR", tmp_path / "packs")
    # 模块级缓存 / 内存态复位
    packs.invalidate_scan_cache()
    packs._disabled.clear()
    skills.reset_extra_dirs()
    custom_tools._up_cache["t"] = 0.0
    # 对话上下文复位（多用户隔离用例依赖 usage.current_user）
    tokens = [usage.current_user.set("anonymous"),
              usage.current_session.set(""),
              usage.current_run.set("")]
    yield
    usage.current_user.reset(tokens[0])
    usage.current_session.reset(tokens[1])
    usage.current_run.reset(tokens[2])


@pytest.fixture
def make_user():
    """在隔离 STATE_DB 中创建用户（返回一次性 pak- Key）。"""
    def _make(username: str, role: str = "user") -> str:
        key = auth.add_user(username, role=role)
        assert key, f"用户 {username} 创建失败"
        return key
    return _make


@pytest.fixture
def login_as(make_user):
    """创建用户并切换对话上下文身份（工具侧权限判断据此取角色）。"""
    def _login(username: str, role: str = "user") -> str:
        make_user(username, role=role)
        usage.current_user.set(username)
        return username
    return _login


@pytest.fixture
def client_factory():
    """FastAPI TestClient 工厂（带 lifespan：mock 模式装配 Agent 图）。"""
    from fastapi.testclient import TestClient
    from server.main import app

    def _client():
        return TestClient(app)
    return _client
