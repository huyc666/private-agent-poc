"""custom_tools：自定义工具注册中心（静态校验 / 专属域归属校验 / fail-closed 执行）。"""
import asyncio
import os
import time

from server import config, custom_tools, usage

VALID_CODE = (
    'def demo_tool(x: int = 1) -> str:\n'
    '    """演示工具。x 为整数。"""\n'
    '    return f"x={x}"\n'
)

OTHER_CODE = (
    'def other_tool(y: str = "a") -> str:\n'
    '    """另一个工具。"""\n'
    '    return y\n'
)


def _code_for(name: str) -> str:
    """按工具名生成合法源码（文件名 = 函数名是落盘校验约定）。"""
    return (f'def {name}(x: int = 1) -> str:\n'
            f'    """演示工具。"""\n'
            f'    return f"x={{x}}"\n')


# ---------------------------------------------------------------- 静态校验

def test_validate_accepts_valid_code():
    assert custom_tools.validate_tool_code("demo_tool", VALID_CODE) is None


def test_validate_rejects_rule_violations():
    assert "工具名" in custom_tools.validate_tool_code("Bad-Name", VALID_CODE)
    assert "上限" in custom_tools.validate_tool_code(
        "big_tool", "def big_tool():\n    'doc'\n" + "x = 1\n" + "#" * 21_000)
    assert "语法错误" in custom_tools.validate_tool_code("demo_tool", "def broken(:\n")
    assert "docstring" in custom_tools.validate_tool_code(
        "demo_tool", "def demo_tool():\n    return 1\n")
    assert "*args" in custom_tools.validate_tool_code(
        "demo_tool", "def demo_tool(*args):\n    '""\"doc""\"'\n")
    assert "顶层函数" in custom_tools.validate_tool_code(
        "demo_tool", "def other_name():\n    '""\"doc""\"'\n")


# ---------------------------------------------------------------- 落盘与索引

def test_write_custom_tool_global_path():
    out = custom_tools.write_custom_tool("demo_tool", VALID_CODE)
    assert "custom_tools/demo_tool.py" in out
    assert (config.CUSTOM_TOOLS_DIR / "demo_tool.py").is_file()
    assert [t["name"] for t in custom_tools.list_custom_tools()] == ["demo_tool"]


def test_write_custom_tool_rejects_invalid():
    assert "创建失败" in custom_tools.write_custom_tool("demo_tool", "def broken(:")
    assert "创建失败" in custom_tools.write_custom_tool("9bad", VALID_CODE)


def test_write_personal_tool_routes_and_shadow_guard():
    custom_tools.write_custom_tool("shadowed", _code_for("shadowed"))   # 通用层已有
    out = custom_tools.write_custom_tool("shadowed", _code_for("shadowed"), scope="alice")
    assert "同名" in out and "遮蔽" in out                    # 专属与通用同名拒绝
    out = custom_tools.write_custom_tool("mine_tool", _code_for("mine_tool"), scope="alice")
    assert "custom_tools_personal/alice/mine_tool.py" in out
    # 索引按域过滤：本人可见
    assert [t["name"] for t in custom_tools.list_personal_tools("alice")] == ["mine_tool"]
    # 管理角色全局视图带域标注
    all_view = custom_tools.list_personal_all()
    assert any(t["scope"] == "alice" and t["name"] == "mine_tool" for t in all_view)


def test_list_personal_tools_skips_shadowed():
    custom_tools.write_custom_tool("shadowed", _code_for("shadowed"))
    custom_tools.write_custom_tool("ok_tool", _code_for("ok_tool"), scope="alice")
    # 手放同名专属文件（绕过写入拦截）
    pdir = config.PERSONAL_CUSTOM_TOOLS_DIR / "alice"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "shadowed.py").write_text(_code_for("shadowed"))
    names = [t["name"] for t in custom_tools.list_personal_tools("alice")]
    assert "shadowed" not in names and "ok_tool" in names


