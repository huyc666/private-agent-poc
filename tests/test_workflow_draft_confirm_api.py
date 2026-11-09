"""4.3 草稿确认端点：AC5 红测试（confirm / discard / GET 详情）。

只按 docs/specs/4.3-doc-to-workflow.md §2.4 的端点契约写断言：
- 归属校验、状态机、哈希校验、同名拒绝、并发只成功一个、body owner/scope 被忽略；
- 端点当前未实现 → 相关断言应失败（404），红因即「未实现」。

夹具说明：草稿资产由测试直接按 §2.3 的元数据契约写入草稿目录，使端点用例
不依赖生成链路是否已实现（分层红测试）；资产格式与 spec 字段一一对应。
"""
import asyncio
import builtins
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from server import approval, audit, auth, config, orchestration as orch, usage

API = "/api/orchestration-drafts"


# ---------------------------------------------------------------- 夹具

def _drafts_root():
    root = getattr(config, "ORCHESTRATION_DRAFTS_DIR", None)
    if root is None:
        root = Path(config.TOOL_WORKSPACE) / "orchestration_drafts"
    return Path(root)


def _definition(name="doc-flow"):
    return {
        "name": name, "version": "1", "description": "文档生成流程",
        "max_parallel": 2,
        "roles": [
            {"id": "intake", "name": "受理", "prompt": "整理业务要求", "tools": []},
            {"id": "reviewer", "name": "审核", "prompt": "按制度复核", "tools": []},
        ],
        "steps": [
            {"id": "intake", "role": "intake", "mode": "single"},
            {"id": "review", "role": "reviewer", "mode": "single",
             "inputs": ["intake"]},
        ],
        "aggregate": {"role": "reviewer", "inputs": ["review"]},
    }


def _make_draft(*, username="alice", role="user", name="doc-flow",
                status="pending", scope=None, source_text="制度正文",
                definition=None):
    """按 §2.3 契约写一份草稿资产；返回定位信息。"""
    definition = definition or _definition(name)
    if scope is None:
        scope = config.user_scope(username) if role == "user" else "global"
    src_scope = config.user_scope(username)
    src_dir = Path(config.UPLOADS_DIR) / src_scope
    src_dir.mkdir(parents=True, exist_ok=True)
    src = src_dir / "制度.md"
    src.write_text(source_text, encoding="utf-8")

    did = "drf-" + hashlib.sha256(
        f"{username}:{name}:{status}".encode()).hexdigest()[:12]
    raw = yaml.safe_dump(definition, allow_unicode=True,
                         sort_keys=False).encode("utf-8")
    root = _drafts_root() / scope
    root.mkdir(parents=True, exist_ok=True)
    yaml_path = root / f"{did}.yaml"
    yaml_path.write_bytes(raw)
    meta = {
        "draft_id": did, "owner": username, "owner_role": role, "scope": scope,
        "source_path": f"uploads/{src_scope}/{src.name}",
        "source_sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
        "source_chars": len(source_text),
        "definition_name": name, "definition_version": "1",
        "definition_hash": hashlib.sha256(raw).hexdigest(),
        "status": status, "created_at": time.time(),
        "generation_notes": "生成说明：按文档角色生成",
        "validation": {"status": "ok", "errors": []},
    }
    meta_path = root / f"{did}.meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {"draft_id": did, "scope": scope, "root": root, "yaml": yaml_path,
            "meta": meta_path, "meta_data": meta, "source": src,
            "name": name}


def _read_meta(info):
    return json.loads(Path(info["meta"]).read_text(encoding="utf-8"))


def _target(info):
    if info["scope"] == "global":
        return Path(config.ORCHESTRATIONS_DIR) / f"{info['name']}.yaml"
    return (Path(config.PERSONAL_ORCHESTRATIONS_DIR) / info["scope"]
            / f"{info['name']}.yaml")


def _client(client_factory, username, role="user"):
    key = auth.add_user(username, role=role)
    assert key, f"创建用户 {username} 失败"
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    return c


