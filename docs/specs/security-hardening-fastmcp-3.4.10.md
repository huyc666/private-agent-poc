# 安全加固：fastmcp 直接依赖漏洞修复（spec）

状态：**已验证（本机 .venv，升级完成，未提交）**；待提交版本 **v0.17.54**。
基线 `52fc3b8`（HEAD，v0.17.53；对照 `3056522` = v0.17.52，见 §7.1）。
流程档位：安全加固；裁剪：无。

**spec 独立评审：已执行** → `docs/reviews/2026-11-11-fastmcp-spec-review.md`
（2026-11-11，结论「有条件通过」，P0 = 0 / P1 = 6 / P2 = 5）。6 条 P1 已逐条闭环，
P2-4/P2-5 的证据与口径已补齐，逐条对照见 §9。
**整改复审：已执行 → `docs/reviews/2026-11-11-v0.17.54-rereview.md`**
（2026-11-11，结论「**通过**」，本轮新增 P0 = 0 / P1 = 0，剩余 P2 6 条）。
N1–N4 已按复审建议修正（§9），N5（§6 沿用旧用例名）与 N6（P2-1 结构补强）
经用户裁决接受、不再改动。
**代码评审：已执行 → `docs/reviews/2026-11-11-v0.17.54-code-review.md`**
（2026-11-11，结论「有条件通过」，P0 = 0 / P1 = 1 / P2 = 3）。P1 是提交清单漏列
被本 spec 引用的评审/复审报告（非代码缺陷），由用户补齐；P2-1～P2-3 已按报告处置
（§9.1）。
**代码评审复审：已执行 → `docs/reviews/2026-11-11-v0.17.54-code-review-rereview.md`**
（2026-11-11，结论「**通过**」，本轮新增 P0 = 0 / P1 = 0，剩余 P2 4 条）。N1–N4 已逐条
闭环（§9.2）；随本批提交的评审/复审产物共 4 份，提交清单见 §7.4。本轮仍不提交，
提交由用户执行。

本轮只处理 GHSA-7xgj-8392-5q4r 这一条**直接依赖**命中。传递依赖的存量命中
（authlib/certifi/filelock/joserfc/pillow/pip）不顺手升级，单列留档见 §7.3。

## 一、现状与证据（命令与输出）

1. 已安装版本低于 PyPI 最新稳定版（快照时间 `2026-11-11 00:01 +08:00`；当时 4.1.8
   尚未发布，见 §2.1 证据第 7 条）：

```text
$ .venv\Scripts\python.exe -c "import fastmcp; print(fastmcp.__version__)"
3.4.8
$ .venv\Scripts\python.exe -m pip index versions fastmcp
INSTALLED: 3.4.8
LATEST:    4.1.7
```

说明：项目 requirements 带 `<4`，**项目约束下可解析的最新 3.x 是 3.4.11**；
`LATEST: 4.1.7` 是 PyPI 全局最新版，不在本项目可解析集合内。

2. 按已安装环境扫描命中（2026-11-11 00:01 +08:00，pip-audit 2.10.1，默认
   PyPI JSON 后端）：

```text
$ .venv\Scripts\python.exe -m pip_audit --local
Found 80 known vulnerabilities in 7 packages
Name     Version   ID                  Fix Versions
fastmcp  3.4.8     GHSA-7xgj-8392-5q4r 3.4.10,4.1.2
（其余命中：authlib 8 / certifi 2 / filelock 17 / joserfc 1 / pillow 39 / pip 12）
```

按 requirements.txt 声明扫描不能作为判定依据：解析器会取满足约束的最新版，本次会落到
已修复的 3.4.11，所以「扫声明」看不出宽区间风险；而宽区间在锁文件、离线缓存或旧解析
结果下仍可能装回 3.4.8。这正是本轮要加安全下限的原因——依赖文件必须自带下限，
不能依赖解析时机。

3. 漏洞本体（GitHub Advisory 原文）：

- Summary：FastMCP: Post-transform component authorization is skipped on direct calls
- 受影响区间：`>=3.0.0, <3.4.10` 与 `>=4.0.0, <4.1.2`；首个修复版分别为
  `3.4.10`、`4.1.2`
- 语义：启用 server/provider/session transform 且组件带 AuthCheck 时，直调路径
  先应用 transform 再返回组件，**没有对 transform 后的组件重新授权**；知道组件
  标识的低权限调用方可绕过策略直接调用。属于 CWE-862（缺失授权），CVSS 3.1 6.8。
- 对号入座：本项目只在 `tools/local_tools_server.py` 用 FastMCP 起 stdio Server
  （`FastMCP(...)` + `@mcp.tool` + `mcp.run()`），既没有配置 transform，也没有
  组件级 AuthCheck，**当前不可利用**；但依赖版本本身落在受影响区间，按依赖安全
  下限必须升级。

## 二、目标版本与选型理由

**目标版本：3.4.10。** 不选 4.1.2。

1. 3.4.10 确实是修复版：GHSA 明确 `first_patched_version = 3.4.10`，受影响区间
   上界为 `< 3.4.10`；3.4.10 与当前 3.4.8 同属 3.4.x 补丁线。
2. 依赖面零变化：PyPI 元数据比对，`fastmcp-slim` 3.4.10 与 3.4.8 的
   `requires_dist` 逐条一致（均要求 `mcp<2.0,>=1.24.0`、`starlette>=1.0.1`、
   `authlib>=1.6.11`、`joserfc>=1.1.0`），升级只换补丁号，不引入新的传递依赖。
   原始输出与 URL 见 §2.1 证据第 1 条。
