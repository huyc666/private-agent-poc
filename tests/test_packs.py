"""领域包注册中心：清单解析校验 / 启用白名单与运行时禁用 / 包脚手架创建 /
工具挂载去重与 env 冲突告警 / 签名 / 删除。"""
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from server import config, custom_tools, packs, packsign, skills

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


def test_ac4_hot_reload_reverifies_pack_signature(packs_dir, monkeypatch):
    """SEC-4（spec v0.17.32 AC4）：强制验签模式下运行中篡改包内工具文件 →
    热重载触发重扫并重新验签，包转为 invalid/unavailable（fail-closed），
    篡改工具不再挂载——不得沿用启动时的缓存验签结果。"""
    import time

    monkeypatch.setattr(config, "PACK_SIGNING_KEY", "testkey")
    _mk_pack(packs_dir, "sig-pack", _valid_manifest("sig-pack",
              tools=["tools/greet.py"], prompt="prompt.md"),
             {"tools/greet.py": TOOL_SRC, "prompt.md": "领域提示词"})
    from server import agent
    packsign.sign_pack_files(packs_dir / "sig-pack")
    assert packs.scan_packs()[0]["trust"] == "signed"
    agent._rebuild_graph()
    assert "greet" in agent.STATE.tool_names          # 篡改前工具已挂载
    # 运行中替换工具内容（st_mtime_ns 变化 → 触发热重载检测）
    (packs_dir / "sig-pack" / "tools" / "greet.py").write_text(
        "def greet(name: str) -> str:\n    return 'tampered'\n", encoding="utf-8")
    time.sleep(0.02)                                 # 保证 mtime_ns 可分辨
    agent.maybe_reload_custom_tools()
    p = packs.scan_packs()[0]
    assert p["trust"] == "invalid"                   # 重扫后重新验签
    assert p["status"] == "unavailable"
    assert "签名校验失败" in p["error"]
    assert "greet" not in agent.STATE.tool_names     # 篡改包工具不挂载


# ---- v0.17.33 AC2/AC3：包路径边界 + 内容哈希检测（复验实测）----

def test_ac2_pack_paths_confined_to_pack_dir(packs_dir, tmp_path):
    """包清单越界路径不进包（fail-closed）：tools/skills/prompt 三入口的
    ../ 与绝对路径——复验实测 ../outside.py 曾以 status=ok 被接受挂载。"""
    # ../ 从包目录只弹一级：越界目标在 packs_dir 下（而非 packs_dir.parent），
    # 文件必须真实存在，旧实现才会暴露「直接拼接即接受」的问题
    parent = packs_dir
    parent.mkdir(exist_ok=True)
    (parent / "outside_tool.py").write_text(
        "def outside(): pass", encoding="utf-8")
    (parent / "outside_prompt.md").write_text("越界提示词", encoding="utf-8")
    esc_skill = parent / "escape_skill"
    esc_skill.mkdir(exist_ok=True)
    (esc_skill / "SKILL.md").write_text("# 越界技能", encoding="utf-8")
    abs_tool = tmp_path / "abs_tool.py"
    abs_tool.write_text("def abs_tool(): pass", encoding="utf-8")

    m = _valid_manifest("p1",
                        tools=["../outside_tool.py", str(abs_tool),
                               "tools/greet.py"],
                        skills=["../escape_skill"],
                        prompt="../outside_prompt.md")
    _mk_pack(packs_dir, "p1", m, {"tools/greet.py": TOOL_SRC})
    # ../escape_skill 目录会一并被 scan 视为无效包，按名称定位目标包
    info = next(p for p in packs.scan_packs() if p["name"] == "p1")
    assert [p.stem for p in info["tools"]] == ["greet"]   # 越界项全部拒收
    assert info["skills"] == []                          # 越界技能拒收
    assert not info["prompt"]                             # 越界提示词拒收


def test_ac3_pack_tamper_with_restored_mtime_detected(packs_dir, monkeypatch):
    """篡改包工具内容并还原 mtime_ns（复验实测的绕过手法）→ 内容哈希签名
    仍检测到变更 → 热重载触发重扫重验 → 强制验签下包被剔除（fail-closed）。"""
    import os
    from server import agent
    monkeypatch.setattr(config, "PACK_SIGNING_KEY", "testkey")
    _mk_pack(packs_dir, "sig-pack2", _valid_manifest("sig-pack2",
              tools=["tools/greet.py"]), {"tools/greet.py": TOOL_SRC})
    packsign.sign_pack_files(packs_dir / "sig-pack2")
    agent._rebuild_graph()
    assert "greet" in agent.STATE.tool_names
    tf = packs_dir / "sig-pack2" / "tools" / "greet.py"
    st = tf.stat()
    tf.write_text("def greet(name: str) -> str:\n    return 'tampered'\n",
                  encoding="utf-8")
    os.utime(tf, ns=(st.st_atime_ns, st.st_mtime_ns))    # 还原 mtime
    assert tf.stat().st_mtime_ns == st.st_mtime_ns
    agent.maybe_reload_custom_tools()
    p = packs.scan_packs()[0]
    assert p["trust"] == "invalid"                       # mtime 未变也重验签
    assert p["status"] == "unavailable"
    assert "greet" not in agent.STATE.tool_names


