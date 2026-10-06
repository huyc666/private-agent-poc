"""配置：全部通过环境变量驱动，私有化部署友好。"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---- LLM ----
# 直连 vLLM：LLM_BASE_URL=http://<GPU机器>:8000/v1, LLM_API_KEY=EMPTY
# 走 LiteLLM 网关（生产推荐，限流/审计/fallback）：LLM_BASE_URL=http://127.0.0.1:4000/v1,
#   LLM_API_KEY=<网关 master_key>，MODEL_NAME 用网关里的逻辑模型名（如 qwen-27b）
MOCK_LLM = os.environ.get("MOCK_LLM", "true").lower() in ("1", "true", "yes")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:8000/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "EMPTY")
MODEL_NAME = os.environ.get("MODEL_NAME", "qwen-27b")
TEMPERATURE = float(os.environ.get("TEMPERATURE", "0.7"))
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "2048"))

# ---- MCP ----
MCP_ENABLED = os.environ.get("MCP_ENABLED", "true").lower() in ("1", "true", "yes")
MCP_SERVER_SCRIPT = PROJECT_ROOT / "tools" / "local_tools_server.py"

# ---- 工具沙箱边界：read_text_file 只能读这个目录以内 ----
TOOL_WORKSPACE = Path(os.environ.get("TOOL_WORKSPACE", str(PROJECT_ROOT))).resolve()

# ---- 天气工具（Open-Meteo，免费无需 key；内网可改为内部代理地址）----
WEATHER_GEOCODING_URL = os.environ.get(
    "WEATHER_GEOCODING_URL", "https://geocoding-api.open-meteo.com/v1/search")
WEATHER_API_URL = os.environ.get(
    "WEATHER_API_URL", "https://api.open-meteo.com/v1/forecast")
WEATHER_TIMEOUT = float(os.environ.get("WEATHER_TIMEOUT", "10"))

# ---- 代码执行沙箱 ----
# 未配置时 run_python_code 工具拒绝执行（fail-closed：不允许未隔离的代码执行）
SANDBOX_URL = os.environ.get("SANDBOX_URL", "")
SANDBOX_TIMEOUT = float(os.environ.get("SANDBOX_TIMEOUT", "20"))
# 沙箱共享令牌（v0.17.1 安全加固）：Agent 调用沙箱 /run 与 /run_tool 时携带
# X-Sandbox-Token 头；沙箱侧配置了 SANDBOX_AUTH_TOKEN 后校验该头，不匹配拒绝。
# 留空 = 不携带（兼容未启用鉴权的既有沙箱部署）。生产建议生成随机值：
# python -c "import secrets; print(secrets.token_hex(32))"
SANDBOX_AUTH_TOKEN = os.environ.get("SANDBOX_AUTH_TOKEN", "")

# ---- Skills 目录 ----
SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", str(PROJECT_ROOT / "skills"))).resolve()

# ---- 自定义工具目录（对话式创建，免重启热加载）----
CUSTOM_TOOLS_DIR = Path(os.environ.get("CUSTOM_TOOLS_DIR", str(PROJECT_ROOT / "custom_tools"))).resolve()

# ---- Token 计量（按用户/任务/会话，SQLite 零依赖存储）----
USAGE_DB = os.environ.get("USAGE_DB", str(PROJECT_ROOT / "usage.db"))

# ---- 领域包（Domain Pack）：通用内核 + 可插拔领域能力 ----
# 每个包是 packs/ 下的一个目录（pack.yaml + tools/ + skills/ + prompt.md），
# 禁用某个包后其工具/技能/提示词即从 Agent 消失，可整体替换。
PACKS_DIR = Path(os.environ.get("PACKS_DIR", str(PROJECT_ROOT / "packs"))).resolve()
# 逗号分隔的包名白名单；留空 = 加载全部可用包。运行时 enable/disable 为内存态，重启后以此为准。
ENABLED_PACKS = os.environ.get("ENABLED_PACKS", "")

# ---- 包可信签名（v0.13.0）----
# 配置后进入强制模式：只有带有效 HMAC 签名的包才会加载（未签名/篡改 → 整包不可用）。
# 留空 = 开放模式（POC 默认）：行为同旧版，仅在管理视图展示信任状态。
PACK_SIGNING_KEY = os.environ.get("PACK_SIGNING_KEY", "")

# ---- 会话与审批持久化（P2 无状态化：重启不丢、同机多进程可协作）----
# 对话记录（sessions.py）与审批记录（approval.py）共用一个 SQLite 库；
# WAL + busy_timeout 支持同机多进程共享同一库文件；跨机多副本须替换为 Postgres
# （SQLite 不适合网络共享存储）。
STATE_DB = os.environ.get("STATE_DB", str(PROJECT_ROOT / "state.db"))
# 会话历史读取条数上限（展示/记录侧；模型上下文由 checkpointer 持有）
SESSION_MAX_HISTORY = int(os.environ.get("SESSION_MAX_HISTORY", "20"))

# ---- Checkpointer（会话/审批中断现场持久化）----
# memory：进程内存（重启即丢）；postgres：PostgreSQL 持久化（生产推荐）。
# ⚠️ postgres 模式仅支持 Linux/macOS 部署：psycopg 异步模式与 Windows 的
#    ProactorEventLoop 不兼容（而 MCP stdio 子进程在 Windows 又依赖 Proactor），
#    Windows 开发机请保持 memory 模式，postgres 模式到 Docker 机器上启用。
CHECKPOINTER = os.environ.get("CHECKPOINTER", "memory").lower()
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/agent_poc")

# ---- API 认证（v0.14.0，设计见 AUTH_DESIGN.md）----
# false（默认）= 开放模式，行为与认证引入前一致；true = Bearer API Key 认证
AUTH_ENABLED = os.environ.get("AUTH_ENABLED", "false").lower() in ("1", "true", "yes")
# 认证模式下用户表为空时，以此为用户名创建首个 admin 并打印一次性 Key
AUTH_BOOTSTRAP_ADMIN = os.environ.get("AUTH_BOOTSTRAP_ADMIN", "")
# 账号密码登录（v0.15.0）签发的 pat- 令牌有效期（小时）
AUTH_TOKEN_TTL_HOURS = float(os.environ.get("AUTH_TOKEN_TTL_HOURS", "12"))
# SSO 钩子（v0.16.0）：反向代理身份头模式。配置后（如 X-Forwarded-User），
# 请求带该头即信任其用户名（认证由上游企业网关/IdP 完成），角色仍查本地用户表。
# ⚠️ 仅在 Agent 位于受信反向代理之后、且代理会剥离客户端自报同名头时启用，
#    否则任何人都能伪造身份头直连端口。
AUTH_IDENTITY_HEADER = os.environ.get("AUTH_IDENTITY_HEADER", "")

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "你是部署在企业内网的私有化 Agent 助手。你可以使用提供的工具来回答问题，"
    "涉及计算、时间、文件内容时优先调用工具，不要编造结果。回答使用简体中文。",
)