3. 4.x 有兼容硬阻塞：`fastmcp-slim` 4.1.8 要求 `mcp<3.0.0,>=2.3.0`，而项目锁
   `mcp>=1.10,<2`——真正的绑定方是 `langchain-mcp-adapters` 0.3.2（声明
   `mcp<2.0.0,>=1.24.0`，当前装 1.30.0，且其源码 import 了 mcp 2.x 已移除的
   子模块，见 §2.1 证据第 5、6 条）。升 4.x 必须连带升级 mcp 到 2.x 并替换
   MCP 客户端适配层，超出「直接依赖漏洞修复」范围。

### 2.1 最新版盘点与 4.x 迁移面评估（背景）

本节是 2026-11-11 的盘点，作为 §8.1 跟踪项的**背景证据**保留：记录 3.x/4.x 最新
版本、4.1.8 的依赖约束、以及为什么当前不以最新版为目标。

**版本盘点**（PyPI JSON 元数据，抓取时间 `2026-11-11 07:26 +08:00`）：

| 包 | 最新稳定版 | 上传时间（UTC） | 备注 |
|---|---|---|---|
| fastmcp | 4.1.8 | 2026-11-10T20:44:54 | 4.x 线最新；本轮装 3.4.10 |
| fastmcp（3.x 线） | 3.4.11 | 2026-10-20T22:44:33 | 同线补丁，仅高于安全下限一级 |
| mcp | 2.3.0 | — | 1.x 线当前为 1.30.0 |
| langchain-mcp-adapters | 0.3.2 | — | 已是最新，无更高版本或预发布版 |
| litellm（可选网关） | 1.108.5 | — | `[proxy]`/`[mcp]` extra 要求 `mcp>=2.3,<3` + `httpx2>=2.10` |

**4.1.8 的依赖约束**（`fastmcp-slim` 元数据）：`mcp>=2.3.0,<3`、`mcp-types>=2,<3`、
`httpx2>=2.5`、`pydantic[email]>=2.12`、`starlette>=1.0.1`、`authlib>=1.9.5`、
`joserfc>=1.7.6`。运行环境这一侧已经够格：Python 3.12.10、fastapi 0.143.0、
pydantic 2.14.0、starlette 1.7.0，httpx2 2.13.1 也已在依赖树里（langsmith/openai
引入）。也就是说**4.x 的阻塞不在 Python/FastAPI/Starlette**。

**真正的硬阻塞是 MCP 客户端适配层**：

- `server/agent.py::load_mcp_tools`（L68-83）用 `MultiServerMCPClient.get_tools()`
  拿 LangChain 工具，适配层 0.3.2 声明 `mcp<2.0.0,>=1.24.0`；
- 不止是版本声明：适配层源码 `tools.py:27-28` 直接
  `from mcp.server.fastmcp.tools import Tool` 与
  `from mcp.server.fastmcp.utilities.func_metadata import ...`；
- mcp 2.3.0 的 wheel 里 `mcp/server/fastmcp` 只剩**单文件模块**
  `mcp/server/fastmcp.py`（769 字节，内容只有一句说明 + `raise ModuleNotFoundError`），
  原包内容已迁到 `mcp/server/mcpserver/`；因此上面两行 import 在 mcp 2.x 下
  直接 `ModuleNotFoundError`（证据见下）；
- 该异常被 `load_mcp_tools` 的 `except Exception` 吞掉 → 返回空列表，MCP 通道的
  `get_city_weather` 静默消失（内置同名工具还在，问题不会崩但会「无声降级」），
  这属于必须验收的行为回归，不适合混在漏洞修复里做。

**fastmcp 4 对本地 Server 代码的影响**：mcp 2.3.0 的 `fastmcp.py` 迁移提示明确
「FastMCP 改名为 `mcp.server.mcpserver.MCPServer`」，迁移指南见
`https://py.sdk.modelcontextprotocol.io/v2/migration/#fastmcp-renamed-to-mcpserver`。
本项目 `tools/local_tools_server.py` 只用到 `FastMCP(...)`、`@mcp.tool`、`mcp.run()`，
以及测试里的 `list_tools()/call_tool()`；这些在 4.x 是否原样保留**需在触发升级时按
该迁移指南逐项复核**，本节不作「零改动」的承诺。若未来触发升级，要动的是客户端侧
和依赖：

| 变更点 | 规模 | 说明 |
|---|---|---|
| `server/agent.py::load_mcp_tools` | ~15 行重写 + 40-60 行包装 | 换 `fastmcp.Client`（stdio，传 `Path`），把 MCP 工具转成 LangChain `StructuredTool`（名字/描述/`input_schema` 映射，每次调用开会话，保持现有「失败降级为空列表」语义） |
| `requirements.txt` | 2 行 | `fastmcp>=4.1.8,<5`、`mcp>=2.3,<3`，并删掉 `langchain-mcp-adapters` |
| `tests/test_agent_llm.py` | 0 行 | 降级用例只断言「脚本缺失 → 空列表」，语义不变 |
| `tests/test_dependency_versions.py` | 小改 | 下限改 4.1.8；MCP 调用用例按新客户端路径调整 |
| `docs/DEPLOYMENT.md` | 1 段 | 现有注记「litellm[proxy] 拉 mcp 2.x 与 fastmcp 3.x 冲突」将失效——4.x 与 litellm 反而对齐，改为提示「同一环境装 litellm[proxy] 不再冲突」 |

**4.x 特有的待验风险**：`fastmcp-slim` 4.1.8 的 HTTP 客户端从 `httpx` 换成
`httpx2>=2.5`（元数据可复核，见证据第 2 条）。TLS 信任库行为是否随之改为操作系统
信任库，本轮**没有可复核来源，不作断言**；触发升级时须在内网代理/自签 CA 场景
单独验证证书链并留证。代码里若有 `except httpx.` 包住 MCP 调用会变成死分支
（本项目目前没有这类捕获，`tools/load_test.py` 的 httpx 与 MCP 无关）。