def test_broken_file_listed_with_marker():
    p = config.CUSTOM_TOOLS_DIR / "broken.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("def broken(:\n", encoding="utf-8")
    rows = custom_tools.list_custom_tools()
    assert rows[0]["description"].startswith("⚠️")


def test_delete_custom_tool_file():
    custom_tools.write_custom_tool("demo_tool", VALID_CODE)
    assert "已删除" in custom_tools.delete_custom_tool_file("demo_tool")
    assert "不存在" in custom_tools.delete_custom_tool_file("demo_tool")
    assert "非法工具名" in custom_tools.delete_custom_tool_file("Bad")


# ---------------------------------------------------------------- 执行模式

def test_exec_mode_local_without_sandbox():
    assert custom_tools.exec_mode() == "local"          # auto + SANDBOX_URL 空 → local


def test_exec_mode_sandbox_down_fail_closed(monkeypatch):
    monkeypatch.setenv("CUSTOM_TOOLS_EXEC", "sandbox")
    monkeypatch.setattr(config, "SANDBOX_URL", "")
    custom_tools._up_cache["t"] = 0.0
    assert custom_tools.exec_mode() == "sandbox-down"   # 强制模式不可达 fail-closed


# ---------------------------------------------------------------- 专属工具归属校验

def test_resolve_personal_user_role_boundaries(login_as):
    login_as("alice")
    custom_tools.write_custom_tool("mine_tool", _code_for("mine_tool"), scope="alice")
    custom_tools.write_custom_tool("global_one", _code_for("global_one"))

    scope, path = custom_tools._resolve_personal("mine_tool")
    assert scope == "alice" and path.is_file()
    # 跨域访问被拒
    assert "不属于你的专属域" in custom_tools._resolve_personal("bob/mine_tool")
    # 不存在
    assert "不存在或不可见" in custom_tools._resolve_personal("ghost_tool")
    # 通用工具名提示直呼
    assert "已直接挂载" in custom_tools._resolve_personal("global_one")
    # 非法名
    assert "非法工具名" in custom_tools._resolve_personal("Bad-Name")


def test_resolve_personal_admin_disambiguation(login_as):
    login_as("admin1", role="admin")
    custom_tools.write_custom_tool("shared_name", _code_for("shared_name"), scope="alice")
    custom_tools.write_custom_tool("shared_name", _code_for("shared_name"), scope="bob")
    assert "多个专属域" in custom_tools._resolve_personal("shared_name")
    scope, _ = custom_tools._resolve_personal("alice/shared_name")
    assert scope == "alice"


def test_resolve_personal_open_mode_rejected(monkeypatch, login_as):
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    assert "开放模式" in custom_tools._resolve_personal("mine_tool")


def test_run_personal_tool_local_mode_fail_closed(login_as):
    """专属工具未经审批 → 不允许主进程内执行（须沙箱隔离）。"""
    login_as("alice")
    custom_tools.write_custom_tool("mine_tool", _code_for("mine_tool"), scope="alice")
    out = asyncio.run(custom_tools.run_personal_tool("mine_tool", {"x": 1}))
    assert "fail-closed" in out and "沙箱" in out


def test_run_personal_tool_args_must_be_dict(login_as):
    login_as("alice")
    out = asyncio.run(custom_tools.run_personal_tool("mine_tool", "not-dict"))
    assert "args 必须是参数对象" in out


# ---------------------------------------------------------------- 签名提取与热重载

def test_extract_signature_params_and_defaults():
    custom_tools.write_custom_tool("demo_tool", VALID_CODE)
    meta = custom_tools._extract_signature(config.CUSTOM_TOOLS_DIR / "demo_tool.py")
    assert meta["name"] == "demo_tool"
    assert meta["params"][0]["required"] is False       # 有默认值 → 可选
    assert meta["params"][0]["default"] == 1
    assert meta["description"].startswith("演示工具")


def test_signature_tracks_nanosecond_changes():
    custom_tools.write_custom_tool("demo_tool", VALID_CODE)
    sig1 = custom_tools.signature()
    p = config.CUSTOM_TOOLS_DIR / "demo_tool.py"
    ns = time.time_ns() + 1_000_000
    os.utime(p, ns=(ns, ns))
    assert custom_tools.signature() != sig1


def test_personal_index_prompt_includes_own_tools(login_as):
    login_as("alice")
    custom_tools.write_custom_tool("mine_tool", _code_for("mine_tool"), scope="alice")
    prompt = custom_tools.personal_index_prompt()
    assert "mine_tool" in prompt and "专属自定义工具" in prompt


def test_personal_index_prompt_empty_for_user_without_tools(login_as):
    login_as("alice")
    assert custom_tools.personal_index_prompt() == ""
