"""审批端点：审批记录查询与危险操作决议。自 server/main.py 原样搬迁。"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server import approval, audit
from server.webdeps import ApproveRequest, _acl_identity

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/approvals")
async def approval_list(request: Request, limit: int = 50):
    """最近审批记录（审计）：工具、参数摘要、状态（pending/approved/rejected/
    expired/abandoned，abandoned = 已批准但执行丢失，见 approval.mark_abandoned）。
    user 角色只看自己创建的；approver/admin 看全部。"""
    ident = _acl_identity(request)
    uid = ident["username"] if ident and ident["role"] == "user" else ""
    # limit<=0 回退默认（全项目评审问题3：SQLite LIMIT 负数 = 全量返回，防绕过 200 条上限）
    return {"approvals": approval.list_recent(
        limit=min(limit if limit > 0 else 50, 200), user_id=uid)}


@router.post("/api/approve")
async def approve(req: ApproveRequest, request: Request):
    """前端提交危险操作审批决定。
    决议权限（v0.17.9 收紧）：仅 admin/approver 可决议任何 pending——此前
    「user 可决议自己创建的」存在自审自批漏洞（发起者自己批准即可放行
    危险操作）；user 角色发起的审批等待 approver/admin 决议。
    ok=false 时 error 区分 forbidden（无权限）与 not_found（不存在/已决议）。"""
    u = request.state.user or {"username": "", "role": "admin"}  # 开放模式：任何人可决议（旧行为）
    result = approval.resolve(req.approval_id, req.approve, u["username"], u["role"])
    if result == "ok":
        audit.log(u["username"], u["role"],
                  "审批通过" if req.approve else "审批驳回",
                  target=req.approval_id)
        return {"ok": True}
    if result == "forbidden":
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    return {"ok": False, "error": "not_found_or_decided"}
