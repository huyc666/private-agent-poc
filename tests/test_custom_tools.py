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
    # v0.17.32 SEC-3：exec_mode 改读 config 常量（import 时固化），setenv 不再生效
    monkeypatch.setattr(config, "CUSTOM_TOOLS_EXEC", "sandbox")
    monkeypatch.setattr(config, "SANDBOX_URL", "")
    custom_tools._up_cache["t"] = 0.0
    assert custom_tools.exec_mode() == "sandbox-down"   # 强制模式不可达 fail-closed


# ---------------------------------------------------------------- SEC-3 执行模式默认值
# 默认值与运行模式联动（对齐 AUTH_ENABLED 先例）：Mock=开发者模式默认 auto；
# 真实 LLM 模式默认 sandbox fail-closed（沙箱不可达不在主进程 exec 工具代码）。

def test_ac3_default_mock_mode_keeps_auto(monkeypatch):
    monkeypatch.setattr(config, "CUSTOM_TOOLS_EXEC", "")   # 未显式配置
    monkeypatch.setattr(config, "MOCK_LLM", True)
    monkeypatch.setattr(config, "SANDBOX_URL", "")
    custom_tools._up_cache["t"] = 0.0
    assert custom_tools.exec_mode() == "local"    # auto 语义：不可达回退本地


def test_ac3_default_real_mode_sandbox_fail_closed(monkeypatch):
    monkeypatch.setattr(config, "CUSTOM_TOOLS_EXEC", "")   # 未显式配置
    monkeypatch.setattr(config, "MOCK_LLM", False)
    monkeypatch.setattr(config, "SANDBOX_URL", "")
    custom_tools._up_cache["t"] = 0.0
    assert custom_tools.exec_mode() == "sandbox-down"   # 真实模式默认 fail-closed


def test_ac3_explicit_config_overrides_default(monkeypatch):
    monkeypatch.setattr(config, "MOCK_LLM", False)
    monkeypatch.setattr(config, "SANDBOX_URL", "")
    expect = {"auto": "local", "local": "local", "sandbox": "sandbox-down"}
    for explicit in ("auto", "local", "sandbox"):
        monkeypatch.setattr(config, "CUSTOM_TOOLS_EXEC", explicit)
        custom_tools._up_cache["t"] = 0.0
        assert custom_tools.exec_mode() == expect[explicit]


def test_ac3_auto_fallback_local_warns_rate_limited(monkeypatch, caplog):
    """auto 回退 local 不再静默：显著 WARNING + 60s 限频防刷屏。"""
    monkeypatch.setattr(config, "CUSTOM_TOOLS_EXEC", "")
    monkeypatch.setattr(config, "MOCK_LLM", True)
    monkeypatch.setattr(config, "SANDBOX_URL", "")
    custom_tools._up_cache["t"] = 0.0
    custom_tools._auto_warn_cache["t"] = 0.0    # 前序用例已告警过，重置限频
    assert custom_tools.exec_mode() == "local"
    assert "主进程" in caplog.text
    custom_tools._up_cache["t"] = 0.0          # 限频窗口内重复回退不再告警
    caplog.clear()
    custom_tools.exec_mode()
    assert "主进程" not in caplog.text


# ---------------------------------------------------------------- 专属工具归属校验

def test_resolve_personal_user_role_boundaries(login_as):
    login_as("alice")
    # v0.17.32 SEC-2：域目录名带哈希后缀（user_scope），字面量 "alice" 不再命中
    alice_scope = config.user_scope("alice")
    custom_tools.write_custom_tool("mine_tool", _code_for("mine_tool"), scope=alice_scope)
    custom_tools.write_custom_tool("global_one", _code_for("global_one"))

    scope, path = custom_tools._resolve_personal("mine_tool")
    assert scope == alice_scope and path.is_file()
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
    custom_tools.write_custom_tool("mine_tool", _code_for("mine_tool"),
                                    scope=config.user_scope("alice"))
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
    """v0.17.33 改内容哈希后口径更新：内容变化即检测（原用例的 touch-only
    场景不再触发——内容未变时重建本就无意义，且快速连续写入的新内容
    必然改哈希，纳秒精度问题天然消除）；仅 touch 不改内容不再触发。"""
    custom_tools.write_custom_tool("demo_tool", VALID_CODE)
    sig1 = custom_tools.signature()
    p = config.CUSTOM_TOOLS_DIR / "demo_tool.py"
    ns = time.time_ns() + 1_000_000
    os.utime(p, ns=(ns, ns))
    assert custom_tools.signature() == sig1           # touch-only：内容未变不触发
    custom_tools.write_custom_tool("demo_tool", VALID_CODE + "\n# 变更\n")
    assert custom_tools.signature() != sig1          # 内容变化即触发


