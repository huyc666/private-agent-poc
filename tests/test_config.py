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