**证据留证（2026-11-11 07:25–07:26 +08:00 复核，原始输出）**

1. `fastmcp-slim` 3.4.10 与 3.4.8 的 `requires_dist` 逐行一致：

```text
$ (Invoke-RestMethod https://pypi.org/pypi/fastmcp-slim/3.4.10/json).info.requires_dist
pydantic-settings>=2.0.0
pydantic[email]>=2.11.7
authlib>=1.6.11; extra == "client"
httpx<1.0,>=0.28.1; extra == "client"
mcp<2.0,>=1.24.0; extra == "client"
starlette>=1.0.1; extra == "client"
...
authlib>=1.6.11; extra == "server"
httpx<1.0,>=0.28.1; extra == "server"
joserfc>=1.1.0; extra == "server"
mcp<2.0,>=1.24.0; extra == "server"
starlette>=1.0.1; extra == "server"
（同命令对 3.4.8 输出逐行相同；`fastmcp` 3.4.10 的 requires_dist 为
 `fastmcp-slim[client,server]==3.4.10`）
```

2. `fastmcp-slim` 4.1.8 元数据（`https://pypi.org/pypi/fastmcp-slim/4.1.8/json`）：

```text
mcp-types<3.0.0,>=2.0.0
pydantic[email]>=2.12.0
authlib>=1.9.5; extra == "client"
httpx2>=2.5.0; extra == "client"
mcp<3.0.0,>=2.3.0; extra == "client"
starlette>=1.0.1; extra == "client"
...
joserfc>=1.7.6; extra == "server"
mcp<3.0.0,>=2.3.0; extra == "server"
```

3. litellm 1.108.5 的 extra（`https://pypi.org/pypi/litellm/1.108.5/json`）：

```text
mcp<3,>=2.3.0; extra == "mcp"
httpx2<3,>=2.10.0; extra == "mcp"
mcp<3,>=2.3.0; extra == "proxy"
httpx2<3,>=2.10.0; extra == "proxy"
```

4. `langchain-mcp-adapters` 0.3.2 的 mcp 约束
   （`https://pypi.org/pypi/langchain-mcp-adapters/0.3.2/json`）：

```text
mcp<2.0.0,>=1.24.0
```

5. mcp 2.3.0 wheel 内容
   （`pip download --no-deps --no-cache-dir -d %TEMP%\pa_poc_mcp230 mcp==2.3.0`，
   检查脚本 `%TEMP%\pa_poc_wheel_check.py`）：

```text
WHEEL ...\mcp-2.3.0-py3-none-any.whl
TOTAL_ENTRIES 130
FASTMCP_ENTRIES ['mcp/server/fastmcp.py']
MCP_SERVER_SAMPLE ['mcp/server/__init__.py', ..., 'mcp/server/mcpserver/server.py', ...]
```

`mcp/server/fastmcp.py` 全文只有说明与抛错（无 API）：

```text
"""Removed in mcp 2: `FastMCP` is now `mcp.server.mcpserver.MCPServer`.
This module has no API. Importing it, or anything below it, raises
`ModuleNotFoundError` with a message that points at the migration guide. ..."""
raise ModuleNotFoundError(_MESSAGE, name=__name__)
```

6. 适配层实际 import（本机
   `.venv/Lib/site-packages/langchain_mcp_adapters/tools.py:27-28`）：

```text
from mcp.server.fastmcp.tools import Tool as FastMCPTool
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase, FuncMetadata
```

mcp 2.x 下第一行即 `ModuleNotFoundError`；`server/agent.py:77-83` 的
`except Exception` 会把它降级为空列表。

7. 版本快照（`https://pypi.org/pypi/fastmcp/json`）：`4.1.8` 上传于
   `2026-11-10T20:44:54Z`（= 2026-11-11 04:44:54 +08:00），`3.4.11` 上传于
   `2026-10-20T22:44:33Z`。§1 的 00:01 快照显示 `LATEST: 4.1.7` 是因为 4.1.8
   当时尚未发布（早于 04:44），两处快照不冲突。

8. AC3 的真实 stdio 实测（`2026-11-11 07:25:56 +08:00`，仓库根目录执行）：

```text
$ $env:MCP_ENABLED='true'; .venv\Scripts\python.exe %TEMP%\pa_poc_mcp_e2e.py
（FastMCP 3.4.10 横幅，Server: poc-local-tools, 3.4.10）
Starting MCP server 'poc-local-tools' with transport 'stdio'
TOOLS ['get_city_weather']
ARGS {'city': {'type': 'string'}}
```

脚本 `%TEMP%\pa_poc_mcp_e2e.py`（仓库外临时文件）：

```python
import asyncio, sys
from langchain_mcp_adapters.client import MultiServerMCPClient

async def main():
    client = MultiServerMCPClient({
        "local-tools": {
            "command": sys.executable,
            "args": ["tools/local_tools_server.py"],
            "transport": "stdio",
        }
    })
    tools = await client.get_tools()
    print("TOOLS", [t.name for t in tools])
    print("ARGS", tools[0].args)

asyncio.run(main())
```

**结论：本轮不动 4.x，转为跟踪项，不设版本目标。** 3.4.10 已满足安全与可用性要求；
4.x 只在 §8.1 的触发条件命中后再评估——当前没有已发布的 **LangChain** MCP 适配层
支持 mcp 2.x（`langchain-mcp-adapters` 0.3.2 声明 `mcp<2`），硬升会让 MCP 通道静默
失效，这是「现在不做」的依据，不是排期承诺。受限的只是这个适配层，不是 MCP 客户端
实现本身：`fastmcp-slim` 4.1.8 自带的 client extra 已经是 `mcp>=2.3`。

## 三、变更面

