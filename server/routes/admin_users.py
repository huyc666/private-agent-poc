"""管理控制台端点（admin 专属）：用户与 Key 管理（PLAN 3.1）+
包管理（PLAN 3.2）。自 server/main.py 原样搬迁。"""
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from server import audit, auth, packs
from server.webdeps import (AdminDepartmentSet, AdminPasswordSet,
                            AdminRoleSet, AdminUserCreate, AdminUserOption,
                            _admin_guard)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---- PLAN 3.1 管理控制台：用户与 Key 管理（admin 专属，CLI 能力收口到页面）----

def _audit(operator: str, action: str, target: str, role: str = "") -> None:
    """管理动作结构化日志（AC10）+ 统一审计落库（PLAN 6.3）。
    绝不打印明文 Key 等敏感值。"""
    logger.info(f"[admin] {operator} {action} {target}")
    audit.log(operator, role, action, target=target)


@router.get("/api/admin/users")
async def admin_list_users(request: Request) -> Any:
    """用户全量清单（AC2）：口径与 CLI list 一致，状态判定（正常/已过期/已吊销）
    由前端按 revoked / expires_at 呈现。"""
    if err := _admin_guard(request):
        return err
    return {"users": auth.list_users()}


@router.post("/api/admin/users")
async def admin_create_user(request: Request, req: AdminUserCreate) -> Any:
    """建用户（AC3）：响应携带一次性明文 Key，服务端不落日志不复存。"""
    if err := _admin_guard(request):
        return err
    key = auth.add_user(req.username, role=req.role, days=req.days,
                        department=req.department)
    if key is None:
        return JSONResponse(
            {"error": "创建失败：用户名已存在、为保留名（shared）、超长（>64 字）或角色非法"},
            status_code=400)
    _audit(request.state.user["username"], "创建用户", req.username.strip(),
           role=request.state.user["role"])
    return {"key": key}


@router.post("/api/admin/users/{username}/rotate")
async def admin_rotate_key(request: Request, username: str,
                           req: AdminUserOption) -> Any:
    """轮换 Key（AC4）：旧 Key 立即失效。已吊销用户拒绝轮换——rotate 会清
    吊销位，须封死「借轮换复活」的口子（吊销不可恢复，spec 澄清 4）。"""
    if err := _admin_guard(request):
        return err
    # 评审问题1：路径参数先归一化（auth 层 set_role 等会 strip），否则带空格
    # 变体（如 %20admin）可绕过「不能操作自己」的自保护比较
    username = (username or "").strip()
    if auth.user_by_name(username) is None:
        return JSONResponse({"error": "用户不存在或已吊销"}, status_code=404)
    key = auth.rotate_user(username, days=req.days)
    if key is None:
        return JSONResponse({"error": "用户不存在"}, status_code=404)
    _audit(request.state.user["username"], "轮换 Key", username,
           role=request.state.user["role"])
    return {"key": key}


@router.post("/api/admin/users/{username}/password")
async def admin_reset_password(request: Request, username: str,
                               req: AdminPasswordSet) -> Any:
    """重置密码（AC5）：长度服务端强校验，明文不落日志。"""
    if err := _admin_guard(request):
        return err
    username = (username or "").strip()
    if auth.user_by_name(username) is None:
        return JSONResponse({"error": "用户不存在或已吊销"}, status_code=404)
    if len(req.password or "") < auth.MIN_PASSWORD_LEN:
        return JSONResponse(
            {"error": f"密码至少 {auth.MIN_PASSWORD_LEN} 位"}, status_code=400)
    if not auth.set_password(username, req.password):
        return JSONResponse({"error": "密码设置失败"}, status_code=400)
    _audit(request.state.user["username"], "重置密码", username,
           role=request.state.user["role"])
    return {"ok": True}


