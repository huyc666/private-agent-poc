"""SSE 流式聊天端点与会话归属双判（锁外快查 + 锁内终判）。
自 server/main.py 原样搬迁。"""
import asyncio
import json
import logging
import os
import secrets
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from server import agent, auth, session_lock, sessions, usage
from server.webdeps import ChatRequest

logger = logging.getLogger(__name__)

router = APIRouter()


def _chat_session_denied(identity: dict, session_id: str) -> bool:
    """chat 锁外快查（SEC-1 快路径，v0.17.37 升级 meta 口径）：tombstone 或
    既有归属冲突 → 403 快速返回，避免开流后才拒；空会话（新会话首轮）放行
    ——首轮写入前无从校验归属（v0.17.32 语义保留）。返回 True = 拒绝。
    评审 #2 修复：存量会话（meta 无）改用与锁内终判一致的 resolve_owner
    口径（最早 user 消息作者归属，顺带回填 meta）——旧 owners 全体口径会把
    合法属主因他人也曾发言而误拒 403（过严方向，无越权但伤可用性）。
    终判以锁内 _chat_session_verdict 为准（锁外检查存在并发窗口，
    沿 v0.17.33 双查先例）。"""
    meta = sessions.get_meta(session_id)
    if meta is not None:
        if meta["deleted"] is not None:
            return True                        # tombstone：删除即封禁（含 admin）
        if identity["role"] == "admin":
            return False
        return meta["owner_id"] not in (identity["username"], "anonymous")
    if identity["role"] == "admin":
        return False
    owner = sessions.resolve_owner(session_id)
    if owner is None:
        return False            # 空会话（新会话首轮）放行，终判负责原子抢注
    return owner not in (identity["username"], "anonymous")


def _chat_session_verdict(identity: dict, session_id: str) -> bool:
    """chat 锁内终判（v0.17.37 P0-1）：返回 True=放行 / False=拒绝。
    - tombstone → 拒（删除 = 不可逆终局，含 admin——admin 治理语义保留在
      读/删端点，chat 不得复活已删除会话的模型上下文）；
    - admin 放行（不注册不改写归属——接入他人会话不漂移所有权，报告建议 5）；
    - meta 有归属：本人或 anonymous 宽恕放行，否则拒；
    - meta 无：messages 有 → resolve_owner 回填存量归属后按上判；皆无 →
      全新会话，register_session 原子抢注（并发恰一人成功，失败=被抢 → 拒）。"""
    meta = sessions.get_meta(session_id)
    if meta is not None and meta["deleted"] is not None:
        return False
    if identity["role"] == "admin":
        return True
    if meta is not None:
        return meta["owner_id"] in (identity["username"], "anonymous")
    owner = sessions.resolve_owner(session_id)
    if owner is None:
        return sessions.register_session(
            session_id, identity["username"]) == "ok"
    return owner in (identity["username"], "anonymous")


