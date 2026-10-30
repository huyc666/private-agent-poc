"""配置：全部通过环境变量驱动，私有化部署友好。"""
import hashlib
import os
import re
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
# 文档上传目录（v0.17.7）：落在工作区内 → 模型用 read_text_file 直接可读，
# 无需新开读取通道；read_text_file 的敏感文件拦截/路径越界校验同样覆盖
UPLOADS_DIR = TOOL_WORKSPACE / "uploads"
# 模型输出文件目录（v0.17.7）：save_output_file 工具落盘处，GET /api/files 下载
OUTPUTS_DIR = TOOL_WORKSPACE / "outputs"


def scope_clean(username: str) -> str:
    """身份 → 文件域目录名（uploads/outputs 的用户子目录，多用户隔离 v0.17.7）。
    非法字符归一为下划线、48 字限长、空值回退 shared。端点侧与工具侧
    （tools_builtin 经 usage.current_user 取身份）共用同一规则，保证寻址一致。"""
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]", "_", (username or "").strip())
    return s.strip("_")[:48] or "shared"


def user_scope(username: str) -> str:
    """身份 → 唯一文件域目录名（SEC-2，v0.17.32）：scope_clean 基名 + 原始
    用户名 SHA-256 前 8 位后缀，彻底消除清洗碰撞（a@b 与 a_b 清洗后同名
    不再共享 uploads/outputs/专属技能/专属工具目录）。回退 shared 不带
    后缀（开放模式遗留共享域兼容）。存量老域名目录由 main._migrate_scope_dirs
    启动时 rename 迁移；哈希输入为 strip 后的原始用户名（与 add_user 落库口径
    一致），基名由 scope_clean 清洗（对含哈希后缀的目录名再清洗仍幂等）。"""
    base = scope_clean(username)
    if base == "shared":
        return "shared"
    digest = hashlib.sha256(
        (username or "").strip().encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def scope_segment(name: str) -> str:
    """URL 域段清洗（评审修复 C2，v0.17.32）：与 scope_clean 同字符集清洗与
    shared 回退，但长度上限放宽到 57——user_scope 域名 = 48 字基名 + '-' +
    8 位 hex，最长 57 字符；此前 URL 寻址侧沿用 scope_clean 的 48 字截断，
    基名 ≥40 字用户的哈希后缀被截掉，approver/admin 全局视图按 URL 寻址
    其域即 404。仅用于 URL 中已生成域名的二次校验；用户名→域名仍走
    user_scope。"""
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]", "_", (name or "").strip())
    return s.strip("_")[:57] or "shared"

# 单文件大小上限（MB），防超大文件灌爆内存/磁盘
UPLOAD_MAX_MB = int(os.getenv("UPLOAD_MAX_MB", "10"))

# ---- 上下文压缩（v0.17.7：滑动窗口 + 滚动摘要，只改模型视野不动历史）----
# 历史估算 token 超过「模型窗口 × 比例」时触发：窗口外旧消息滚动摘要注入模型视野；
# checkpointer 原始历史完整保留（界面恢复/审计不受影响）。按 token 而非轮数触发。
CONTEXT_COMPACT_ENABLED = os.getenv("CONTEXT_COMPACT_ENABLED", "true").lower() in ("1", "true", "yes")
CONTEXT_MAX_TOKENS = int(os.getenv("CONTEXT_MAX_TOKENS", "128000"))        # 模型上下文窗口
CONTEXT_COMPACT_RATIO = float(os.getenv("CONTEXT_COMPACT_RATIO", "0.6"))   # 触发比例
CONTEXT_RECENT_TOKENS = int(os.getenv("CONTEXT_RECENT_TOKENS", "8000"))    # 永不裁的近期窗口
CONTEXT_SUMMARY_HYSTERESIS_TOKENS = int(os.getenv("CONTEXT_SUMMARY_HYSTERESIS_TOKENS", "4000"))  # 重摘滞回

# 文本类文件白名单后缀（上传与模型输出共用）：可执行二进制一律不在名单内
FILE_TEXT_EXTS = {".md", ".markdown", ".txt", ".csv", ".tsv", ".json",
                  ".yaml", ".yml", ".xml", ".html", ".htm", ".py", ".js", ".ts",
                  ".java", ".go", ".rs", ".sql", ".sh", ".bat",
                  ".ini", ".cfg", ".conf", ".toml", ".log", ".rst"}

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

# ---- 自定义工具执行模式（SEC-3，v0.17.32）----
# 显式指定 auto / local / sandbox；未显式配置时按运行模式联动默认：
# Mock 模式（开发）= auto（沙箱优先，不可达回退主进程，方便离线开发）；
# 真实 LLM 模式 = sandbox（沙箱不可达 fail-closed，生产不得静默把工具代码
# 放进主进程执行）。auto 回退 local 会打显著 WARNING（60s 限频）。
CUSTOM_TOOLS_EXEC = os.environ.get("CUSTOM_TOOLS_EXEC", "").strip().lower()

