"""知识库访问判定与根目录定位（批 4 收尾：自 server/main.py 迁出，纠正
routes → main 的反向依赖）。

叶子模块：只依赖 packs / custom_tools，不反向引用 main 或 routes。
测试 monkeypatch 锚点即本模块（_kb_home / _declared_kb_packs）。
"""
from pathlib import Path

from . import custom_tools, packs


def _kb_home() -> Path:
    """知识库根目录（kb/<库名>/<文档名>）。独立函数便于测试替身注入。"""
    return Path(__file__).resolve().parents[1] / "kb"


def _declared_kb_packs() -> list[dict]:
    """启用包中声明了 knowledge_bases 的清单（kb 名 → 包归属 + 包级 RBAC）。
    独立函数便于测试替身注入。"""
    return [p for p in packs.enabled_packs() if p.get("knowledge_bases")]


def kb_allowed(kb: str, role: str | None, department: str | None = None,
               packs_list: list[dict] | None = None) -> bool:
    """定位端点权限（fail-closed，PLAN 2.4 多包 OR 合并）：kb 名必须被启用包
    knowledge_bases 声明，且**任一声明包**按「角色 + 部门」双重校验通过即放行
    （跨领域通用库形态：同一库挂多个领域包，各包自治授权，任一授权链成立即可见；
    任一维度未配置 = 该维度放行，与工具层/提示层同口径；role=None 视为开放模式
    放行；admin 全局视图跨部门放行；未分配部门不命中白名单）。堵住「包 RBAC 挡了
    检索工具、定位端点仍可枚举翻库」的旁路（PLAN 2.2 澄清 4）。
    全部声明包拒绝或库未声明 → False（不区分「不存在」与「无权」）。"""
    for p in (packs_list if packs_list is not None else _declared_kb_packs()):
        if kb in (p.get("knowledge_bases") or []):
            if role is None:
                return True  # 开放模式（认证关闭）
            # 角色+部门判定复用 pack_scope_denied（与提示层/执行层同一实现，防口径漂移）；
            # department=None 归一为 "" 保持本函数纯函数语义（不回落上下文，fail-closed）
            if custom_tools.pack_scope_denied(
                    p.get("name", kb), p.get("allowed_roles") or [],
                    p.get("allowed_departments") or [],
                    role=role, department=department or "") is None:
                return True
    return False