@router.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    # 服务端生成不可预测会话 ID（P0-1，v0.17.37）：token_hex(8)=16 位 hex，
    # 替代 uuid4().hex[:8]（可预测且空间小）；客户端自报 ID 仍兼容（续聊既有会话）
    session_id = req.session_id or secrets.token_hex(8)
    run_id = uuid.uuid4().hex[:12]  # 任务维度：一次请求的完整运行
    # 认证模式：user_id 只信中间件解析的身份（请求体 user_id 字段忽略，兼容旧客户端）；
    # 开放模式：沿用请求体自报（演示用）
    if auth.enabled():
        user_id = request.state.user["username"]
        # SEC-1（v0.17.32）：会话归属校验——防止登录用户拿他人 session_id
        # 向其会话写入并接续 LangGraph thread_id 的模型上下文（跨用户上下文
        # 泄露 + 会话状态污染）。校验先于任何写库/进流完成。
        if _chat_session_denied(request.state.user, session_id):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        # v0.17.39（spec 3.2）：请求级不可变身份快照——中间件解析一次，
        # 执行层经 stream_reply 注入后只读（HTTP 层与执行层同源；SSO 部门
        # 取 IdP 头值，角色恒为本地表）。auth_via 映射 "header" → "sso"。
        u = request.state.user
        identity = usage.Identity(
            username=u["username"], role=u.get("role") or "user",
            department=u.get("department") or "",
            auth_source={"header": "sso"}.get(request.state.auth_via,
                                              request.state.auth_via))
    else:
        user_id = (req.user_id or "anonymous").strip()[:64] or "anonymous"
        # 开放模式无凭据来源，身份固定 admin/无部门（同旧版全局开放契约）；
        # username 与自报 user_id 对齐，避免下游两口径分叉（评审 minor-1）
        identity = usage.Identity(username=user_id, role="admin",
                                  department="", auth_source="open")

    # 对话记录落库在下方锁内进行（顺序与执行顺序一致）；模型上下文由
    # checkpointer 接续，所以只把本轮新消息交给图，历史不再重复传入。
    new_message = [{"role": "user", "content": req.message}]

    collected: list[str] = []
    finished = False

    async def event_stream():
        nonlocal finished
        owner = f"{run_id}@{os.getpid()}"
        # 同会话串行化（v0.16.4 + v0.17.1 加固）：
        # 1) 进程内 asyncio.Lock（agent.session_lock）承担同进程快速互斥；
        # 2) STATE_DB 分布式租约（SessionLease）承担跨进程互斥——
        #    多副本指向同一 STATE_DB 时，同一 session 的并发请求在此排队，
        #    避免 checkpointer 状态写冲突与对话记录交错；不同会话互不干扰。
        async with agent.session_lock(session_id):
            lease = session_lock.SessionLease(session_id, owner)
            try:
                await lease.__aenter__()
            except session_lock.LeaseTimeout as e:
                # 租约等待超时（架构评审 P3）：此前无界等待——同会话另一请求
                # 长时间持有租约（如审批 resume 挂起）时新请求无限挂起且用户
                # 无任何反馈。现在返回可理解的错误事件，用户稍后重试即可。
                yield f"data: {json.dumps({'type': 'error', 'content': str(e)}, ensure_ascii=False)}\n\n"
                return
            try:
                # SEC-1（v0.17.33 复验 R1 → v0.17.37 P0-1 升级）：锁外首查
                # 存在并发窗口——取得锁与租约后、首次写入前终判一次
                # （_chat_session_verdict：tombstone 拒 / meta 归属判 / 存量
                # 回填 / 全新会话原子抢注，并发恰一人成功）；被拒以 SSE error
                # 事件终止（已开流，HTTP 状态码不可再改），错误语不区分原因
                # （防探测）。SQLite 读写挪线程池（同下方 append 先例）。
                if auth.enabled() and not await asyncio.to_thread(
                        _chat_session_verdict, request.state.user, session_id):
                    yield f"data: {json.dumps({'type': 'error', 'content': '会话归属校验失败或会话已删除，已拒绝本次写入'}, ensure_ascii=False)}\n\n"
                    return
                # v0.17.38 P0（spec 3.3）：代际栅栏——开场绑定当前代际（删除时
                # 递增），此后消息写入/审批创建/checkpoint/危险工具 resume 各
                # 提交点校验「活跃且代际未变」，旧代际 fail-closed；开放模式
                # tombstone 同口径（认证模式经上方终判，此处双检无害）。
                gen = await asyncio.to_thread(sessions.fence_value, session_id)
                if gen is None:
                    yield f"data: {json.dumps({'type': 'error', 'content': '会话已删除，本轮写入被拒'}, ensure_ascii=False)}\n\n"
                    return
                # SQLite 写为同步调用，挪到线程池避免阻塞事件循环（v0.16.1）
                if not await asyncio.to_thread(
                        sessions.append_fenced, session_id, gen, user_id,
                        "user", req.message):
                    yield f"data: {json.dumps({'type': 'error', 'content': '会话已删除，本轮写入被拒'}, ensure_ascii=False)}\n\n"
                    return
                discarded = False   # 本轮回复是否因会话删除被拒绝落库
                try:
                    yield f"data: {json.dumps({'type': 'session', 'session_id': session_id, 'run_id': run_id}, ensure_ascii=False)}\n\n"
                    citations: list[dict] = []  # 本轮 run 的出处（多工具合并）
                    async for ev in agent.stream_reply(new_message, session_id=session_id,
                                                       user_id=user_id, run_id=run_id,
                                                       generation=gen, identity=identity):
                        if lease.lost:
                            # 租约被其他进程接管（心跳续租失败）：继续推进
                            # 等于两进程同时写同一会话，互斥已失效——立即中止
                            # 本轮并让已产出部分按「不完整」落库（架构评审 P2）
                            yield f"data: {json.dumps({'type': 'error', 'content': '会话租约已被其他进程接管，本轮回复已中止，请重新发送消息'}, ensure_ascii=False)}\n\n"
                            break
                        if ev["type"] == "token":
                            collected.append(ev["content"])
                        elif ev["type"] == "citations":
                            # 出处卡片数据（v0.17.25）：随 assistant 消息持久化，
                            # 历史回放可重渲染；同轮多次工具调用由前端去重
                            citations.extend(ev.get("citations") or [])
                        yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    else:
                        finished = True
                finally:
                    # 客户端中途断连时生成器被 aclose()：已产出的部分回复也要落库，
                    # 保证对话记录成对（user 消息已先于流写入），并标注不完整供审计识别
                    if collected:
                        text = "".join(collected)
                        if not finished:
                            text += ("\n\n（会话租约被其他进程接管，回复中止）"
                                     if lease.lost else "\n\n（连接中断，回复不完整）")
                        # v0.17.38 P0：assistant 写回经代际栅栏——会话在流中途
                        # 被删除时拒绝落库（删除后存储不增），改以 error 事件告知
                        discarded = not await asyncio.to_thread(
                            sessions.append_fenced, session_id, gen, user_id,
                            "assistant", text,
                            extra=(json.dumps(citations, ensure_ascii=False)
                                   if citations else None))
                if discarded:
                    logger.warning(f"[sessions] 会话 {session_id} 已删除，本轮 assistant 回复被丢弃")
                    yield f"data: {json.dumps({'type': 'error', 'content': '会话已被删除，本轮回复已丢弃'}, ensure_ascii=False)}\n\n"
            finally:
                await lease.__aexit__()  # 停心跳 + 释放租约（与 __aenter__ 配对）

    return StreamingResponse(event_stream(), media_type="text/event-stream")
