"""依赖安全下限与 MCP Server 注册回归（AC1–AC3）。

GHSA-7xgj-8392-5q4r（FastMCP 直调跳过 transform 后的组件授权）影响
3.0.0–3.4.9 与 4.0.0–4.1.1；3.x 线首个修复版是 3.4.10。项目锁定 mcp 1.x
（langchain-mcp-adapters 0.3.2 要求 mcp<2），而 fastmcp 4.x 要求 mcp>=2.3，
因此本轮允许集固定为 3.4.10 <= v < 4：4.x 即使已修复（4.1.2+）也不在允许集内。
"""
import asyncio
from importlib.metadata import version
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FASTMCP_SECURITY_FLOOR = "3.4.10"
FASTMCP_UPPER_BOUND = "4"
MCP_STDIO_TIMEOUT = 30   # 秒：stdio 子进程卡住时快速失败，不拖住整套回归


def test_ac1_fastmcp_within_security_window():
    """已安装 fastmcp 必须落在安全窗口 [3.4.10, 4) 内。"""
    installed = Version(version("fastmcp"))
    assert installed >= Version(FASTMCP_SECURITY_FLOOR), (
        f"fastmcp {installed} 低于安全下限 {FASTMCP_SECURITY_FLOOR}"
        f"（GHSA-7xgj-8392-5q4r）"
    )
    assert installed < Version(FASTMCP_UPPER_BOUND), (
        f"fastmcp {installed} 超出允许集 3.4.10 <= v < 4："
        "4.x 要求 mcp>=2.3，与项目 mcp<2 锁定冲突"
    )


def _fastmcp_requirement() -> Requirement:
    for raw in (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        if requirement.name.lower() == "fastmcp":
            return requirement
    raise AssertionError("requirements.txt 未声明 fastmcp 直接依赖")


def test_ac2_requirements_fastmcp_window():
    """requirements.txt 的区间必须同时排除漏洞版本与 4.x。"""
    specifier = _fastmcp_requirement().specifier
    assert not specifier.contains("3.4.9"), (
        "requirements.txt 下限低于安全下限：3.4.9 仍在允许集内（修复版从 3.4.10 起）"
    )
    assert not specifier.contains("4.0.0"), (
        "requirements.txt 上界缺失：4.0.0 仍在允许集内（要求 mcp>=2.3，与 mcp<2 冲突）"
    )
    assert not specifier.contains("4.1.2"), "4.x 即使已修复也不在本轮允许集内"
    assert specifier.contains(FASTMCP_SECURITY_FLOOR), (
        f"requirements.txt 下限未覆盖安全版本 {FASTMCP_SECURITY_FLOOR}"
    )
    assert specifier.contains("3.4.11"), "requirements.txt 应允许 3.4.x 后续补丁版"


def test_ac3_mcp_server_registers_and_calls_weather_tool(monkeypatch):
    """FastMCP 升级后，本地 MCP Server 的工具注册与调用链不变。"""
    import tools.local_tools_server as srv

    names = {tool.name for tool in asyncio.run(srv.mcp.list_tools())}
    assert names == {"get_city_weather"}

    monkeypatch.setattr(srv.weather, "query_weather", lambda city: f"{city}-stub")
    result = asyncio.run(srv.mcp.call_tool("get_city_weather", {"city": "北京"}))
    text = "".join(getattr(block, "text", "") for block in result.content)
    assert "北京-stub（MCP 通道）" in text


def test_ac3_load_mcp_tools_over_stdio():
    """真实 stdio 子进程：适配层能拉起本地 Server 并拿到工具（非空）。

    拒绝 fastmcp 4.x 的核心理由是「适配层 import 失败会让 load_mcp_tools 静默
    返回空列表」；只覆盖失败降级（conftest 默认 MCP_ENABLED=false）证明不了成功
    路径仍然通，所以这里直接走 agent.load_mcp_tools() 的真实子进程链路。
    """
    from server import agent

    async def _load():
        return await asyncio.wait_for(agent.load_mcp_tools(), timeout=MCP_STDIO_TIMEOUT)

    tools = asyncio.run(_load())
    assert tools, "load_mcp_tools 返回空列表：MCP 子进程未拉起或适配层已失效"
    assert "get_city_weather" in {tool.name for tool in tools}