# ---- v0.17.34 AC2/AC3/AC4：领域包信任链闭环（复验实测）----

def test_ac2_packs_signature_covers_skill_files(packs_dir):
    """AC2：packs_signature 载荷补包内技能文件——篡改 SKILL.md 并还原
    mtime，包级签名仍变化（复验实测此前载荷不含 skills，双层检测全漏）。"""
    _mk_pack(packs_dir, "p1", _valid_manifest("p1", skills=["skills/demo"]),
             {"skills/demo/SKILL.md": "---\nname: demo\n---\n指令v1"})
    sig1 = packs.packs_signature()
    f = packs_dir / "p1" / "skills" / "demo" / "SKILL.md"
    st = f.stat()
    f.write_text("---\nname: demo\n---\n指令v2", encoding="utf-8")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))     # 还原 mtime
    assert packs.packs_signature() != sig1


def test_ac2_signed_pack_skill_tamper_invalidates_trust(packs_dir, monkeypatch):
    """AC2 端到端：强制验签下篡改已签名包的 SKILL.md 并还原 mtime →
    内容哈希触发重扫重验 → trust=invalid 整包剔除（fail-closed）。"""
    from server import agent
    monkeypatch.setattr(config, "PACK_SIGNING_KEY", "testkey")
    _mk_pack(packs_dir, "sig-pack3", _valid_manifest("sig-pack3",
              skills=["skills/demo"]),
             {"skills/demo/SKILL.md": "---\nname: demo\n---\n指令"})
    packsign.sign_pack_files(packs_dir / "sig-pack3")
    agent._rebuild_graph()
    # 稳定检测基线：图重建先捕获签名（agent.py L101-102）后注册包技能目录
    # （L105-106），首建后下一轮比较必然发现「新增目录」多重建一次；先让
    # 基线吸收该时序差异，使后续检测只对真实篡改敏感——否则本用例会被无关
    # 变化触发重扫而假绿（现状代码也能通过）。
    agent.maybe_reload_custom_tools()
    f = packs_dir / "sig-pack3" / "skills" / "demo" / "SKILL.md"
    st = f.stat()
    f.write_text("---\nname: demo\n---\n被篡改的指令", encoding="utf-8")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))     # 还原 mtime
    agent.maybe_reload_custom_tools()
    p = next(p for p in packs.scan_packs() if p["name"] == "sig-pack3")
    assert p["trust"] == "invalid"                       # 重扫后重新验签
    assert p["status"] == "unavailable"
    assert "签名校验失败" in p["error"]


def test_ac3_noncanonical_manifest_paths_rejected(packs_dir, capsys):
    """AC3：清单规范路径白名单（fail-closed）——工具须平铺 tools/<文件名>.py
    （对齐沙箱执行路径）、技能须 skills/<一级目录>、提示词须 prompt.md。
    复验实测 code/demo_tool.py 曾以 status=ok 挂载且不在签名/检测载荷内
    （签名与检测双重盲区），声明路径与沙箱执行路径也可能脱钩。
    评审修复 C2 回归：Windows 混合分隔符 tools/mix\\tool.py 物理上 resolve 成
    嵌套文件，会绕过 PurePosixPath 白名单的平铺意图——须按非规范拒收。"""
    _mk_pack(packs_dir, "wild-paths", _valid_manifest("wild-paths",
              tools=["code/demo_tool.py", "tools/nested/tool.py",
                     "tools/mix\\tool.py", "tools/greet.py"],
              skills=["skills/a/b", "skills/pointed/SKILL.md",
                      "skills/mix\\dir", "skills/ok"],
              prompt="docs/prompt.md"),
             {"code/demo_tool.py": TOOL_SRC,
              "tools/nested/tool.py": TOOL_SRC,
              "tools/greet.py": TOOL_SRC,
              "skills/a/b/SKILL.md": "---\nname: b\n---\nx",
              "skills/pointed/SKILL.md": "---\nname: p\n---\nx",
              "skills/ok/SKILL.md": "---\nname: ok\n---\nx",
              "docs/prompt.md": "越界位置提示词"})
    info = next(p for p in packs.scan_packs() if p["name"] == "wild-paths")
    assert info["status"] == "ok"                        # 包本体仍可加载
    assert [t.stem for t in info["tools"]] == ["greet"]  # 非规范工具拒收
    assert [s.name for s in info["skills"]] == ["ok"]     # 非规范技能拒收
    assert not info["prompt"]                            # 非规范提示词拒收
    assert capsys.readouterr().out.count("非规范") == 7  # 逐项告警可定位（含 C2 两项）


