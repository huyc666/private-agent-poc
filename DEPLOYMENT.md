# POC 部署手册 — 私有化 Agent 开发运行框架（v0.12.0 首版，持续更新至 v0.17.8）

> 生产上机（GPU+Docker 机器）请先看 [PRODUCTION.md](PRODUCTION.md)（生产部署清单：密钥、强制签名、验收用例）；
> 本手册为各组件的详细部署步骤，v0.13.0–v0.17.8 同样适用（新增配置项见 .env.example：
> 认证 AUTH_*、沙箱令牌 SANDBOX_AUTH_TOKEN、受信代理 AUTH_PROXY_ALLOWED_IPS 等）。

> 目标读者：运维 / 部署工程师。按顺序照做即可，每步都有验证命令。
> 全程离线内网可完成（Docker 镜像与 pip 包需提前通过内网镜像仓库/代理准备）。

## 一、总体架构与组件清单

| 组件 | 作用 | 必需性 | 端口 |
|---|---|---|---|
| vLLM 推理服务 | 加载 Qwen 27B，提供 OpenAI 兼容接口 | 必需（真实模式；无 GPU 时可临时用 DeepSeek 云端，见步骤 2 附注） | 8000 |
| Agent 服务 | 对话界面 + Agent 运行时 + 工具系统 + Skills + 自定义工具热加载 | 必需 | 7100 |
| 代码沙箱 | 隔离执行 Python 代码与自定义工具 | 可选（不部署则 `run_python_code` 禁用、自定义工具回退本地执行）；v0.17.4 起需配 `SANDBOX_AUTH_TOKEN`，未配置默认拒绝执行（fail-closed） | 127.0.0.1:9001 |
| PostgreSQL | checkpointer 持久化（会话/审批现场重启不丢） | 可选（默认 memory 模式） | 127.0.0.1:5432 |
| Langfuse 观测栈 | 调用链追踪（6 容器） | 可选 | 3000（可用 `LANGFUSE_WEB_PORT` 改宿主端口）, 9090-9091 |

功能一览（全部已在 DeepSeek deepseek-chat 真实链路实测通过）：

- **Agent 循环**：LangGraph ReAct + SSE 流式输出 + 会话记忆
- **多 Agent 编排**：`research_topic` 工具触发 orchestrator-worker 子图（拆题 → Send 并行调研 → 汇总），worker 只持安全工具子集
- **工具三通道**：内置工具（进程内）/ MCP 工具（stdio 子进程）/ 对话式自定义工具（热加载）
- **Skills 技能系统**：渐进式披露（索引注入系统提示词，按需 load_skill 拉全文）
- **人工审批流**：危险操作三态（批准/拒绝/超时），fail-closed
- **天气多数据源降级链**：Open-Meteo 主源 → itboy 兜底源，均免 key
- **观测**：Langfuse 接入，不可达时自动降级不阻塞主链路

## 二、机器准备

**GPU 机器（跑 vLLM）**：

| 项目 | 要求 |
|---|---|
| GPU | 27B FP16 ≈ 54GB 显存 + KV cache：A100/H100 80G 单卡，或 2×RTX 4090 24G（`--tensor-parallel-size 2`），或用 AWQ/FP8 量化版降到单卡 24G |
| 运行时 | Docker + NVIDIA Container Toolkit |
| 模型权重 | 提前下载到 `./models/` 目录（内网通过模型仓库分发） |

**应用机器（跑 Agent / 沙箱 / Langfuse，可与 GPU 机器同机）**：

| 项目 | 要求 |
|---|---|
| CPU/内存 | 4 核 / 8GB 起步（Langfuse 栈建议 8GB） |
| Python | 3.11+（建虚拟环境） |
| Docker | 部署沙箱 / Langfuse 时需要 |

## 三、部署步骤

### 步骤 0：获取代码与环境

