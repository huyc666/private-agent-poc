"""领域包注册中心：清单解析校验 / 启用白名单与运行时禁用 / 包脚手架创建 /
工具挂载去重与 env 冲突告警 / 签名 / 删除。"""
import os

import pytest
import yaml

from server import config, custom_tools, packs, packsign

CORE = packs.CORE_VERSION


def _mk_pack(root, name, manifest: dict, files: dict | None = None):
    """在 tmp PACKS_DIR 下落一个包：manifest dict 写为 pack.yaml，files 为相对路径→内容。"""
    d = root / name
    d.mkdir(parents=True)
    (d / "pack.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    for rel, text in (files or {}).items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    packs.invalidate_scan_cache()
    return d


def _valid_manifest(name: str, **extra) -> dict:
    m = {"name": name, "version": "0.1.0", "description": "测试领域包",
         "core_version": f">=0.1.0"}
    m.update(extra)
    return m


TOOL_SRC = 'def greet(name: str) -> str:\n    """问候"""\n    return f"hi {name}"\n'


@pytest.fixture
def packs_dir():
    return config.PACKS_DIR


# ---------------------------------------------------------------- 清单解析

def test_parse_ok(packs_dir):
    _mk_pack(packs_dir, "demo-ops", _valid_manifest("demo-ops",
              tools=["tools/greet.py"], skills=["skills/demo"],
              prompt="prompt.md", env={"PA_DEMO_KEY": "v"}),
              {"tools/greet.py": TOOL_SRC,
               "skills/demo/SKILL.md": "---\nname: demo\n---\n指令",
               "prompt.md": "领域提示词"})
    packs_list = {p["name"]: p for p in packs.scan_packs()}
    p = packs_list["demo-ops"]
    assert p["status"] == "ok"
    assert [t.stem for t in p["tools"]] == ["greet"]
    assert [s.name for s in p["skills"]] == ["demo"]
    assert p["prompt"].endswith("prompt.md")
    assert p["env"] == {"PA_DEMO_KEY": "v"}
    assert p["trust"] == "unsigned"          # 开放模式（未配签名密钥）


def test_parse_missing_manifest(packs_dir):
    d = packs_dir / "no-yaml"
    d.mkdir(parents=True)
    p = packs.scan_packs()[0]
    assert p["status"] == "error"
    assert p["error"] == "缺少 pack.yaml"


def test_parse_name_mismatch(packs_dir):
    _mk_pack(packs_dir, "dir-name", _valid_manifest("other-name"))
    p = packs.scan_packs()[0]
    assert p["status"] == "error"
    assert "不一致" in p["error"]


def test_parse_bad_name(packs_dir):
    _mk_pack(packs_dir, "p1", _valid_manifest("Bad Name"))
    p = packs.scan_packs()[0]
    assert p["status"] == "error"
    assert "非法包名" in p["error"]


def test_parse_core_version_unsatisfied(packs_dir):
    _mk_pack(packs_dir, "future-pack", _valid_manifest("future-pack",
              core_version=">=999.0.0"))
    p = packs.scan_packs()[0]
    assert p["status"] == "unavailable"
    assert "内核版本" in p["error"]


def test_parse_missing_required_env(packs_dir):
    _mk_pack(packs_dir, "need-env", _valid_manifest("need-env",
              requires_env=["PA_TEST_MISSING_ENV_XYZ"]))
    p = packs.scan_packs()[0]
    assert p["status"] == "unavailable"
    assert "缺少环境变量" in p["error"]


def test_parse_missing_tool_file_skipped(packs_dir, capsys):
    _mk_pack(packs_dir, "ghost-tool", _valid_manifest("ghost-tool",
              tools=["tools/not_exist.py"]))
    p = packs.scan_packs()[0]
    assert p["status"] == "ok"
    assert p["tools"] == []
    assert "不存在" in capsys.readouterr().out


# ---------------------------------------------------------------- 启用/禁用

def test_enabled_by_default(packs_dir):
    _mk_pack(packs_dir, "p1", _valid_manifest("p1"))
    assert packs.is_enabled("p1")
    assert [p["name"] for p in packs.enabled_packs()] == ["p1"]


def test_enabled_packs_whitelist(packs_dir, monkeypatch):
    _mk_pack(packs_dir, "p1", _valid_manifest("p1"))
    _mk_pack(packs_dir, "p2", _valid_manifest("p2"))
    monkeypatch.setattr(config, "ENABLED_PACKS", "p2")
    assert not packs.is_enabled("p1")
    assert packs.is_enabled("p2")
    assert [p["name"] for p in packs.enabled_packs()] == ["p2"]


def test_platform_pack_immunity(packs_dir, monkeypatch):
    _mk_pack(packs_dir, "core-plat", _valid_manifest("core-plat", platform=True))
    monkeypatch.setattr(config, "ENABLED_PACKS", "")        # 白名单为空 = 全部
    assert "不可禁用" in packs.disable_pack("core-plat")
    assert packs.is_enabled("core-plat")                    # 平台包始终启用
    monkeypatch.setattr(config, "ENABLED_PACKS", "other")
    info = {p["name"]: p for p in packs.scan_packs()}["core-plat"]
    assert packs.is_enabled(info)                           # 白名单对平台包无效


def test_disable_enable_roundtrip(packs_dir):
    _mk_pack(packs_dir, "p1", _valid_manifest("p1"))
    assert "已禁用" in packs.disable_pack("p1")
    assert not packs.is_enabled("p1")
    assert packs.enabled_packs() == []
    assert "已启用" in packs.enable_pack("p1")
    assert packs.is_enabled("p1")


def test_disable_missing_pack(packs_dir):
    assert "不存在" in packs.disable_pack("nobody")


def test_enable_broken_pack_refused(packs_dir):
    _mk_pack(packs_dir, "broken", _valid_manifest("broken",
              requires_env=["PA_TEST_MISSING_ENV_XYZ"]))
    assert "不可启用" in packs.enable_pack("broken")


def test_disable_env_excluded_hint(packs_dir, monkeypatch):
    _mk_pack(packs_dir, "p1", _valid_manifest("p1"))
    monkeypatch.setattr(config, "ENABLED_PACKS", "other")
    assert "ENABLED_PACKS" in packs.disable_pack("p1")       # 白名单已排除：提示无需禁用


# ---------------------------------------------------------------- 包脚手架

def test_create_pack_validations(packs_dir):
    # create_pack 会先 strip().lower()，含空格的非法名才会被拦下
    assert "只允许小写字母" in packs.create_pack("Bad Name", "x")
    _mk_pack(packs_dir, "exist", _valid_manifest("exist"))
    assert "已存在" in packs.create_pack("exist", "x")
    assert "description" in packs.create_pack("ok-name", "  ")
    assert "路径分隔符" in packs.create_pack("p9", "x", tool_name="a/b")
    assert "工具未通过校验" in packs.create_pack(
        "p9", "x", tool_name="bad_tool", tool_code="def bad_tool():\n    pass\n")
    assert "技能名" in packs.create_pack("p9", "x", skill_name="Bad Name")
    assert "非法变量名" in packs.create_pack("p9", "x", env={"9bad": "v"})


def test_create_pack_ok(packs_dir):
    r = packs.create_pack("office-ops", "办公领域包",
                          "## 办公领域能力", "quarter_of",
                          'def quarter_of(month: int) -> str:\n'
                          '    """返回月份所属季度。"""\n'
                          '    return f"Q{(month - 1) // 3 + 1}"\n',
                          skill_name="weekly-report",
                          skill_description="周报生成",
                          skill_instructions="按周汇总",
                          env={"PA_OFFICE_KEY": "1"})
    assert "已创建" in r
    d = packs_dir / "office-ops"
    assert (d / "pack.yaml").is_file()
    assert (d / "tools" / "quarter_of.py").is_file()
    assert (d / "skills" / "weekly-report" / "SKILL.md").is_file()
    assert (d / "prompt.md").is_file()
    info = {p["name"]: p for p in packs.scan_packs()}["office-ops"]
    assert info["status"] == "ok"
    assert info["env"] == {"PA_OFFICE_KEY": "1"}


def test_create_pack_auto_sign_in_enforced_mode(packs_dir, monkeypatch):
    """强制签名模式下 create_pack 落盘即自动签名（可信路径），包立即可加载。"""
    monkeypatch.setattr(config, "PACK_SIGNING_KEY", "testkey")
    r = packs.create_pack("signed-ops", "描述", prompt="提示词")
    assert "已自动签名" in r
    info = {p["name"]: p for p in packs.scan_packs()}["signed-ops"]
    assert info["status"] == "ok"
    assert info["trust"] == "signed"


def test_create_pack_enforced_rejects_unsigned(packs_dir, monkeypatch):
    """强制模式下手工放入的未签名包 → 整包不可用（fail-closed）。"""
    monkeypatch.setattr(config, "PACK_SIGNING_KEY", "testkey")
    _mk_pack(packs_dir, "handmade", _valid_manifest("handmade"),
             {"tools/greet.py": TOOL_SRC})
    info = {p["name"]: p for p in packs.scan_packs()}["handmade"]
    assert info["status"] == "unavailable"
    assert "签名校验失败" in info["error"]


# ---------------------------------------------------------------- 工具挂载

def test_load_pack_tools_dedup(packs_dir, capsys):
    _mk_pack(packs_dir, "pa", _valid_manifest("pa", tools=["tools/greet.py"]),
             {"tools/greet.py": TOOL_SRC})
    _mk_pack(packs_dir, "pb", _valid_manifest("pb", tools=["tools/greet.py"]),
             {"tools/greet.py": TOOL_SRC})
    tools = packs.load_pack_tools()
    names = [t.name for t in tools]
    assert names.count("greet") == 1                          # 跨包同名跳过后挂载者
    assert "工具名冲突" in capsys.readouterr().out


def test_load_pack_tools_env_conflict_warning(packs_dir, capsys, monkeypatch):
    """跨包同名 env 键冲突 → WARNING 点名两包与值（检测只告警不改运行时语义）。"""
    key = "PA_TEST_ENV_CLASH_KEY"
    monkeypatch.delenv(key, raising=False)
    _mk_pack(packs_dir, "pa", _valid_manifest("pa", env={key: "from-a"}))
    _mk_pack(packs_dir, "pb", _valid_manifest("pb", env={key: "from-b"}))
    try:
        packs.load_pack_tools()
        out = capsys.readouterr().out
        assert "WARNING" in out and key in out
        assert "先加载的「pa」生效" in out
    finally:
        os.environ.pop(key, None)                             # 清理 setdefault 注入


def test_load_pack_tools_env_external_override_hint(packs_dir, capsys, monkeypatch):
    key = "PA_TEST_ENV_EXTERNAL_KEY"
    monkeypatch.setenv(key, "external")                       # 外部显式设置优先
    _mk_pack(packs_dir, "pa", _valid_manifest("pa", env={key: "from-a"}))
    _mk_pack(packs_dir, "pb", _valid_manifest("pb", env={key: "from-b"}))
    try:
        packs.load_pack_tools()
        out = capsys.readouterr().out
        assert "外部显式设置" in out                          # 两包默认值均被覆盖
    finally:
        monkeypatch.delenv(key, raising=False)


def test_effective_env(packs_dir, monkeypatch):
    key = "PA_TEST_EFF_ENV_KEY"
    monkeypatch.delenv(key, raising=False)
    _mk_pack(packs_dir, "p1", _valid_manifest("p1", env={key: "pack_default"}))
    info = {p["name"]: p for p in packs.scan_packs()}["p1"]
    assert packs.effective_env(info) == {key: "pack_default"}  # 未显式设置 → 包默认值
    monkeypatch.setenv(key, "external")
    assert packs.effective_env(info) == {key: "external"}      # 外部设置优先
    monkeypatch.delenv(key, raising=False)


def test_prompt_fragments(packs_dir):
    _mk_pack(packs_dir, "p1", _valid_manifest("p1", prompt="prompt.md"),
             {"prompt.md": "领域能力提示词A"})
    assert packs.prompt_fragments() == "领域能力提示词A"
    packs.disable_pack("p1")
    assert packs.prompt_fragments() == ""                      # 禁用包片段随之消失


def test_packs_signature(packs_dir):
    _mk_pack(packs_dir, "p1", _valid_manifest("p1"))
    sig1 = packs.packs_signature()
    assert sig1[0][0][0] == "p1"                               # (名字, mtimes 元组)
    packs.disable_pack("p1")
    assert packs.packs_signature() != sig1                     # 禁用集合进入签名


def test_delete_pack(packs_dir):
    _mk_pack(packs_dir, "p1", _valid_manifest("p1"))
    assert "非法包名" in packs.delete_pack("Bad Name!")
    assert "不存在" in packs.delete_pack("nobody")
    assert "已删除" in packs.delete_pack("p1")
    assert not (packs_dir / "p1").exists()
    assert packs.scan_packs() == []                            # 缓存已失效


def test_list_packs_view(packs_dir):
    _mk_pack(packs_dir, "p1", _valid_manifest("p1", tools=["tools/greet.py"],
              env={"K1": "v1"}, permissions=["fs:read"]),
              {"tools/greet.py": TOOL_SRC})
    view = packs.list_packs()[0]
    assert view["name"] == "p1"
    assert view["enabled"] is True
    assert view["tools"] == ["greet"]
    assert view["env_keys"] == ["K1"]
    assert view["permissions"] == ["fs:read"]
    assert view["trust"] == "unsigned"