def test_ac4_skill_leaf_symlink_rejected(packs_dir, tmp_path, capsys):
    """AC4：SKILL.md 叶子指向包外的符号链接 → 技能拒收（复验 R3：技能
    目录包含校验通过，但叶子仅 is_file() 会跟随链接导致越界内容入索引）。
    环境无法创建符号链接时跳过（Windows 需开发者模式/管理员）。"""
    _mk_pack(packs_dir, "leaf-link", _valid_manifest("leaf-link",
              skills=["skills/hijack", "skills/normal"]),
             {"skills/normal/SKILL.md": "---\nname: normal\n---\n包内技能"})
    outside = tmp_path / "outside_skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: hijack\n---\n包外越界技能",
                                      encoding="utf-8")
    hijack_dir = packs_dir / "leaf-link" / "skills" / "hijack"
    hijack_dir.mkdir(parents=True)
    try:
        os.symlink(outside / "SKILL.md", hijack_dir / "SKILL.md")
    except OSError:
        pytest.skip("当前环境无法创建符号链接（Windows 需开发者模式/管理员）")
    info = next(p for p in packs.scan_packs() if p["name"] == "leaf-link")
    assert [s.name for s in info["skills"]] == ["normal"]   # 越界叶子拒收
    assert "SKILL.md 指向包目录之外" in capsys.readouterr().out


def test_review_skill_manifest_duplicate_entries_deduped(packs_dir, capsys):
    """评审修复 E1：清单重复声明同一技能目录 → 只挂载一次（与工具分支的
    tool_stems 去重对称，v0.17.27）——此前重复条目会重复进入 info["skills"]
    与签名载荷（无运行时危害，register_extra_dir 去重兜底，仅分支不对称）。"""
    _mk_pack(packs_dir, "dup-skills", _valid_manifest("dup-skills",
              skills=["skills/ok", "skills/ok"]),
             {"skills/ok/SKILL.md": "---\nname: ok\n---\nx"})
    info = next(p for p in packs.scan_packs() if p["name"] == "dup-skills")
    assert [s.name for s in info["skills"]] == ["ok"]
    assert "重复声明" in capsys.readouterr().out


# ---- v0.17.35 AC1-AC4：技能挂载同一集合 + 验签异常收口（复验报告 R4-R6）----

def test_ac1_undeclared_sibling_skill_invisible(packs_dir):
    """AC1/R4：清单只声明 good、磁盘另有未声明 bad/SKILL.md → bad 不得经
    共同 skills/ 父目录进入任何入口——索引不可见、load_skill 返回 None、
    不进技能层签名（报告最小复现场景，不依赖符号链接即可复现）。"""
    _mk_pack(packs_dir, "decl-only", _valid_manifest("decl-only",
              skills=["skills/good"]),
             {"skills/good/SKILL.md": "---\nname: good\n---\n已声明技能",
              "skills/bad/SKILL.md": "---\nname: bad\n---\n未声明技能"})
    packs.register_pack_skills()
    idx = {s["name"] for s in skills.list_skills()}
    assert "good" in idx                                   # 声明技能照常可用
    assert "bad" not in idx                                # 未声明兄弟不入索引
    assert skills.load_skill("good") is not None
    assert skills.load_skill("bad") is None                # 未声明兄弟不可加载
    sig_names = {rel.split("/")[0] for rel, _ in skills.signature()}
    assert sig_names == {"good"}                           # 三口同一份已验证集合


def test_ac1_rejected_sibling_not_remounted_via_parent(packs_dir, tmp_path):
    """AC1/R4：同时声明 good 与 hijack，但 hijack 因 SKILL.md 符号链接指向
    包外被解析期拒绝 → good 正常可用，hijack 不得经父目录注册重新进入
    索引/加载（报告 R4 测试要求 2）。环境无法创建符号链接时跳过（CI 恒跑，
    沿用 v0.17.34 AC4 先例）。"""
    _mk_pack(packs_dir, "mix-link", _valid_manifest("mix-link",
              skills=["skills/good", "skills/hijack"]),
             {"skills/good/SKILL.md": "---\nname: good\n---\n包内技能"})
    outside = tmp_path / "outside_skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: hijack\n---\n包外越界技能",
                                      encoding="utf-8")
    hijack_dir = packs_dir / "mix-link" / "skills" / "hijack"
    hijack_dir.mkdir(parents=True)
    try:
        os.symlink(outside / "SKILL.md", hijack_dir / "SKILL.md")
    except OSError:
        pytest.skip("当前环境无法创建符号链接（Windows 需开发者模式/管理员）")
    info = next(p for p in packs.scan_packs() if p["name"] == "mix-link")
    assert [s.name for s in info["skills"]] == ["good"]     # 越界叶子解析期已拒
    packs.register_pack_skills()
    idx = {s["name"] for s in skills.list_skills()}
    assert idx == {"good"}                                  # 被拒兄弟不得经父目录复活
    assert skills.load_skill("good") is not None
    assert skills.load_skill("hijack") is None