```bash
cd agent-poc
python -m venv .venv
# Windows: .venv\Scripts\pip install -r requirements.txt -i <内网pip源>
# Linux:   .venv/bin/pip install -r requirements.txt -i <内网pip源>
cp .env.example .env
```

验证（Mock 模式，无 GPU）：

```bash
# Windows: .venv\Scripts\python.exe server\main.py
# Linux:   .venv/bin/python server/main.py
curl http://localhost:7100/api/health
# 返回 mode:"mock" 且 tools 列表 ≥15 个（12 内置 + 3 MCP + 自定义工具，
# 出厂自带示例 dice_roll 时为 16）即成功；
# 浏览器打开 http://localhost:7100 可对话（Mock 演示回复）
```

Mock 模式可体验：工具调用链路 / 审批流（「删除文件 test.txt」）/
Skills（「有哪些技能」「创建一个技能」）/ 自定义工具（「创建一个工具」）。

### 步骤 1：部署 vLLM（GPU 机器）

```bash
# 模型权重放入 ./models/Qwen3-27B（或修改 docker-compose.yml 的 MODEL_DIR/MODEL_PATH）
docker compose up -d vllm

# 验证（首次加载模型需几分钟）
curl http://localhost:8000/v1/models
```

关键参数（compose 已预置）：`--enable-auto-tool-choice --tool-call-parser hermes`
（Qwen 系列在 vLLM 下启用 function calling 的必要开关）。

多卡张量并行：在 command 末尾加 `--tensor-parallel-size 2`。

### 步骤 2：Agent 接入真实模型

编辑 `.env`：

```
MOCK_LLM=false
LLM_BASE_URL=http://<GPU机器IP>:8000/v1
MODEL_NAME=qwen-27b
```

重启 Agent 服务，验证：

```bash
curl http://localhost:7100/api/health   # mode 应为 "live"
```

浏览器里问「北京天气怎么样」，应看到模型自主调用天气工具。

### 步骤 2b（可选，生产推荐）：LiteLLM 网关

所有 LLM 请求统一走网关，获得**限流、审计、主备 fallback**，对 Agent 完全透明
（OpenAI 兼容，MODEL_NAME 用逻辑名即可）：

```bash
docker compose -f docker-compose.gateway.yml up -d
curl http://127.0.0.1:4000/health/liveliness   # "I'm alive!"
```

`.env` 改为指向网关并重启 Agent：

```
LLM_BASE_URL=http://127.0.0.1:4000/v1
LLM_API_KEY=sk-poc-gateway     # 网关 master_key（生产必改，与 gateway/litellm_config.yaml 同步）
MODEL_NAME=qwen-27b            # 逻辑模型名：vLLM 宕机时网关自动切 deepseek-chat
```

路由/限流/fallback 规则见 `gateway/litellm_config.yaml`（纯 ASCII 注释——
litellm 按系统默认编码读 YAML，中文注释会在中文版 Windows 上启动失败，已踩坑）。
正式私有化部署请删除配置中的 deepseek 备模型（或改为第二台 vLLM）。

无 Docker 的开发机也可以本机起网关：
`pip install "litellm[proxy]" && litellm --config gateway/litellm_config.yaml --port 4000`
（注意：litellm[proxy] 会把 mcp 包升到 2.x 与 fastmcp 3.x 冲突，
requirements.txt 已锁定 `mcp>=1.10,<2`，如被冲掉请重装该约束）。

> **附注（无 GPU 的临时开发方案）**：任何 OpenAI 兼容接口都能接入。
> DeepSeek 为例：`LLM_BASE_URL=https://api.deepseek.com/v1`、
> `LLM_API_KEY=sk-...`、`MODEL_NAME=deepseek-chat`（或 deepseek-reasoner）。
> ⚠️ 数据会发送到云端，仅用于开发联调，正式私有化部署必须切回 vLLM。

### 步骤 3（可选）：部署代码执行沙箱