# ---- Skills 目录 ----
SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", str(PROJECT_ROOT / "skills"))).resolve()
# 用户专属技能目录（v0.17.9）：user 角色创建的技能落到 skills_personal/<域>/，
# 仅本人会话可见可用；approver/admin 创建的进通用 skills/（全员共享）。
# 专属技能索引按请求身份动态注入（不参与图重建签名）
PERSONAL_SKILLS_DIR = Path(os.environ.get(
    "PERSONAL_SKILLS_DIR", str(PROJECT_ROOT / "skills_personal"))).resolve()

# ---- 自定义工具目录（对话式创建，免重启热加载）----
CUSTOM_TOOLS_DIR = Path(os.environ.get("CUSTOM_TOOLS_DIR", str(PROJECT_ROOT / "custom_tools"))).resolve()
# 用户专属自定义工具目录（v0.17.12）：user 角色创建的工具落到
# custom_tools_personal/<域>/，仅本人会话可见可用（与技能层同机制：
# 清单按请求身份过滤 + 执行时归属校验，不做按用户缓存图）；
# approver/admin 创建的进通用 custom_tools/（全员共享，仍走审批门）。
PERSONAL_CUSTOM_TOOLS_DIR = Path(os.environ.get(
    "PERSONAL_CUSTOM_TOOLS_DIR", str(PROJECT_ROOT / "custom_tools_personal"))).resolve()

# ---- 运行时数据目录（v0.17.19：SQLite 运行库归位 data/，根目录只放工程文件）----
_DATA_DIR = PROJECT_ROOT / "data"
try:
    _DATA_DIR.mkdir(exist_ok=True)
except OSError as e:
    print(f"[config] 运行时数据目录创建失败: {type(e).__name__}")


def _migrate_legacy_db(name: str) -> None:
    """把根目录遗留的 <name>（及 -wal/-shm 伴生文件）迁入 data/。
    仅默认路径（env 未显式配置）时调用；幂等：源不存在直接跳过，data/ 已有
    同名库则不覆盖（视为重启残留的旧副本，以 data/ 现役库为准）；跨进程并发
    启动竞态吞异常，失败重试 3 次后放弃并 WARNING（不阻断启动，需人工迁移）。"""
    import time

    for suffix in ("", "-wal", "-shm"):
        legacy = PROJECT_ROOT / (name + suffix)
        if not legacy.is_file():
            continue
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            print(f"[config] WARNING: data/ 目录创建失败: {type(e).__name__}")
            return
        target = _DATA_DIR / (name + suffix)
        if target.exists():
            print(f"[config] WARNING: data/{name + suffix} 已存在，根目录遗留"
                  f"{name + suffix} 未迁移（保留 data/ 现役库，请人工核对）")
            continue
        moved = False
        for attempt in range(3):
            try:
                legacy.replace(target)
                moved = True
                break
            except OSError:
                time.sleep(0.2 * (attempt + 1))
        if moved:
            print(f"[config] 已迁移运行时库: {name + suffix} → data/")
        else:
            print(f"[config] WARNING: 根目录 {name + suffix} 迁移失败（3 次重试），"
                  "请手动移动到 data/ 后重启")


# ---- Token 计量（按用户/任务/会话，SQLite 零依赖存储）----
USAGE_DB = os.environ.get("USAGE_DB", str(_DATA_DIR / "usage.db"))
# 模型单价（元/百万 tokens）：JSON {"模型名": {"input": 输入单价, "output": 输出单价}}。
# 落账时按当时单价折算金额并固化；缺单价模型仅记 token 不折算；自建 vLLM 可配 0。
MODEL_PRICES = os.environ.get("MODEL_PRICES", "")

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
STATE_DB = os.environ.get("STATE_DB", str(_DATA_DIR / "state.db"))
if "USAGE_DB" not in os.environ:
    _migrate_legacy_db("usage.db")
if "STATE_DB" not in os.environ:
    _migrate_legacy_db("state.db")
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

# ---- API 认证与多用户（v0.14.0，设计见 AUTH_DESIGN.md）----
# 默认值联动运行模式（v0.17.6）：Mock 模式（MOCK_LLM=true）= 开发者模式，
# 默认免登录直接进入；真实 LLM 模式默认要求登录（生产链路必须认证）。
# 显式设置 AUTH_ENABLED 可覆盖默认（如真实模式本地调试想免登录：
# AUTH_ENABLED=false）。认证开启后所有 /api/* 须持有效凭据（pak-/pat-）。
AUTH_ENABLED = os.environ.get(
    "AUTH_ENABLED", "false" if MOCK_LLM else "true").lower() in ("1", "true", "yes")
