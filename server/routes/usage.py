"""审计流水查询/导出（PLAN 6.3）与 Token 用量/部门报表端点。
自 server/main.py 原样搬迁。"""
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from server import audit, auth, usage
from server.webdeps import _acl_identity, _admin_guard

router = APIRouter()


@router.get("/api/audit")
async def audit_query(request: Request, actor: str = "", action: str = "",
                      since: int = 0, limit: int = 200) -> Any:
    """审计流水查询（PLAN 6.3，AC5）：admin 专属。过滤：操作人/动作/起始时间
    （毫秒）；limit 收敛 [1,1000]（防全表拉取）。拒绝路径复用 _admin_guard。"""
    if err := _admin_guard(request):
        return err
    return {"records": audit.query(actor=actor, action=action,
                                   since_ms=since, limit=limit)}


@router.get("/api/audit/export")
async def audit_export(request: Request, actor: str = "", action: str = "",
                       since: int = 0) -> Any:
    """审计流水导出 CSV（AC5）：admin 专属；含 UTF-8 BOM 保证 Excel 中文不乱码。"""
    import csv
    import io as _io
    if err := _admin_guard(request):
        return err
    rows = audit.query(actor=actor, action=action, since_ms=since,
                       limit=1000)
    buf = _io.StringIO()
    buf.write("\ufeff")  # UTF-8 BOM：Excel 打开 CSV 中文防乱码
    w = csv.writer(buf)
    w.writerow(["时间", "操作人", "角色", "动作", "目标", "详情"])
    for r in rows:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"] / 1000))
        w.writerow([ts, r["actor"], r["role"], r["action"],
                    r["target"], r["detail"]])
    # 直接返回 writer 输出（默认行尾已是 \r\n，且 \ufeff BOM 在首）：不做
    # splitlines 再拼接——str.splitlines 按 \u2028/\x85 等 Unicode 行边界切分，
    # 会把 csv 未引用该类字符的记录裂成多条残行（评审 C2，可被含特殊字符的
    # 用户名污染导出完整性）
    return Response(
        buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 'attachment; filename="audit_log.csv"'})


@router.get("/api/usage")
async def usage_stats(request: Request, user_id: str = "", session_id: str = "", days: int = 0) -> dict:
    """Token 消耗聚合：总量 + 按用户 / 按会话 / 按任务（run）分组。
    days>0 时只统计最近 N 天。user 角色强制只统计本人（查询参数不可越权）。"""
    ident = _acl_identity(request)
    if ident and ident["role"] == "user":
        user_id = ident["username"]
    return usage.summary(user_id=user_id, session_id=session_id, days=days)


def _dept_report_guard(request: Request) -> JSONResponse | None:
    """部门报表 ACL（v0.17.21，PLAN 6.2）：管理视角——user 角色 403；
    approver/admin 与开放模式（_acl_identity 返回 None）放行，同 /api/usage 口径。"""
    ident = _acl_identity(request)
    if ident and ident["role"] == "user":
        return JSONResponse({"error": "forbidden",
                             "message": "部门报表仅限管理角色查看"}, status_code=403)


@router.get("/api/usage/departments")
async def usage_departments(request: Request, days: int = 0) -> Any:
    """部门维度用量报表（含 6.5 金额列）：按 api_users 当前部门映射聚合，
    无部门用户计入「未分配」。days>0 只统计最近 N 天。"""
    denied = _dept_report_guard(request)
    if denied:
        return denied
    return usage.department_usage(days=days, department_map=auth.department_map())


@router.get("/api/usage/departments.csv")
async def usage_departments_csv(request: Request, days: int = 0) -> Any:
    """部门成本报表 CSV 导出（UTF-8 带 BOM，Excel 直开不乱码）。"""
    denied = _dept_report_guard(request)
    if denied:
        return denied
    text, filename = usage.department_csv(days=days, department_map=auth.department_map())
    return Response(content=text.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
