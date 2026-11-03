"""会话 REST 端点（列表/历史/删除）与归属判定、删除编排。
自 server/main.py 原样搬迁。"""
import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server import agent, approval, session_lock, sessions
from server.webdeps import _acl_identity

logger = logging.getLogger(__name__)

router = APIRouter()


def _can_access_session(identity: dict, session_id: str) -> bool:
    """user/approver 角色只能访问归属自己的会话；admin 保留全局视图（管理职责）。
    v0.17.29（PLAN 3.1）：approver 由全局视图收紧为仅本人（会话内容属用户隐私，
    审批所需信息经审批单流转，无需读取会话原文）。
    v0.17.37（P0-1）：归属判定升级 session_meta 口径（所有权表 + anonymous
    宽恕），不再依赖消息记录推断——admin 接入他人会话不再漂移所有权
    （报告建议 5）；meta 无记录的存量会话经 resolve_owner 回填（最早 user
    消息属主）后判定，与 chat 端点同口径。"""
    if identity["role"] == "admin":
        return True
    meta = sessions.get_meta(session_id)
    if meta is not None:
        return meta["owner_id"] in (identity["username"], "anonymous")
    owner = sessions.resolve_owner(session_id)
    return owner is not None and owner in (identity["username"], "anonymous")


def _session_tombstoned(identity: dict | None, session_id: str) -> bool:
    """tombstone 会话对非 admin 按不存在处理（404，spec 3.2.3）：删除即终局，
    非 admin 不可再读/删（消息已清，404 防探测会话曾存在）；admin 全局视图
    保留治理语义（审计可见「该会话曾被删除」）。开放模式无身份 → 不启用
    该语义（行为同旧版）。"""
    if not identity or identity.get("role") == "admin":
        return False
    meta = sessions.get_meta(session_id)
    return meta is not None and meta["deleted"] is not None


async def _purge_session(session_id: str) -> int:
    """删除编排（v0.17.37 P0-1，spec 3.3）：删除 = 不可逆终局，覆盖全存储面。
    返回删除的消息条数。
    1) tombstone 先封禁入口——此后该 ID 的任何复用（chat/注册）被拒；
    2) 展示记录物理删除（既有行为）；
    3) checkpointer thread 物理删除（MemorySaver / AsyncPostgresSaver 同口径
       原生支持 adelete_thread）；异常记日志不阻断——tombstone 已封禁入口，
       残留数据不可达（报告「checkpoint 无法可靠删除时至少永久封禁」的
       兜底语义）；
    4) 进程内压缩摘要缓存；
    5) 挂起审批 → rejected（decided_by=system，fail-closed，等待方轮询即中止）；
    6) 会话租约行清除（运行中请求心跳续租失败自动中止，lease.lost 已有语义）。
    并发决策（spec 记录）：删除不取会话租约（治理动作即时性，不等待审批
    挂起中的长租约）；与运行中 chat 极端并发下可能残留少量 checkpoint，
    但封禁已生效、残留不可达。"""
    await asyncio.to_thread(sessions.mark_deleted, session_id)
    deleted = await asyncio.to_thread(sessions.delete_session, session_id)
    cp = agent.STATE.checkpointer
    if cp is not None:
        try:
            await cp.adelete_thread(session_id)
        except Exception as e:
            logger.error(f"[sessions] checkpoint 删除失败（tombstone 已封禁，"
                         f"残留不可达）: {e}")
    agent._compact_cache.pop(session_id, None)
    await asyncio.to_thread(approval.reject_pending_for_session, session_id)
    await asyncio.to_thread(session_lock.purge, session_id)
    return deleted


@router.get("/api/sessions")
async def session_list(request: Request, limit: int = 50):
    """会话列表（对话记录来自 SQLite，重启后仍在）。
    user/approver 只列自己的会话（v0.17.29 收紧）；admin 列全部。"""
    ident = _acl_identity(request)
    uid = ident["username"] if ident and ident["role"] in ("user", "approver") else ""
    # limit<=0 回退默认（全项目评审问题3：SQLite LIMIT 负数 = 全量返回，防绕过 200 条上限）
    return {"sessions": sessions.list_sessions(
        limit=min(limit if limit > 0 else 50, 200), user_id=uid)}


@router.get("/api/sessions/{session_id}/history")
async def session_history(request: Request, session_id: str, limit: int = 0):
    """一个会话的最近 N 条消息（默认 SESSION_MAX_HISTORY）。user 角色限本人
    会话；tombstone 会话（非 admin）按不存在处理（404，v0.17.37 P0-1）。"""
    ident = _acl_identity(request)
    if ident:
        if _session_tombstoned(ident, session_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _can_access_session(ident, session_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
    return {"session_id": session_id, "messages": sessions.history(session_id, limit)}


@router.delete("/api/sessions/{session_id}")
async def session_delete(request: Request, session_id: str):
    """删除会话（隐私/治理，v0.17.37 P0-1 语义升级：删除 = 不可逆终局）。
    覆盖全存储面（_purge_session）：展示记录 + checkpointer 模型上下文 +
    压缩摘要缓存 + 挂起审批（fail-closed）+ 会话租约，并写 tombstone
    永久封禁该 ID——删除后任何人（含原属主）不得以该 ID 接续旧上下文。
    user/approver 限本人会话；admin 全局视图。"""
    ident = _acl_identity(request)
    if ident:
        if _session_tombstoned(ident, session_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        if not _can_access_session(ident, session_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
    return {"deleted": await _purge_session(session_id)}
