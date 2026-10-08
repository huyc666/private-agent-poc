# 分步部署执行手册 — GPU + Docker 生产机（v0.17.8）

目标：在一台 Linux GPU 机器上把框架从 zip 包部署到「上线验收通过」。
每步给出**可复制命令**与**验收点**，任一步验收不过即停下排查，不要带错继续。
配套文档：验收清单 [PRODUCTION.md](PRODUCTION.md) · 安全核查 [SECURITY.md](SECURITY.md) · 故障排查 [DEPLOYMENT.md](DEPLOYMENT.md) 第七节。

> 全程假设工作目录为解压后的 `agent-poc/`，以 `~` 表示。

---

## Step 0 · 前提检查（5 分钟）

```bash
nvidia-smi                      # 能看到 GPU，显存 ≥54GB（FP16）或用量化模型
docker --version && docker compose version
ls ./models/Qwen3-27B           # 模型权重已在本机（私有化不走在线拉取）
python3 --version               # ≥ 3.11
```

**验收点**：四项全部有输出；显存不足时准备 AWQ/FP8 量化版模型并改 `MODEL_PATH`。

---

## Step 1 · 解压与依赖

```bash
unzip private-agent-poc-v0.17.8.zip -d agent-poc && cd agent-poc
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt    # 内网环境指向内部 pip 源
```

**验收点**：`.venv/bin/python -c "import langgraph, fastapi, httpx"` 无报错。

---

## Step 2 · 生成四套密钥

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"   # 执行 4 次，得 K1/K2/K3/K4
```

- K1 → `PACK_SIGNING_KEY`（包签名根密钥）
- K2 → 网关 master key（替换 `sk-poc-gateway`）
- K3 → PostgreSQL 密码（替换 `postgres`）
- K4 → `SANDBOX_AUTH_TOKEN`（沙箱调用令牌，Agent 与沙箱容器必须一致；v0.17.4 起未配置则沙箱拒绝执行）

**验收点**：四个值各不相同，已记入企业密码保管库（不要写进聊天/邮件）。

---

## Step 3 · 编写 .env（生产基线）

```bash
cp .env.example .env
cat > .env <<EOF
MOCK_LLM=false
LLM_BASE_URL=http://127.0.0.1:4000/v1
LLM_API_KEY=<K2>
MODEL_NAME=qwen-27b
CHECKPOINTER=postgres
DATABASE_URL=postgresql://postgres:<K3>@127.0.0.1:5432/agent_poc
SANDBOX_URL=http://127.0.0.1:9001
SANDBOX_AUTH_TOKEN=<K4>
CUSTOM_TOOLS_EXEC=sandbox
PACK_SIGNING_KEY=<K1>
AUTH_ENABLED=true
AUTH_BOOTSTRAP_ADMIN=admin
AUTH_TOKEN_TTL_HOURS=12
STATE_DB=./state.db
EOF
chmod 600 .env
```

**验收点**：`.env` 无 `DEEPSEEK_API_KEY`；权限 600；`MOCK_LLM=false`。

---

## Step 4 · 清理开发期残留（网关配置）

编辑 `gateway/litellm_config.yaml`：

1. 删除 `deepseek-chat` 整个 `model_list` 条目；
2. 删除 `router_settings.fallbacks` 一行（或改为第二台 vLLM）；
3. `general_settings.master_key` 改为 `<K2>`。

编辑 `docker-compose.postgres.yml`：`POSTGRES_PASSWORD` 改为 `<K3>`。

**验收点**：`grep -n deepseek gateway/litellm_config.yaml` 无输出；
`grep master_key gateway/litellm_config.yaml` 显示新密钥。

---

## Step 5 · 启动 vLLM（最耗时，先起）

```bash
export MODEL_DIR=./models MODEL_PATH=/models/Qwen3-27B
docker compose up -d vllm
docker logs -f poc-vllm        # 等待 "Application startup complete"（首载 5-15 分钟）
curl http://127.0.0.1:8000/v1/models
```

**验收点**：`/v1/models` 返回里含 `qwen-27b`；`nvidia-smi` 显存占用符合预期。

---

## Step 6 · 启动 PostgreSQL / 沙箱 / 网关

```bash
docker compose -f docker-compose.postgres.yml up -d
docker compose -f docker-compose.sandbox.yml  up -d   # 自动读 .env 的 SANDBOX_AUTH_TOKEN 注入容器
docker compose -f docker-compose.gateway.yml  up -d
```

**验收点**（逐条 200/healthy）：

```bash
docker compose -f docker-compose.postgres.yml ps    # healthy
curl http://127.0.0.1:9001/health                   # 沙箱 200
curl -X POST http://127.0.0.1:9001/run -H 'Content-Type: application/json' \
  -d '{"code":"print(1)"}'                           # 无令牌 → 401/503 拒绝（fail-closed 生效）
