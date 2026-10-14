"""config.scope_clean：身份 → 文件域目录名的唯一清洗规则。

端点侧（main._user_scope）与工具侧（tools_builtin._file_scope）共用，
任何变体漂移都会破坏多用户文件隔离——此组用例锁定规则全貌。
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