- `requirements.txt`：`fastmcp>=2.3` → `fastmcp>=3.4.10,<4`。下限是**安全下限**
  （低于它的版本带 GHSA-7xgj-8392-5q4r），不是「建议版本」也不是精确锁定；
  上界 `<4` 与 `mcp<2` 的锁定配套，防止解析器把项目拽到需要 mcp 2.x 的 4.x 线。
- `tests/test_dependency_versions.py`（新增）：`test_ac1_*`（已安装版本落在
  `[3.4.10, 4)`）、`test_ac2_*`（requirements 下界恰为 3.4.10 且排除 4.x）、
  `test_ac3_*`（MCP Server 注册/调用回归，含真实 stdio 子进程的成功路径）。
- `docs/ENGINEERING-METRICS.md`：S5 改用与 §7 一致的 OSV 口径（105/6），
  PyPI 默认后端的 79/6 降为对照；一期补齐清单第 4 项的结项数字同步。
- `README.md`：版本号 v0.17.53 → **v0.17.54**，顶部「版本记录」新增一条，
  只写用户可感知行为（依赖安全下限升级），不写 CVE 细节。
- `docs/PLAN.md`：**仅在**「后续轮次待办（安全评审遗留）」表末尾追加第 6 行
  （§8.3）；不动 P1-P7 功能表、不动 6.8 条目。
- 不改任何业务代码；不动 `web/index.html`、`server/usage.py`、
  `tests/test_usage_run_wallclock.py`。`docs/specs/6.8-*` **仅**改 P2-3 指定的一处：
  `docs/specs/6.8-run-wallclock-duration.md:595` 的时点限定（「v0.17.54 之前」），
  其余内容不动。
- **并行改动冲突风险**：`docs/PLAN.md`、`README.md`、`docs/ENGINEERING-METRICS.md`
  都处在多会话并行改动范围内。提交时必须按路径 + 片段核对，只带上本 spec 列出的
  改动，不得整文件 `git add` 后直接提交，避免把其他会话的改动一起带走。

## 四、验收标准（AC 账本）

| AC | 验证方式 | 用例/留证 | 红态 | 实现证据 | 收敛 |
|---|---|---|---|---|---|
| AC1 | 单测：已安装 fastmcp 同时满足 `>=3.4.10` 与 `<4` | `tests/test_dependency_versions.py::test_ac1_fastmcp_within_security_window` | 红态（3.4.8 低于下限） | `tests/test_dependency_versions.py:21-31` | 已收敛 |
| AC2 | 单测：区间下界恰为 3.4.10（3.4.9 不满足、3.4.10 满足），且不含 4.0.0 / 4.1.2 | `tests/test_dependency_versions.py::test_ac2_requirements_fastmcp_window` | 红态（`>=2.3` 可解析 3.4.9，且无上界） | `tests/test_dependency_versions.py:45-58` | 已收敛 |
| AC3 | 单测（in-process）+ 单测（真实 stdio 子进程，30s 超时）+ 手工 stdio 实测 | `tests/test_dependency_versions.py::test_ac3_mcp_server_registers_and_calls_weather_tool`、`::test_ac3_load_mcp_tools_over_stdio`；手工留证见 §2.1 证据第 8 条 | 无红态·回归（升级前后均绿） | `tests/test_dependency_versions.py:61-88` | 已收敛 |
| AC4 | 手工实测：`pip-audit --local --vulnerability-service osv --timeout 60`，直接依赖命中 = 0 | 命令 + 原始输出见 §7.2 | 手工留证 | `docs/specs/security-hardening-fastmcp-3.4.10.md` §7.2 | 已收敛 |
| AC5 | 全量回归 `pytest -q` 全绿，数字入档 | 见 §7.1 的时点表 | 无红态·回归（以升级前基线对照） | `docs/specs/security-hardening-fastmcp-3.4.10.md` §7.1 | 已收敛 |

## 五、红测试清单

- `test_ac1_fastmcp_within_security_window`：`importlib.metadata.version("fastmcp")`
  必须落在 `[3.4.10, 4)`——下限挡漏洞版本，上界挡 4.x。
- `test_ac2_requirements_fastmcp_window`：解析 requirements.txt 的 fastmcp 包行，
  断言区间下界恰为 `3.4.10`（`3.4.9` 不满足、`3.4.10` 满足），且不含 `4.0.0`、
  `4.1.2`、含 `3.4.11`——防止约束被改回「宽到能装回漏洞版本」或「放宽到 4.x」。
- `test_ac3_mcp_server_registers_and_calls_weather_tool`：in-process 拉起
  `tools/local_tools_server.py` 的 `FastMCP` 实例，断言注册工具集恰为
  `{get_city_weather}` 与工具调用返回文本——这是升级后 fastmcp API 是否变形的
  直接证据，属 AC3 的回归用例（红态为「无红态·回归」）。
- `test_ac3_load_mcp_tools_over_stdio`：真实 stdio 子进程调用
  `agent.load_mcp_tools()`，断言返回列表非空且含 `get_city_weather`。拒绝 4.x 的
  核心理由就是「适配层 import 失败 → 静默空列表」，此前只有仓库外手工脚本能发现，
  这条用例把它纳入全量回归（conftest 默认 `MCP_ENABLED=false`，既有用例只测失败降级）。
  子进程调用包在 `asyncio.wait_for(..., timeout=30)` 内（`MCP_STDIO_TIMEOUT`）：
  子进程启动卡住时以失败中止，不会拖住整套回归；`pytest.ini` 无全局 timeout，
  因此该保护写在用例内而不是配置里。

## 六、红运行记录

命令：`.venv\Scripts\python.exe -m pytest tests/test_dependency_versions.py -v`

