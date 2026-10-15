"""包级 RBAC（PLAN 1.2）：pack.yaml allowed_roles 解析校验（fail-closed）、
执行侧角色拦截（最终权限边界，同步/异步双路径）、提示层片段按角色过滤、
动态提示词注入、管理视图标注与改盘热生效。"""
import asyncio

import pytest
import yaml

from server import agent, config, custom_tools, packs

TOOL_SRC = ('def greet(name: str) -> str:\n'
            '    """问候"""\n'
            '    return f"hi {name}"\n')
ASYNC_SRC = ('async def afetch(url: str) -> str:\n'
             '    """异步抓取"""\n'
             '    return f"got {url}"\n')


@pytest.fixture
def packs_dir():
    """隔离 PACKS_DIR（conftest isolated_env 已指向 tmp_path/skills 之外的本测试域）。"""
    return config.PACKS_DIR


def _mk_pack(root, name, roles=None, tools=("tools/greet.py",), files=None):
    """在 tmp PACKS_DIR 下落一个带 prompt.md 的包；roles=None 表示不配置白名单。"""
    manifest = {"name": name, "version": "0.1.0", "description": f"{name} 包",
                "core_version": ">=0.1.0", "tools": list(tools),
                "prompt": "prompt.md"}
    if roles is not None:
        manifest["allowed_roles"] = roles
    all_files = {"prompt.md": f"{name} 的领域提示词片段",
                 "tools/greet.py": TOOL_SRC}
    all_files.update(files or {})
    d = root / name
    d.mkdir(parents=True)
    (d / "pack.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    for rel, text in all_files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    packs.invalidate_scan_cache()
    return d


def _pack_tools() -> dict:
    """挂载全部启用包工具（测试默认 local 模式，含 RBAC 包装），按名索引。"""
    tools = packs.load_pack_tools()
    assert tools, "包工具未挂载"
    return {t.name: t for t in tools}


# ---------------------------------------------------------------- 清单解析

def test_parse_roles_absent_unrestricted(packs_dir):
    _mk_pack(packs_dir, "open-pack")
    p = packs.scan_packs()[0]
    assert p["status"] == "ok"
    assert p["allowed_roles"] == []


def test_parse_roles_valid_dedup_sorted(packs_dir):
    _mk_pack(packs_dir, "rbac-pack", roles=["user", "approver", "user"])
    p = packs.scan_packs()[0]
    assert p["status"] == "ok"
    assert p["allowed_roles"] == ["approver", "user"]


def test_parse_roles_empty_list_error(packs_dir):
    _mk_pack(packs_dir, "bad-pack", roles=[])
    p = packs.scan_packs()[0]
    assert p["status"] == "error"
    assert "非空" in p["error"]


def test_parse_roles_invalid_role_error(packs_dir):
    _mk_pack(packs_dir, "typo-pack", roles=["approver", "owner"])
    p = packs.scan_packs()[0]
    assert p["status"] == "error"
    assert "owner" in p["error"]
    assert "user" in p["error"]        # 错误文案列出合法值


def test_parse_roles_not_list_error(packs_dir):
    _mk_pack(packs_dir, "str-pack", roles="approver")   # 字符串而非列表
    p = packs.scan_packs()[0]
    assert p["status"] == "error"


# ---------------------------------------------------------------- 执行侧拦截

def test_tool_denied_for_user_outside_whitelist(packs_dir, login_as):
    _mk_pack(packs_dir, "rbac-pack", roles=["approver"])
    login_as("alice", role="user")
    out = _pack_tools()["greet"].invoke({"name": "bob"})
    assert "仅限" in out and "approver" in out


def test_tool_allowed_for_role_in_whitelist(packs_dir, login_as):
    _mk_pack(packs_dir, "rbac-pack", roles=["approver"])
    login_as("appr1", role="approver")
    assert _pack_tools()["greet"].invoke({"name": "bob"}) == "hi bob"


def test_tool_allowed_for_admin_even_outside_whitelist(packs_dir, login_as):
    _mk_pack(packs_dir, "rbac-pack", roles=["user"])
    login_as("root1", role="admin")
    assert _pack_tools()["greet"].invoke({"name": "bob"}) == "hi bob"


def test_tool_unrestricted_pack_all_roles(packs_dir, login_as):
    _mk_pack(packs_dir, "open-pack")
    login_as("alice", role="user")
    assert _pack_tools()["greet"].invoke({"name": "x"}) == "hi x"


def test_tool_async_denied_and_allowed(packs_dir, login_as):
    _mk_pack(packs_dir, "rbac-pack", roles=["approver"],
             tools=["tools/greet.py", "tools/afetch.py"],
             files={"tools/afetch.py": ASYNC_SRC})
    tool = _pack_tools()["afetch"]
    login_as("alice", role="user")
    assert "仅限" in asyncio.run(tool.coroutine(url="u"))
    login_as("appr1", role="approver")
    assert asyncio.run(tool.coroutine(url="u")) == "got u"


def test_role_denied_helper_semantics(packs_dir, login_as):
    login_as("alice", role="user")
    assert custom_tools.pack_role_denied("p", ["approver"]) is not None
    assert custom_tools.pack_role_denied("p", []) is None          # 未配置不限制
    assert custom_tools.pack_role_denied("p", ["user"]) is None    # 白名单命中
    login_as("root1", role="admin")
    assert custom_tools.pack_role_denied("p", ["user"]) is None    # admin 全局视图
    assert custom_tools.pack_role_denied("p", ["approver"], role="approver") is None


# ---------------------------------------------------------------- 提示层过滤

def test_fragments_filtered_by_role(packs_dir):
    _mk_pack(packs_dir, "rbac-pack", roles=["approver"])
    _mk_pack(packs_dir, "open-pack")
    approver_view = packs.prompt_fragments(role="approver")
    assert "rbac-pack 的领域提示词片段" in approver_view
    assert "open-pack 的领域提示词片段" in approver_view
    user_view = packs.prompt_fragments(role="user")
    assert "rbac-pack 的领域提示词片段" not in user_view
    assert "open-pack 的领域提示词片段" in user_view


def test_fragments_role_none_unfiltered(packs_dir):
    _mk_pack(packs_dir, "rbac-pack", roles=["approver"])
    assert "rbac-pack 的领域提示词片段" in packs.prompt_fragments()


def test_dynamic_prompt_injects_role_filtered_fragments(packs_dir, login_as,
                                                        monkeypatch):
    _mk_pack(packs_dir, "rbac-pack", roles=["approver"])
    _mk_pack(packs_dir, "open-pack")
    monkeypatch.setattr(agent.STATE, "system_prompt_static", "静态基准",
                        raising=False)   # 建图前 AppState 无该属性
    login_as("alice", role="user")
    sys_text = agent._dynamic_prompt({"messages": []})[0].content
    assert sys_text.startswith("静态基准")
    assert "open-pack 的领域提示词片段" in sys_text
    assert "rbac-pack 的领域提示词片段" not in sys_text
    login_as("appr1", role="approver")
    sys_text = agent._dynamic_prompt({"messages": []})[0].content
    assert "rbac-pack 的领域提示词片段" in sys_text


# ---------------------------------------------------------------- 管理视图与热生效

def test_list_packs_exposes_allowed_roles(packs_dir):
    _mk_pack(packs_dir, "rbac-pack", roles=["approver"])
    info = {p["name"]: p for p in packs.list_packs()}["rbac-pack"]
    assert info["allowed_roles"] == ["approver"]


def test_manifest_edit_takes_effect_after_invalidate(packs_dir):
    _mk_pack(packs_dir, "rbac-pack", roles=None)
    yaml_path = packs_dir / "rbac-pack" / "pack.yaml"
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    data["allowed_roles"] = ["approver"]
    yaml_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    packs.invalidate_scan_cache()
    p = packs.scan_packs()[0]
    assert p["allowed_roles"] == ["approver"]