def test_ac2_loadable_set_matches_verified_set(packs_dir):
    """AC2/R4：索引 == 可加载 == 技能层签名载荷，三口同一份「已验证接受的
    具体技能集合」；多个明确声明的正常技能全部可用（兼容回归，报告 R4
    测试要求 3）；frontmatter 名与目录名不同的声明技能经兜底扫描仍可加载。"""
    _mk_pack(packs_dir, "multi", _valid_manifest("multi",
              skills=["skills/one", "skills/two", "skills/alias"]),
             {"skills/one/SKILL.md": "---\nname: one\n---\n技能一",
              "skills/two/SKILL.md": "---\nname: two\n---\n技能二",
              "skills/alias/SKILL.md": "---\nname: aliased-name\n---\n别名技能",
              "skills/ghost/SKILL.md": "---\nname: ghost\n---\n未声明"})
    packs.register_pack_skills()
    idx = {s["name"] for s in skills.list_skills()}
    assert idx == {"one", "two", "aliased-name"}
    assert all(skills.load_skill(n) for n in ("one", "two", "alias"))
    assert skills.load_skill("aliased-name") is not None   # frontmatter 名兜底可加载
    assert skills.load_skill("ghost") is None
    sig_names = {rel.split("/")[0] for rel, _ in skills.signature()}
    assert sig_names == {"one", "two", "alias"}             # 签名载荷同一集合


def test_ac3_scan_survives_resource_read_failure(packs_dir, monkeypatch, capsys):
    """AC3/R5：签名资源读取 OSError 不中断整个扫描——相关包 fail-closed 转
    invalid/unavailable，其他正常包不受影响（复验实测此前 verify 直接上抛
    OSError 炸掉 scan_packs，启动/热重载整条链路跟着失败）。"""
    monkeypatch.setattr(config, "PACK_SIGNING_KEY", "testkey")
    _mk_pack(packs_dir, "sick", _valid_manifest("sick", tools=["tools/greet.py"]),
             {"tools/greet.py": TOOL_SRC})
    _mk_pack(packs_dir, "healthy", _valid_manifest("healthy"))
    packsign.sign_pack_files(packs_dir / "sick")
    packsign.sign_pack_files(packs_dir / "healthy")
    real_read = Path.read_bytes

    def _boom(self):
        if "sick" in self.parts and self.name == "greet.py":
            raise OSError("injected read failure")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", _boom)
    infos = {p["name"]: p for p in packs.scan_packs()}      # 不抛异常
    assert infos["sick"]["trust"] == "invalid"              # 读失败 → 验签失败
    assert infos["sick"]["status"] == "unavailable"
    assert "签名校验失败" in infos["sick"]["error"]
    assert infos["healthy"]["trust"] == "signed"            # 其他包不受影响
    assert infos["healthy"]["status"] == "ok"
    assert "读取失败" in capsys.readouterr().out            # 告警可定位


def test_ac3_sign_failure_explicit_not_partial(packs_dir, monkeypatch):
    """AC3/R5：签名生成路径遇不可读资源 → 明确报错且不写入任何签名——
    不得静默跳过不可读文件签出覆盖不完整的签名（报告 R5 修复要求 2）。"""
    monkeypatch.setattr(config, "PACK_SIGNING_KEY", "testkey")
    _mk_pack(packs_dir, "unreadable", _valid_manifest("unreadable",
              tools=["tools/greet.py"]), {"tools/greet.py": TOOL_SRC})
    real_read = Path.read_bytes

    def _boom(self):
        if any(part.startswith("unreadable") for part in self.parts) \
                and self.name == "greet.py":
            raise OSError("injected read failure")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", _boom)
    r = packs.sign_pack("unreadable")                       # 工具入口：明确报错
    assert "签名失败" in r and "读取异常" in r
    text = (packs_dir / "unreadable" / "pack.yaml").read_text(encoding="utf-8")
    assert "signature:" not in text                         # 未写入任何签名
    r2 = packs.create_pack("unreadable-b", "描述",
                           tool_name="greet", tool_code=TOOL_SRC)
    assert "已创建" in r2 and "自动签名失败" in r2          # 落盘成功、签名明确失败
    text2 = (packs_dir / "unreadable-b" / "pack.yaml").read_text(encoding="utf-8")
    assert "signature:" not in text2


def test_ac4_skill_reference_rename_changes_hot_signature(packs_dir):
    """AC4/R6：技能附属文件纯改名（references/a.md→b.md，内容不变）→
    包级热检测签名必须变化（复验实测 reference_rename_hot_signature_
    unchanged=True：条目只含内容哈希漏检，但 packsign HMAC 载荷含路径，
    强制重扫已 invalid——缓存与磁盘信任状态不一致）。"""
    _mk_pack(packs_dir, "rename-pack", _valid_manifest("rename-pack",
              skills=["skills/demo"]),
             {"skills/demo/SKILL.md": "---\nname: demo\n---\n指令",
              "skills/demo/references/a.md": "参考资料内容不变"})
    sig1 = packs.packs_signature()
    f = packs_dir / "rename-pack" / "skills" / "demo" / "references" / "a.md"
    f.rename(f.with_name("b.md"))
    assert packs.packs_signature() != sig1                  # 纯改名必须触发热检测


