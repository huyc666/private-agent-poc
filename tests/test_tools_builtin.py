"""tools_builtin：运行时安全边界（工作区读写边界 / 敏感文件拦截 / 密钥脱敏 /
多用户四类目录越域拦截 / 文件名清洗 / 公文 docx 转换）。"""
import io
import zipfile

from server import config, tools_builtin, usage


def _ws_file(rel: str, content: str = "hello"):
    p = config.TOOL_WORKSPACE / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------- 读取边界

def test_read_within_workspace():
    _ws_file("notes.md", "内容")
    assert tools_builtin.read_text_file("notes.md") == "内容"


def test_read_outside_workspace_denied():
    assert "路径越出工作区边界" in tools_builtin.read_text_file("../outside.md")


def test_read_sensitive_env_and_db_blocked():
    """v0.17.4 F1：.env 及变体与运行时 SQLite 库一律拒绝读取（即使审批通过）。"""
    for name in (".env", ".env.production", "state.db", "usage.db",
                 "state.db-wal", "state.db-shm"):
        _ws_file(name, "SECRET=1")
        assert "敏感文件" in tools_builtin.read_text_file(name), name


def test_read_masks_secret_values_per_line():
    _ws_file("conf.cfg",
             "API_KEY = abc123\n"
             'MY_TOKEN: "xyz456"\n'
             "DATABASE_URL = postgres://h/db\n"
             "plain_text = visible\n")
    out = tools_builtin.read_text_file("conf.cfg")
    assert "abc123" not in out and "xyz456" not in out     # 值被掩码
    assert "***（已脱敏）" in out
    assert "postgres://h/db" in out                         # 非敏感键不动
    assert "plain_text = visible" in out


def test_read_nonexistent_file_message():
    assert "文件不存在" in tools_builtin.read_text_file("ghost.md")


def test_read_truncated_at_20k():
    _ws_file("big.txt", "x" * 30_000)
    assert len(tools_builtin.read_text_file("big.txt")) == 20_000


# ---------------------------------------------------------------- 多用户越域拦截

def test_user_cannot_read_others_domains(login_as):
    """v0.17.9/v0.17.12：user 角色读他人专属技能/专属工具/上传/产出一律拒绝。"""
    login_as("alice")
    cases = {
        "uploads/bob/f.md": "上传/产出文件",
        "outputs/bob/f.md": "上传/产出文件",
        "skills_personal/bob/skill-a/SKILL.md": "专属技能",
        "custom_tools_personal/bob/tool_a.py": "专属自定义工具",
    }
    for rel, kind in cases.items():
        _ws_file(rel, "他人内容")
        out = tools_builtin.read_text_file(rel)
        assert "他人" in out and kind in out, rel


def test_user_can_read_own_domain(login_as):
    login_as("alice")
    _ws_file("uploads/alice/f.md", "自己的文档")
    assert tools_builtin.read_text_file("uploads/alice/f.md") == "自己的文档"


def test_approver_global_view_reads_any_domain(login_as):
    login_as("ap1", role="approver")
    _ws_file("uploads/alice/f.md", "alice 的文档")
    assert tools_builtin.read_text_file("uploads/alice/f.md") == "alice 的文档"


def test_open_mode_uses_shared_domain(monkeypatch):
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    _ws_file("uploads/shared/f.md", "共享文档")
    assert tools_builtin.read_text_file("uploads/shared/f.md") == "共享文档"


def test_list_uploaded_docs_scoped_to_current_user(login_as):
    login_as("alice")
    _ws_file("uploads/alice/a.md", "x")
    _ws_file("uploads/bob/b.md", "y")
    out = tools_builtin.list_uploaded_docs()
    assert "uploads/alice/a.md" in out
    assert "bob" not in out                                  # 他人文档对模型不可见


def test_list_uploaded_docs_empty_when_no_dir(login_as):
    login_as("alice")
    assert "暂无上传文档" in tools_builtin.list_uploaded_docs()