```text
FAILED tests/test_dependency_versions.py::test_fastmcp_at_or_above_security_floor
  AssertionError: fastmcp 3.4.8 低于安全下限 3.4.10（GHSA-7xgj-8392-5q4r）
FAILED tests/test_dependency_versions.py::test_guard_requirements_fastmcp_floor
  AssertionError: requirements.txt 仍可解析回漏洞版本 3.4.8
2 failed, 1 passed in 6.15s
```

红态与预期一致：两条断言因「装的是 3.4.8 / 约束是 `>=2.3`」而失败；MCP 注册用例在
旧版本上已绿，说明它测的是行为不变，不是版本号。

记录中的用例名是 2026-11-11 评审整改前的旧名；整改只做改名与新增上界断言，
失败点仍是原两条（AC1 先撞下限、AC2 先撞下限），红态结论不变。整改后复跑见 §7.4。

代码评审 P2-1 把 AC2 的地板抽样从 `3.4.8` 改为 `3.4.9`（受影响区最后一个漏洞版本）
后，同一红态下报错文案变为「下限低于安全下限：3.4.9 仍在允许集内（修复版从 3.4.10
起）」；触发条件不变——`fastmcp>=2.3` 仍能解析到 3.4.9，而 `>=3.4.9,<4` 这类回退
也会被新断言拦下。

## 七、升级后验证

### 7.1 全量回归

命令：`.venv\Scripts\python.exe -m pytest -q`

| 时点 | passed | skipped | failed |
|---|---|---|---|
| 升级前（HEAD `52fc3b8`，v0.17.53，含 `b530009` 的 2 个 6.8 用例） | 1198 | 20 | 0 |
| 对照（`3056522`，v0.17.52，不含上述 2 个） | 1196 | 20 | 0 |
| 升级后（fastmcp 3.4.10，P1 整改后） | **1201** | **20** | **0** |
| 升级后（代码评审 P2-2 补 stdio 成功路径用例后，当前工作区） | **1202** | **20** | **0** |

口径说明：`3056522` 的 README 是 v0.17.52；v0.17.53 是其后 `b530009`，该提交给
`tests/test_usage_run_wallclock.py` 新增 2 个用例（16→18）。因此 1198 对应 HEAD
（含这 2 个），1196 对应 `3056522`，两者不可混用。新增 3 个用例（AC1/AC2/AC3 各 1），
1198 + 3 = 1201；代码评审后又补 1 条 AC3 的真实 stdio 成功路径用例，1201 + 1 = 1202。
skipped 语义不变（20，门控用例默认跳过）。**无失败，因此没有改任何断言，也没有为
适配版本改代码。** 升级后墙钟 503s 与基线 233s 不可比——同机有并行会话任务在占用资源，
本轮只以 passed/skipped/failed 对照。

评审 P1 整改后复跑（2026-11-11 07:32 +08:00）、复审 N1–N4 修正后复跑，以及代码评审
P2-2 补用例后的全量复跑（142.38s / 复跑 144.89s、145.15s）分别为 1201 / 1201 / **1202**
passed，skipped 20 / failed 0 不变；1202 − 1201 = 新增的
`test_ac3_load_mcp_tools_over_stdio`。

### 7.2 pip-audit 复扫

正式口径按 `docs/ENGINEERING-METRICS.md` S5 的规定使用 **OSV 后端**；默认 PyPI JSON
后端的结果只作对照。

命令：`.venv\Scripts\pip-audit.exe --local --vulnerability-service osv --timeout 60`
（扫描开始 `2026-11-11 07:23:35 +08:00`，pip-audit 2.10.1，退出码 1 = 有命中）。

```text
Found 105 known vulnerabilities in 6 packages
Name     Version   ID ...
authlib  1.8.0     ...
certifi  2026.7.22 ...
filelock 4.2.0     ...
joserfc  1.7.5     ...
pillow   12.3.0    ...
pip      25.0.1    ...
（fastmcp 不在结果中）
```

| 时点 | 后端 | 命中条数 / 包数 | 直接依赖命中 |
|---|---|---|---|
| 升级前 2026-11-11 00:01 | PyPI JSON（默认） | 80 / 7 | 1（fastmcp 3.4.8） |
| 升级后 2026-11-11 00:08 | PyPI JSON（默认） | 79 / 6 | 0 |
| 升级后 2026-11-11 07:23:35 | **OSV（正式记录）** | **105 / 6** | **0** |

口径说明：升级前没有跑 OSV（环境已升级，无法回退），所以 105/6 只作为升级后的正式
数字；跨后端不做增减对比。两个后端对两个判定点的结论一致：**fastmcp 均已从结果中
消失，直接依赖命中均为 0**，达成 S5 阻断线。

### 7.3 残留命中（传递依赖，本轮不升，留档）

OSV 会把同一公告的多个来源/别名（如 PYSEC 与 CVE）各算一行，因此「原始行」大于
「去重 ID」。下表为 `2026-11-11 07:23:35` 的 OSV 结果：

| 包 | 当前版本 | OSV 命中（原始行 / 去重 ID） | 首个修复版 | 来源（传递） |
|---|---|---|---|---|
| authlib | 1.8.0 | 14 / 8 | 1.9.0 / 1.9.1 | fastmcp-slim[server] |
| certifi | 2026.7.22 | 2 / 1 | 2026.10.18 | httpx 等 |
| filelock | 4.2.0 | 17 / 17 | 4.2.1 | fastembed 等 |
| joserfc | 1.7.5 | 1 / 1 | 1.7.6 | fastmcp-slim[server] |
| pillow | 12.3.0 | 59 / 41 | 13.0.0 | python-pptx / fastembed |
| pip | 25.0.1 | 12 / 6 | 25.3 / 26.x | 安装器自身，非应用依赖 |

同一天的 PyPI JSON 后端对同一环境为**原始行 79、按 ID 去重 72**；OSV 为
**原始行 105、按 ID 去重 74**。两个后端的告警 ID 集合不同，只各自计数，不做逐条对比。
以上均非 requirements.txt 直接声明，本轮按边界不动，单独留档。