def _as_identity(username, role="user"):
    usage.current_user.set(username)
    usage.current_identity.set(usage.Identity(
        username=username, role=role, department="finance",
        auth_source="bearer"))


# ---------------------------------------------------------------- GET 详情

def test_ac5_get_detail_owner_ok(client_factory):
    info = _make_draft()
    r = _client(client_factory, "alice").get(f"{API}/{info['draft_id']}")
    assert r.status_code == 200, r.text
    body = r.text
    assert info["draft_id"] in body
    assert "doc-flow" in body
    assert "reviewer" in body, "详情需含角色/工具矩阵"
    assert "制度.md" in body, "详情需含来源文档相对路径"


def test_ac5_get_detail_admin_ok(client_factory):
    info = _make_draft()
    r = _client(client_factory, "root", role="admin").get(
        f"{API}/{info['draft_id']}")
    assert r.status_code == 200, r.text


def test_ac5_get_detail_other_user_404(client_factory):
    info = _make_draft()
    r = _client(client_factory, "bob").get(f"{API}/{info['draft_id']}")
    assert r.status_code == 404, "他人草稿不回显存在性"


def test_ac5_get_detail_anonymous_401(client_factory):
    info = _make_draft()
    assert client_factory().get(f"{API}/{info['draft_id']}").status_code == 401


# ---------------------------------------------------------------- confirm 正常路径与幂等

def test_ac5_owner_confirm_writes_personal_definition(client_factory):
    info = _make_draft()
    r = _client(client_factory, "alice").post(
        f"{API}/{info['draft_id']}/confirm")
    assert r.status_code == 200, r.text
    target = _target(info)
    assert target.exists(), "确认后必须落个人域定义"
    assert orch.validate_definition(yaml.safe_load(target.read_text(
        encoding="utf-8"))) == []
    assert _read_meta(info)["status"] == "confirmed"


def test_ac5_confirm_anonymous_401(client_factory):
    info = _make_draft()
    assert client_factory().post(
        f"{API}/{info['draft_id']}/confirm").status_code == 401


def test_ac5_confirm_other_user_404(client_factory):
    info = _make_draft()
    r = _client(client_factory, "bob").post(
        f"{API}/{info['draft_id']}/confirm")
    assert r.status_code == 404
    assert not _target(info).exists()


def test_ac5_approver_cannot_confirm_other_personal_draft(client_factory):
    info = _make_draft()
    r = _client(client_factory, "carol", role="approver").post(
        f"{API}/{info['draft_id']}/confirm")
    assert r.status_code == 404, "approver 对他人个人草稿只读"
    assert not _target(info).exists()


def test_ac5_confirm_idempotent_then_target_missing_409(client_factory):
    info = _make_draft()
    c = _client(client_factory, "alice")
    assert c.post(f"{API}/{info['draft_id']}/confirm").status_code == 200
    again = c.post(f"{API}/{info['draft_id']}/confirm")
    assert again.status_code == 200 and (
        "already_confirmed" in again.text or "已确认" in again.text), again.text

    _target(info).unlink()
    missing = c.post(f"{API}/{info['draft_id']}/confirm")
    assert missing.status_code == 409
    assert "target_definition_missing" in missing.text or "不存在" in missing.text


def test_ac5_confirm_discarded_409(client_factory):
    info = _make_draft(status="discarded")
    r = _client(client_factory, "alice").post(
        f"{API}/{info['draft_id']}/confirm")
    assert r.status_code == 409
    assert "discard" in r.text or "已丢弃" in r.text
    assert not _target(info).exists()


def test_ac5_confirm_definition_hash_mismatch_409(client_factory):
    info = _make_draft()
    Path(info["yaml"]).write_text("name: tampered\n", encoding="utf-8")
    r = _client(client_factory, "alice").post(
        f"{API}/{info['draft_id']}/confirm")
    assert r.status_code == 409
    assert _read_meta(info)["status"] == "pending"
    assert not _target(info).exists()


