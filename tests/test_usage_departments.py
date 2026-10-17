"""6.2 部门成本报表：by_department 聚合视图、CSV 导出、ACL 拒绝路径。
用例编号对应 docs/specs/6.2-department-usage-report.md 验收标准 AC1~AC6。

部门口径（用户 2026-10-17 确认）：查询时按 api_users 当前映射聚合，
无部门/未知用户计入「未分配」；报表为管理视角，user 角色 403。
"""
import csv
import io
import sqlite3
import time

import pytest

from server import config, usage


def _record(user, session, run, model="m", pt=1000, ct=1000):
    usage.current_user.set(user)
    usage.current_session.set(session)
    usage.current_run.set(run)
    usage.record(model, {"input_tokens": pt, "output_tokens": ct})


# ---------------- AC1 聚合正确性 + 未分配分组 ----------------

def test_ac1_department_aggregation(monkeypatch):
    monkeypatch.setattr(usage, "_PRICES", {"m": {"input": 2.0, "output": 4.0}})
    _record("alice", "s1", "r1", pt=1_000_000, ct=0)   # 输入金额 2.0
    _record("bob", "s2", "r2", pt=500_000, ct=0)       # 1.0
    _record("carol", "s3", "r3", pt=100_000, ct=0)     # 无部门 → 未分配，0.2
    view = usage.department_usage(
        department_map={"alice": "研发部", "bob": "研发部", "carol": ""})
    rows = {r["department"]: r for r in view["by_department"]}
    assert set(rows) == {"研发部", "未分配"}
    d = rows["研发部"]
    assert d["calls"] == 2
    assert d["prompt_tokens"] == 1_500_000 and d["total_tokens"] == 1_500_000
    assert d["prompt_cost"] == pytest.approx(3.0)
    assert d["completion_cost"] == pytest.approx(0.0)
    assert d["total_cost"] == pytest.approx(3.0)
    u = rows["未分配"]
    assert u["calls"] == 1 and u["total_tokens"] == 100_000
    assert u["total_cost"] == pytest.approx(0.2)


def test_ac1_unknown_user_goes_unassigned():
    """映射外用户（删号遗留/开放模式匿名）不丢数据，计入「未分配」。"""
    _record("ghost", "s1", "r1")
    view = usage.department_usage(department_map={})
    assert len(view["by_department"]) == 1
    assert view["by_department"][0]["department"] == "未分配"
    assert view["by_department"][0]["calls"] == 1


# ---------------- AC2 days 过滤（与 /api/usage 同语义） ----------------

def test_ac2_days_filter():
    _record("alice", "s1", "r1", pt=1000, ct=0)
    conn = sqlite3.connect(str(config.USAGE_DB))
    conn.execute("UPDATE usage_records SET ts = ?", (time.time() - 10 * 86400,))
    conn.commit()
    conn.close()
    assert usage.department_usage(days=1, department_map={"alice": "研发部"})["by_department"] == []
    rows = usage.department_usage(days=30, department_map={"alice": "研发部"})["by_department"]
    assert rows[0]["calls"] == 1


# ---------------- AC3 CSV 导出 ----------------

def test_ac3_csv_content():
    monkeypatch_prices = {"m": {"input": 2.0, "output": 2.0}}
    usage._PRICES.update(monkeypatch_prices)
    try:
        _record("alice", "s1", "r1", pt=1_000_000, ct=0)
        text, filename = usage.department_csv(department_map={"alice": "研发部"})
        assert text.startswith("\ufeff")                      # UTF-8 BOM，Excel 直开不乱码
        assert filename.startswith("usage-departments-") and filename.endswith(".csv")
        rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
        header = rows[0]
        assert header[0] == "部门" and len(header) == 8
        assert "调用次数" in header and "总金额（元）" in header
        data = rows[1]
        assert data[0] == "研发部"
        assert data[1] == "1" and data[4] == "1000000"
        assert float(data[7]) == pytest.approx(2.0)           # 总金额
    finally:
        usage._PRICES.pop("m", None)


def test_ac3_csv_formula_injection_neutralized():
    """拒绝路径：部门名以 = + - @ 开头（含前导空白）时单元格加 ' 前缀，
    Excel/WPS 打开不再当公式执行；普通部门名与数值列不受影响。"""
    _record("eve", "s1", "r1", pt=1000, ct=0)
    _record("fred", "s2", "r2", pt=1000, ct=0)
    text, _ = usage.department_csv(
        department_map={"eve": "=1+1|cmd", "fred": "+2+2"})
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    cells = {r[0]: r for r in rows[1:]}
    assert cells["'=1+1|cmd"][0] == "'=1+1|cmd"
    assert cells["'+2+2"][1] == "1"
    # 数值列原样直通（无 ' 前缀）
    assert cells["'=1+1|cmd"][1] == "1"


def test_ac3_csv_endpoint_headers(client_factory, make_user):
    key = make_user("boss", role="approver")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    r = c.get("/api/usage/departments.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd and ".csv" in cd
    assert r.content.startswith(b"\xef\xbb\xbf")


def test_ac3_json_view_endpoint(client_factory, make_user):
    key = make_user("boss2", role="approver")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    r = c.get("/api/usage/departments?days=7")
    assert r.status_code == 200 and "by_department" in r.json()


# ---------------- AC4 ACL 拒绝路径（user 403 / 开放模式放行） ----------------

def test_ac4_user_role_forbidden(client_factory, make_user):
    key = make_user("dept-user", role="user")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    assert c.get("/api/usage/departments").status_code == 403
    assert c.get("/api/usage/departments.csv").status_code == 403


def test_ac4_admin_allowed(client_factory, make_user):
    key = make_user("dept-admin", role="admin")
    c = client_factory()
    c.headers.update({"Authorization": f"Bearer {key}"})
    assert c.get("/api/usage/departments").status_code == 200
    assert c.get("/api/usage/departments.csv").status_code == 200


def test_ac4_open_mode_allowed(client_factory, monkeypatch):
    """开放模式（未启用认证）：与 /api/usage 同口径，不过滤。"""
    monkeypatch.setattr(config, "AUTH_ENABLED", False)
    c = client_factory()
    assert c.get("/api/usage/departments").status_code == 200


# ---------------- AC5 缺单价模型：金额 0、token 正常（视图层不二次折算） ----------------

def test_ac5_unpriced_model_token_only():
    usage._PRICES.pop("m-free", None)
    _record("dave", "s1", "r1", model="m-free", pt=250_000, ct=0)
    view = usage.department_usage(department_map={"dave": "市场部"})
    row = view["by_department"][0]
    assert row["department"] == "市场部"
    assert row["total_tokens"] == 250_000
    assert row["prompt_cost"] == 0.0 and row["completion_cost"] == 0.0
    assert row["total_cost"] == 0.0


# ---------------- AC6 空库 ----------------

def test_ac6_empty_db():
    view = usage.department_usage(department_map={"alice": "研发部"})
    assert view == {"by_department": []}
    text, filename = usage.department_csv(department_map={})
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    assert len(rows) == 1 and rows[0][0] == "部门"          # 仅表头
