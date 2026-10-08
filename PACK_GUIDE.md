# 领域包开发指南 · Private Agent POC

版本：v0.17.6 · 2026-10-08
读者：要把具体业务接入框架的开发者/业务方。读完可按 7 步流程交付一个可上线的领域包。
参照实现：`packs/weather-ops`（领域包完整样例）、`packs/core-utils`（平台包样例）。

## 一、领域包是什么

一个领域包 = 一个自包含目录，装着一个业务领域所需的全部能力：

```
packs/hr-leave/                  ← 包目录（名字即包名，小写字母/数字/连字符）
├── pack.yaml                    ← 清单：声明这个包有什么、要什么（必填）
├── prompt.md                    ← 领域提示词：教 Agent 在这个领域怎么干活（可选）
├── tools/                       ← 工具：Agent 可调用的函数，一个文件一个工具（可选）
│   └── query_leave_balance.py
└── skills/                      ← 技能：沉淀的工作流程，一个目录一个技能（可选）
    └── leave-application/
        └── SKILL.md
```

**设计理念**：框架内核是通用的、稳定的；行业知识全部装在包里。接新业务 = 做一个新包，不动框架。包可以随时启用/禁用（对话里说一声即可），整个包可以整体替换。

## 二、三种创建方式

| 方式 | 适用 | 做法 |
|---|---|---|
| **对话式创建** | 快速原型、演示 | 对 Agent 说「创建一个领域包」，触发审批后自动落盘并签名 |
| **手工开发**（推荐正式业务） | 正式接入 | 按本指南写目录 → 审计 → 签名 → 启用 |
| **克隆样例** | 学习 | 复制 `packs/weather-ops` 改名改造 |

## 三、七步开发流程

### Step 1 · 明确能力边界

先回答三个问题，写进包描述：
- 这个包让 Agent 多会什么？（如：查询员工请假余额）
- 需要什么外部资源？（数据库地址、内部 API、密钥——都要走环境变量）
- 有什么绝对不能做的？（写进提示词的边界声明）

### Step 2 · 搭骨架

```bash
mkdir -p packs/hr-leave/tools packs/hr-leave/skills/leave-application
```

### Step 3 · 写工具（tools/*.py）

硬性规范（违反会加载失败或运行报错）：

1. **完全自包含**：禁止 `import server.*`——沙箱隔离子进程里没有 server 包
2. **签名即接口**：函数名即工具名；类型注解生成参数 schema；docstring 是给模型看的说明书

```python
import os
import urllib.request
import json

_HR_API = os.environ.get("HR_API_BASE", "http://hr.internal.example/api")
_TIMEOUT = float(os.environ.get("HR_API_TIMEOUT", "10"))


def query_leave_balance(employee_id: str) -> str:
    """查询员工的年假余额。employee_id 为工号，例如 "E10086"。
    返回剩余天数、已用天数和冻结天数。"""
    try:
        req = urllib.request.Request(
            f"{_HR_API}/leave/{employee_id}",
            headers={"Authorization": f"Bearer {os.environ.get('HR_API_TOKEN', '')}"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        return (f"工号 {employee_id}：年假剩余 {d['remain']} 天，"
                f"已用 {d['used']} 天，冻结 {d['frozen']} 天")
    except Exception as e:
        return f"查询失败: {e}（请确认 HR_API_BASE / HR_API_TOKEN 配置）"
```

要点：
- **返回 str**：错误也返回说明性文案，不要抛异常（异常会变成模型看不懂的堆栈）
- **密钥绝不写死**：一律 `os.environ.get`，默认值只放非敏感的地址类配置
- **网络调用必带超时**

### Step 4 · 写技能（skills/<名字>/SKILL.md）

技能 = 沉淀的工作流程（工具是手脚，技能是经验）。frontmatter 必填：

```markdown
---
name: leave-application
description: 帮员工走请假申请的标准流程（查余额 → 核对规则 → 生成申请单）
---

# 请假申请流程

1. 先用 `query_leave_balance` 查员工余额，余额不足直接告知，不要继续。
2. 核对请假规则：……
3. 按以下格式生成申请单：……
```

### Step 5 · 写领域提示词（prompt.md）

告诉 Agent 在这个领域的行为准则，会被拼进系统提示词：

```markdown
## 领域能力：人事请假（hr-leave 包）

- 涉及请假余额、请假规则的查询，必须使用工具，不要凭记忆编造数字。
- 员工没提供工号时，先问工号再查询。
- 只能查询，不能代替员工提交申请；提交动作一律提示员工去 OA 完成。
```

### Step 6 · 写清单（pack.yaml）

```yaml
name: hr-leave                 # 必须与目录名一致
version: 1.0.0
description: 人事请假领域包：余额查询工具、请假流程技能与领域提示词
core_version: ">=0.8.0"        # 要求的内核最低版本

requires_env:                  # 缺失则整包不加载（fail-closed）
  - HR_API_TOKEN
requires_modules: []           # pip 依赖声明；私有化环境不自动装，缺失整包不加载

env:                           # 包级默认值（外部显式设置的同名变量优先）
                               # 跨包声明同名键会触发加载 WARNING（v0.17.2）：
                               # 进程 env 只有一份，先到先得，点名两包与生效方
  HR_API_TIMEOUT: "10"

permissions:                   # 权限声明：显示在启用审批卡片上，让审批人知情
  - "访问内部 HR API（只读）"

tools:
  - tools/query_leave_balance.py
skills:
  - skills/leave-application
prompt: prompt.md
```

字段速查：