def test_ac5_confirm_source_hash_mismatch_409(client_factory):
    info = _make_draft()
    Path(info["source"]).write_text("制度正文（生成后被改动）", encoding="utf-8")
    r = _client(client_factory, "alice").post(
        f"{API}/{info['draft_id']}/confirm")
    assert r.status_code == 409
    assert _read_meta(info)["status"] == "pending"
    assert not _target(info).exists()


def test_ac5_confirm_same_name_409_no_overwrite(client_factory):
    info = _make_draft()
    target = _target(info)
    target.parent.mkdir(parents=True, exist_ok=True)
    original = "name: doc-flow\nversion: '1'\n# 既有定义\n"
    target.write_text(original, encoding="utf-8")
    r = _client(client_factory, "alice").post(
        f"{API}/{info['draft_id']}/confirm")
    assert r.status_code == 409
    assert target.read_text(encoding="utf-8") == original, "不得静默覆盖"


def test_ac5_concurrent_confirm_only_one_succeeds(client_factory):
    info = _make_draft()
    key = auth.add_user("alice", role="user")
    assert key
    c1 = client_factory()
    c2 = client_factory()
    for c in (c1, c2):
        c.headers.update({"Authorization": f"Bearer {key}"})
    url = f"{API}/{info['draft_id']}/confirm"
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = sorted(pool.map(lambda c: c.post(url).status_code, [c1, c2]))
    assert codes == [200, 409], f"并发确认只允许一个成功：{codes}"
    assert _target(info).exists()


def test_ac5_body_owner_and_scope_ignored(client_factory):
    info = _make_draft()
    r = _client(client_factory, "alice").post(
        f"{API}/{info['draft_id']}/confirm",
        json={"owner": "bob", "scope": "bob-scope"})
    assert r.status_code == 200, r.text
    assert _target(info).exists()
    bob_target = Path(config.PERSONAL_ORCHESTRATIONS_DIR) / "bob-scope" / "doc-flow.yaml"
    assert not bob_target.exists(), "body 的 owner/scope 必须被忽略"


# ---------------------------------------------------------------- discard 三态

def test_ac5_discard_pending_200_removes_yaml(client_factory):
    info = _make_draft()
    r = _client(client_factory, "alice").post(
        f"{API}/{info['draft_id']}/discard")
    assert r.status_code == 200
    assert not Path(info["yaml"]).exists()
    assert _read_meta(info)["status"] == "discarded"


def test_ac5_discard_confirmed_keeps_definition(client_factory):
    info = _make_draft()
    c = _client(client_factory, "alice")
    assert c.post(f"{API}/{info['draft_id']}/confirm").status_code == 200
    r = c.post(f"{API}/{info['draft_id']}/discard")
    assert r.status_code == 200
    assert _target(info).exists(), "丢弃 confirmed 草稿不得影响已启用定义"
    assert not Path(info["yaml"]).exists()


def test_ac5_discard_discarded_409(client_factory):
    info = _make_draft(status="discarded")
    assert _client(client_factory, "alice").post(
        f"{API}/{info['draft_id']}/discard").status_code == 409


def test_ac5_discard_anonymous_401(client_factory):
    info = _make_draft()
    assert client_factory().post(
        f"{API}/{info['draft_id']}/discard").status_code == 401


def test_ac5_discard_other_user_404(client_factory):
    info = _make_draft()
    assert _client(client_factory, "bob").post(
        f"{API}/{info['draft_id']}/discard").status_code == 404
    assert Path(info["yaml"]).exists()


# ---------------------------------------------------------------- global 域：审批门

def test_ac5_global_confirm_endpoint_rejected(client_factory):
    info = _make_draft(username="carol", role="approver", scope="global")
    r = _client(client_factory, "root", role="admin").post(
        f"{API}/{info['draft_id']}/confirm")
    assert r.status_code in (403, 404), "global 草稿必须走审批，端点不得直通"
    assert not _target(info).exists()