```bash
# 先生成沙箱令牌（Agent 与沙箱必须一致；v0.17.4 起未配置令牌 = 拒绝执行）
python3 -c "import secrets; print(secrets.token_hex(32))"
SANDBOX_AUTH_TOKEN=<上面生成的令牌> docker compose -f docker-compose.sandbox.yml up -d
curl http://127.0.0.1:9001/health   # {"status":"ok","tools_dir":"/tools",...}
```

`.env` 配置并重启 Agent：

```
SANDBOX_URL=http://127.0.0.1:9001
SANDBOX_AUTH_TOKEN=<同一个令牌>
```

> v0.17.4 起沙箱鉴权 fail-closed：`/run`、`/run_tool` 校验 `X-Sandbox-Token`，
> 未配置令牌时默认拒绝执行（503）。仅兼容存量未鉴权部署可显式
> `SANDBOX_OPEN_MODE=true` 临时放开（不建议生产使用）。

部署后两个变化：

1. `run_python_code` 工具解锁（未部署时一律拒绝执行，fail-closed）；
2. **自定义工具与领域包工具自动切换为沙箱隔离执行**：`custom_tools/` 与 `packs/`
   以只读卷挂入容器，Agent 主进程只用 ast 静态提取签名（零自定义代码执行），
   实际调用走沙箱 `/run_tool` 端点的隔离子进程（领域包工具带 `pack` 字段，
   从 `/packs/<包名>/tools/` 取文件）。`curl http://localhost:7100/api/health`
   中 `custom_tools_exec` 显示 `sandbox` 即生效。

沙箱容器锁定措施：只读文件系统、去掉全部 capabilities、禁提权、非 root、
CPU 0.5 核 / 内存 256MB / PID 64 限额、仅监听 127.0.0.1。

执行模式由 `CUSTOM_TOOLS_EXEC` 控制（默认 `auto`）：

| 取值 | 行为 |
|---|---|
| `auto` | 沙箱可达走沙箱，宕机自动回退本地执行，恢复后自动切回 |
| `sandbox` | 强制沙箱；不可达时 fail-closed，不加载任何自定义工具 |
| `local` | 强制主进程本地执行（仅建议开发调试用） |

### 步骤 4（可选）：部署 Langfuse 观测栈

```bash
docker compose -f docker-compose.langfuse.yml up -d
# 首次启动 ClickHouse 迁移需 1~2 分钟
curl http://localhost:3000/api/public/health   # {"status":"OK",...}
```

> 3000 端口被占用时改宿主端口：`LANGFUSE_WEB_PORT=3100 docker compose ... up -d`
> （Agent 侧 `.env` 同步设 `LANGFUSE_HOST=http://localhost:3100`；NEXTAUTH_URL 已随该参数联动）。
> 国内网络拉镜像慢时，可把 compose 中镜像名加镜像源前缀（如 `ghcr.m.daocloud.io/...`）。

控制台 http://localhost:3000 ，预置账号 `admin@poc.local` / `admin1234`
（组织/项目/密钥已由 LANGFUSE_INIT_* 自动创建：pk-lf-local / sk-lf-local）。

`.env` 开启观测并重启 Agent：

```
LANGFUSE_ENABLED=true
# Langfuse 在别的机器时改：LANGFUSE_HOST=http://<观测机器IP>:3000
```

验证：页面头部徽章变绿「📊 Langfuse 已连接」；发一条消息后控制台 Traces 出现记录。
**Langfuse 宕机不影响聊天**（自动降级停用，徽章变红）。

### 步骤 5（可选，生产推荐）：PostgreSQL 持久化

会话记忆与审批中断现场默认存在进程内存（MemorySaver，重启即丢）。
换成 PostgreSQL 后服务重启不丢，也是多实例部署的前提：

```bash
docker compose -f docker-compose.postgres.yml up -d
docker exec poc-postgres pg_isready -U postgres   # accepting connections
```

