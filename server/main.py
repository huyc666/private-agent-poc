"""FastAPI 入口：静态页面 + SSE 流式聊天接口 + 健康检查 + 会话/审批记录查询。

启动：python server/main.py [--host 0.0.0.0] [--port 7100]

无状态化说明（P2，v0.13.0 起）：
- 对话记录存 SQLite（sessions.py / STATE_DB），重启不丢，不再是进程内存 dict；
- 审批记录同样落库（approval.py），跨进程可决议；
- 模型上下文由 LangGraph checkpointer 持有（memory/postgres），
  因此真实模式下每次请求只把「新用户消息」交给图——历史由 checkpointer 接续，
  避免了旧实现「全量历史 + checkpointer 追加」导致的上下文重复。
"""
import asyncio
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from server import (agent, approval, auth, config, custom_tools, packs,
                    session_lock, sessions, telemetry, usage)


def _bearer_token(request: Request) -> str:
    h = request.headers.get("Authorization", "")
    return h[7:].strip() if h.startswith("Bearer ") else ""


APPROVAL_SCAN_INTERVAL = 300.0  # 周期扫描间隔（秒）：已批准但未执行的审批 → abandoned


async def _approval_scan_loop():
    """后台周期任务：把「已批准但从未执行」的审批标记为 abandoned。
    等待协程可能在进程存活时死亡（如客户端断开被取消），仅靠启动扫描
    捕捉不到，需周期兜底（v0.17.2）。单条 UPDATE，开销可忽略。
    v0.17.3：循环体异常容错——单次扫描失败只记日志继续，后台任务不会
    静默死亡（否则兜底永久失效且无人知晓）。
    架构评审 P1 修复：宽限期——刚批准正在 resume 执行的记录处于「approved 且未
    标记 executed」的正常中间态，扫描跳过决议时间距今不足 grace 的记录，
    避免误标正在执行的长任务（grace 覆盖审批等待上限 + 扫描间隔）。"""
    grace = approval.APPROVAL_TIMEOUT + APPROVAL_SCAN_INTERVAL
    while True:
        await asyncio.sleep(APPROVAL_SCAN_INTERVAL)
        try:
            n = await asyncio.to_thread(approval.mark_abandoned, grace)
            if n:
                print(f"[approval] 周期扫描：{n} 条「已批准但执行丢失」标记为 abandoned")
        except Exception as e:
            # 单次失败不终止任务：DB 短暂锁竞争/瞬时错误恢复后下一轮自动重试
            print(f"[approval] 周期扫描失败（下轮重试）: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动清理带宽限期：同机多进程共享 STATE_DB 时，其他进程可能有在途
    # resume（approved 尚未 mark_executed），刚决议的记录不判为丢失
    cleaned = approval.cleanup_stale(
        grace=approval.APPROVAL_TIMEOUT + APPROVAL_SCAN_INTERVAL)
    if cleaned:
        print(f"[approval] 启动清理：{cleaned} 条残留审批已处理"
              "（pending→expired，approved 未执行→abandoned）")
    auth.bootstrap_admin()  # 认证模式首启：创建引导 admin 并打印一次性 Key
    # v0.17.4（安全审计 F2）：SSO 身份头必须配合受信代理白名单才生效；
    # 配置了身份头却没配白名单 = 身份头永远不生效（fail-closed），启动时明确告警
    if auth.enabled() and config.AUTH_IDENTITY_HEADER and not config.AUTH_PROXY_ALLOWED_IPS:
        print("[auth] WARNING: 已配置 AUTH_IDENTITY_HEADER 但未配置"
              " AUTH_PROXY_ALLOWED_IPS——身份头将被忽略（fail-closed，防伪造）。"
              "请在 .env 配置受信代理来源 IP 白名单后重启，SSO 身份头才会生效。")
    await agent.build_agent()
    scan_task = asyncio.create_task(_approval_scan_loop())
    yield
    scan_task.cancel()  # 周期扫描随服务退出结束
    telemetry.flush()  # 关闭前冲刷 Langfuse 缓冲的观测数据
    await agent.close_checkpointer()  # 关闭 PostgresSaver 连接（若启用）


app = FastAPI(title="Private Agent POC", lifespan=lifespan)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """认证中间件：开放模式放行（user=None，各端点按旧行为处理）；
    认证模式下 /api/*（除 /api/health 与 /api/auth/login）必须持有效身份——
    反向代理身份头（SSO 钩子）或 Bearer 凭据（pak- Key / pat- 令牌），
    身份解析后写入 request.state.user，成为 user_id 的唯一可信来源。"""
    request.state.user = None
    if not auth.enabled():
        return await call_next(request)
    token = _bearer_token(request)
    # SSO 钩子：身份解析唯一入口（身份头模式优先，其次 Bearer 凭据）。
    # v0.17.4（安全审计 F2）：身份头必须来自受信代理白名单才生效——
    # 未命中白名单/未配置白名单一律忽略（fail-closed），直连端口伪造头无效。
    header_user = ""
    if config.AUTH_IDENTITY_HEADER:
        if auth.proxy_ip_allowed(request.client.host if request.client else None):
            header_user = request.headers.get(config.AUTH_IDENTITY_HEADER, "")
        elif request.headers.get(config.AUTH_IDENTITY_HEADER):
            print("[auth] 身份头来自非受信来源（不在 AUTH_PROXY_ALLOWED_IPS 白名单），"
                  "已忽略（防伪造冒充）")
    # 解析涉及 SQLite 同步调用，挪到线程池避免阻塞事件循环（v0.16.1）
    request.state.user = await asyncio.to_thread(
        auth.resolve_identity, token, header_user)
    path = request.url.path
    if path.startswith("/api/") and path not in ("/api/health", "/api/auth/login") \
            and request.state.user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def login_endpoint(req: LoginRequest):
    """账号密码登录（v0.15.0）：签发 pat- 短期令牌。
    成功失败都走同一路径，不区分「用户不存在」与「密码错误」（防枚举）。"""
    if not auth.enabled():
        return JSONResponse({"error": "当前为开放模式，无需登录"}, status_code=400)
    result = auth.login(req.username, req.password)
    if result is None:
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
    return result


@app.post("/api/auth/logout")
async def logout_endpoint(request: Request):
    """注销：使当前 pat- 登录令牌立即失效（pak- Key 请用吊销/轮换）。"""
    token = _bearer_token(request)
    auth.logout(token)
    return {"ok": True}


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    user_id: str = "anonymous"   # 用户标识（token 计量维度；API 认证落地后由网关/中间件注入）


@app.get("/")
async def index():
    return FileResponse(config.PROJECT_ROOT / "web" / "index.html")


@app.get("/api/health")
async def health(request: Request):
    # 认证模式未携带有效 Key：只回最小信息（不泄露模型/工具/包等内部状态）
    if auth.enabled() and request.state.user is None:
        return {"status": "ok", "auth_enabled": True}
    pack_list, pending = await asyncio.gather(
        asyncio.to_thread(packs.list_packs),
        asyncio.to_thread(approval.pending_count),
    )
    info = {
        "status": "ok",
        "auth_enabled": auth.enabled(),
        "mode": "mock" if config.MOCK_LLM else "live",
        "model": config.MODEL_NAME,
        "llm_base_url": config.LLM_BASE_URL,
        "tools": agent.STATE.tool_names,
        "mcp_enabled": config.MCP_ENABLED,
        "custom_tools_exec": custom_tools.exec_mode(),
        "packs": pack_list,
        "langfuse_enabled": telemetry.enabled(),
        "langfuse_connected": telemetry.get_client() is not None,
        "pending_approvals": pending,
    }
    if request.state.user is not None:
        info["identity"] = request.state.user  # 前端徽章显示服务端确认的身份
    return info


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    session_id = req.session_id or uuid.uuid4().hex[:8]
    run_id = uuid.uuid4().hex[:12]  # 任务维度：一次请求的完整运行
    # 认证模式：user_id 只信中间件解析的身份（请求体 user_id 字段忽略，兼容旧客户端）；
    # 开放模式：沿用请求体自报（演示用）
    if auth.enabled():
        user_id = request.state.user["username"]
    else:
        user_id = (req.user_id or "anonymous").strip()[:64] or "anonymous"

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
                # SQLite 写为同步调用，挪到线程池避免阻塞事件循环（v0.16.1）
                await asyncio.to_thread(sessions.append, session_id, user_id,
                                        "user", req.message)
                try:
                    yield f"data: {json.dumps({'type': 'session', 'session_id': session_id, 'run_id': run_id}, ensure_ascii=False)}\n\n"
                    async for ev in agent.stream_reply(new_message, session_id=session_id,
                                                       user_id=user_id, run_id=run_id):
                        if lease.lost:
                            # 租约被其他进程接管（心跳续租失败）：继续推进
                            # 等于两进程同时写同一会话，互斥已失效——立即中止
                            # 本轮并让已产出部分按「不完整」落库（架构评审 P2）
                            yield f"data: {json.dumps({'type': 'error', 'content': '会话租约已被其他进程接管，本轮回复已中止，请重新发送消息'}, ensure_ascii=False)}\n\n"
                            break
                        if ev["type"] == "token":
                            collected.append(ev["content"])
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
                        await asyncio.to_thread(
                            sessions.append, session_id, user_id, "assistant", text)
            finally:
                await lease.__aexit__()  # 停心跳 + 释放租约（与 __aenter__ 配对）

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _acl_identity(request: Request) -> dict | None:
    """资源级 ACL（v0.14.1）：返回当前身份 {username, role}；
    开放模式返回 None（不过滤，行为同旧版）。
    使用规则：role == 'user' 只能访问自己的资源；approver/admin 全局视图。"""
    if not auth.enabled():
        return None
    return request.state.user


def _can_access_session(identity: dict, session_id: str) -> bool:
    """user 角色只能访问全部消息都属于自己（或开放模式遗留 anonymous）的会话。"""
    if identity["role"] in ("admin", "approver"):
        return True
    owners = sessions.session_users(session_id)
    return bool(owners) and owners <= {identity["username"], "anonymous"}


@app.get("/api/sessions")
async def session_list(request: Request, limit: int = 50):
    """会话列表（对话记录来自 SQLite，重启后仍在）。
    user 角色只列自己的会话；approver/admin 列全部。"""
    ident = _acl_identity(request)
    uid = ident["username"] if ident and ident["role"] == "user" else ""
    return {"sessions": sessions.list_sessions(limit=min(limit, 200), user_id=uid)}


@app.get("/api/sessions/{session_id}/history")
async def session_history(request: Request, session_id: str, limit: int = 0):
    """一个会话的最近 N 条消息（默认 SESSION_MAX_HISTORY）。user 角色限本人会话。"""
    ident = _acl_identity(request)
    if ident and not _can_access_session(ident, session_id):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return {"session_id": session_id, "messages": sessions.history(session_id, limit)}


@app.delete("/api/sessions/{session_id}")
async def session_delete(request: Request, session_id: str):
    """删除一个会话的对话记录（隐私/治理）。user 角色限本人会话。"""
    ident = _acl_identity(request)
    if ident and not _can_access_session(ident, session_id):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return {"deleted": sessions.delete_session(session_id)}


@app.get("/api/approvals")
async def approval_list(request: Request, limit: int = 50):
    """最近审批记录（审计）：工具、参数摘要、状态（pending/approved/rejected/
    expired/abandoned，abandoned = 已批准但执行丢失，见 approval.mark_abandoned）。
    user 角色只看自己创建的；approver/admin 看全部。"""
    ident = _acl_identity(request)
    uid = ident["username"] if ident and ident["role"] == "user" else ""
    return {"approvals": approval.list_recent(limit=min(limit, 200), user_id=uid)}


@app.get("/api/usage")
async def usage_stats(request: Request, user_id: str = "", session_id: str = "", days: int = 0):
    """Token 消耗聚合：总量 + 按用户 / 按会话 / 按任务（run）分组。
    days>0 时只统计最近 N 天。user 角色强制只统计本人（查询参数不可越权）。"""
    ident = _acl_identity(request)
    if ident and ident["role"] == "user":
        user_id = ident["username"]
    return usage.summary(user_id=user_id, session_id=session_id, days=days)


class ApproveRequest(BaseModel):
    approval_id: str
    approve: bool


@app.post("/api/approve")
async def approve(req: ApproveRequest, request: Request):
    """前端提交危险操作审批决定。
    决议权限：admin/approver 可决议任何 pending；普通 user 只能决议自己创建的。
    ok=false 时 error 区分 forbidden（无权限）与 not_found（不存在/已决议）。"""
    u = request.state.user or {"username": "", "role": "admin"}  # 开放模式：任何人可决议（旧行为）
    result = approval.resolve(req.approval_id, req.approve, u["username"], u["role"])
    if result == "ok":
        return {"ok": True}
    if result == "forbidden":
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    return {"ok": False, "error": "not_found_or_decided"}


def _parse_host_port():
    host, port = "0.0.0.0", 7100
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
        elif a.startswith("--host="):
            host = a.split("=", 1)[1]
        elif a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        elif a.startswith("--port="):
            port = int(a.split("=", 1)[1])
    if os.environ.get("PORT"):
        port = int(os.environ["PORT"])
    return host, port


if __name__ == "__main__":
    import uvicorn

    host, port = _parse_host_port()
    print(f"Private Agent POC 启动于 http://localhost:{port}  (模式: {'mock' if config.MOCK_LLM else 'live'})")
    uvicorn.run(app, host=host, port=port)