def test_guard_detail_whitelist_hides_internal_target_path(client_factory):
    """安全复审 L3：详情按字段白名单返回，不把服务端绝对路径透给前端。"""
    info = _make_draft(status="confirmed")
    meta = _read_meta(info)
    meta["target_path"] = "/srv/secret/definitions/doc-flow.yaml"
    Path(info["meta"]).write_text(json.dumps(meta, ensure_ascii=False),
                                  encoding="utf-8")
    r = _client(client_factory, "alice").get(f"{API}/{info['draft_id']}")
    assert r.status_code == 200, r.text
    assert "target_path" not in r.text
    assert "/srv/secret" not in r.text


def test_guard_user_role_cannot_read_others_global_draft():
    """安全复审 L1：user 角色读 global 域时按 owner 过滤（与列表侧同口径）。"""
    info = _make_draft(username="ghost", role="approver", scope="global")
    ident = usage.Identity(username="anonymous", role="user", department="",
                           auth_source="sso")
    assert orch.get_draft_detail(info["draft_id"], ident)["status"] == 404


def test_ac5_stale_meta_cannot_resurrect_deleted_target():
    """安全复审 L2：审批回调按旧 meta 快照执行时，必须按 draft_id 重读磁盘。"""
    info = _make_draft(username="root", role="approver", scope="global",
                       name="stale-flow")
    stale = dict(info["meta_data"])
    first = orch._do_confirm("global", dict(stale))
    assert first["status"] == 200, first
    assert _target(info).exists()
    # 目标仍存在：第二次按旧快照确认应返回 200 already_confirmed（不是 target_exists）
    second = orch._do_confirm("global", dict(stale))
    assert second["status"] == 200 and second.get("already_confirmed"), second
    # 目标被删后：不得按旧快照重新写回
    _target(info).unlink()
    third = orch._do_confirm("global", dict(stale))
    assert third["status"] == 409, third
    assert third["error"] == "target_definition_missing"
    assert not _target(info).exists(), "旧快照不得把已删除定义重新写回"


def test_ac5_confirm_audit_records_source_path(client_factory):
    """安全复审 L9：确认审计带来源文档相对路径（spec §2.5）。"""
    info = _make_draft()
    r = _client(client_factory, "alice").post(f"{API}/{info['draft_id']}/confirm")
    assert r.status_code == 200, r.text
    rows = audit.query(action="确认流程草稿")
    assert rows, "应写确认审计"
    assert "source=uploads/" in rows[0]["detail"]
    assert "制度.md" in rows[0]["detail"]


def test_ac5_global_confirm_tool_requires_approval(client_factory):
    """confirm_orchestration_draft 只登记审批，不直接启用（不得模型自确认）。"""
    info = _make_draft(username="carol", role="approver", scope="global")
    _as_identity("carol", role="approver")
    fn = getattr(orch, "confirm_orchestration_draft", None)
    if fn is None:
        pytest.fail("未实现：server.orchestration.confirm_orchestration_draft")
    import asyncio
    import inspect
    result = fn(info["draft_id"])
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    assert isinstance(result, str)
    assert "审批" in result, f"global 确认必须返回待审批，不得直接启用：{result!r}"
    assert not _target(info).exists()
    assert _read_meta(info)["status"] == "pending"


def test_ac5_global_confirmation_needs_two_deciders():
    """职责分离：发起者不得自批，需另一名 approver/admin 决议（fail-closed）。"""
    _as_identity("carol", role="approver")
    aid = approval.create_approval("confirm_orchestration_draft",
                                   {"draft_id": "drf-000000000000"})
    assert approval.resolve(aid, True, "carol", "approver") == "forbidden"
    assert approval.resolve(aid, True, "dave", "approver") == "ok"


# ---------------------------------------------------------------- 篡改/畸形输入

