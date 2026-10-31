"""skills：技能注册中心（创建分流 / 专属域遮蔽 / 技能包安全导入 / 内容哈希签名）。"""
import io
import os
import time
import zipfile
from pathlib import Path

import pytest

from server import config, skills


def _make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


SKILL_MD = "---\nname: demo-skill\ndescription: 演示技能\n---\n\n# 演示\n内容\n"


# ---------------------------------------------------------------- 创建与索引

def test_create_skill_general_for_admin(login_as):
    login_as("admin1", role="admin")
    out = skills.create_skill("weekly-report", "周报", "# 周报\n内容")
    assert "skills/weekly-report/SKILL.md" in out          # 逻辑路径
    assert (config.SKILLS_DIR / "weekly-report" / "SKILL.md").is_file()


def test_create_skill_routes_to_personal_domain_for_user(login_as):
    login_as("alice")
    alice = config.user_scope("alice")
    out = skills.create_skill("my-notes", "个人笔记", "# 笔记")
    assert f"skills_personal/{alice}/my-notes/SKILL.md" in out
    assert "仅本人可见" in out
    assert (config.PERSONAL_SKILLS_DIR / alice / "my-notes" / "SKILL.md").is_file()
    # 通用层不受影响
    assert not (config.SKILLS_DIR / "my-notes").exists()


def test_create_skill_rejects_bad_name():
    out = skills.create_skill("Bad_Name", "x", "y")
    assert "创建失败" in out


def test_create_skill_exists_conflict_and_overwrite(login_as):
    login_as("admin1", role="admin")
    skills.create_skill("dup-skill", "d1", "内容一")
    out = skills.create_skill("dup-skill", "d2", "内容二")
    assert "已存在" in out and "overwrite" in out
    out = skills.create_skill("dup-skill", "d2", "内容二", overwrite=True)
    assert "已更新" in out


def test_create_skill_overwrite_clears_stale_siblings(login_as):
    login_as("admin1", role="admin")
    skills.create_skill("clean-skill", "d", "内容", overwrite=True)
    stale = config.SKILLS_DIR / "clean-skill" / "references"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "old.md").write_text("旧附属文件")
    skills.create_skill("clean-skill", "d", "新内容", overwrite=True)
    assert not stale.exists()                              # 旧附属文件不残留


def test_user_cannot_create_skill_shadowing_global(login_as):
    login_as("admin1", role="admin")
    skills.create_skill("shared-skill", "通用", "内容")
    login_as("alice")
    out = skills.create_skill("shared-skill", "专属", "内容")
    assert "同名" in out and "遮蔽" in out                  # 直接拒绝防混淆
    assert not (config.PERSONAL_SKILLS_DIR / "alice" / "shared-skill").exists()


def test_list_skills_includes_personal_only_for_own_scope(login_as):
    login_as("admin1", role="admin")
    skills.create_skill("global-skill", "通用", "x")
    login_as("alice")
    skills.create_skill("alice-only", "专属", "y")
    names = [s["name"] for s in skills.list_skills(config.user_scope("alice"))]
    assert "global-skill" in names and "alice-only" in names
    assert [s["name"] for s in skills.list_skills(None)] == ["global-skill"]


def test_load_skill_prefers_global_layer(login_as):
    login_as("admin1", role="admin")
    skills.create_skill("weather-report", "通用版", "通用内容")
    login_as("alice")
    # 手放同名专属技能（绕过创建拦截）→ 加载时通用层优先
    pdir = config.PERSONAL_SKILLS_DIR / "alice" / "weather-report"
    pdir.mkdir(parents=True)
    (pdir / "SKILL.md").write_text(
        "---\nname: weather-report\ndescription: 专属版\n---\n专属内容")
    assert "通用内容" in skills.load_skill("weather-report", "alice")


def test_load_skill_rejects_invalid_name():
    assert skills.load_skill("../evil") is None


def test_personal_index_prompt_lists_own_skills(login_as):
    login_as("alice")
    skills.create_skill("mine", "专属技能", "x")
    prompt = skills.personal_index_prompt(config.user_scope("alice"))
    assert "mine" in prompt and "仅本人可见" in prompt


# ---------------------------------------------------------------- 技能包导入安全

def test_import_zip_valid_directory_form(login_as):
    login_as("admin1", role="admin")
    data = _make_zip({"demo-skill/SKILL.md": SKILL_MD,
                      "demo-skill/references/extra.md": "补充"})
    result = skills.import_skill_zip(data)
    assert result["name"] == "demo-skill"
    assert result["path"] == "skills/demo-skill"
    assert (config.SKILLS_DIR / "demo-skill" / "references" / "extra.md").is_file()


def test_import_zip_root_skill_md(login_as):
    login_as("admin1", role="admin")
    result = skills.import_skill_zip(_make_zip({"SKILL.md": SKILL_MD}))
    assert result["path"] == "skills/demo-skill"


def test_import_zip_rejects_bad_archive():
    with pytest.raises(ValueError, match="不是有效的 zip"):
        skills.import_skill_zip(b"not-a-zip")
    with pytest.raises(ValueError, match="压缩包为空"):
        skills.import_skill_zip(_make_zip({}))
    with pytest.raises(ValueError, match="未找到 SKILL.md"):
        skills.import_skill_zip(_make_zip({"other.md": "x"}))


