"""跨路由域共享的端点辅助与请求模型（叶子模块）。

编码规范批 4 路由拆分：server.main 与 server.routes.* 共同使用的辅助函数/
Pydantic 请求模型集中于此，避免 routes ↔ main 循环导入（本模块不得 import
server.main 或 server.routes）。

收纳规则：只放被 ≥2 个模块使用的项；单域 helper 随域走（_audit →
routes/admin_users.py，_safe_rel/_docx_to_text 等 → routes/files.py，
知识库权限判定 → server/kb_access.py）。
"""
from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from server import auth


def _bearer_token(request: Request) -> str:
    h = request.headers.get("Authorization", "")
    return h[7:].strip() if h.startswith("Bearer ") else ""


def _admin_guard(request: Request) -> JSONResponse | None:
    """管理端点统一守卫（七端点复用；与包级 RBAC 同构的双层拦截——前端隐藏
    入口只是体验层，服务端判定才是防线）。返回 JSONResponse 表示拦截。"""
    if not auth.enabled():
        return JSONResponse(
            {"error": "开放模式无用户体系，管理端点不可用"}, status_code=400)
    if request.state.user is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.state.user.get("role") != "admin":
        return JSONResponse({"error": "需要管理员角色"}, status_code=403)
    return None


def _acl_identity(request: Request) -> dict | None:
    """资源级 ACL（v0.14.1）：返回当前身份 {username, role}；
    开放模式返回 None（不过滤，行为同旧版）。
    使用规则：role == 'user'/'approver' 只能访问自己的资源（v0.17.29 会话
    隐私收紧，approver 由全局视图改为仅本人）；admin 全局视图。"""
    if not auth.enabled():
        return None
    return request.state.user


class LoginRequest(BaseModel):
    username: str
    password: str


class AdminUserCreate(BaseModel):
    username: str
    role: str = "user"
    # 上界 10 年：防止超大值写出永不触发的 expires_at（评审问题3；CLI 暂未加）
    days: int = Field(default=0, ge=0, le=3650)
    department: str = ""


class AdminUserOption(BaseModel):
    days: int = Field(default=0, ge=0, le=3650)


class AdminPasswordSet(BaseModel):
    password: str


class AdminRoleSet(BaseModel):
    role: str


class AdminDepartmentSet(BaseModel):
    department: str = ""


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None
    user_id: str = "anonymous"   # 用户标识（token 计量维度；API 认证落地后由网关/中间件注入）


class KbSanitizeRequest(BaseModel):
    html: str


class ApproveRequest(BaseModel):
    approval_id: str
    approve: bool