# 认证模式下用户表为空时，以此为用户名创建首个 admin 并打印一次性 Key
AUTH_BOOTSTRAP_ADMIN = os.environ.get("AUTH_BOOTSTRAP_ADMIN", "")
# 账号密码登录（v0.15.0）签发的 pat- 令牌有效期（小时）
AUTH_TOKEN_TTL_HOURS = float(os.environ.get("AUTH_TOKEN_TTL_HOURS", "12"))
# SSO 钩子（v0.16.0）：反向代理身份头模式。配置后（如 X-Forwarded-User），
# 请求带该头即信任其用户名（认证由上游企业网关/IdP 完成），角色仍查本地用户表。
# ⚠️ v0.17.4（安全审计 F2）：身份头必须配合 AUTH_PROXY_ALLOWED_IPS 受信代理
#    白名单一起使用——仅来源 IP 在白名单内的请求才信任身份头，否则忽略并
#    fail-closed（任何人直连 Agent 端口都无法伪造身份头冒充他人）。
#    代理需负责剥离客户端自报的同名头。
AUTH_IDENTITY_HEADER = os.environ.get("AUTH_IDENTITY_HEADER", "")
# 受信反向代理来源 IP 白名单（逗号分隔，支持单个 IP 或 CIDR，如
# "127.0.0.1,10.0.0.0/8"）。留空 = 不信任任何身份头（fail-closed 默认）。
# 仅配置了 AUTH_IDENTITY_HEADER 且来源命中此白名单时，SSO 身份头才生效。
AUTH_PROXY_ALLOWED_IPS = os.environ.get("AUTH_PROXY_ALLOWED_IPS", "")
# SSO 部门头（v0.17.11，PLAN 6.1）：可选。配置后受信代理可经该头透传用户部门
# （如 X-Forwarded-Department），头值优先于本地用户表（IdP 是部门信息的权威源）；
# 未配置 / 头缺失时使用本地表 department 字段。同样受 AUTH_PROXY_ALLOWED_IPS 保护。
AUTH_IDENTITY_DEPARTMENT_HEADER = os.environ.get("AUTH_IDENTITY_DEPARTMENT_HEADER", "")

SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "你是部署在企业内网的私有化 Agent 助手。你可以使用提供的工具来回答问题，"
    "涉及计算、时间、文件内容时优先调用工具，不要编造结果。回答使用简体中文。",
)

# 文档上传指引（v0.17.7）：与 SYSTEM_PROMPT 分开定义、由 agent 装配时无条件
# 追加——用户自定义 SYSTEM_PROMPT 环境变量也不会丢失这段能力说明
UPLOAD_PROMPT = (
    "\n\n## 文档上传\n"
    "用户可通过前端 📎 按钮上传文档（存放在 uploads/ 下、按用户域分目录）：\n"
    "- 先用 list_uploaded_docs 查看已上传文档清单，再用 read_text_file 按"
    "清单给出的逻辑路径（uploads/<域>/<文件名>）读取内容进行分析；\n"
    "- 分析/总结严格基于文档实际内容，不要编造；\n"
    "- 用户要求时，可基于文档内容创建自定义工具、技能或领域包"
    "（工具代码中的路径、字段、参数名以文档实际内容为准）。\n"
    "- 用户也可上传技能包 zip（📦，含 SKILL.md + references/ 等多文件）："
    "服务端会直接安全解包挂载到 skills/<技能名>/ 目录并立即可发现；"
    "导入成功后你应读取 skills/<技能名>/ 下的文件了解该技能内容。\n"
    "- 需要给用户交付文件（如按格式整理后的文稿）时，用 save_output_file 保存"
    "（Word 文档传 as_docx=true 并以 .docx 命名，会自动套用公文版式），"
    "并在最终回复中附上工具返回的下载链接（/api/files/<域>/<文件名>）。\n"
    "- 与用户交流时只使用逻辑路径（skills/…、packs/…、custom_tools/…、"
    "custom_tools_personal/…、uploads/…、outputs/…），绝不提及服务器文件系统的绝对路径"
    "（如盘符/用户目录/项目部署位置）——部署布局属于内部信息。"
)

# 知识库问答约束（v0.17.25，PLAN 2.2）：多库协作规则 + 防编造约束。
# 由 agent 装配进系统提示词（system_prompt_static），与 UPLOAD_PROMPT 同模式。
KB_QA_PROMPT = (
    "\n\n## 知识库问答规范\n"
    "- 存在多个知识库检索工具时，按问题涉及的领域分别调用各库的检索工具，"
    "综合各库结果作答，并把各库出处合并列出；某个库未检索到时也要如实说明"
    "（如「HR 知识库中未找到相关规定」），不要因为一个库有结果就跳过其他库。\n"
    "- 回答中的制度条文必须来自检索工具返回的内容，出处必须使用检索结果给出的"
    "文档名与章节；检索不到时如实告知用户，不得编造或虚构条文、出处。\n"
    "- 工具返回中可能附带 <kb-citations> 标签包裹的结构化数据，那是给系统"
    "处理用的：不要在回复中复述该标签或其中的 JSON，出处展示由系统自动完成，"
    "你只需正常作答。\n"
    "- 与用户交流时引用知识库条文使用【来源：文档名 · 章节】行提及，"
    "不要自行构造链接或路径。"
)
