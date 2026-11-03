"""认证端点：账号密码登录 / 注销（自 server/main.py 原样搬迁）。"""
import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server import audit, auth, config
from server.webdeps import LoginRequest, _bearer_token

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/auth/login")
async def login_endpoint(req: LoginRequest, request: Request) -> JSONResponse:
    """账号密码登录（v0.15.0）：签发 pat- 短期令牌。
    成功失败都走同一路径，不区分「用户不存在」与「密码错误」（防枚举）。
    PLAN 6.3：登录成败均落审计（who/when/what + 来源 IP）。"""
    if not auth.enabled():
        return JSONResponse({"error": "当前为开放模式，无需登录"}, status_code=400)
    ip = request.client.host if request.client else ""
    # PBKDF2 200k 迭代是 CPU 密集同步调用（实测 300ms+），挪线程池避免阻塞
    # 事件循环（全项目评审问题4；同 resolve_identity 的 to_thread 先例）
    result = await asyncio.to_thread(auth.login, req.username, req.password)
    if result is None:
        audit.log(req.username.strip()[:64], "", "登录失败", detail=ip)
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
    audit.log(req.username.strip()[:64], "", "登录成功", detail=ip)
    # v0.17.37 P0-2（spec 4.5）：登录态迁 HttpOnly Cookie——JS 不可读
    # （XSS 窃取面消除），SameSite=Lax 挡跨站附带；AUTH_COOKIE_SECURE=true
    # （HTTPS 部署）追加 Secure；响应体 token 字段保留（程序化 curl 兼容）。
    resp = JSONResponse(result)
    resp.set_cookie(
        "pap_token", result["token"], httponly=True, samesite="lax",
        max_age=int(config.AUTH_TOKEN_TTL_HOURS * 3600), path="/",
        secure=config.AUTH_COOKIE_SECURE)
    return resp


@router.post("/api/auth/logout")
async def logout_endpoint(request: Request) -> JSONResponse:
    """注销：使当前 pat- 登录令牌立即失效（pak- Key 请用吊销/轮换）。
    v0.17.37 P0-2：按 Bearer 或 pap_token Cookie 注销（cookie 登录态同样
    可登出），并以 Max-Age=0 清除浏览器侧 Cookie。logout 豁免 CSRF 校验
    （登出本身无破坏性，且 cookie 过期后登出不应被拦）。"""
    token = _bearer_token(request) or request.cookies.get("pap_token", "")
    await asyncio.to_thread(auth.logout, token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("pap_token", path="/")
    return resp