def test_ac4_rename_triggers_reverification(packs_dir, monkeypatch):
    """AC4/R6 端到端：强制验签下附属文件纯改名 → 热检测触发重扫重验 →
    HMAC 载荷含路径 → trust=invalid 整包剔除（fail-closed），缓存与磁盘
    信任状态不再不一致。"""
    from server import agent
    monkeypatch.setattr(config, "PACK_SIGNING_KEY", "testkey")
    _mk_pack(packs_dir, "sig-rename", _valid_manifest("sig-rename",
              skills=["skills/demo"]),
             {"skills/demo/SKILL.md": "---\nname: demo\n---\n指令",
              "skills/demo/references/a.md": "参考资料"})
    packsign.sign_pack_files(packs_dir / "sig-rename")
    agent._rebuild_graph()
    # 稳定检测基线：图重建先捕获签名后注册包技能目录，首建后下一轮比较必然
    # 多一次重建；先吸收该时序差异，使后续检测只对真实改动敏感
    # （同 test_ac2_signed_pack_skill_tamper_invalidates_trust 先例）
    agent.maybe_reload_custom_tools()
    f = packs_dir / "sig-rename" / "skills" / "demo" / "references" / "a.md"
    f.rename(f.with_name("b.md"))
    agent.maybe_reload_custom_tools()
    p = next(p for p in packs.scan_packs() if p["name"] == "sig-rename")
    assert p["trust"] == "invalid"                           # 重扫后按路径验签失败
    assert p["status"] == "unavailable"
    assert "签名校验失败" in p["error"]


# ---- v0.17.35 评审修复回归（TRAE-code-review C2/C5/C6）----

def _mk_link(target: Path, link: Path) -> bool:
    """构造目录链接：优先 os.symlink（Linux / Windows 开发者模式），
    Windows 回退 junction（mklink /J 无需管理员）；均不可用返回 False
    （调用方 skip，CI/Linux symlink 恒可用，沿用 v0.17.34 AC4 先例）。"""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except OSError:
        pass
    if sys.platform == "win32":
        import subprocess
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True)
        return r.returncode == 0
    return False


def test_review_grouped_parent_registration_survives_link_split(packs_dir):
    """评审 C5（v0.17.36 R7 收紧后断言更新）：清单声明技能 skills/x 是指向
    包内 tools/ 的目录链接——报告 R7 修复要求 1 明示指向 tools/ 的链接同拒，
    v0.17.36 起技能目录解析后必须仍位于 <pack>/skills/ 树内，x 在解析期即
    拒收；合法声明技能 y 不陪葬、三口一致的核心回归价值保留（链接环境
    不可用时 skip，CI 恒跑）。"""
    _mk_pack(packs_dir, "split-pack",
             _valid_manifest("split-pack", skills=["skills/x", "skills/y"]),
             {"skills/y/SKILL.md": "---\nname: y\n---\n正常技能"})
    tools_dir = packs_dir / "split-pack" / "tools"
    tools_dir.mkdir()
    (tools_dir / "SKILL.md").write_text("---\nname: x\n---\n链接技能",
                                         encoding="utf-8")
    if not _mk_link(tools_dir, packs_dir / "split-pack" / "skills" / "x"):
        pytest.skip("当前环境无法创建目录链接")
    info = next(p for p in packs.scan_packs() if p["name"] == "split-pack")
    assert [s.name for s in info["skills"]] == ["y"]   # R7：链接技能解析期拒收
    packs.register_pack_skills()
    idx = {s["name"] for s in skills.list_skills()}
    assert idx == {"y"}                                 # 合法声明技能不得陪葬
    assert skills.load_skill("y") is not None
    assert skills.load_skill("x") is None               # 链接名不可加载
    assert skills.load_skill("tools") is None          # 目标目录名同样不可加载
    # 三口一致：技能层签名只含 y，tools/SKILL.md 不经任何入口服务
    sig_rels = {rel for rel, _ in skills.signature()}
    assert "y/SKILL.md" in sig_rels
    assert "tools/SKILL.md" not in sig_rels


def test_review_signature_survives_relative_to_mismatch(packs_dir, monkeypatch):
    """评审 C2：扫描缓存填充后包目录（或其祖先）被换成指向别处的链接时，
    缓存条目路径与现算包根前缀分叉（relative_to ValueError）→
    packs_signature 不得上抛（否则炸掉每轮对话开头热检测且缓存永不
    失效），条目退化为绝对路径键继续参与比较、差异触发重扫重验自愈。"""
    _mk_pack(packs_dir, "rel-pack",
             _valid_manifest("rel-pack", tools=["tools/greet.py"]),
             {"tools/greet.py": TOOL_SRC})
    packs.packs_signature()                          # 先正常填充扫描缓存
    real_rel = Path.relative_to

    def _mismatch(self, *other):
        if any(part == "rel-pack" for part in self.parts):
            raise ValueError("injected: not in the subpath")
        return real_rel(self, *other)

    monkeypatch.setattr(Path, "relative_to", _mismatch)
    sig = packs.packs_signature()                    # 不抛异常
    entries = dict(sig[0][0][1])                     # 第一个包的条目字典化
    tool = packs_dir / "rel-pack" / "tools" / "greet.py"
    assert str(tool) in entries                      # ValueError 条目退化绝对路径键
    assert "pack.yaml" in entries                    # 固定键不受影响