def test_import_zip_rejects_zip_slip():
    data = _make_zip({"SKILL.md": SKILL_MD,
                      "nested/../../../evil.md": "x"})
    with pytest.raises(ValueError, match="非法条目路径"):
        skills.import_skill_zip(data)


def test_import_zip_rejects_disallowed_extension():
    data = _make_zip({"demo-skill/SKILL.md": SKILL_MD,
                      "demo-skill/tool.exe": "bin"})
    with pytest.raises(ValueError, match="不支持的文件类型"):
        skills.import_skill_zip(data)


def test_import_zip_rejects_multiple_top_level_dirs():
    data = _make_zip({"a/SKILL.md": SKILL_MD, "b/other.md": "x"})
    with pytest.raises(ValueError, match="顶层内容"):
        skills.import_skill_zip(data)


def test_import_zip_rejects_bad_frontmatter_name():
    data = _make_zip({"SKILL.md": "---\nname: Bad Name\n---\n内容"})
    with pytest.raises(ValueError, match="name"):
        skills.import_skill_zip(data)


def test_import_zip_conflict_and_atomic_overwrite(login_as):
    login_as("admin1", role="admin")
    data = _make_zip({"demo-skill/SKILL.md": SKILL_MD,
                      "demo-skill/references/old.md": "旧文件"})
    skills.import_skill_zip(data)
    # 未声明 overwrite → 409 冲突
    with pytest.raises(FileExistsError):
        skills.import_skill_zip(data)
    # 覆盖导入：整目录替换，旧附属文件清掉
    new = _make_zip({"demo-skill/SKILL.md": SKILL_MD})
    skills.import_skill_zip(new, overwrite=True)
    assert not (config.SKILLS_DIR / "demo-skill" / "references").exists()


def test_import_zip_routes_to_personal_scope(login_as):
    login_as("admin1", role="admin")
    skills.create_skill("taken", "通用", "x")
    login_as("alice")
    # 专属导入与通用技能同名 → 拒绝
    with pytest.raises(ValueError, match="同名"):
        skills.import_skill_zip(_make_zip({"taken/SKILL.md": SKILL_MD.replace(
            "demo-skill", "taken")}), personal_scope="alice")
    ok = _make_zip({"alice-private/SKILL.md": SKILL_MD.replace(
        "demo-skill", "alice-private")})
    result = skills.import_skill_zip(ok, personal_scope="alice")
    assert result["path"] == "skills_personal/alice/alice-private"


def test_failed_import_does_not_damage_existing(login_as):
    """导入中途校验失败：原技能不受损（先校验后落盘）。"""
    login_as("admin1", role="admin")
    skills.import_skill_zip(_make_zip({"demo-skill/SKILL.md": SKILL_MD}))
    bad = _make_zip({"demo-skill/SKILL.md": SKILL_MD,
                     "demo-skill/bad.exe": "x"})
    with pytest.raises(ValueError):
        skills.import_skill_zip(bad, overwrite=True)
    # 原技能仍在
    assert skills.load_skill("demo-skill") is not None


# ---------------------------------------------------------------- 变更检测

def test_ac1_skill_signature_detects_tamper_with_restored_mtime(login_as):
    """AC1：篡改技能内容并还原 mtime（v0.17.33 复验实测绕过手法）→
    内容哈希仍检出。取代旧 test_signature_uses_nanosecond_mtime——其断言
    基于 mtime 检测，内容哈希口径下语义反转（时间戳不再参与签名）。"""
    login_as("admin1", role="admin")
    skills.create_skill("fast-skill", "d", "v1")
    p = config.SKILLS_DIR / "fast-skill" / "SKILL.md"
    sig1 = skills.signature()
    st = p.stat()
    p.write_text("---\nname: fast-skill\ndescription: d\n---\n\nv2\n",
                 encoding="utf-8")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))     # 还原 mtime
    assert skills.signature() != sig1                     # 内容哈希仍检出


def test_ac1_skill_signature_ignores_mtime_only_change(login_as):
    """AC1 反向：仅改 mtime、内容不变 → 签名稳定（检测只认内容）。"""
    login_as("admin1", role="admin")
    skills.create_skill("stable-skill", "d", "v")
    p = config.SKILLS_DIR / "stable-skill" / "SKILL.md"
    sig1 = skills.signature()
    ns = time.time_ns() + 1_000_000_000
    os.utime(p, ns=(ns, ns))                              # mtime 前进、内容不变
    assert skills.signature() == sig1


def test_ac1_skill_signature_read_failure_fallback(login_as, monkeypatch):
    """AC1：读失败返回 '?' 兜底（对齐 custom_tools / packs 的 _file_sig
    口径，v0.17.33 评审 C1 先例）——异常状态参与签名触发一次重载，
    不静默吞掉、更不上抛打断本轮对话。"""
    login_as("admin1", role="admin")
    skills.create_skill("sig-err-skill", "d", "v")

    def _boom(self):
        raise OSError("file locked")
    monkeypatch.setattr(Path, "read_bytes", _boom)
    sig = skills.signature()
    assert sig and all(h == "?" for _rel, h in sig)
