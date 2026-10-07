# 生产部署清单 — 私有化 Agent 框架（v0.17.5）

面向 GPU+Docker 机器的正式上机清单。基础部署细节见 [DEPLOYMENT.md](DEPLOYMENT.md)，
本清单聚焦**生产化差异项**与**逐项验收**。逐项打勾，全部通过即上线。

## 〇、前提确认

- [ ] GPU 机器：显存满足 27B 模型（FP16 需 54GB+，如 A100/H100 80G 单卡或 2×24G 双卡；
      不足则用 AWQ/FP8 量化版），已装 NVIDIA 驱动 + nvidia-container-toolkit
- [ ] Docker + docker compose 可用
- [ ] 模型权重已下载到机器本地（如 `./models/Qwen3-27B`，私有化不走 HuggingFace 在线拉取）
- [ ] Python 3.11+（建 venv 用）；内网 pip 源可用
- [ ] 已拿到 `private-agent-poc-v0.17.5.zip` 并解压

## 一、密钥生成（生产必做，逐项替换 POC 默认值）

```bash
python -c "import secrets; print(secrets.token_hex(32))"   # 生成 4 个不同的密钥
```

- [ ] `PACK_SIGNING_KEY`（包签名密钥，强制信任模式的根）
- [ ] 网关 master key（替换 `sk-poc-gateway`）：改 `gateway/litellm_config.yaml` 的
      `master_key` + `.env` 的 `LLM_API_KEY`（两处一致）
- [ ] PostgreSQL 密码（替换 `postgres`）：改 `docker-compose.postgres.yml` 的
      `POSTGRES_PASSWORD` + `.env` 的 `DATABASE_URL`（两处一致）
- [ ] `SANDBOX_AUTH_TOKEN`（沙箱调用令牌，v0.17.4 起 fail-closed）：`.env` 与
      `docker-compose.sandbox.yml` 读取同一个值（compose 用 `${SANDBOX_AUTH_TOKEN}`
      自动从 `.env` 注入）；未配置时沙箱 `/run`、`/run_tool` 一律拒绝（503）

## 一·五、API 认证（v0.14.0，多用户必做）

- [ ] `.env` 设置 `AUTH_ENABLED=true`、`AUTH_BOOTSTRAP_ADMIN=<管理员用户名>`
- [ ] 启动 Agent 后从日志抄下引导 admin 的一次性 Key（pak- 前缀，只打印一次）
- [ ] 为每个用户签发 Key：`python tools/manage_users.py add <用户名> --role user`
      （有权批准危险操作的人用 `--role approver`；离职/轮换用 `revoke`；
      定期轮换用 `rotate <用户名> --days N`，限期 Key 用 `add --days N`）
- [ ] 浏览器端使用者建议改走账号密码登录：`python tools/manage_users.py passwd <用户名>`
      设置密码后，前端登录框输入账号密码即可（签发 12 小时 pat- 令牌，
      `AUTH_TOKEN_TTL_HOURS` 可调）；Key 留给脚本/服务调用
- [ ] 验收：无 Key 访问 `/api/chat` 返回 401；持 user Key 决议他人审批返回 403；
      错误密码登录返回 401 且不区分「用户不存在/密码错误」；同一账号连错 5 次
      锁定 30 秒（v0.17.2 登录限流）
- [ ] （可选，接企业 SSO）Agent 置于受信反向代理之后，`.env` 配置
      `AUTH_IDENTITY_HEADER=X-Forwarded-User`（或网关实际注入的身份头），并配置
      `AUTH_PROXY_ALLOWED_IPS=<代理IP/CIDR，逗号分隔>`（v0.17.4 起身份头只信任白名单
      来源，未配白名单身份头不生效——fail-closed）；
      本地仍需用 `manage_users.py add` 建同名用户以分配角色（认证在外、授权在内）

## 二、移除开发期残留

- [ ] `gateway/litellm_config.yaml` 中**删除 deepseek-chat fallback 整段**及
      `router_settings.fallbacks`（生产只用内网 vLLM；或换成第二个自建模型做 fallback）
- [ ] `.env` 中不配置 `DEEPSEEK_API_KEY`
- [ ] 确认 `.env` 的 `MOCK_LLM=false`

## 三、启动（按序）

- [ ] **vLLM**：`docker compose up -d vllm`
  验收：`curl http://127.0.0.1:8000/v1/models` 返回 `qwen-27b`
- [ ] **PostgreSQL**：`docker compose -f docker-compose.postgres.yml up -d`
  验收：healthcheck 通过（`docker compose -f docker-compose.postgres.yml ps` 显示 healthy）