def test_review_sign_pack_survives_encode_error(packs_dir, monkeypatch):
    """评审 C6：签名生成路径遇 UnicodeEncodeError（Linux surrogateescape
    文件名使载荷 encode 失败）与 OSError 同口径——明确报错、不写入任何
    签名（调用方捕获元组扩展，Windows 同文件读取为 PermissionError 已兜底）。"""
    monkeypatch.setattr(config, "PACK_SIGNING_KEY", "testkey")
    _mk_pack(packs_dir, "enc-pack", _valid_manifest("enc-pack"))

    def _boom(d):
        raise UnicodeEncodeError("utf-8", "\ud800", 0, 1,
                                 "surrogates not allowed")

    monkeypatch.setattr(packsign, "_payload", _boom)
    r = packs.sign_pack("enc-pack")
    assert "签名失败" in r and "未写入任何签名" in r
    text = (packs_dir / "enc-pack" / "pack.yaml").read_text(encoding="utf-8")
    assert "signature:" not in text


# ---- v0.17.36 AC1-AC4：严格 skills 树布局 / 加载快照 / 链接循环哨兵（R7-R9）----

def test_ac1_pack_internal_dir_link_rejected(packs_dir, monkeypatch, capsys):
    """AC1/R7：skills/x -> ../hidden（目录链接指向 skills 树之外的包内目录）
    ——强制模式下不得出现「技能可加载且包 signed」：技能目录解析后必须仍
    位于 <pack>/skills/ 树内（hidden/、tools/ 等一律拒收，junction 与
    POSIX symlink 同形拒绝；POSIX 形态即报告实测的 rglob 不跟随目录链接
    HMAC 盲区，Windows junction 本机可真跑、CI/Linux 真符号链接恒跑）。"""
    monkeypatch.setattr(config, "PACK_SIGNING_KEY", "testkey")
    _mk_pack(packs_dir, "link-pack", _valid_manifest("link-pack",
              skills=["skills/x", "skills/y"]),
             {"skills/y/SKILL.md": "---\nname: y\n---\n正常技能",
              "hidden/SKILL.md": "---\nname: blind-spot\n---\n盲区技能"})
    if not _mk_link(packs_dir / "link-pack" / "hidden",
                    packs_dir / "link-pack" / "skills" / "x"):
        pytest.skip("当前环境无法创建目录链接")
    packsign.sign_pack_files(packs_dir / "link-pack")
    info = next(p for p in packs.scan_packs() if p["name"] == "link-pack")
    assert [s.name for s in info["skills"]] == ["y"]    # x 解析后出 skills 树 → 拒收
    assert info["trust"] == "signed"                    # 包本体与正常技能照常
    assert info["status"] == "ok"
    packs.register_pack_skills()
    idx = {s["name"] for s in skills.list_skills()}
    assert "y" in idx and "blind-spot" not in idx        # 链接目标不入索引
    assert skills.load_skill("y") is not None
    for name in ("x", "hidden", "blind-spot"):          # 链接名/目标名/frontmatter 名全不可加载
        assert skills.load_skill(name) is None
    assert "skills 树" in capsys.readouterr().out       # 拒收告警可定位


def test_ac2_mounted_set_covers_all_collections(packs_dir, monkeypatch):
    """AC2/R7 修复要求 4：签名集合、热检测集合、索引集合和加载集合再次
    同一——实际挂载技能目录全部落在 packsign HMAC 载荷内（报告 AC1）；目录
    链接形态拒收后不得在任何集合留痕；frontmatter 别名技能经兜底加载计入
    挂载集合。"""
    monkeypatch.setattr(config, "PACK_SIGNING_KEY", "testkey")
    _mk_pack(packs_dir, "cover-pack", _valid_manifest("cover-pack",
              skills=["skills/one", "skills/alias", "skills/x"]),
             {"skills/one/SKILL.md": "---\nname: one\n---\n技能一",
              "skills/alias/SKILL.md": "---\nname: aliased-name\n---\n别名技能",
              "hidden/SKILL.md": "---\nname: blind-spot\n---\n盲区技能"})
    if not _mk_link(packs_dir / "cover-pack" / "hidden",
                    packs_dir / "cover-pack" / "skills" / "x"):
        pytest.skip("当前环境无法创建目录链接")
    packsign.sign_pack_files(packs_dir / "cover-pack")
    info = next(p for p in packs.scan_packs() if p["name"] == "cover-pack")
    assert [s.name for s in info["skills"]] == ["one", "alias"]   # x 拒收
    packs.register_pack_skills()
    idx = {s["name"] for s in skills.list_skills()}
    assert idx == {"one", "aliased-name"}                       # 索引集合
    assert all(skills.load_skill(n) for n in ("one", "alias", "aliased-name"))
    assert skills.load_skill("x") is None                        # 加载集合无链接痕迹
    assert skills.load_skill("hidden") is None
    assert skills.load_skill("blind-spot") is None
    sig_dirs = {rel.split("/")[0] for rel, _ in skills.signature()}
    assert sig_dirs == {"one", "alias"}                          # 技能层签名集合
    hot = dict(next(e for e in packs.packs_signature()[0]
                    if e[0] == "cover-pack")[1])
    hot_dirs = {k.split("/")[1] for k in hot if k.startswith("skills/")}
    assert hot_dirs == {"one", "alias"}                          # 热检测集合
    payload = {rel for rel, _ in
               packsign._canonical_parts(packs_dir / "cover-pack")}
    assert {"skills/one/SKILL.md", "skills/alias/SKILL.md"} <= payload  # HMAC 覆盖
    assert info["trust"] == "signed" and info["status"] == "ok"