`.env` 配置并重启 Agent：

```
CHECKPOINTER=postgres
DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/agent_poc
```

首次启动自动建表（幂等）。启动日志出现
`checkpointer: PostgresSaver @ 127.0.0.1:5432/agent_poc` 即生效；
连不上会 fail-fast 并给出明确指引。

> ⚠️ postgres 模式仅支持 Linux/macOS 部署：psycopg 异步模式与 Windows 的
> ProactorEventLoop 不兼容（而 MCP stdio 子进程在 Windows 又依赖 Proactor）。
> Windows 开发机请保持 `CHECKPOINTER=memory`。
> POC 默认口令 postgres/postgres 仅用于演示，**生产必改**（同步改 DATABASE_URL）。

### 步骤 6（可选）：天气数据源

默认可用，无需配置。降级链：Open-Meteo（主源，全球城市，免 key）→
itboy 免费天气（兜底，国内区县，免 key）。本机无外网时两个源都不可达，
工具返回明确错误并提示配置代理：

```
# 内网通过代理访问 Open-Meteo：
WEATHER_GEOCODING_URL=http://<内部代理>/v1/search
WEATHER_API_URL=http://<内部代理>/v1/forecast
# 或关闭兜底源：WEATHER_ITBOY_ENABLED=false
```

## 四、日常使用：Skills、自定义工具与领域包

**Skills（制作知识/流程）**：聊天里说「创建一个技能：……」，模型生成 SKILL.md
落盘到 `skills/`；之后任务匹配技能描述时，模型自动 `load_skill` 拉取指令执行。
技能是「给模型看的说明书」，不占上下文（渐进式披露）。

**自定义工具（制作可执行能力）**：聊天里说「创建一个工具 xxx：……」，模型现场
编写 Python 代码 → 审批卡片（含代码预览）→ 批准落盘到 `custom_tools/` →
**免重启热加载**，下一轮对话即可调用。删除工具同样需要审批。

工具文件约定：文件名即工具名，文件内定义同名函数，docstring 即工具描述，
参数带类型注解、返回 `str`，不支持 `*args/**kwargs`。

**领域包（可插拔领域能力，v0.8.0 起）**：`packs/<包名>/` 一个目录打包
工具+技能+提示词（pack.yaml 清单 + tools/ + skills/ + prompt.md）。
包分两类：**平台包**（pack.yaml 标 `platform: true`，v0.12.0 起）承载跨领域
通用基础能力（如 `core-utils` 的时间/计算），始终启用、对话中不可禁用，
先于领域包加载；**领域包**承载业务专业能力，可对话式管理。
聊天里说「禁用/启用 xxx 包」→ 审批 → 免重启即时生效（运行时内存态，
重启后按 `ENABLED_PACKS` 白名单恢复，留空 = 全部加载；平台包不受此影响）。
示例包 `weather-ops` 就是原内置天气能力的整体搬迁——禁用它后天气工具与
weather-report 技能即从 Agent 消失（模型可能改用内核保留的 MCP 演示工具
`get_city_weather`，属预期）。部署新领域有两种方式：①把包目录拷进 `packs/`，
下一轮对话自动挂载；②（v0.9.0 起）直接对 Agent 说「**创建一个领域包 xxx，
带一个 xx 工具**」——模型现场编写 pack.yaml 与工具代码，审批后落盘即挂载；
说「删除 xxx 包」→ 审批后整包删除（不可恢复，模型会先二次确认）。
包声明的 `requires_env`（必须由部署方提供的变量，如密钥）缺失、
`requires_modules`（Python 模块依赖，v0.10.0 起）未安装、
或 `core_version` 与当前内核不匹配时，整包不加载并在
`/api/health` 的 `packs` 字段标注原因——私有化环境**不做运行时自动安装**，
请先把依赖 `pip install` 进 Agent venv（local 模式）或打入沙箱镜像（sandbox 模式）。
包可用 `env` 段声明环境变量默认值（v0.10.0 起）：`.env`/系统环境显式设置的值
优先，否则注入包声明的默认值；local 模式注入进程环境，sandbox 模式随
`/run_tool` 请求透传给隔离子进程（变量名白名单校验、单值限长 2000 字符、
每请求限 50 个）。包内配置项变更（pack.yaml 的 env/requires_* 段）建议重启
Agent 服务确保完全生效。
注意：包工具文件必须**完全自包含**（不得 import server 模块），
否则沙箱隔离执行时会失败。