curl http://127.0.0.1:4000/health/liveliness        # 网关 200
```

---

## Step 7 · 包签名（强制信任模式）

```bash
.venv/bin/python tools/sign_pack.py --all
```

**验收点**：core-utils、weather-ops 均提示已签名；`packs/*/SIGNATURE` 文件存在。

---

## Step 8 · 启动 Agent 服务

```bash
nohup .venv/bin/python server/main.py --host 0.0.0.0 --port 7100 > agent.log 2>&1 &
sleep 20 && tail -30 agent.log
```

**验收点**：日志同时满足——`checkpointer: PostgresSaver`；模型已连接（无 LLM 告警）；
两个包 `已挂载`；无 fail-closed 告警；**首行打印引导 admin 的一次性 Key（立即抄走保存）**。

---

## Step 9 · 认证初始化

```bash
# 为每个使用者建账号（审批人加 --role approver；限期外包人员加 --days 30）
.venv/bin/python tools/manage_users.py add zhangsan --role user
.venv/bin/python tools/manage_users.py add lisi --role approver
# 浏览器端使用者设置登录密码（交互输入，不回显）
.venv/bin/python tools/manage_users.py passwd zhangsan
.venv/bin/python tools/manage_users.py list
```

**验收点**：`list` 状态/角色/密码列正确；
`curl -X POST http://127.0.0.1:7100/api/chat` 无凭据返回 **401**。

---

## Step 10 · 上线验收（逐项实测）

按 [PRODUCTION.md](PRODUCTION.md) 第五节 11 条用例逐项执行
（真实对话 / 工具链 / 审批流 / 审计 / 会话持久化 / 沙箱强制 / 签名强制 /
未签名拦截 / token 计量 / 网关 fallback / 观测），再按
[SECURITY.md](SECURITY.md) 第七章「部署红线」过一遍。

**验收点**：11 条用例全过 + 红线 6 项全勾 → 上线。

---

## 回滚

```bash
# 停 Agent（容器组件无需动），解压上一版本 zip，恢复旧 .env，重启即可
kill $(pgrep -f "server/main.py")
```

保留旧 zip 与旧 `.env` 是回滚前提；数据库结构变更均带自动迁移且向后兼容。

## 上线后运维速查

- **手工放包**：直接向 `packs/` 拷贝目录**不会热生效**（扫描结果有内存缓存）——重启服务生效，或走对话式创建流程（对话中让 Agent 调 `create_pack`，审批落盘后免重启挂载）；强制签名模式下手工包还需 `tools/sign_pack.py <包名>` 签名
- **备份**：`state.db`、`usage.db`、PostgreSQL 数据卷
- **密钥轮换**：换 `PACK_SIGNING_KEY` 后必须 `tools/sign_pack.py --all` 重签
- **用户轮换**：`manage_users.py rotate <用户名> --days N`，旧 Key 立即失效
- **水平扩展**：前置负载均衡 + 多副本 Agent（状态已外置，见 CONCURRENCY.md 第六节）