### 7.4 兑现关

- AC1：已收敛。`importlib.metadata.version("fastmcp")` = 3.4.10，同时满足
  `>= 3.4.10` 与 `< 4`（`tests/test_dependency_versions.py:21-31`）。
- AC2：已收敛。requirements.txt 为 `fastmcp>=3.4.10,<4`；下界断言为 `3.4.9` 不满足、
  `3.4.10` 满足（差一个补丁号也能发现），上界断言不含 `4.0.0` / `4.1.2`，并允许
  `3.4.11`（`tests/test_dependency_versions.py:45-58`）。
- AC3：已收敛。in-process 用例断言注册集恰为 `{get_city_weather}` 且调用返回
  `北京-stub（MCP 通道）`；真实 stdio 子进程用例（30s 超时包裹）断言 `load_mcp_tools()`
  非空且含 `get_city_weather`（`:74-88`）；手工 stdio 实测的命令、脚本与原始输出见
  §2.1 证据第 8 条。
- AC4：已收敛。OSV 后端直接依赖命中 0；PyPI 后端同样为 0（§7.2）。
- AC5：已收敛。1202 passed / 20 skipped / 0 failed（§7.1 末行；代码评审 P2-2 补
  1 条 stdio 成功路径用例后）。
- 版本与文档：README 顶部版本 v0.17.54 + 版本记录已同步（4 个依赖约束用例 /
  1202 passed）；`docs/ENGINEERING-METRICS.md` S5 与一期补齐清单已对齐 OSV 口径；
  `docs/PLAN.md` 仅追加第 6 行（§8.3）；
  `docs/specs/6.8-run-wallclock-duration.md:595` 的 `>=2.3` 已加「v0.17.54 之前」限定。
- 边界：未改业务代码；未触碰 `web/index.html`、`server/usage.py`、
  `tests/test_usage_run_wallclock.py`；6.8 spec 只改 P2-3 指定的一处括号。
  未提交、未 push。
- 提交清单：**11 个路径**——实现/文档 7 个（`requirements.txt`、
  `tests/test_dependency_versions.py`、`docs/specs/security-hardening-fastmcp-3.4.10.md`、
  `README.md`、`docs/PLAN.md`、`docs/ENGINEERING-METRICS.md`、
  `docs/specs/6.8-run-wallclock-duration.md`）+ 评审/复审产物 4 份
  （`docs/reviews/2026-11-11-fastmcp-spec-review.md`、
  `docs/reviews/2026-11-11-v0.17.54-rereview.md`、
  `docs/reviews/2026-11-11-v0.17.54-code-review.md`、
  `docs/reviews/2026-11-11-v0.17.54-code-review-rereview.md`）。按路径 + 片段核对，
  不整文件 `git add`。复审报告 §6 的「10 个路径」按当时清单计算，未计入 P2-3 新改的
  6.8 spec，以本节为准。
- 代码评审：已执行（`docs/reviews/2026-11-11-v0.17.54-code-review.md`，P0 0 / P1 1 /
  P2 3，结论「有条件通过」）。P1 为提交清单漏列被引用的评审/复审报告，由用户补齐；
  P2-1～P2-3 已闭环（§9.1）。代码评审复审已通过
  （`docs/reviews/2026-11-11-v0.17.54-code-review-rereview.md`，P0 0 / P1 0），
  N1–N4 已闭环（§9.2）。本轮不提交，提交由用户执行。

## 八、遗留与跟踪

### 8.1 跟踪项：fastmcp 4.x（mcp 2.x）

**状态**：跟踪中，不设版本目标。3.4.10 已满足安全（直接依赖命中 0）与可用性
（MCP 注册/调用不回归）要求；4.x 不是待办任务，只在触发条件命中后再评估。

**背景**：见 §2.1——3.x/4.x 最新版本盘点、4.1.8 对 `mcp>=2.3`/`httpx2` 的约束、
`langchain-mcp-adapters` 0.3.2 的硬阻塞证据、逐文件改动量。此处不重复。

**触发条件**（按可判定谓词表述，满足任一即启动评估）：

1. **允许集内出现新漏洞且修复版仍在 3.x**：`3.4.10 <= v < 4` 的任一版本被公开
   CVE/GHSA 标记为受影响，且该区间内存在修复版 → 直接把安全下限抬到该修复版
   （与本轮同一流程），**不触发** 4.x 评估。判据：`pip-audit --local
   --vulnerability-service osv` 输出，或公告的 `affected` /
   `first_patched_version` 字段；不按「高危」二字主观分级。
2. **唯一修复版落在 4.x**：条件 1 的公告只有 4.x 修复版 → 这时才进入 4.x 评估。
3. **适配层客观信号**：`langchain-mcp-adapters` 任一已发布版本的 `requires_dist`
   中 `mcp` 下界 ≥ 2.3（即声明支持 mcp 2.x）。判据命令：
   `(Invoke-RestMethod https://pypi.org/pypi/langchain-mcp-adapters/<version>/json).info.requires_dist`。
   信号成立后仍需用户裁决「升级成本是否可接受」——这是成本决策，不是技术判定。
4. **能力需求（用户裁决项）**：项目需要 4.x 独有能力时，由用户明确提出并列出
   能力清单（例：mcp 2.x 的某项协议特性）。没有用户提出的能力清单，不启动。

**触发后的评估与验收要点**（预案，非当前任务）：

- 优先等适配层，成本最低；若不走适配层，预案是自建 `fastmcp.Client` 包装层：
  `server/agent.py::load_mcp_tools` 约 15 行重写 + 40-60 行把 MCP 工具转成 LangChain
  `StructuredTool`（名字/描述/`input_schema` 映射，每次调用建立 stdio 会话），并删除
  `langchain-mcp-adapters` 直接依赖。