def test_ac3_swap_in_check_read_gap_no_outside_content(packs_dir, monkeypatch,
                                                        tmp_path):
    """AC3/R8：准入（_skill_entry_ok）返回后、内容读取前换入指向包外的
    junction（精确复现报告 toctou_swapped / outside_content_loaded 场景）——
    直查加载与索引不得返回包外内容；frontmatter 别名兜底加载同防护；正常
    技能与别名仍可加载；signature 检测到变化（检测层职责，读盘不返回内容）。"""
    monkeypatch.setattr(config, "PACK_SIGNING_KEY", "testkey")
    _mk_pack(packs_dir, "swap-pack", _valid_manifest("swap-pack",
              skills=["skills/good", "skills/alias"]),
             {"skills/good/SKILL.md": "---\nname: good\n---\n包内已验内容",
              "skills/alias/SKILL.md": "---\nname: aliased-name\n---\n别名技能"})
    packsign.sign_pack_files(packs_dir / "swap-pack")
    packs.register_pack_skills()
    sig_before = skills.signature()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: good\n---\n包外越界内容",
                                      encoding="utf-8")
    good_dir = packs_dir / "swap-pack" / "skills" / "good"
    swapped = []
    real_ok = skills._skill_entry_ok

    def _swap_after_ok(smd, allowed, root, source):
        ok = real_ok(smd, allowed, root, source)
        if ok and smd.parent.name == "good" and not swapped:
            swapped.append(True)                # 检查通过瞬间换链（TOCTOU 窗口）
            shutil.rmtree(good_dir)
            _mk_link(outside, good_dir)
        return ok

    monkeypatch.setattr(skills, "_skill_entry_ok", _swap_after_ok)
    content = skills.load_skill("good")          # 直查分支：准入后、读取前换链
    assert swapped                               # 换链确实发生在窗口内
    assert content is not None and "包外越界内容" not in content
    assert "包内已验内容" in content              # 只能返回已验签内容（快照）
    descs = " ".join(s["description"] for s in skills.list_skills())
    assert "包外越界内容" not in descs           # 索引同样不泄露包外内容
    assert skills.load_skill("aliased-name") is not None   # 别名兜底仍可加载
    assert skills.load_skill("alias") is not None          # 正常技能仍可加载
    assert skills.signature() != sig_before      # 检测层捕获换链 → 触发重建重验


def test_ac4_scan_survives_root_resolve_loop(packs_dir, monkeypatch):
    """AC4/R9 修复要求 2：扫描入口对链接循环收口——包根/资源 resolve 抛
    RuntimeError（注入复现报告 Symlink loop）不得中断 scan_packs，异常包
    fail-closed 转 error 态，健康包继续扫描（沿 C2 注入先例，本机恒跑）。"""
    _mk_pack(packs_dir, "boom", _valid_manifest("boom", skills=["skills/s"]),
             {"skills/s/SKILL.md": "---\nname: s\n---\n指令"})
    _mk_pack(packs_dir, "healthy", _valid_manifest("healthy"))
    real_resolve = Path.resolve

    def _loop(self, strict=False):
        if "boom" in self.parts:
            raise RuntimeError("Symlink loop")
        return real_resolve(self, strict)

    monkeypatch.setattr(Path, "resolve", _loop)
    infos = {p["name"]: p for p in packs.scan_packs()}
    assert infos["boom"]["status"] == "error"          # 异常包 fail-closed
    assert "Symlink loop" in infos["boom"]["error"]     # 错误可定位
    assert infos["healthy"]["status"] == "ok"           # 健康包不受影响