def test_personal_index_prompt_includes_own_tools(login_as):
    login_as("alice")
    custom_tools.write_custom_tool("mine_tool", _code_for("mine_tool"),
                                   scope=config.user_scope("alice"))
    prompt = custom_tools.personal_index_prompt()
    assert "mine_tool" in prompt and "专属自定义工具" in prompt


def test_personal_index_prompt_empty_for_user_without_tools(login_as):
    login_as("alice")
    assert custom_tools.personal_index_prompt() == ""


# ---- v0.17.33 AC3/AC4：内容哈希签名 + 非法执行模式白名单（复验实测）----

def test_ac3_signature_detects_tamper_with_restored_mtime(tmp_path, monkeypatch):
    """内容哈希签名：篡改内容并还原 mtime_ns 仍触发变更检测——复验实测
    mtime 签名可被『改内容 + 还原 mtime』绕过（不重建不重验）。"""
    import os
    d = tmp_path / "ct"
    d.mkdir()
    monkeypatch.setattr(config, "CUSTOM_TOOLS_DIR", d)
    f = d / "t.py"
    f.write_text("def t():\n    return 1\n", encoding="utf-8")
    sig1 = custom_tools.signature()
    st = f.stat()
    f.write_text("def t():\n    return 2\n", encoding="utf-8")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert f.stat().st_mtime_ns == st.st_mtime_ns    # mtime 已还原
    assert custom_tools.signature() != sig1          # 哈希仍感知篡改


def test_ac4_invalid_exec_config_fails_closed(monkeypatch):
    """非法执行模式值（如拼错的 sandbxo）不再静默落 auto 回退本地——
    按 sandbox 处理：沙箱不可达即 sandbox-down 拒绝（fail-closed）。"""
    monkeypatch.setattr(config, "CUSTOM_TOOLS_EXEC", "sandbxo")
    monkeypatch.setattr(config, "MOCK_LLM", False)
    monkeypatch.setattr(config, "SANDBOX_URL", "")
    assert custom_tools.exec_mode() == "sandbox-down"


# ---- v0.17.33 评审修复：C1 读失败兜底 / C4 MOCK+非法值组合 ----

def test_review_c1_signature_read_failure_falls_back(tmp_path, monkeypatch):
    """评审修复 C1 回归防线：单文件读失败（占用/权限/竞态）不得沿签名
    检测打断对话主链路——对齐 packs._file_sig 的 '?' 兜底；'?' 与任何
    成功哈希不同（异常状态触发重载，不吞），其余文件哈希不受影响。"""
    import pathlib
    d = tmp_path / "ct"
    d.mkdir()
    monkeypatch.setattr(config, "CUSTOM_TOOLS_DIR", d)
    (d / "ok_tool.py").write_text(_code_for("ok_tool"), encoding="utf-8")
    (d / "flaky_tool.py").write_text(_code_for("flaky_tool"), encoding="utf-8")
    sig_ok = custom_tools.signature()

    real_read_bytes = pathlib.Path.read_bytes

    def _boom(self):
        if self.name == "flaky_tool.py":
            raise PermissionError(13, "Permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", _boom)
    sig_bad = custom_tools.signature()            # 不上抛
    got = dict(sig_bad)
    assert got["flaky_tool.py"] == "?"            # 读失败落 '?' 兜底
    assert got["ok_tool.py"] == dict(sig_ok)["ok_tool.py"]
    assert sig_bad != sig_ok                      # 异常状态仍触发重载