**Token 计量（v0.11.0 起）**：每次模型调用自动按 用户/任务/会话 三维度
落账到 SQLite（`USAGE_DB`，默认 `usage.db`，零额外组件）。
查询：`curl "http://localhost:7100/api/usage?days=7"` 返回总量 +
按用户/会话/任务分组；前端每条回答下方展示本次消耗与会话累计，
头部 👤 徽标切换用户标识（URL `?user=xxx` 亦可）。
注意：vLLM 与 DeepSeek 均会返回 usage；若某提供方不返回，该次调用跳过不计。
usage.db 是运行数据，备份/迁移时随项目目录一并处理即可。

## 五、安全机制说明（评审要点）

| 机制 | 行为 |
|---|---|
| 人工审批流 | 危险工具（`delete_workspace_file` / `create_custom_tool` / `delete_custom_tool` / `create_pack` / `delete_pack` / `enable_pack` / `disable_pack`）执行前推送审批卡片；批准/拒绝/超时（120s）三态，**超时与异常一律视为拒绝（fail-closed）** |
| 审批 API | `POST /api/approve` `{approval_id, approve}`；审批记录一次性，重放无效 |
| 工作区边界 | 文件类工具禁止访问 `TOOL_WORKSPACE` 以外的路径 |
| 自定义工具静态校验 | 落盘前 ast 校验：语法、同名函数、docstring、20KB 上限、禁止 *args/**kwargs |
| 自定义工具执行隔离 | 沙箱模式主进程零自定义代码执行（ast 提取签名 + `/run_tool` 隔离子进程）；强制 sandbox 模式不可达时 fail-closed |
| 沙箱 fail-closed | 无沙箱服务时 `run_python_code` 一律拒绝执行 |
| 持久化 | `CHECKPOINTER=postgres` 后会话/审批现场落库，重启不丢；连接失败 fail-fast 并给出指引（仅 Linux/macOS） |
| 观测降级 | Langfuse 不可达时自动停用，不阻塞主链路 |

新增危险工具的方法：在工具函数内调用 `langgraph.types.interrupt()`
（参照 `delete_workspace_file` / `create_custom_tool` 实现），审批 UI/协议自动复用。
真实模式下该机制依赖 LangGraph checkpointer（已配置 MemorySaver，
生产换 PostgresSaver），interrupt 中断/恢复链路已通过 DeepSeek 云端真实模型联调验收（v0.3.1）。

## 六、端口速查

| 端口 | 服务 | 说明 |
|---|---|---|
| 7100 | Agent Web/API | 对外 |
| 4000 | LiteLLM 网关 | 仅 127.0.0.1 |
| 8000 | vLLM | 建议仅内网 |
| 3000 | Langfuse UI | 仅内网 |
| 9001 | 代码沙箱（/run + /run_tool） | 仅 127.0.0.1 |
| 5432 | PostgreSQL（checkpointer） | 仅 127.0.0.1 |
| 9090/9091 | MinIO（Langfuse 依赖） | 仅内网 |

## 七、故障排查

| 现象 | 排查 |
|---|---|
| 页面红灯「服务离线」 | Agent 服务未启动：重新打开预览卡片或手动 `python server/main.py` |
| 聊天报「模型调用失败」 | vLLM 未就绪：`curl http://<GPU机器>:8000/v1/models`；检查 `.env` 的 LLM_BASE_URL |
| 模型不调用工具 | vLLM 缺少 `--enable-auto-tool-choice --tool-call-parser hermes` |
| 工具列表少于 15 个 | 有旧进程残留占端口：`netstat -ano \| findstr 7100`，结束后重启（Windows 上 uvicorn 允许端口双绑，旧代码进程会抢答） |
| 审批按钮提示「已失效」 | 审批超时 120 秒，重新发起操作；memory 模式下服务重启也会清空待审批现场 |
| 启动报「PostgreSQL checkpointer 连接失败」 | postgres 未启动：`docker compose -f docker-compose.postgres.yml up -d`；Windows 开发机不支持 postgres 模式，改回 `CHECKPOINTER=memory` |
| 天气查询失败 | 本机无外网：配 WEATHER_API_URL 指向内部代理；兜底源可用 WEATHER_ITBOY_ENABLED 开关 |
| 自定义工具没生效 | `/api/health` 看 `custom_tools_exec`；`sandbox-down` 表示强制沙箱但不可达（fail-closed）；工具文件损坏会跳过并打日志 |
| 领域包没挂载 | `/api/health` 看 `packs` 字段的状态与 error（缺 pack.yaml / requires_env 未满足 / 包名与目录不一致）；ENABLED_PACKS 白名单不含该包也会显示已禁用 |
| Langfuse 徽章红色 | 观测栈未启动或 LANGFUSE_HOST 不对；不影响聊天 |

## 八、目录结构

```
agent-poc/
├── server/            # Agent 服务
│   ├── main.py            # FastAPI 入口：/api/chat (SSE)、/api/approve、/api/health
│   ├── agent.py           # LangGraph Agent 构建、内置工具、MCP 加载、热重载、流式输出
│   ├── multi_agent.py     # 多 Agent 编排（orchestrator-worker 子图，research_topic 触发）
│   ├── packs.py           # 领域包注册中心（扫描/pack.yaml 校验/挂载/启停，v0.8.0）
│   ├── usage.py           # Token 计量（回调采集 + SQLite 聚合，v0.11.0）
│   ├── custom_tools.py    # 自定义工具注册中心（ast 校验/签名提取/沙箱代理/本地加载）
│   ├── skills.py          # Skills 注册中心（渐进式披露，多源合并）
│   ├── weather.py         # 天气多数据源降级链（MCP 示例服务器共用）
│   ├── approval.py        # 审批记录与等待
│   ├── telemetry.py       # Langfuse 观测（降级安全）
│   └── config.py          # 全部配置走环境变量
├── custom_tools/      # 对话式创建的自定义工具（.py 即工具，免重启；含示例 dice_roll）
├── packs/             # 包目录（v0.8.0）：可插拔能力
│   ├── core-utils/        # 平台包（v0.12.0，platform: true 不可禁用）：时间/计算
│   └── weather-ops/       # 领域包示例（pack.yaml + tools/ + skills/ + prompt.md）
├── skills/            # Skills 技能目录（<名称>/SKILL.md，含示例 daily-brief）
├── tools/             # MCP Server 示例（stdio）：目录列举、字数统计、天气
├── sandbox/           # 代码沙箱（Dockerfile + FastAPI 执行器：/run + /run_tool）
├── web/index.html     # 聊天前端（零 CDN，断网可用）
├── docker-compose.yml           # vLLM
├── docker-compose.langfuse.yml  # 观测栈
├── docker-compose.sandbox.yml   # 代码沙箱（含 custom_tools 只读挂载）
├── gateway/           # LiteLLM 网关配置（路由/限流/fallback，纯 ASCII）
├── docker-compose.gateway.yml   # LiteLLM 网关
├── docker-compose.postgres.yml  # PostgreSQL（checkpointer 持久化）
├── .env.example       # 全部配置项（复制为 .env）
└── requirements.txt
```
