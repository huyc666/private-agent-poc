"""config：scope_clean 身份清洗规则 + 运行时库迁移（v0.17.19）。

端点侧（main._user_scope）与工具侧（tools_builtin._file_scope）共用
scope_clean，任何变体漂移都会破坏多用户文件隔离——此组用例锁定规则全貌。
"""
from server import config


def test_basic_identity_unchanged():
    assert config.scope_clean("alice") == "alice"
    assert config.scope_clean("User_01") == "User_01"


def test_chinese_username_kept():
    assert config.scope_clean("张三") == "张三"


def test_illegal_chars_normalized_to_underscore():
    assert config.scope_clean("a/b") == "a_b"
    assert config.scope_clean("a b") == "a_b"
    assert config.scope_clean("a..b") == "a__b"


def test_length_capped_at_48():
    assert len(config.scope_clean("x" * 200)) == 48


def test_empty_and_none_fallback_to_shared():
    assert config.scope_clean("") == "shared"
    assert config.scope_clean(None) == "shared"
    assert config.scope_clean("   ") == "shared"


def test_all_illegal_chars_fallback_to_shared():
    # 清洗后为空 → 回退 shared（保留域，不可注册为用户名）
    assert config.scope_clean("###") == "shared"
    assert config.scope_clean("///") == "shared"


def test_leading_trailing_underscores_stripped():
    assert config.scope_clean("_bob_") == "bob"
    assert config.scope_clean("___") == "shared"


def test_scope_clean_is_deterministic():
    assert config.scope_clean("bad/name!#") == config.scope_clean("bad/name!#")


# ---- SEC-2（spec v0.17.32 AC2）：user_scope 哈希域 ----
# scope_clean 只保证目录名合法，不保证跨用户唯一（a@b 与 a_b 同为 a_b）；
# user_scope 在其上加 sha256 后缀，不同用户名必得不同目录名。

def test_ac2_user_scope_no_cross_user_collision():
    assert config.user_scope("a@b") != config.user_scope("a_b")
    assert config.user_scope("a b") != config.user_scope("a_b")
    assert config.user_scope("a/b") != config.user_scope("a_b")
    assert config.user_scope("a.b") != config.user_scope("a_b")


def test_ac2_user_scope_deterministic_and_hash_suffixed():
    assert config.user_scope("alice") == config.user_scope("alice")
    assert config.user_scope("alice").startswith("alice-")
    assert len(config.user_scope("alice").rsplit("-", 1)[1]) == 8  # sha256 前 8 hex


def test_ac2_user_scope_shared_fallback():
    """全非法字符/空名回退保留域 shared（不带哈希，兼容开放模式遗留）。"""
    assert config.user_scope("") == "shared"
    assert config.user_scope(None) == "shared"
    assert config.user_scope("###") == "shared"


def test_ac2_user_scope_keeps_unicode():
    assert config.user_scope("张三").startswith("张三-")


def test_ac2_user_scope_strips_source():
    """哈希输入为 strip 后的原始用户名（与 add_user 落库口径一致）。"""
    assert config.user_scope(" alice ") == config.user_scope("alice")


# ---- URL 域段清洗（评审修复 C2，v0.17.32）----

def test_scope_segment_keeps_full_hashed_domain():
    """长哈希域名（最长 57 字符）不被截断——此前沿用 scope_clean 的 48 截断
    会截掉基名 ≥40 字用户的哈希后缀，全局视图按 URL 寻址其域 404。"""
    long_scope = config.user_scope("u" * 48)
    assert len(long_scope) == 57
    assert config.scope_segment(long_scope) == long_scope


def test_scope_segment_cleans_like_scope_clean():
    """与 scope_clean 同字符集清洗与 shared 回退，仅长度上限放宽到 57
    （超长输入截到 57，不再触碰合法域名长度）。"""
    assert config.scope_segment("bad name") == "bad_name"
    assert config.scope_segment("  ") == "shared"
    assert config.scope_segment("!!!") == "shared"
    assert config.scope_segment("x" * 60) == "x" * 57


# ---- 运行时库迁移（v0.17.19：根目录遗留 state.db/usage.db → data/）----

def test_migrate_legacy_db_moves_root_dbs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "_DATA_DIR", tmp_path / "data")
    (tmp_path / "state.db").write_bytes(b"x")
    (tmp_path / "state.db-wal").write_bytes(b"w")
    (tmp_path / "usage.db").write_bytes(b"u")
    config._migrate_legacy_db("state.db")
    config._migrate_legacy_db("usage.db")
    d = tmp_path / "data"
    assert (d / "state.db").read_bytes() == b"x"
    assert (d / "state.db-wal").read_bytes() == b"w"
    assert (d / "usage.db").read_bytes() == b"u"
    assert not (tmp_path / "state.db").exists()
    assert not (tmp_path / "state.db-wal").exists()
    assert not (tmp_path / "usage.db").exists()


def test_migrate_legacy_db_noop_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "_DATA_DIR", tmp_path / "data")
    config._migrate_legacy_db("state.db")   # 无遗留文件：不建库、不报错
    assert not (tmp_path / "data" / "state.db").exists()


def test_migrate_legacy_db_never_overwrites_existing(tmp_path, monkeypatch):
    """data/ 已有现役库时不覆盖（根目录残留视为重启旧副本，等人工核对）。"""
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(config, "_DATA_DIR", tmp_path / "data")
    d = tmp_path / "data"
    d.mkdir()
    (d / "state.db").write_bytes(b"current")
    (tmp_path / "state.db").write_bytes(b"stale")
    config._migrate_legacy_db("state.db")
    assert (d / "state.db").read_bytes() == b"current"
    assert (tmp_path / "state.db").read_bytes() == b"stale"