一并考虑的组件/文档：

- `server/agent.py::load_mcp_tools`（保留失败降级为空列表的语义）。
- `requirements.txt`：`fastmcp>=4.1.8,<5`、`mcp>=2.3,<3`。
- `docs/DEPLOYMENT.md` L139-141 的 litellm 隔离注记（4.x 后与 litellm[proxy] 的
  mcp 2.x 要求反而一致，注记需改写）。
- `tests/test_dependency_versions.py`：下限改 4.1.8；MCP 注册/调用用例改走新客户端。
- `tests/test_agent_llm.py`：降级用例预期不变，但需在 mcp 2.x 环境下重跑确认。

**触发后的验收要点**：

- `pip check` 干净，`pip-audit --local --vulnerability-service osv` 直接依赖命中仍为 0；
- MCP 通道真实 stdio 往返可用（`get_city_weather` 经 MCP 进程返回带「（MCP 通道）」
  后缀），且 `MCP_ENABLED=true` 时不再出现「适配层 import 失败 → 空列表」的静默降级；
- 全量回归与基线持平；
- `httpx2` 的 TLS 信任库行为变化在内网代理/自签 CA 场景单独验证并留证（本轮未复核，
  不预设结论）。

**风险等级**：中。协议层从 MCP SDK v1 换到 v2（snake_case 字段、会话语义变化），
但本项目只依赖高层 Server/Client 接口，业务工具代码不受影响。

### 8.2 版本口径

**验证于 fastmcp 3.4.10；允许集固定为 `3.4.10 <= v < 4`。**

requirements 的 `fastmcp>=3.4.10,<4` 同时表达两件事：下限是安全下限（低于 3.4.10
的版本带 GHSA-7xgj-8392-5q4r），上界 `<4` 排除 4.x——**4.x 即使已修复（4.1.2+）
也不在本轮允许集内**，因为 4.x 要求 mcp>=2.3，与项目 mcp 1.x 锁定冲突。解析到
3.4.11 或更高的 3.x 补丁版属预期行为，不要求把本机环境或文档基线对齐到 3.4.11；
3.x 有新补丁也不触发 §8.1 的评估，只有命中触发条件 1/2 才触发。

落地断言：AC1 同时断言 `>= 3.4.10` 与 `< 4`；AC2 直接断言下界恰为 `3.4.10`——
`3.4.9`（受影响区最后一个漏洞版本）不满足、`3.4.10` 满足，同时区间不含 `4.0.0` /
`4.1.2` 且含 `3.4.11`（`tests/test_dependency_versions.py:21-58`）。

### 8.3 PLAN 登记（已落位）

已按 `docs/PLAN.md` §「计划外交付记录（评审与修复）」→
「### 后续轮次待办（安全评审遗留，未计入任何已交付版本）」表格末尾新增第 6 行
（现有 1-5 已全部交付并删除线标记）。**这一行是本轮唯一的 PLAN 改动**；
P1-P7 功能表与 6.8 条目未动。措辞为跟踪项 + 客观触发条件，不含「必须升级」：

> | 6 | **fastmcp 4.x（mcp 2.x）跟踪项**（不设版本目标） | 当前安全下限 fastmcp 3.4.10 / mcp 1.x 已达标（直接依赖命中 0）。最新 4.1.8 要求 mcp>=2.3，而 langchain-mcp-adapters 0.3.2（最新）声明 mcp<2.0.0 且 import 了 mcp 2.x 已移除的 `mcp.server.fastmcp`，硬升会让 `load_mcp_tools` 静默降级为空列表。**触发条件**（满足任一再评估）：① `3.4.10 <= v < 4` 内出现新漏洞且 3.x 有修复版 → 直接抬安全下限，不评估 4.x；② 唯一修复版落在 4.x；③ 适配层 `requires_dist` 的 mcp 下界 ≥ 2.3（客观信号，成本仍由用户裁决）；④ 用户明确提出 4.x 独有能力清单。触发后评估：自建 `fastmcp.Client` 包装层（预案：`load_mcp_tools` 约 15 行重写 + 40–60 行 MCP→LangChain 工具转换）与 httpx2 OS truststore 自签 CA 部署验收 | spec：docs/specs/security-hardening-fastmcp-3.4.10.md §8.1 |

不放进 P1-P7 功能表：它不是功能项，性质与「后续轮次待办」的安全遗留一致，且该表
已有「启动任一项时走 spec-first 红线流程」的现成约束。

## 九、独立评审整改记录（2026-11-11）

评审报告：`docs/reviews/2026-11-11-fastmcp-spec-review.md`（P0 0 / P1 6 / P2 5，
结论「有条件通过」）。本轮整改如下：

整改复审报告：`docs/reviews/2026-11-11-v0.17.54-rereview.md`（2026-11-11，
本轮新增 P0 0 / P1 0，结论「**通过**」，剩余 P2 6 条）。复审新增问题的处置见下表
N1–N6。

