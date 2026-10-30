"""SEC-2（spec v0.17.32 AC2）：用户文件域哈希化——存量迁移 + 跨用户域隔离。

user_scope = scope_clean + sha256 后缀，不同用户名必得不同目录名，从根本上
消除 a@b / a_b 清洗碰撞。存量迁移（main._migrate_scope_dirs，启动 lifespan
执行）把老命名目录 rename 到新域名；碰撞存量无法自动归属，跳过并告警。
"""
import pytest

from server import auth, config
from server.main import _migrate_scope_dirs
import server.tools_builtin as tb

LEGACY_ROOTS = ("UPLOADS_DIR", "OUTPUTS_DIR", "PERSONAL_SKILLS_DIR",
                "PERSONAL_CUSTOM_TOOLS_DIR")


def _seed(root, scope, rel, content="x"):
    """在 <root>/<scope>/<rel> 落一个文件（rel 可含子目录），返回路径。"""
    d = root / scope
    d.mkdir(parents=True, exist_ok=True)
    p = d / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------- 存量迁移

def test_ac2_migrate_renames_legacy_scope_dir(make_user):
    make_user("zhang_san")
    _seed(config.UPLOADS_DIR, "zhang_san", "a.md")
    _migrate_scope_dirs()
    new_dir = config.UPLOADS_DIR / config.user_scope("zhang_san")
    assert (new_dir / "a.md").is_file()
    assert not (config.UPLOADS_DIR / "zhang_san").exists()


def test_ac2_migrate_idempotent(make_user):
    make_user("zhang_san")
    _seed(config.UPLOADS_DIR, "zhang_san", "a.md")
    _migrate_scope_dirs()
    _migrate_scope_dirs()      # 幂等：第二遍不再搬动
    new_dir = config.UPLOADS_DIR / config.user_scope("zhang_san")
    assert (new_dir / "a.md").is_file()
    dirs = [p.name for p in config.UPLOADS_DIR.iterdir() if p.is_dir()]
    assert dirs == [config.user_scope("zhang_san")]


def test_ac2_migrate_collision_dir_skipped_with_warning(make_user, capsys):
    """存量碰撞目录（老域名映射多用户）无法自动归属：不动 + WARNING 人工整改。"""
    make_user("a@b")
    make_user("a_b")           # 两用户的老域名清洗后同为 a_b
    _seed(config.UPLOADS_DIR, "a_b", "contested.md")
    _migrate_scope_dirs()
    assert (config.UPLOADS_DIR / "a_b" / "contested.md").is_file()   # 不动
    assert "碰撞" in capsys.readouterr().out


def test_ac2_migrate_existing_new_scope_skips(make_user, capsys):
    """新域名目录已存在（人工先迁移过/残留）：跳过 rename，两侧保留并告警。"""
    make_user("zhang_san")
    _seed(config.UPLOADS_DIR, "zhang_san", "a.md")
    _seed(config.UPLOADS_DIR, config.user_scope("zhang_san"), "b.md")
    _migrate_scope_dirs()
    assert (config.UPLOADS_DIR / "zhang_san" / "a.md").is_file()
    assert (config.UPLOADS_DIR / config.user_scope("zhang_san") / "b.md").is_file()
    assert "跳过" in capsys.readouterr().out


def test_ac2_migrate_unclaimed_dir_untouched():
    """无人认领的目录（非任何用户的老域名）不动：admin 全局视图仍可见。"""
    _seed(config.UPLOADS_DIR, "orphan", "a.md")
    _migrate_scope_dirs()
    assert (config.UPLOADS_DIR / "orphan" / "a.md").is_file()


def test_ac2_migrate_shared_domain_excluded(make_user):
    """shared/ 保留域（开放模式遗留）不参与迁移。"""
    make_user("zhang_san")
    _seed(config.UPLOADS_DIR, "shared", "legacy.md")
    _migrate_scope_dirs()
    assert (config.UPLOADS_DIR / "shared" / "legacy.md").is_file()


def test_ac2_migrate_revoked_user_still_claims(make_user):
    """已吊销用户的域目录同样迁移（吊销不删记录，文件保留审计价值）。"""
    make_user("gone_user")
    auth.revoke_user("gone_user")
    _seed(config.OUTPUTS_DIR, "gone_user", "report.md")
    _migrate_scope_dirs()
    assert (config.OUTPUTS_DIR / config.user_scope("gone_user")
            / "report.md").is_file()


def test_ac2_migrate_covers_personal_dirs(make_user):
    """四类根目录全覆盖：uploads/outputs 之外的专属技能与专属工具域。"""
    make_user("zhang_san")
    _seed(config.PERSONAL_SKILLS_DIR, "zhang_san", "myskill/SKILL.md")
    _seed(config.PERSONAL_CUSTOM_TOOLS_DIR, "zhang_san", "my_tool.py")
    _migrate_scope_dirs()
    s = config.user_scope("zhang_san")
    assert (config.PERSONAL_SKILLS_DIR / s / "myskill" / "SKILL.md").is_file()
    assert (config.PERSONAL_CUSTOM_TOOLS_DIR / s / "my_tool.py").is_file()


# ---------------------------------------------------------------- 跨用户域隔离

def test_ac2_colliding_users_get_distinct_domains(login_as):
    """核心场景：a@b 与 a_b 的文件域必须不同（此前清洗后同为 a_b）。"""
    login_as("a@b")
    scope_a = tb._file_scope()
    login_as("a_b")
    scope_b = tb._file_scope()
    assert scope_a != scope_b
    assert scope_a == config.user_scope("a@b")
    assert scope_b == config.user_scope("a_b")


def test_ac2_cross_user_file_read_blocked(login_as):
    """user 角色不可读他人域内文件（提示词注入/路径猜测面，回归防线）。"""
    login_as("alice")
    scope_a = config.user_scope("alice")
    _seed(config.UPLOADS_DIR, scope_a, "secret.md", "机密内容")
    login_as("mallory")
    denied = tb.read_text_file(f"uploads/{scope_a}/secret.md")
    assert "不可读取" in denied