def test_ac5_confirm_done_cache_bounded_and_expiring(monkeypatch):
    """并发判定缓存不会无限累积：超上限淘汰最旧、过期条目清理。"""
    with orch._CONFIRM_DONE_LOCK:
        orch._CONFIRM_DONE.clear()
        orch._CONFIRM_DONE["drf-000000000000"] = time.perf_counter() - 10
    monkeypatch.setattr(orch, "_CONFIRM_DONE_TTL", 0.0)
    orch._mark_confirm_done("drf-000000000001")
    assert "drf-000000000000" not in orch._CONFIRM_DONE, "过期条目必须清理"

    monkeypatch.setattr(orch, "_CONFIRM_DONE_TTL", 300.0)
    monkeypatch.setattr(orch, "_CONFIRM_DONE_MAX", 2)
    with orch._CONFIRM_DONE_LOCK:
        orch._CONFIRM_DONE.clear()
    for i in range(5):
        orch._mark_confirm_done(f"drf-{i:012x}")
    with orch._CONFIRM_DONE_LOCK:
        keys = list(orch._CONFIRM_DONE)
    assert keys == ["drf-000000000003", "drf-000000000004"], \
        f"缓存必须受硬上限约束并淘汰最旧：{keys}"


def test_ac5_confirm_tampered_meta_name_409(client_factory):
    """元数据中的定义名被篡改成非 `_NAME_RE` 形式时拒绝落盘。"""
    info = _make_draft()
    meta = _read_meta(info)
    meta["definition_name"] = "../escaped"
    Path(info["meta"]).write_text(json.dumps(meta, ensure_ascii=False),
                                  encoding="utf-8")
    r = _client(client_factory, "alice").post(
        f"{API}/{info['draft_id']}/confirm")
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "definition_invalid"
    assert not (Path(config.PERSONAL_ORCHESTRATIONS_DIR)
                / config.user_scope("alice") / "escaped.yaml").exists()


def test_ac5_confirm_meta_yaml_name_mismatch_409(client_factory):
    """元数据定义名与草稿正文 name 不一致时拒绝（防按 A 名登记 B 内容）。"""
    info = _make_draft(definition=_definition("other-flow"), name="doc-flow")
    r = _client(client_factory, "alice").post(
        f"{API}/{info['draft_id']}/confirm")
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "definition_invalid"
    assert not _target(info).exists()
    assert _read_meta(info)["status"] == "pending"


def test_ac5_get_detail_malformed_draft_id_404(client_factory):
    """draft_id 不符合 `drf-<12hex>` 时按不存在处理（不枚举）。"""
    r = _client(client_factory, "alice").get(f"{API}/drf-not-a-draft")
    assert r.status_code == 404, r.text


def test_ac5_get_detail_corrupted_yaml_degrades(client_factory):
    """草稿 YAML 损坏时详情仍可读出元数据，矩阵降级为空而非 500。"""
    info = _make_draft()
    Path(info["yaml"]).write_text("roles: [\n", encoding="utf-8")
    r = _client(client_factory, "alice").get(f"{API}/{info['draft_id']}")
    assert r.status_code == 200, r.text
    body = r.json()["draft"]
    assert body["role_tools"] == []
    assert body["role_count"] == 0
    assert body["draft_id"] == info["draft_id"]


# ---------------------------------------------------------------- 状态机/异常分支补覆盖
# 以下用例针对 _do_confirm / _do_discard 的可达错误分支逐条取证；均为真实
# 运行时可能出现的输入（篡改的元数据、并发重叠、写盘失败），不是为凑覆盖率
# 而伪造的不可达路径。

def test_guard_confirm_rejects_bad_draft_id_in_meta():
    """元数据 draft_id 形态非法时拒绝，不拼路径。"""
    out = orch._do_confirm("global", {"draft_id": "not-a-draft"})
    assert out["status"] == 409 and out["error"] == "definition_invalid"


def test_guard_confirm_missing_meta_after_snapshot():
    """审批回调持有旧快照但磁盘元数据已被删除 → draft_missing。"""
    info = _make_draft()
    stale = dict(info["meta_data"])
    Path(info["meta"]).unlink()
    out = orch._do_confirm(info["scope"], stale)
    assert out["status"] == 409 and out["error"] == "draft_missing"


