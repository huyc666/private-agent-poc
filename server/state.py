"""应用运行时状态：集中持有进程级可变状态（替代原 agent.py 的 8 个模块级全局）。

收口目标：
- 状态有明确属主，读写都经过 STATE，便于审查、测试与将来替换；
- 为无状态化/多副本扩展铺路：届时把 AppState 的字段逐个外移到持久层
  （会话/审批已规划走 checkpointer + 外部存储），本模块保持不变。

依赖方向：本模块不 import 任何 server 模块，处于依赖图最底层，
tools_admin / mock / agent 均可安全引用。
"""
from dataclasses import dataclass, field


@dataclass
class AppState:
    agent: object = None          # LangGraph 编译后的 Agent 图（Mock 模式为 None）
    llm: object = None            # ChatOpenAI 实例（Mock 模式为 None）
    checkpointer: object = None   # MemorySaver / AsyncPostgresSaver
    checkpointer_cm: object = None  # PostgresSaver 连接上下文管理器（退出时关闭）
    mcp_tool_list: list = field(default_factory=list)   # MCP 工具对象列表
    tool_names: list = field(default_factory=list)      # 当前全部工具名（健康检查展示）
    mcp_tools: dict = field(default_factory=dict)       # MCP 工具名 -> 工具对象
    pack_tools: dict = field(default_factory=dict)      # 领域包工具名 -> 工具对象
    custom_sig: tuple | None = None  # 自定义工具/领域包/沙箱状态签名（热重载检测）


STATE = AppState()