| 编号 | 整改位置 | 闭环状态 |
|---|---|---|
| P1-1 | §8.2 改为 `3.4.10 <= v < 4`（明确 4.x 即使已修复也不在允许集内）；AC1 增加 `< 4` 断言、AC2 增加 `4.0.0`/`4.1.2` 上界断言（`tests/test_dependency_versions.py:21-58`） | 已闭环（整改后 3 passed） |
| P1-2 | §7.2/§7.3 以 OSV `105/6` 为正式记录，PyPI `79/6` 降为对照并注明后端；`docs/ENGINEERING-METRICS.md` S5 与一期补齐清单同步；§3 补列该文档 | 已闭环 |
| P1-3 | 头部与 §7.1 基线改为：1198 = `52fc3b8`（HEAD，含 `b530009` 的 2 个用例）、1196 = `3056522`（v0.17.52），不再混用 | 已闭环 |
| P1-4 | §3 改为「不动 PLAN 功能表与 6.8 条目，仅追加第 6 行」，并补并行改动冲突提示；§8.3 标明该行是本轮唯一 PLAN 改动 | 已闭环 |
| P1-5 | §4 账本改为 6 列（验证方式 / 用例·留证 / 红态 / 实现证据 / 收敛），红态只用允许值；用例统一为 `test_ac1_*`/`test_ac2_*`/`test_ac3_*`；删除「守护不占 AC」的旧说法，§5 与账本口径一致 | 已闭环 |
| P1-6 | README 升到 v0.17.54 并新增版本记录（依赖安全下限升级，不写 CVE 细节）；§3 与 §7.4 记录 | 已闭环 |
| P2-4 | 新增 §2.1「证据留证」8 条：PyPI 元数据 URL + 抓取时间 + 原始 requires_dist、mcp 2.3.0 wheel 清单与 `fastmcp.py` 原文、适配层 import 行号、版本上传时间、AC3 stdio 完整命令/脚本/原始输出；不可复核的 truststore 与「零改动」说法已删除或改为待验 | 已闭环 |
| P2-5 | §8.1 触发条件改为可判定谓词（漏洞区间 + 修复版位置 / 适配层 `requires_dist` 下界 / 用户能力清单），并区分「技术信号」与「成本裁决」 | 已闭环 |
| P2-2 / P2-3 | 版本快照差异已用上传时间解释（4.1.8 于 04:44 发布，晚于 00:01 快照）；「扫声明零命中」的因果表述改正 | 顺带修正（位于本轮必改的证据段） |
| P2-1 | 结构补强（独立的背景/约束/待澄清小节） | 未做（超出「修 P1 + P2-4/P2-5」的指令范围） |
| N1 | §7.3 计数口径改为「PyPI 原始行 79 / 去重 72，OSV 原始行 105 / 去重 74」，并注明两后端 ID 集合不同 | 已修 |
| N2 | §2.1 结论把「没有任何 MCP 客户端适配层支持 mcp 2.x」收窄为「没有已发布的 **LangChain** MCP 适配层支持」，并注明 `fastmcp-slim` 4.1.8 自带 client extra 已是 `mcp>=2.3` | 已修 |
| N3 | README v0.17.54 条目删除组件变换/组件级授权等实现细节，只保留「存在一处权限校验缺陷、升级后消除」 | 已修 |
| N4 | `tests/test_dependency_versions.py::_fastmcp_requirement` 改为解析后按包名精确匹配 `fastmcp`（不再按前缀取首行） | 已修 |
| N5 | §6 红运行记录沿用整改前旧用例名 | 接受，不再改动（用户裁决） |
| N6 | P2-1 结构补强 | 接受，不再改动（用户裁决） |

### 9.1 代码评审整改（2026-11-11）

代码评审报告：`docs/reviews/2026-11-11-v0.17.54-code-review.md`（P0 0 / P1 1 / P2 3，
结论「有条件通过」）。P1 是提交清单漏列被本 spec 引用的评审/复审报告（非代码缺陷），
由用户补齐，本轮未改代码。

| 编号 | 整改位置 | 闭环状态 |
|---|---|---|
| P2-1 | `tests/test_dependency_versions.py:48-50`：AC2 下界断言由「远低于下限的 3.4.8」改为「`3.4.9` 不满足、`3.4.10` 满足」，约束回退一个补丁号即红 | 已闭环 |
| P2-2 | `tests/test_dependency_versions.py:74-88` 新增 `test_ac3_load_mcp_tools_over_stdio`：真实 stdio 子进程断言 `load_mcp_tools()` 非空且含 `get_city_weather`。未加门控——连跑 4 次 3.69–4.67s 全绿，全量耗时 142.38s（上一轮 139.76s），成本可接受且稳定，因此不需要降级为手工留证；超时保护见 §9.2 N3 | 已闭环 |
| P2-3 | `docs/specs/6.8-run-wallclock-duration.md:595` 的 `>=2.3` 加时点限定「v0.17.54 之前」，只改这一处（用户指示，覆盖评审「本批不动 6.8」的建议） | 已闭环 |

### 9.2 代码评审复审整改（2026-11-11）

复审报告：`docs/reviews/2026-11-11-v0.17.54-code-review-rereview.md`（2026-11-11，
结论「**通过**」，本轮新增 P0 = 0 / P1 = 0，剩余 P2 4 条）。四条 P2 逐条闭环如下：

| 编号 | 整改位置 | 闭环状态 |
|---|---|---|
| N1 | `README.md:9` 版本记录由「3 个依赖约束用例 / 全量 1201」改为「4 个 / 1202（20 skipped 语义不变）」，与 §7.1 实测一致 | 已闭环 |
| N2 | §3 边界句由「不动 `docs/specs/6.8-*`」改为「仅改 P2-3 指定的一处时点限定，其余不动」，与 §7.4 / §9.1 的改动记录一致 | 已闭环 |
| N3 | `tests/test_dependency_versions.py:83-86` 用 `asyncio.wait_for(..., timeout=MCP_STDIO_TIMEOUT)`（30s）包裹 `load_mcp_tools()`；`pytest.ini` 无全局 timeout，故保护写在用例内 | 已闭环 |
| N4 | 计数口径统一：本批评审/复审产物 **4 份**、提交清单 **11 个路径**（7 个实现/文档 + 4 份报告），§7.4 已逐项列出；复审报告 §6 的「10 个路径」漏计 P2-3 新改的 6.8 spec | 已闭环 |

复审判定的上轮三项（P2-1 AC2 下界、P2-2 stdio 成功路径、P2-3 6.8 单行改动）
均已闭环，本轮未引入新 P0/P1。
