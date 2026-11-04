"""编排定义创建入口（v0.17.44，spec D7 / AC14）。

个人自助（user 落 orchestrations_personal/<域>/）；全局定义（approver/admin/
开放模式）必须走审批，职责分离沿用 v0.17.41 口径。
"""
import asyncio

import pytest

from server import approval, auth, config, orchestration as orch, usage


VALID_YAML = """name: team-demo
version: '1'
roles:
  - id: worker
    prompt: 执行任务
    tools: []
steps:
  - id: run
    role: worker
    mode: single
"""

INVALID_YAML = """name: bad-demo
roles: []
steps: []
"""


def _identity(username="alice", role="user", department="finance"):
    return usage.Identity(username=username, role=role,
                          department=department, auth_source="bearer")


def _install_interrupt(monkeypatch, decision, calls=None):
    import langgraph.types as types

    def fake_interrupt(payload):
        if calls is not None:
            calls.append(payload)
        return decision

    monkeypatch.setattr(types, "interrupt", fake_interrupt)


def _set_identity(identity):
    usage.current_identity.set(identity)
    usage.current_user.set(identity.username)


def test_global_definition_requires_approval_then_persists(monkeypatch):
    calls = []
    _set_identity(_identity(username="boss", role="approver", department="hr"))
    _install_interrupt(monkeypatch, {"approve": True}, calls)

    message = asyncio.run(orch.create_orchestration(VALID_YAML))
    assert "已创建" in message
    assert calls and calls[0]["tool"] == "create_orchestration"
    assert calls[0]["args"]["name"] == "team-demo"
    assert (config.ORCHESTRATIONS_DIR / "team-demo.yaml").exists()


def test_global_definition_rejected_does_not_persist(monkeypatch):
    _set_identity(_identity(username="boss", role="approver", department="hr"))
    _install_interrupt(monkeypatch, {"approve": False})

    message = asyncio.run(orch.create_orchestration(VALID_YAML))
    assert "拒绝" in message
    assert not (config.ORCHESTRATIONS_DIR / "team-demo.yaml").exists()


def test_approval_unavailable_fails_closed(monkeypatch):
    import langgraph.types as types

    def boom(payload):
        raise RuntimeError("no graph context")

    monkeypatch.setattr(types, "interrupt", boom)
    _set_identity(_identity(username="boss", role="approver", department="hr"))
    message = asyncio.run(orch.create_orchestration(VALID_YAML))
    assert "未执行" in message
    assert not (config.ORCHESTRATIONS_DIR / "team-demo.yaml").exists()


def test_personal_definition_is_self_service_without_approval(monkeypatch):
    calls = []
    _set_identity(_identity(username="alice", role="user", department="finance"))
    _install_interrupt(monkeypatch, {"approve": True}, calls)

    message = asyncio.run(orch.create_orchestration(VALID_YAML))
    assert "已创建" in message
    assert calls == []
    scope = config.user_scope("alice")
    assert (config.PERSONAL_ORCHESTRATIONS_DIR / scope / "team-demo.yaml").exists()
    assert not (config.ORCHESTRATIONS_DIR / "team-demo.yaml").exists()


def test_invalid_definition_rejected_before_approval(monkeypatch):
    calls = []
    _set_identity(_identity(username="boss", role="approver", department="hr"))
    _install_interrupt(monkeypatch, {"approve": True}, calls)

    message = asyncio.run(orch.create_orchestration(INVALID_YAML))
    assert "创建失败" in message
    assert calls == []
    assert not (config.ORCHESTRATIONS_DIR / "bad-demo.yaml").exists()


def test_existing_definition_not_overwritten_without_flag(monkeypatch):
    calls = []
    target = config.ORCHESTRATIONS_DIR / "team-demo.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("原内容", encoding="utf-8")
    _set_identity(_identity(username="boss", role="approver", department="hr"))
    _install_interrupt(monkeypatch, {"approve": True}, calls)

    message = asyncio.run(orch.create_orchestration(VALID_YAML))
    assert "已存在" in message
    assert calls == []
    assert target.read_text(encoding="utf-8") == "原内容"


def test_global_create_rechecks_existence_after_approval(monkeypatch):
    """审批窗口内同名文件出现时，overwrite=False 不得被静默覆盖。"""
    target = config.ORCHESTRATIONS_DIR / "team-demo.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    _set_identity(_identity(username="boss", role="approver", department="hr"))

    def fake_require_approval(tool, args, title, execute):
        target.write_text("审批窗口内新建", encoding="utf-8")
        return execute()

    monkeypatch.setattr(approval, "require_approval", fake_require_approval)
    message = asyncio.run(orch.create_orchestration(VALID_YAML))
    assert "已存在" in message
    assert target.read_text(encoding="utf-8") == "审批窗口内新建"


def test_overwrite_flag_replaces_definition(monkeypatch):
    target = config.ORCHESTRATIONS_DIR / "team-demo.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("原内容", encoding="utf-8")
    _set_identity(_identity(username="boss", role="approver", department="hr"))
    _install_interrupt(monkeypatch, {"approve": True})

    message = asyncio.run(orch.create_orchestration(VALID_YAML, overwrite=True))
    assert "已创建" in message
    assert "team-demo" in target.read_text(encoding="utf-8")


def test_user_cannot_write_global_definition_via_name_path(monkeypatch):
    """定义名只允许 slug，路径成分/绝对路径在写盘前即被拒。"""
    _set_identity(_identity(username="alice", role="user", department="finance"))
    message = asyncio.run(orch.create_orchestration(
        VALID_YAML.replace("name: team-demo", "name: ../global")))
    assert "创建失败" in message
    assert not (config.ORCHESTRATIONS_DIR / "../global.yaml").exists()