def test_ac4_signature_sentinel_on_root_loop(packs_dir, monkeypatch):
    """AC4/R9 修复要求 1：缓存内包根被换成链接循环（注入 resolve
    RuntimeError）——packs_signature 每包捕获 (OSError, RuntimeError) 生成
    可比较异常哨兵：热检测不上抛（每轮对话开头不得中断）、异常包条目为
    哨兵、健康包条目正常；哨兵与正常态不同 → 差异触发重扫重验自愈。"""
    _mk_pack(packs_dir, "loopme", _valid_manifest("loopme"))
    _mk_pack(packs_dir, "calm", _valid_manifest("calm"))
    packs.scan_packs()                                  # 先填充扫描缓存
    real_resolve = Path.resolve

    def _loop(self, strict=False):
        if "loopme" in self.parts:
            raise RuntimeError("Symlink loop")
        return real_resolve(self, strict)

    monkeypatch.setattr(Path, "resolve", _loop)
    sig = packs.packs_signature()                       # 不得上抛
    entries = {name: dict(hs) for name, hs in sig[0]}
    assert any(k.startswith("__error__") for k in entries["loopme"])   # 哨兵条目
    assert "pack.yaml" in entries["calm"]               # 健康包条目正常
    assert entries["loopme"] != entries["calm"]          # 哨兵参与比较触发重扫


def test_ac4_real_root_loop_fail_closed_and_self_heals(packs_dir, monkeypatch):
    """AC4/R9：真链接自循环（包目录被 rmtree 后换成指向自身的链接）——
    packs_signature 不上抛、条目变化（哨兵）可被热检测捕获，健康包不受影响；
    恢复真实目录并重扫后重新验签恢复 signed（异常签名不得缓存永久可信，
    修复要求 3）。评审 F4 修正：「junction 无法自环」陈述不实（mklink /J
    自环实测可创建），构造改走 _mk_link（symlink → junction 回退），Windows
    无特权也真跑；链接环境完全不可用才降级 skip（CI/Linux symlink 恒跑）。"""
    monkeypatch.setattr(config, "PACK_SIGNING_KEY", "testkey")
    d = _mk_pack(packs_dir, "selfloop", _valid_manifest("selfloop",
              skills=["skills/one"]),
             {"skills/one/SKILL.md": "---\nname: one\n---\n指令"})
    packsign.sign_pack_files(d)
    _mk_pack(packs_dir, "calm2", _valid_manifest("calm2"))
    packs.scan_packs()                                  # 缓存填充（循环注入前）
    before = packs.packs_signature()
    shutil.rmtree(d)
    if not _mk_link(d, d):         # symlink → junction 回退（均可自环）
        pytest.skip("当前环境无法创建自循环链接")
    during = packs.packs_signature()                    # 不得上抛
    assert during != before                             # 循环被检测层捕获
    entries = {name: dict(hs) for name, hs in during[0]}
    assert "pack.yaml" in entries["calm2"]              # 健康包继续参与
    # 恢复真实目录与内容 → 重扫 → 重新验签恢复（自愈，不得缓存永久可信）
    d.unlink()                     # 先移除自循环链接（symlink/junction 均不
    d.mkdir(parents=True)          # 跟随；POSIX mkdir 遇已存在链接路径抛 EEXIST）
    (d / "pack.yaml").write_text(
        yaml.safe_dump(_valid_manifest("selfloop", skills=["skills/one"]),
                       allow_unicode=True, sort_keys=False), encoding="utf-8")
    (d / "skills" / "one").mkdir(parents=True)
    (d / "skills" / "one" / "SKILL.md").write_text("---\nname: one\n---\n指令",
                                                  encoding="utf-8")
    packsign.sign_pack_files(d)
    packs.invalidate_scan_cache()
    info = next(p for p in packs.scan_packs() if p["name"] == "selfloop")
    assert info["trust"] == "signed" and info["status"] == "ok"
    assert packs.packs_signature() != during


def test_review_nested_alias_same_basename_snapshot_not_clobbered(packs_dir):
    """评审 A2（v0.17.36）：树内嵌套别名与真实技能同 basename（skills/x ->
    skills/y/z 与 skills/z 同名 z）——包级快照按真实路径键控，两组各服务
    各自已验内容，不串包覆盖（链接环境不可用时 skip，CI/Linux 恒跑）。"""
    _mk_pack(packs_dir, "clobber-pack", _valid_manifest("clobber-pack",
              skills=["skills/x", "skills/z"]),
             {"skills/y/z/SKILL.md": "---\nname: nested-z\n---\n嵌套技能",
              "skills/z/SKILL.md": "---\nname: flat-z\n---\n平铺技能"})
    if not _mk_link(packs_dir / "clobber-pack" / "skills" / "y" / "z",
                    packs_dir / "clobber-pack" / "skills" / "x"):
        pytest.skip("当前环境无法创建目录链接")
    packs.register_pack_skills()
    nested = skills.load_skill("nested-z")          # 兜底：嵌套组快照
    flat = skills.load_skill("flat-z")             # 兜底：平铺组快照
    assert nested is not None and "嵌套技能" in nested
    assert flat is not None and "平铺技能" in flat
    idx = {s["name"] for s in skills.list_skills()}
    assert idx == {"nested-z", "flat-z"}            # 串包时两者会解析成同名