| 字段 | 必填 | 作用 |
|---|---|---|
| name / version / description | ✅ | 标识；name 只允许小写字母/数字/连字符且与目录同名 |
| core_version | 建议 | 内核版本门槛，不满足整包 unavailable |
| requires_env | — | 依赖的环境变量，缺失整包不加载 |
| requires_modules | — | pip 依赖，缺失整包不加载（不在运行时自动安装） |
| env | — | 包级环境变量默认值 |
| permissions | 建议 | 权限声明（信息性），显示在审批卡片 |
| platform | — | true = 平台包（跨领域通用能力，始终加载、运行时不可禁用），普通业务包不要设 |
| tools / skills / prompt | — | 能力清单（相对路径，逐项校验存在性） |
| signature | 上线时生成 | HMAC 签名，由 sign_pack 写入，不要手填 |

### Step 7 · 测试 → 审计 → 签名 → 上线

```bash
# 1. 测试（开发期开放模式，无需签名即可加载）
python server/main.py --port 7100
#    对话里验证：「查一下工号 E10086 的请假余额」
#    或 curl /api/health 看 packs 数组里 hr-leave 的 status

# 2. 审计：人工通读包内每一行代码（签名前必做，签名的含义就是「已审计」）

# 3. 签名（生产强制模式下不签名不加载）
python tools/sign_pack.py hr-leave

# 4. 启用：对话里说「启用 hr-leave」即可（禁用同理；重启后以 ENABLED_PACKS 白名单为准）
```

**验收**：`/api/health` 的 packs 数组里 `hr-leave` 的 `status=ok`、`trust=signed`、
tools/skills 数量正确。

## 四、治理规则（上线后）

- **启停即时生效**：禁用后其工具/技能/提示词立即从 Agent 消失，无需重启（运行时内存态；持久禁用用 `ENABLED_PACKS` 白名单）
- **签名与内容绑定**：改包内任何文件 → 签名失效 → 强制模式下整包不可用；改动必须重新审计重签
- **改动即审计**：签名不是形式，是「这个版本被人读过」的承诺
- **平台包保护**：`platform: true` 的包运行时不可禁用（通用能力不下线）；业务包不要标 platform

## 五、知识库接入（RAG as a Tool，v0.17.0）

知识库不做成独立应用，而是**检索工具 + 包**：模型判断「该查制度了」就调用检索工具，
签名/启停/环境变量注入等治理体系全部现成。参照实现：`packs/core-knowledge`（平台包）、
`packs/kb-demo`（领域包样例）、`tools/build_kb.py`（建索引）、`kb/`（示例制度文档）。

### 两层归属（跨领域知识放哪）

| 层 | 装什么 | 归属 | 数据目录 |
|---|---|---|---|
| 通用知识库 | 公司级制度（信息安全、行政流程等全员适用） | **平台包**（始终在线、运行时不可禁用） | `kb/common/` |
| 领域知识库 | 部门级知识（请假细则、销售话术等） | 各领域包（随包启停） | `kb/<领域>/` |

### 接入步骤（以新领域为例）

```bash
# 1. 放文档：Markdown/TXT 放进数据目录（按 # 标题自动切块）
mkdir -p kb/finance && cp ~/报销制度.md kb/finance/
# 2. 建索引（BM25，零第三方依赖；知识更新后重跑本步即可）
python tools/build_kb.py kb/finance
# 3. 做检索工具：复制 packs/kb-demo/tools/search_hr_knowledge.py 到新包，
#    只改三处——工具名、KB_DIR 环境变量名/默认目录、docstring 库说明
# 4. prompt.md 写明：什么问题必须先检索、出处必须标注、检索不到如实说明
# 5. 测试 → 审计 → 签名 → 启用（同第三章 Step 7）
```

### 关键设计点

- **数据在包目录之外**：包签名覆盖包内所有文件，知识放包里会导致每次更新都要重签。
  数据放 `kb/`，代码在包里，用环境变量（如 `KB_FINANCE_DIR`）指向数据——
  代码走签名治理，知识走数据更新流程，互不干扰
- **来源必须标注**：检索结果带「文档名 · 章节」，提示词要求模型回答时引用出处——
  防幻觉、可审计
- **检索预算**：top-k 3 条、每条 ≤400 字，别把整篇文档塞回上下文
- **工具名带域前缀**：`search_common_knowledge` / `search_hr_knowledge`，
  避免多包挂载撞名，模型也能从名字区分该查哪个库
- **升级路径**：起步用内置 BM25（中文 bigram，制度类精确匹配够用、完全离线）；
  效果不够再换向量检索（embedding + faiss 等），工具接口不变、对 Agent 透明

> 注意：分发包不含 `kb.db` 索引文件（*.db 不打包），解压后先跑 `tools/build_kb.py`
> 重建索引；检索工具在索引缺失时会返回明确的引导文案。

## 六、常见问题

- **Q：工具里能 import 第三方库吗？**
  A：能，但要在 pack.yaml 声明 `requires_modules`，并由部署侧先装进 Agent venv 或打入沙箱镜像——私有化环境不在运行时自动 pip install（fail-closed）。
- **Q：包可以访问数据库吗？**
  A：可以，连接串走 `requires_env` + 环境变量注入；建议只读账号，写操作留给审批门后的专用工具。
- **Q：沙箱模式和本地模式对包开发有影响吗？**
  A：有。沙箱模式下工具在隔离子进程执行，`env` 段的变量随调用透传；本地模式在主进程执行。工具代码保持自包含即可两种模式通吃。
- **Q：调试时包没加载去哪看？**
  A：启动日志的 `[packs]` 行 + `/api/health` 里每个包的 `error` 字段，原因（缺变量/缺模块/签名失败/清单损坏）都写在里面。
- **Q：一个包最少需要什么？**
  A：只有 pack.yaml 也能加载（空骨架）；但至少要有 tools/skills/prompt 之一才有实际意义。