def test_review_c4_mock_mode_invalid_value_still_fail_closed(monkeypatch):
    """评审补口 C4：MOCK 开发模式（未显式配置时默认宽松 auto）下显式配
    了非法值同样按 sandbox fail-closed——不因开发模式放宽；沙箱不可达即
    sandbox-down、自定义工具全清空，是 v0.17.33 有意的 fail-closed
    （v0.17.32 该场景静默回退 local）。"""
    monkeypatch.setattr(config, "CUSTOM_TOOLS_EXEC", "sandbxo")
    monkeypatch.setattr(config, "MOCK_LLM", True)
    monkeypatch.setattr(config, "SANDBOX_URL", "")
    custom_tools._up_cache["t"] = 0.0
    custom_tools._auto_warn_cache["t"] = 0.0
    assert custom_tools.exec_mode() == "sandbox-down"


def test_s4_validate_syntax_error_sanitized():
    """S4(b)：语法错误对外文案不拼 SyntaxError 原文——其文本含异常类型、
    行号与出错行代码片段（经 tools_admin/packs 透传到用户/模型通道），
    细节只落本机日志（对齐 tools_builtin 保存失败文案先例）。"""
    err = custom_tools.validate_tool_code("demo_tool", "def broken(:\n")
    assert err is not None and "语法错误" in err
    assert "invalid syntax" not in err
    assert "<unknown>" not in err
    assert "SyntaxError" not in err


# ---- B 批次：拒绝路径与边界分支（覆盖率补齐，不堆正向用例）----

def test_personal_dir_none_and_path_guards():
    """空域 → 无专属目录；非法名 / 空域 → personal_tool_path 一律拒绝。"""
    assert custom_tools._personal_dir(None) is None
    assert custom_tools._personal_dir("") is None
    assert custom_tools.personal_tool_path("Bad-Name", "alice") is None
    assert custom_tools.personal_tool_path("ok_tool", "") is None


def test_personal_scopes_empty_when_root_missing(tmp_path, monkeypatch):
    """专属根目录不存在 → 空清单（不是异常）。"""
    monkeypatch.setattr(custom_tools, "_personal_root", lambda: tmp_path / "nope")
    assert custom_tools.personal_scopes() == []


def test_list_personal_tools_skips_illegal_file_name(login_as):
    login_as("alice")
    scope = config.user_scope("alice")
    d = config.PERSONAL_CUSTOM_TOOLS_DIR / scope
    d.mkdir(parents=True, exist_ok=True)
    (d / "Bad-Name.py").write_text(_code_for("bad_name"), encoding="utf-8")
    assert custom_tools.list_personal_tools(scope) == []


def test_list_personal_all_skips_missing_scope(monkeypatch):
    """全局视图：域目录不存在 → 跳过，不抛异常。"""
    monkeypatch.setattr(custom_tools, "personal_scopes", lambda: ["ghost"])
    assert custom_tools.list_personal_all() == []


def test_list_personal_all_skips_illegal_name():
    d = config.PERSONAL_CUSTOM_TOOLS_DIR / "weird"
    d.mkdir(parents=True, exist_ok=True)
    (d / "Bad-Name.py").write_text(_code_for("bad_name"), encoding="utf-8")
    assert custom_tools.list_personal_all() == []


def test_extract_doc_no_matching_function(tmp_path, monkeypatch):
    """文件里没有同名顶层函数 → None（索引标注损坏，不执行代码）。"""
    d = tmp_path / "ct"
    d.mkdir()
    monkeypatch.setattr(config, "CUSTOM_TOOLS_DIR", d)
    (d / "lonely.py").write_text("def other_name():\n    '''x'''\n", encoding="utf-8")
    assert custom_tools._extract_doc(d / "lonely.py") is None
    assert custom_tools.list_custom_tools()[0]["description"].startswith("⚠️")


def test_ann_to_type_unknown_annotation_falls_back_str():
    """不认识的注解（Subscript 等）按 str 处理，不抛异常。"""
    src = ("def demo_tool(x: list[int] = None, y: 'int' = 'a') -> str:\n"
           "    '''文档'''\n    return 'x'\n")
    custom_tools.write_custom_tool("demo_tool", src)
    meta = custom_tools._extract_signature(config.CUSTOM_TOOLS_DIR / "demo_tool.py")
    assert [p["type"] for p in meta["params"]] == [str, str]


def test_extract_signature_broken_and_missing_function(tmp_path):
    broken = tmp_path / "broken_tool.py"
    broken.write_text("def broken(:\n", encoding="utf-8")
    assert custom_tools._extract_signature(broken) is None     # SyntaxError 兜底

    other = tmp_path / "other_tool.py"
    other.write_text("def different_name():\n    '''x'''\n", encoding="utf-8")
    assert custom_tools._extract_signature(other) is None      # 无同名函数