@router.post("/api/admin/users/{username}/role")
async def admin_set_role(request: Request, username: str, req: AdminRoleSet) -> Any:
    """设角色（AC6）：立即生效；admin 不能把自己的角色改为非 admin
    （防最后一个管理员自锁，端点层判定——CLI 保持原行为）。"""
    if err := _admin_guard(request):
        return err
    username = (username or "").strip()
    if req.role not in auth.ROLES:
        return JSONResponse({"error": "非法角色"}, status_code=400)
    operator = request.state.user["username"]
    if operator == username and req.role != "admin":
        return JSONResponse(
            {"error": "不能把自己的角色改为非 admin"}, status_code=400)
    if not auth.set_role(username, req.role):
        return JSONResponse({"error": "用户不存在或已吊销"}, status_code=404)
    _audit(operator, "设角色", f"{username} → {req.role}",
           role=request.state.user["role"])
    return {"ok": True}


@router.post("/api/admin/users/{username}/department")
async def admin_set_department(request: Request, username: str,
                               req: AdminDepartmentSet) -> Any:
    """设部门（AC7）：空串清除；调岗即时生效（报表/知识库分库权限按新部门）。"""
    if err := _admin_guard(request):
        return err
    username = (username or "").strip()
    if not auth.set_department(username, req.department):
        return JSONResponse({"error": "用户不存在或已吊销"}, status_code=404)
    _audit(request.state.user["username"], "设部门",
           f"{username} → {req.department.strip() or '（清空）'}",
           role=request.state.user["role"])
    return {"ok": True}


@router.post("/api/admin/users/{username}/revoke")
async def admin_revoke_user(request: Request, username: str) -> Any:
    """吊销账号（AC8）：pak- Key 与 pat- 令牌立即失效，不可恢复（无解禁）。"""
    if err := _admin_guard(request):
        return err
    username = (username or "").strip()
    operator = request.state.user["username"]
    if operator == username:
        return JSONResponse({"error": "不能吊销自己"}, status_code=400)
    if not auth.revoke_user(username):
        return JSONResponse({"error": "用户不存在或已吊销"}, status_code=404)
    _audit(operator, "吊销账号", username, role=request.state.user["role"])
    return {"ok": True}


# ---- PLAN 3.2 包与审批面板：包管理端点（admin 专属，packs.py 函数收口到页面）----

def _pack_toggle_result(result: tuple[str, str]) -> dict | JSONResponse:
    """启停结构化结果 → HTTP 响应（评审①：按状态分流，不靠文案子串猜测）。"""
    status, msg = result
    if status == "ok":
        return {"ok": True, "message": msg}
    return JSONResponse(
        {"error": msg},
        status_code={"not_found": 404, "refused": 400, "error": 500}[status])


@router.get("/api/admin/packs")
async def admin_list_packs(request: Request) -> Any:
    """包全量清单（AC1）：口径与 packs.list_packs 管理视图一致，含平台包。"""
    if err := _admin_guard(request):
        return err
    return {"packs": packs.list_packs()}


@router.post("/api/admin/packs/{name}/disable")
async def admin_disable_pack(request: Request, name: str) -> Any:
    """停用领域包（AC2/AC3）：立即生效并持久化（STATE_DB disabled_packs 表）。
    平台包拒停（400，前端按钮隐藏只是体验层，服务端是防线）。"""
    if err := _admin_guard(request):
        return err
    result = packs._toggle_pack(name.strip(), enable=False)
    if result[0] == "ok":  # 仅成功动作记审计（AC4）
        _audit(request.state.user["username"], "停用包", name.strip(),
               role=request.state.user["role"])
    return _pack_toggle_result(result)


@router.post("/api/admin/packs/{name}/enable")
async def admin_enable_pack(request: Request, name: str) -> Any:
    """启用领域包（AC2/AC3）：status≠ok 拒启（400，不把故障包引进运行时）。"""
    if err := _admin_guard(request):
        return err
    result = packs._toggle_pack(name.strip(), enable=True)
    if result[0] == "ok":  # 仅成功动作记审计（AC4）
        _audit(request.state.user["username"], "启用包", name.strip(),
               role=request.state.user["role"])
    return _pack_toggle_result(result)
