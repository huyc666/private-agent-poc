"""记忆管理 API 路由（PLAN 8.1）。"""
from fastapi import APIRouter, HTTPException, Request

from server import audit, auth, memories

router = APIRouter(prefix="/api/memories", tags=["memories"])


def _current_user(request: Request) -> str:
    """从 request.state 取当前用户（auth_middleware 注入）。"""
    user = request.state.user
    if user is None:
        raise HTTPException(401, "未认证")
    return user["username"]


@router.get("")
async def list_memories(request: Request,
                        enabled: int = None,
                        category: str = None):
    """查询记忆列表：user 只见本人；admin 见全量。"""
    current = _current_user(request)
    role = request.state.user["role"]

    # user 角色强制只查本人；admin 见全量
    if role == "user":
        user_id = current
    else:
        user_id = None  # None 表示全量

    items = memories.list_all(user_id, enabled=enabled, category=category)
    return items


@router.post("", status_code=201)
async def create_memory(request: Request, body: dict):
    """创建记忆（当前用户）。"""
    current = _current_user(request)
    content = body.get("content", "").strip()
    category = body.get("category", "fact")

    if not content:
        raise HTTPException(400, "content 不能为空")
    if category not in ("preference", "fact", "project", "context"):
        raise HTTPException(400, "category 非法")

    memory_id = memories.create(current, content, category)
    audit.log(current, "", "memory_create",
              target=str(memory_id), detail=f"category={category}")
    return {"id": memory_id}


@router.patch("/{memory_id}")
async def update_memory(request: Request, memory_id: int, body: dict):
    """编辑记忆（user 仅能编辑本人；admin 可编辑任意）。"""
    current = _current_user(request)
    role = request.state.user["role"]

    # 归属判定：user 仅能操作本人；admin 反查目标记录归属（管理职责，AC5）
    all_visible = memories.list_all(None if role == "admin" else current)
    target = next((m for m in all_visible if m["id"] == memory_id), None)
    if target is None:
        raise HTTPException(404, "记忆不存在")
    user_id = target["user_id"]

    ok = memories.update(memory_id, user_id, **body)
    if not ok:
        raise HTTPException(404, "记忆不存在")

    changed_fields = ",".join(k for k in body.keys() if k != "content")
    audit.log(current, "", "memory_update",
              target=str(memory_id), detail=f"fields={changed_fields}")
    return {"ok": True}


@router.delete("/{memory_id}", status_code=204)
async def delete_memory(request: Request, memory_id: int):
    """删除记忆（user 仅能删除本人；admin 可删除任意）。"""
    current = _current_user(request)
    role = request.state.user["role"]

    # 归属判定：user 仅能操作本人；admin 反查目标记录归属（管理职责，AC5）
    all_visible = memories.list_all(None if role == "admin" else current)
    target = next((m for m in all_visible if m["id"] == memory_id), None)
    if target is None:
        raise HTTPException(404, "记忆不存在")
    user_id = target["user_id"]

    ok = memories.delete(memory_id, user_id)
    if not ok:
        raise HTTPException(404, "记忆不存在")

    audit.log(current, "", "memory_delete", target=str(memory_id))


@router.post("/merge")
async def merge_memories(request: Request, body: dict):
    """合并记忆（PLAN 8.3）：保留 keep_id，删除 merge_from_ids。"""
    current = _current_user(request)
    role = request.state.user["role"]

    keep_id = body.get("keep_id")
    merge_from_ids = body.get("merge_from_ids", [])

    if not keep_id or not isinstance(merge_from_ids, list) or not merge_from_ids:
        raise HTTPException(400, "keep_id 和 merge_from_ids 必填")

    # 归属判定：所有记忆必须属于当前用户（或 admin 可操作任意）
    all_visible = memories.list_all(None if role == "admin" else current)
    visible_ids = {m["id"] for m in all_visible}

    if keep_id not in visible_ids:
        raise HTTPException(404, "keep_id 记忆不存在或无权限")
    for mid in merge_from_ids:
        if mid not in visible_ids:
            raise HTTPException(404, f"记忆 {mid} 不存在或无权限")

    # 反查目标 user_id（admin 可跨用户合并）
    target = next(m for m in all_visible if m["id"] == keep_id)
    user_id = target["user_id"]

    ok = memories.merge(keep_id, merge_from_ids, user_id)
    if not ok:
        raise HTTPException(500, "合并失败")

    audit.log(current, "", "memory_merge",
              target=str(keep_id),
              detail=f"merged_from={','.join(str(i) for i in merge_from_ids)}")
    return {"ok": True}


@router.get("/similar")
async def similar_memories(request: Request):
    """合并建议（PLAN 8.5）：返回 LLM 裁决为 uncertain 的相似记忆对。
    user 只见本人；admin 见全量。"""
    current = _current_user(request)
    role = request.state.user["role"]
    user_id = current if role == "user" else None
    pairs = memories.find_similar_pairs_llm(user_id)
    if role == "user":
        pairs = [p for p in pairs
                 if p["keep"]["user_id"] == current
                 and p["drop"]["user_id"] == current]
    return pairs
