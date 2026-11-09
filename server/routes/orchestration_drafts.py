"""4.3 流程草稿端点：详情 / 确认 / 丢弃（spec docs/specs/4.3-doc-to-workflow.md §2.4）。

身份快照口径（v0.17.39）：从 request.state.user 一次性构造 usage.Identity
显式下传编排层，端点内不按用户名重查本地表；开放模式无身份 → 以 open/admin
快照下传（与旧行为一致：全局视图，不做资源过滤）。

确认的权限与状态机全部在编排层判定（confirm_draft/discard_draft）：
- 他人草稿 404（不回显存在性）；global 草稿端点直通 403（必须走审批）；
- 哈希复校验、排他创建（open(..., "x")）、三态迁移均在编排层完成。
"""
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server import auth, orchestration, usage

router = APIRouter()


def _identity(request: Request) -> usage.Identity:
    """请求级身份快照：auth_via 由中间件写入，仅用于审计/诊断。"""
    u = request.state.user
    if not auth.enabled() or u is None:
        return usage.Identity(username=(u or {}).get("username", ""),
                              role="admin", department="",
                              auth_source="open")
    return usage.Identity(username=u["username"], role=u["role"],
                          department=u.get("department", ""),
                          auth_source=getattr(request.state, "auth_via", "")
                          or "bearer")


def _respond(result: dict) -> JSONResponse:
    """编排层结果 → HTTP 响应（status 字段映射状态码，其余字段即响应体）。"""
    status = int(result.get("status", 500))
    body = {k: v for k, v in result.items() if k != "status"}
    return JSONResponse(body, status_code=status)


@router.get("/api/orchestration-drafts/{draft_id}")
async def draft_detail(request: Request, draft_id: str) -> Any:
    """草稿详情：owner 本人或 approver/admin 可读；他人/跨域 404。
    返回元数据 + YAML 原文 + 角色/工具矩阵（前端以 textContent 渲染）。"""
    result = orchestration.get_draft_detail(draft_id, _identity(request))
    return _respond(result)


@router.post("/api/orchestration-drafts/{draft_id}/confirm")
async def draft_confirm(request: Request, draft_id: str) -> Any:
    """确认草稿：pending → confirmed 并写入可用定义；global 域必须走审批。
    body 中的 owner/scope 一律忽略（归属只取自服务端身份与元数据）。"""
    result = orchestration.confirm_draft(draft_id, _identity(request))
    return _respond(result)


@router.post("/api/orchestration-drafts/{draft_id}/discard")
async def draft_discard(request: Request, draft_id: str) -> Any:
    """丢弃草稿：pending 置 discarded 并删 YAML；confirmed 只清草稿资产，
    已启用定义不受影响。"""
    result = orchestration.discard_draft(draft_id, _identity(request))
    return _respond(result)