def test_guard_confirm_concurrent_repeat_409():
    """完成时刻晚于本次请求进入时刻 → 判为并发重叠（409），不当成功重试。"""
    info = _make_draft(status="confirmed")
    target = _target(info)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(Path(info["yaml"]).read_text(encoding="utf-8"),
                      encoding="utf-8")
    started_at = time.perf_counter() - 1.0
    with orch._CONFIRM_DONE_LOCK:
        orch._CONFIRM_DONE[info["draft_id"]] = time.perf_counter()
    try:
        out = orch._do_confirm(info["scope"], dict(info["meta_data"]),
                               started_at=started_at)
    finally:
        with orch._CONFIRM_DONE_LOCK:
            orch._CONFIRM_DONE.pop(info["draft_id"], None)
    assert out["status"] == 409 and out["error"] == "confirm_in_progress"


def test_guard_confirm_invalid_yaml_with_matching_hash():
    """草稿正文损坏且哈希自洽（生成后落盘坏文件）→ 校验失败 409。"""
    info = _make_draft()
    bad = b"roles: [\n"
    Path(info["yaml"]).write_bytes(bad)
    meta = _read_meta(info)
    meta["definition_hash"] = hashlib.sha256(bad).hexdigest()
    Path(info["meta"]).write_text(json.dumps(meta, ensure_ascii=False),
                                  encoding="utf-8")
    out = orch._do_confirm(info["scope"], meta)
    assert out["status"] == 409 and out["error"] == "definition_invalid"


def test_guard_confirm_invalid_source_path_in_meta():
    """元数据来源路径被篡改成越界形态 → source_invalid，不读盘。"""
    info = _make_draft()
    meta = _read_meta(info)
    meta["source_path"] = "../etc/passwd"
    Path(info["meta"]).write_text(json.dumps(meta, ensure_ascii=False),
                                  encoding="utf-8")
    out = orch._do_confirm(info["scope"], meta)
    assert out["status"] == 409 and out["error"] == "source_invalid"


def test_guard_confirm_write_failed_returns_500(monkeypatch):
    """排他创建目标定义时底层 OSError → 500 write_failed（不静默成功）。"""
    info = _make_draft()
    target = _target(info)
    real_open = builtins.open

    def fake_open(file, mode="r", *args, **kwargs):
        if str(file) == str(target) and "x" in mode:
            raise OSError("disk full")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)
    out = orch._do_confirm(info["scope"], _read_meta(info))
    assert out["status"] == 500 and out["error"] == "write_failed"


