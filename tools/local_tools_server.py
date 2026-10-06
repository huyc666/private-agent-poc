"""示例 MCP Server（stdio 传输）：向 Agent 暴露工作区目录与天气工具。

POC 里演示了 MCP 的标准接入方式；业务方新增工具时，
只需在这里（或独立 MCP Server 进程）加函数即可，Agent 端零改动。

对比两种扩展方式：
- 内置工具（server/tools_builtin.py 的 read_text_file）：与 Agent 同进程，延迟最低
- MCP 工具（本文件的 get_city_weather）：独立进程隔离、可单独部署/升级，
  可被任何 MCP 客户端复用；Agent 端通过 stdio 调用，零改动

两个天气工具共用 server/weather.py 的多数据源降级链（Open-Meteo → itboy），
保证内置 / MCP 两条接入路径行为一致。
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import weather  # noqa: E402  server/weather.py，与内置工具共用的天气降级链

mcp = FastMCP("poc-local-tools")

WORKSPACE = Path(__file__).resolve().parent.parent


@mcp.tool
def list_workspace_files(subdir: str = ".") -> str:
    """列出工作区某个子目录下的文件与目录（最多 100 条）。"""
    target = (WORKSPACE / subdir).resolve()
    if not target.is_relative_to(WORKSPACE):
        return "拒绝访问：路径越出工作区边界"
    if not target.is_dir():
        return f"目录不存在: {subdir}"
    entries = []
    for p in sorted(target.iterdir())[:100]:
        entries.append(("[目录] " if p.is_dir() else "[文件] ") + p.name)
    return "\n".join(entries) or "（空目录）"


@mcp.tool
def word_count(path: str) -> str:
    """统计工作区内某个文本文件的行数和字符数。path 相对项目根目录。"""
    target = (WORKSPACE / path).resolve()
    if not target.is_relative_to(WORKSPACE):
        return "拒绝访问：路径越出工作区边界"
    if not target.is_file():
        return f"文件不存在: {path}"
    text = target.read_text(encoding="utf-8", errors="replace")
    return f"行数: {text.count(chr(10)) + 1}, 字符数: {len(text)}"


# ---------------------------------------------------------------- 天气工具（MCP 版）
# 与 server/agent.py 的内置 get_current_weather 功能相同，这里独立部署在 MCP 进程，
# 演示 MCP Server 可以完全自包含、独立部署。查询逻辑复用 server/weather.py 的
# 多数据源降级链（Open-Meteo → itboy），接口地址走环境变量，内网可指向内部代理。


@mcp.tool
def get_city_weather(city: str) -> str:
    """查询指定城市的当前天气（MCP 版本），返回气温、体感温度、湿度、风速和天气状况。city 为城市名，例如 "北京"、"上海"、"Hangzhou"。"""
    try:
        return weather.query_weather(city) + "（MCP 通道）"
    except Exception as e:
        return (f"天气查询失败: {e}。"
                "私有化内网环境请将 WEATHER_GEOCODING_URL / WEATHER_API_URL 配置为内部代理地址")


if __name__ == "__main__":
    mcp.run()