class _FakeUrlOpen:
    def __init__(self, payload: bytes = b"", status: int = 200):
        self._payload, self.status = payload, status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def test_sandbox_up_success_and_exec_mode_auto(monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_URL", "http://sbx.local")
    monkeypatch.setattr(custom_tools.urllib.request, "urlopen",
                        lambda *a, **k: _FakeUrlOpen(status=200))
    custom_tools._up_cache["t"] = 0.0
    assert custom_tools._sandbox_up() is True

    monkeypatch.setattr(config, "CUSTOM_TOOLS_EXEC", "")
    monkeypatch.setattr(config, "MOCK_LLM", True)
    assert custom_tools.exec_mode() == "sandbox"


def test_sandbox_run_tool_ok_failure_and_unreachable(monkeypatch):
    """沙箱调用三态：成功取值 / 沙箱侧失败透传 error / 网络失败只回类型名。"""
    monkeypatch.setattr(config, "SANDBOX_URL", "http://sbx.local")
    monkeypatch.setattr(config, "SANDBOX_AUTH_TOKEN", "tok")
    seen = {}

    def _ok(req, timeout=0):
        seen["header"] = dict(req.header_items())
        return _FakeUrlOpen(b'{"ok": true, "result": 7}')

    monkeypatch.setattr(custom_tools.urllib.request, "urlopen", _ok)
    assert custom_tools._sandbox_run_tool("demo_tool", {"x": 1}) == "7"
    assert "x-sandbox-token" in {k.lower() for k in seen["header"]}

    monkeypatch.setattr(custom_tools.urllib.request, "urlopen",
                        lambda *a, **k: _FakeUrlOpen(b'{"ok": false, "error": "boom"}'))
    assert "沙箱执行失败: boom" in custom_tools._sandbox_run_tool("demo_tool", {})

    def _down(*a, **k):
        raise ConnectionError("MARKER http://internal:8080")
    monkeypatch.setattr(custom_tools.urllib.request, "urlopen", _down)
    out = custom_tools._sandbox_run_tool("demo_tool", {})
    assert "沙箱调用失败（ConnectionError）" in out
    assert "internal:8080" not in out          # 原始异常不回流调用方


def test_make_sandbox_tool_schema_and_proxy(monkeypatch):
    """ast 签名 → args_schema（必填/可选）→ 执行代理回沙箱。"""
    meta = {"name": "demo_tool", "description": "演示", "params": [
        {"name": "x", "type": int, "required": True, "default": None},
        {"name": "y", "type": str, "required": False, "default": "a"},
    ], "is_async": False}
    monkeypatch.setattr(custom_tools, "_sandbox_run_tool",
                        lambda name, args, pack="", env=None, code="": "代理结果")
    tool = custom_tools._make_sandbox_tool(meta)
    assert tool.name == "demo_tool"
    assert tool.args_schema.model_fields["x"].is_required()
    assert not tool.args_schema.model_fields["y"].is_required()
    assert tool.func(x=1, y="b") == "代理结果"


def test_make_local_tool_non_callable_and_import_failure(tmp_path, monkeypatch):
    d = tmp_path / "ct"
    d.mkdir()
    monkeypatch.setattr(config, "CUSTOM_TOOLS_DIR", d)
    (d / "no_fn.py").write_text("no_fn = 1\n", encoding="utf-8")
    assert custom_tools.make_local_tool(d / "no_fn.py") is None

    (d / "boom_tool.py").write_text("raise RuntimeError('import boom')\n", encoding="utf-8")
    assert custom_tools.make_local_tool(d / "boom_tool.py") is None


def test_build_tool_sandbox_signature_failure_and_success(tmp_path, monkeypatch):
    monkeypatch.setattr(custom_tools, "exec_mode", lambda: "sandbox")
    broken = tmp_path / "broken_tool.py"
    broken.write_text("def broken(:\n", encoding="utf-8")
    assert custom_tools.build_tool(broken) is None      # 签名提取失败 fail-closed

    ok = tmp_path / "ok_tool.py"
    ok.write_text(_code_for("ok_tool"), encoding="utf-8")
    assert custom_tools.build_tool(ok) is not None


def test_load_langchain_tools_skips_bad_names_and_failed_build(monkeypatch):
    monkeypatch.setattr(config, "CUSTOM_TOOLS_EXEC", "local")
    custom_tools._dir().mkdir(parents=True, exist_ok=True)
    (config.CUSTOM_TOOLS_DIR / "Bad-Name.py").write_text(_code_for("bad_name"),
                                                         encoding="utf-8")
    (config.CUSTOM_TOOLS_DIR / "broken_tool.py").write_text("def broken(:\n",
                                                            encoding="utf-8")
    assert custom_tools.load_langchain_tools() == []


def test_resolve_personal_admin_not_found(login_as):
    login_as("root", role="admin")
    out = custom_tools._resolve_personal("ghost_tool")
    assert isinstance(out, str) and "不存在或不可见" in out


def test_run_personal_tool_unexpected_mode_and_read_failure(login_as, tmp_path,
                                                            monkeypatch):
    login_as("alice")
    p = tmp_path / "mine_tool.py"
    p.write_text(_code_for("mine_tool"), encoding="utf-8")
    monkeypatch.setattr(custom_tools, "_resolve_personal", lambda name: ("alice", p))
    monkeypatch.setattr(custom_tools, "exec_mode", lambda: "weird")
    out = asyncio.run(custom_tools.run_personal_tool("mine_tool", {}))
    assert "沙箱不可达" in out

    monkeypatch.setattr(custom_tools, "_resolve_personal",
                        lambda name: ("alice", tmp_path / "gone.py"))
    monkeypatch.setattr(custom_tools, "exec_mode", lambda: "sandbox")
    assert "源码读取失败" in asyncio.run(custom_tools.run_personal_tool("mine_tool", {}))


def test_run_personal_tool_signature_failure(tmp_path, monkeypatch, login_as):
    login_as("alice")
    p = tmp_path / "broken_tool.py"
    p.write_text("def broken(:\n", encoding="utf-8")
    monkeypatch.setattr(custom_tools, "_resolve_personal", lambda name: ("alice", p))
    monkeypatch.setattr(custom_tools, "exec_mode", lambda: "sandbox")
    out = asyncio.run(custom_tools.run_personal_tool("broken_tool", {}))
    assert "签名提取失败" in out


def test_run_personal_tool_success_and_sanitized_error(login_as, monkeypatch, tmp_path):
    class _OkTool:
        async def ainvoke(self, args):
            return "执行结果"

    login_as("alice")
    p = tmp_path / "mine_tool.py"
    p.write_text(_code_for("mine_tool"), encoding="utf-8")
    monkeypatch.setattr(custom_tools, "_resolve_personal", lambda name: ("alice", p))
    monkeypatch.setattr(custom_tools, "exec_mode", lambda: "sandbox")
    monkeypatch.setattr(custom_tools, "_make_sandbox_tool", lambda meta, code="": _OkTool())
    assert asyncio.run(custom_tools.run_personal_tool("mine_tool", {})) == "执行结果"

    class _BoomTool:
        async def ainvoke(self, args):
            raise RuntimeError(r"E:\secret\path boom")

    monkeypatch.setattr(custom_tools, "_make_sandbox_tool", lambda meta, code="": _BoomTool())
    out = asyncio.run(custom_tools.run_personal_tool("mine_tool", {}))
    assert "执行失败（内部错误）" in out
    assert "secret" not in out and "E:\\" not in out


def test_personal_index_prompt_guards(login_as, monkeypatch):
    from server import auth
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    assert custom_tools.personal_index_prompt() == ""      # 开放模式无专属域

    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    login_as("alice")
    monkeypatch.setattr(custom_tools, "current_scope", lambda: None)
    assert custom_tools.personal_index_prompt() == ""      # user 无域

    monkeypatch.setattr(auth, "current_role", lambda: "guest")
    assert custom_tools.personal_index_prompt() == ""      # 未知角色


def test_personal_index_prompt_skips_broken_and_qualifies_admin(login_as):
    login_as("root", role="admin")
    scope = "alice"
    d = config.PERSONAL_CUSTOM_TOOLS_DIR / scope
    d.mkdir(parents=True, exist_ok=True)
    (d / "broken_tool.py").write_text("def broken(:\n", encoding="utf-8")
    assert custom_tools.personal_index_prompt() == ""       # 损坏工具不进索引

    (d / "good_tool.py").write_text(_code_for("good_tool"), encoding="utf-8")
    prompt = custom_tools.personal_index_prompt()
    assert "good_tool" in prompt and "（专属域：" in prompt  # 全局视图带域前缀


def test_sandbox_up_probe_error_is_false(monkeypatch):
    """探测抛异常（网络错误）→ 按不可达处理，不向上抛。"""
    monkeypatch.setattr(config, "SANDBOX_URL", "http://sbx.local")
    custom_tools._up_cache["t"] = 0.0

    def _boom(*a, **k):
        raise OSError("MARKER probe failed")
    monkeypatch.setattr(custom_tools.urllib.request, "urlopen", _boom)
    assert custom_tools._sandbox_up() is False


def test_sandbox_run_tool_passes_code_source(monkeypatch):
    """专属工具源码随请求直传沙箱（code 非空分支），调用方拿回结果。"""
    monkeypatch.setattr(config, "SANDBOX_URL", "http://sbx.local")
    seen = {}

    def _ok(req, timeout=0):
        import json as _json
        seen["body"] = _json.loads(req.data.decode())
        return _FakeUrlOpen(b'{"ok": true, "result": "done"}')

    monkeypatch.setattr(custom_tools.urllib.request, "urlopen", _ok)
    out = custom_tools._sandbox_run_tool("demo_tool", {"x": 1},
                                         code="def demo_tool(x):\n    return x\n")
    assert out == "done"
    assert seen["body"]["code"].startswith("def demo_tool")


def test_resolve_personal_user_without_scope_and_unknown_role(login_as, monkeypatch):
    from server import auth
    login_as("alice")
    monkeypatch.setattr(custom_tools, "current_scope", lambda: None)
    assert custom_tools._resolve_personal("mine_tool") == "专属自定义工具仅对登录用户可用"

    monkeypatch.setattr(custom_tools, "current_scope", lambda: "alice")
    monkeypatch.setattr(auth, "current_role", lambda: "guest")
    assert custom_tools._resolve_personal("mine_tool") == "专属自定义工具仅对登录用户可用"


def test_run_personal_tool_rejects_bad_args_and_unresolved(login_as):
    login_as("alice")
    assert "args 必须是参数对象" in asyncio.run(
        custom_tools.run_personal_tool("mine_tool", ["not-a-dict"]))
    out = asyncio.run(custom_tools.run_personal_tool("Bad-Name", {}))
    assert "非法工具名" in out                                # 解析失败原样返回


def test_run_personal_tool_sandbox_down_fail_closed(login_as, tmp_path, monkeypatch):
    login_as("alice")
    p = tmp_path / "mine_tool.py"
    p.write_text(_code_for("mine_tool"), encoding="utf-8")
    monkeypatch.setattr(custom_tools, "_resolve_personal", lambda name: ("alice", p))
    monkeypatch.setattr(custom_tools, "exec_mode", lambda: "sandbox-down")
    assert "沙箱不可达" in asyncio.run(custom_tools.run_personal_tool("mine_tool", {}))


def test_build_and_load_local_tools(monkeypatch):
    """local 模式正向链路：make_local_tool 成功构造 + 列表非空（日志分支）。"""
    monkeypatch.setattr(config, "CUSTOM_TOOLS_EXEC", "local")
    custom_tools.write_custom_tool("ok_tool", _code_for("ok_tool"))
    tool = custom_tools.make_local_tool(config.CUSTOM_TOOLS_DIR / "ok_tool.py")
    assert tool is not None and tool.name == "ok_tool"
    assert [t.name for t in custom_tools.load_langchain_tools()] == ["ok_tool"]


def test_sandbox_down_build_and_load_return_empty(monkeypatch):
    """sandbox-down（fail-closed）：不构造任何工具，列表为空。"""
    monkeypatch.setattr(custom_tools, "exec_mode", lambda: "sandbox-down")
    custom_tools.write_custom_tool("ok_tool2", _code_for("ok_tool2"))
    assert custom_tools.build_tool(config.CUSTOM_TOOLS_DIR / "ok_tool2.py") is None
    assert custom_tools.load_langchain_tools() == []