# ---------------------------------------------------------------- 文件保存

def test_save_output_file_strips_path_and_saves(login_as):
    login_as("alice")
    out = tools_builtin.save_output_file("../../../../报告.md", "内容")
    assert "已保存" in out
    assert "下载链接：/api/files/alice/报告.md" in out      # 逻辑路径（单段路径剥成 basename）
    assert (config.OUTPUTS_DIR / "alice" / "报告.md").is_file()


def test_save_output_file_rejects_illegal_names(login_as):
    login_as("alice")
    for name in ("a<b.md", 'a"b.md', "con.md", "nul.md", "com1.md",
                 "", ".", "..", "x" * 121):
        out = tools_builtin.save_output_file(name, "内容")
        assert "保存失败" in out, name


def test_save_output_file_whitelist_suffix(login_as):
    login_as("alice")
    assert "白名单" in tools_builtin.save_output_file("app.exe", "x")
    assert "已保存" in tools_builtin.save_output_file("ok.json", "{}")


def test_save_output_file_docx_requires_docx_suffix(login_as):
    login_as("alice")
    out = tools_builtin.save_output_file("a.md", "x", as_docx=True)
    assert "必须以 .docx 结尾" in out


def test_save_output_file_size_limit(monkeypatch, login_as):
    monkeypatch.setattr(config, "UPLOAD_MAX_MB", 0)
    login_as("alice")
    assert "大小上限" in tools_builtin.save_output_file("big.md", "x")


def test_mdtext_to_docx_generates_valid_package():
    raw = "# 标题\x01\x02\n\n一、条款\n\n（一）细则\n\n正文段落\n"
    data = tools_builtin._mdtext_to_docx(raw)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert "\x01" not in xml and "\x02" not in xml          # 控制字符剥离
    assert "方正小标宋简体" in xml                            # 标题版式
    assert "黑体" in xml and "楷体" in xml and "仿宋" in xml   # 分层字体


def test_save_docx_success(login_as):
    login_as("alice")
    out = tools_builtin.save_output_file("公文.docx", "# 公文\n\n正文", as_docx=True)
    assert "Word 文档" in out
    with zipfile.ZipFile(config.OUTPUTS_DIR / "alice" / "公文.docx") as z:
        assert z.testzip() is None


# ---------------------------------------------------------------- 删除边界

def test_do_delete_normal_file(login_as):
    login_as("alice")
    _ws_file("trash.md")
    assert "已删除" in tools_builtin.do_delete("trash.md")
    assert not (config.TOOL_WORKSPACE / "trash.md").exists()


def test_do_delete_sensitive_file_blocked_even_exists(login_as):
    """v0.17.5：敏感文件删除与读取对称拦截（审批≠边界放行）。"""
    login_as("ap1", role="admin")
    _ws_file(".env")
    _ws_file("state.db")
    assert "拒绝删除" in tools_builtin.do_delete(".env")
    assert "拒绝删除" in tools_builtin.do_delete("state.db")
    assert (config.TOOL_WORKSPACE / ".env").exists()


def test_do_delete_cross_scope_blocked(login_as):
    login_as("alice")
    _ws_file("uploads/bob/f.md")
    assert "拒绝删除" in tools_builtin.do_delete("uploads/bob/f.md")
    assert (config.TOOL_WORKSPACE / "uploads/bob/f.md").exists()


def test_do_delete_outside_workspace_and_missing(login_as):
    login_as("alice")
    assert "路径越出工作区边界" in tools_builtin.do_delete("../x.md")
    assert "文件不存在" in tools_builtin.do_delete("ghost.md")


def test_run_python_code_fail_closed_without_sandbox():
    """未配置 SANDBOX_URL → 拒绝执行（fail-closed：不允许未隔离的代码执行）。"""
    out = tools_builtin.run_python_code("print(1)")
    assert "代码执行未启用" in out and "fail-closed" in out