- [ ] **沙箱**：`.env` 已配 `SANDBOX_AUTH_TOKEN` 后 `docker compose -f docker-compose.sandbox.yml up -d`
  验收：`curl http://127.0.0.1:9001/health` 返回 200；不带令牌调 `/run` 返回 401（fail-closed 生效）
- [ ] **网关**：`docker compose -f docker-compose.gateway.yml up -d`
  验收：`curl http://127.0.0.1:4000/health/liveliness` 返回 200
- [ ] **包签名**：`.env` 写入 `PACK_SIGNING_KEY` 后执行
  `python tools/sign_pack.py --all`（core-utils / weather-ops 用本机密钥重签）
- [ ] **Agent**：建 venv 装依赖后 `python server/main.py --host 0.0.0.0 --port 7100`
  验收：启动日志含 `checkpointer: PostgresSaver`、`已连接模型 qwen-27b`、
  两个包 `已挂载`，无 fail-closed 告警

## 四、.env 生产基线（对照检查）

```ini
MOCK_LLM=false
LLM_BASE_URL=http://127.0.0.1:4000/v1
LLM_API_KEY=<网关 master key>
MODEL_NAME=qwen-27b
CHECKPOINTER=postgres
DATABASE_URL=postgresql://postgres:<密码>@127.0.0.1:5432/agent_poc
SANDBOX_URL=http://127.0.0.1:9001
SANDBOX_AUTH_TOKEN=<沙箱令牌>       # v0.17.4 起必填，未配置沙箱拒绝执行
CUSTOM_TOOLS_EXEC=sandbox        # 强制沙箱执行自定义代码（不可达则 fail-closed）
PACK_SIGNING_KEY=<包签名密钥>     # 强制信任模式
STATE_DB=./state.db              # 会话/审批持久化；同机多进程可共享，跨机须换 Postgres
ENABLED_PACKS=                   # 留空=全部；生产可显式白名单
```

## 五、验收用例（逐项实测）

| # | 用例 | 操作 | 预期 |
|---|---|---|---|
| 1 | 真实模型对话 | 页面问「现在几点」 | 模型调用 `get_current_time`（平台包工具）并作答 |
| 2 | 工具调用链 | 问「成都天气怎么样」 | 调用 `get_current_weather`，返回真实数据 |
| 3 | 审批流 | 说「删除文件 test.txt」 | 弹审批卡片；拒绝/超时不执行，批准才执行 |
| 4 | 审批审计 | 打开「☰ 记录 → 审批记录」 | 上一条审批状态可见（approved/rejected/expired） |
| 5 | 会话持久化 | 对话后重启 Agent 服务，刷新页面 | 历史消息恢复（分割线标注），模型上下文接续 |
| 6 | 沙箱强制 | 让 Agent「运行 python 代码」 | 经沙箱执行；停掉沙箱容器后再试 → 明确拒绝（fail-closed） |
| 7 | 包签名强制 | 改动 `packs/weather-ops/tools/get_current_weather.py` 任意一字节 | `/api/health` 中 weather-ops 变 `unavailable`（签名校验失败），恢复需审计后 `tools/sign_pack.py weather-ops` 重签 |
| 8 | 未签名拦截 | 手工放一个未签名包进 `packs/` | 整包 `unavailable`，不加载 |
| 9 | token 计量 | 多轮对话后 `GET /api/usage` | 按用户/会话/任务三维度有记录且合计一致 |
| 10 | 网关 fallback（如保留） | 停 vLLM 容器后对话 | 自动切备用模型；Agent 无感 |
| 11 | 观测（可选） | 启动 `docker-compose.langfuse.yml` 并配置密钥后对话 | Langfuse 控制台可见完整 trace |

## 六、上线后运维要点

- **备份**：`state.db`（会话/审批/审计）、`usage.db`（token 计量）、PostgreSQL 数据卷
- **密钥轮换**：更换 `PACK_SIGNING_KEY` 后必须 `tools/sign_pack.py --all` 重签全部包
- **新增包流程**：代码审计 → 放入 `packs/` → `tools/sign_pack.py <包名>` → 对话中启用（挂审批）
- **多副本扩展**（用户量增长时）：会话/审批/计量均已外置；checkpointer 已是 Postgres；
  把 `STATE_DB`/`USAGE_DB` 换 Postgres（SQLite 的 WAL+busy_timeout 只支持同机多进程，
  跨机共享存储不可用），前置负载均衡即可水平扩展
- **故障排查**：见 DEPLOYMENT.md 第七节

## 七、回滚

保留上一版本 zip（v0.12.0）。回滚 = 停服务 → 解压旧包 → 恢复旧 `.env` →
`docker compose` 各组件无需变更（postgres/langfuse/sandbox 均兼容）。
