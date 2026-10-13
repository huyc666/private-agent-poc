"""示例 MCP Server（stdio 传输）：向 Agent 暴露天气工具。

POC 里演示了 MCP 的标准接入方式；业务方新增工具时，
只需在这里（或独立 MCP Server 进程）加函数即可，Agent 端零改动。

对比两种扩展方式：
- 内置工具（server/tools_builtin.py 的 read_text_file）：与 Agent 同进程，延迟最低
- MCP 工具（本文件的 get_city_weather）：独立进程隔离、可单独部署/升级，
  可被任何 MCP 客户端复用；Agent 端通过 stdio 调用，零改动

两个天气工具共用 server/weather.py 的多数据源降级链（Open-Meteo → itboy），
保证内置 / MCP 两条接入路径行为一致。

注意（全项目评审问题5）：不在此提供工作区文件类工具（列目录/数字数）——
MCP Server 是独立进程，取不到主进程的用户身份上下文（contextvars），
做不了 uploads/outputs 的多用户域隔离；文件能力统一走内置工具
（list_uploaded_docs / read_text_file，有完整的分域与敏感文件拦截）。
"""
import sys
from pathlib import Path

from fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
import weather  # noqa: E402  server/weather.py，与内置工具共用的天气降级链

mcp = FastMCP("poc-local-tools")


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
        # 不回显原始异常文本（P1-3 约定：可能携带内部代理地址等部署细节）
        print(f"[mcp] 天气查询失败: {e}")
        return ("天气查询失败（内部错误）。"
                "私有化内网环境请将 WEATHER_GEOCODING_URL / WEATHER_API_URL 配置为内部代理地址")


if __name__ == "__main__":
    mcp.run()