def test_guard_confirm_meta_rewrite_failure_still_200(monkeypatch):
    """定义已落盘后状态回写失败只告警，不把成功结果改判失败。"""
    info = _make_draft()

    def boom(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(orch.os, "replace", boom)
    out = orch._do_confirm(info["scope"], _read_meta(info))
    assert out["status"] == 200
    assert _target(info).exists()


def test_guard_confirm_endpoint_rejects_global_scope():
    """global 域确认端点直通必须 403（与工具侧审批门同口径）。"""
    info = _make_draft(username="carol", role="approver", scope="global")
    ident = usage.Identity(username="carol", role="approver",
                           department="finance", auth_source="bearer")
    out = orch.confirm_draft(info["draft_id"], ident)
    assert out["status"] == 403 and out["error"] == "approval_required"
    assert not _target(info).exists()


def test_guard_discard_bad_draft_id_in_meta():
    out = orch._do_discard("global", {"draft_id": "bad"})
    assert out["status"] == 409 and out["error"] == "definition_invalid"


def test_guard_discard_missing_meta_after_snapshot():
    """回调旧快照到达时磁盘元数据已删除 → draft_missing，不按旧状态落盘。"""
    info = _make_draft()
    stale = dict(info["meta_data"])
    Path(info["meta"]).unlink()
    out = orch._do_discard(info["scope"], stale)
    assert out["status"] == 409 and out["error"] == "draft_missing"


def test_guard_discard_unlink_failure_returns_500(monkeypatch):
    info = _make_draft()

    def boom(self, *args, **kwargs):
        raise OSError("locked")

    monkeypatch.setattr(Path, "unlink", boom)
    out = orch._do_discard(info["scope"], _read_meta(info))
    assert out["status"] == 500 and out["error"] == "discard_failed"


def test_guard_discard_meta_rewrite_failure_still_200(monkeypatch):
    info = _make_draft()

    def boom(*args, **kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(orch.os, "replace", boom)
    out = orch._do_discard(info["scope"], _read_meta(info))
    assert out["status"] == 200 and out["previous_status"] == "pending"
    assert not Path(info["yaml"]).exists()


def test_guard_discard_endpoint_rejects_global_scope():
    info = _make_draft(username="carol", role="approver", scope="global")
    ident = usage.Identity(username="carol", role="approver",
                           department="finance", auth_source="bearer")
    out = orch.discard_draft(info["draft_id"], ident)
    assert out["status"] == 403 and out["error"] == "approval_required"


def test_guard_list_drafts_skips_corrupt_and_other_owner():
    """损坏元数据与同域他人草稿都不出现在列表里（不泄露存在性）。"""
    info = _make_draft()
    meta = _read_meta(info)
    meta["owner"] = "bob"
    Path(info["meta"]).write_text(json.dumps(meta, ensure_ascii=False),
                                  encoding="utf-8")
    (Path(info["root"]) / "drf-ffffffffffff.meta.json").write_text(
        "{not-json", encoding="utf-8")
    _as_identity("alice", role="user")
    assert "当前没有流程草稿" in orch.list_orchestration_drafts()


def test_guard_discard_tool_confirmed_returns_assets_cleared_message():
    info = _make_draft()
    ident = usage.Identity(username="alice", role="user",
                           department="finance", auth_source="bearer")
    assert orch.confirm_draft(info["draft_id"], ident)["status"] == 200
    _as_identity("alice", role="user")
    text = orch.discard_orchestration_draft(info["draft_id"])
    assert "已清理草稿资产" in text, text
    assert _target(info).exists(), "已启用定义不受影响"


def test_guard_confirm_tool_not_found():
    _as_identity("alice", role="user")
    text = asyncio.run(orch.confirm_orchestration_draft("drf-000000000000"))
    assert "确认失败" in text, text


def test_guard_confirm_tool_global_yaml_read_failure():
    """global 草稿正文缺失时审批卡降级为空矩阵，不抛异常。"""
    info = _make_draft(username="carol", role="approver", scope="global")
    Path(info["yaml"]).unlink()
    _as_identity("carol", role="approver")
    text = asyncio.run(orch.confirm_orchestration_draft(info["draft_id"]))
    assert isinstance(text, str)
    assert "审批" in text or "未执行" in text, text


def test_guard_decided_by_for_returns_real_decider():
    """查得到批准记录时必须返回真实决议人（非空分支）。"""
    from server import approval

    args = {"draft_id": "drf-123456789abc", "name": "doc-flow"}
    aid = approval.create_approval("confirm_orchestration_draft", args)
    assert approval.resolve(aid, True, "dave", "approver") == "ok"
    assert approval.decided_by_for("confirm_orchestration_draft", args) == "dave"
    # 查不到 → 空串，由调用方回退并在审计标注 decider_source
    assert approval.decided_by_for("confirm_orchestration_draft",
                                   {"draft_id": "drf-000000000000"}) == ""


def test_guard_global_confirm_payload_and_real_decider(monkeypatch):
    """审批回调真正命中决议记录：payload 带 YAML/矩阵，confirmed_by=决议人。"""
    from server import approval

    info = _make_draft(username="carol", role="approver", scope="global")
    captured = {}

    def fake_require(tool, payload, title, cb):
        captured["tool"] = tool
        captured["payload"] = payload
        aid = approval.create_approval(tool, payload)
        assert approval.resolve(aid, True, "dave", "approver") == "ok"
        return cb()

    monkeypatch.setattr(approval, "require_approval", fake_require)
    _as_identity("carol", role="approver")
    text = asyncio.run(orch.confirm_orchestration_draft(info["draft_id"]))
    assert "已确认并启用" in text, text
    # 审批卡内容断言：完整 YAML + 角色工具矩阵（第二名决议人可据此复核）
    payload = captured["payload"]
    assert "doc-flow" in payload["yaml"]
    assert payload["roles"] == len(payload["role_tools"]) == 2
    assert {r["id"] for r in payload["role_tools"]} == {"intake", "reviewer"}
    # 落盘身份取真实决议人；审计标注 record 来源
    meta = _read_meta(info)
    assert meta["status"] == "confirmed"
    assert meta["confirmed_by"] == "dave"
    rows = audit.query(action="确认流程草稿")
    assert "decided_by=dave" in rows[0]["detail"]
    assert "decider_source=record" in rows[0]["detail"]


def test_guard_global_confirm_decider_fallback_is_annotated(monkeypatch):
    """无决议记录时回退发起人，但审计必须标注 fallback，避免静默失真。"""
    from server import approval

    info = _make_draft(username="carol", role="approver", scope="global")
    monkeypatch.setattr(approval, "require_approval",
                        lambda tool, payload, title, cb: cb())
    _as_identity("carol", role="approver")
    text = asyncio.run(orch.confirm_orchestration_draft(info["draft_id"]))
    assert "已确认并启用" in text, text
    rows = audit.query(action="确认流程草稿")
    assert "decided_by=carol" in rows[0]["detail"]
    assert "decider_source=fallback" in rows[0]["detail"]


def test_guard_discard_uses_fresh_meta_after_confirm():
    """确认后到达的丢弃回调（旧 pending 快照）不得把 confirmed 覆盖成 discarded。"""
    info = _make_draft()
    stale = dict(info["meta_data"])
    assert orch._do_confirm(info["scope"], dict(stale))["status"] == 200
    out = orch._do_discard(info["scope"], dict(stale))
    assert out["status"] == 200
    assert out["previous_status"] == "confirmed"
    assert _read_meta(info)["status"] == "confirmed", "confirmed 不得被覆盖"
    assert _target(info).exists(), "已启用定义不受影响"


def test_guard_global_discard_confirmed_returns_assets_message(monkeypatch):
    """N2：已确认的 global 草稿丢弃只清资产，文案不得谎报「已丢弃」。"""
    from server import approval

    info = _make_draft(username="carol", role="approver", scope="global")
    assert orch._do_confirm("global", dict(info["meta_data"]))["status"] == 200
    monkeypatch.setattr(approval, "require_approval",
                        lambda tool, payload, title, cb: cb())
    _as_identity("carol", role="approver")
    text = orch.discard_orchestration_draft(info["draft_id"])
    assert "已清理草稿资产" in text and "已启用定义不受影响" in text, text
    assert _target(info).exists(), "已启用定义不受影响"
    assert _read_meta(info)["status"] == "confirmed"


def test_guard_global_discard_audit_records_real_decider(monkeypatch):
    """global 丢弃审计记真实决议人，而不是发起人。"""
    from server import approval

    info = _make_draft(username="carol", role="approver", scope="global")

    def fake_require(tool, payload, title, cb):
        aid = approval.create_approval(tool, payload)
        assert approval.resolve(aid, True, "dave", "approver") == "ok"
        return cb()

    monkeypatch.setattr(approval, "require_approval", fake_require)
    _as_identity("carol", role="approver")
    text = orch.discard_orchestration_draft(info["draft_id"])
    assert "已丢弃全局流程草稿" in text, text
    rows = audit.query(action="丢弃流程草稿")
    assert "decided_by=dave" in rows[0]["detail"]
    assert "decider_source=record" in rows[0]["detail"]


def test_guard_global_discard_approval_failure_message(monkeypatch):
    """审批通过但删除资产失败时返回失败文案（不谎报成功）。"""
    from server import approval

    info = _make_draft(username="carol", role="approver", scope="global")
    monkeypatch.setattr(approval, "require_approval",
                        lambda a, p, t, cb: cb())

    def boom(self, *args, **kwargs):
        raise OSError("locked")

    monkeypatch.setattr(Path, "unlink", boom)
    _as_identity("carol", role="approver")
    text = orch.discard_orchestration_draft(info["draft_id"])
    assert "丢弃失败" in text, text
