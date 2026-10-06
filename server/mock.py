"""Mock 演示剧本：MOCK_LLM=true 时不依赖任何 GPU/模型，演示全链路能力。

与生产链路的关系：本模块产出与 agent.stream_reply 完全相同的 SSE 事件序列
（token / tool / approval_required / done），由 stream_reply 在 Mock 模式下调用。
演示剧本变更不会触碰生产文件（v0.13.0 起从 agent.py 拆出）。

覆盖的演示场景：领域包创建（审批）、技能制作/加载/列表、多 Agent 编排、
自定义工具创建（审批）、天气查询（真实接口，内置/MCP 两路）、删除文件（审批）、
平台包时间工具、默认引导语。
"""
import asyncio
from typing import AsyncIterator

from . import approval, custom_tools, packs, skills, telemetry
from .state import STATE
from .tools_builtin import do_delete


async def mock_stream(user_text: str) -> AsyncIterator[dict]:
    """Mock 模式：演示流式输出与工具调用链路（天气为真实接口调用）。
    包一层 Langfuse trace，Mock 模式下也能在观测台里看到完整调用链。"""
    with telemetry.trace_span("mock-chat", input=user_text):
        telemetry.set_trace_io(input=user_text)
        tokens: list[str] = []
        async for ev in _mock_stream_inner(user_text):
            if ev["type"] == "token":
                tokens.append(ev["content"])
            yield ev
        telemetry.set_trace_io(output="".join(tokens))


async def _query_weather_any(city: str, prefer_mcp: bool = False) -> tuple[str, str]:
    """Mock 演示用天气查询：依次尝试 领域包工具 / MCP 工具（prefer_mcp 时 MCP 优先）。
    返回 (工具名, 结果文本)；没有任何天气能力时工具名为空。"""
    sources = []
    if prefer_mcp:
        sources = ["mcp", "pack"]
    else:
        sources = ["pack", "mcp"]
    for src in sources:
        if src == "pack" and "get_current_weather" in STATE.pack_tools:
            r = await asyncio.to_thread(
                STATE.pack_tools["get_current_weather"].invoke, {"city": city})
            return "get_current_weather", str(r)
        if src == "mcp" and "get_city_weather" in STATE.mcp_tools:
            raw = await STATE.mcp_tools["get_city_weather"].ainvoke({"city": city})
            return "get_city_weather", _tool_result_text(raw)
    return "", "天气查询失败：当前没有任何天气能力（weather-ops 领域包未启用且 MCP 不可用）"


async def _mock_stream_inner(user_text: str) -> AsyncIterator[dict]:
    if ("领域包" in user_text or "pack" in user_text.lower()) and \
            any(k in user_text for k in ("创建", "制作", "新建", "写一个")):
        # 演示：对话式创建领域包（写入可执行代码 + 配置，属危险操作，先审批后落盘）
        demo_code = (
            'def quarter_of(month: int) -> str:\n'
            '    """返回指定月份所属的季度（如 5 → Q2）。month 为 1 到 12 的月份数字。"""\n'
            '    m = min(12, max(1, int(month)))\n'
            '    return f"{m} 月属于 Q{(m - 1) // 3 + 1}"\n')
        yield {"type": "tool", "status": "start", "name": "create_pack"}
        aid = approval.create_approval(
            "create_pack", {"name": "office-ops",
                            "description": "办公领域包：季度换算等办公小工具",
                            "with_tool": True, "code_preview": demo_code[:120]})
        yield {"type": "approval_required", "approval_id": aid,
               "tool": "create_pack",
               "args": {"name": "office-ops", "with_tool": True,
                        "code_preview": demo_code[:120]}}
        decision = await approval.wait_decision(aid)
        if decision is True:
            result = await asyncio.to_thread(
                packs.create_pack, "office-ops",
                "办公领域包：季度换算等办公小工具",
                "## 领域能力：办公（office-ops 包）\n\n涉及季度、月份的办公问题时优先使用本包工具。\n",
                "quarter_of", demo_code)
            from . import agent  # 懒导入避免循环依赖
            agent.maybe_reload_custom_tools()
            yield {"type": "tool", "status": "end",
                   "name": "create_pack", "result": result}
            answer = (f"✅ 审批已通过，`create_pack` 执行结果：{result}\n\n"
                      "领域包 = `packs/` 下一个目录打包工具+技能+提示词，"
                      "落盘即热挂载（试试问「5 月是第几季度」）。\n"
                      "真实模式下由模型按你的需求现场编写清单与工具代码，"
                      "Mock 模式这里用固定的办公领域包演示。")
        elif decision is False:
            yield {"type": "tool", "status": "end",
                   "name": "create_pack", "result": "用户拒绝，未执行"}
            answer = ("❌ 你已**拒绝**创建该领域包，未写入任何文件。\n\n"
                      "（创建包会写入可执行代码，fail-closed：拒绝/超时一律不落盘。）")
        else:
            answer = (f"⏱ 审批超时（{approval.APPROVAL_TIMEOUT} 秒未响应），"
                      "领域包创建已自动取消，未写入任何文件。")
    elif "技能" in user_text or "skill" in user_text.lower():
        if any(k in user_text for k in ("创建", "制作", "新建", "写一个")):
            # 演示：通过工具在聊天中制作技能
            yield {"type": "tool", "status": "start", "name": "create_skill"}
            result = await asyncio.to_thread(
                skills.create_skill, "daily-brief",
                "生成每日工作简报（汇总今日完成、明日计划、阻塞问题）",
                "# 每日工作简报\n\n1. 汇总今日完成事项（逐条列出）\n"
                "2. 列出明日计划\n3. 标注阻塞问题与需要的支持\n"
                "4. 输出为简洁的分点 Markdown 格式")
            yield {"type": "tool", "status": "end", "name": "create_skill", "result": result}
            answer = (f"已通过 `create_skill` 工具制作技能：\n\n**{result}**\n\n"
                      "技能以 SKILL.md 文件保存在 `skills/` 目录。真实模式下，模型看到任务匹配"
                      "技能描述时会自动调用 `load_skill` 获取完整指令后执行——即「制作即可用」。")
        elif any(k in user_text for k in ("加载", "使用", "内容", "看看", "查看")):
            yield {"type": "tool", "status": "start", "name": "load_skill"}
            content = await asyncio.to_thread(skills.load_skill, "weather-report")
            yield {"type": "tool", "status": "end", "name": "load_skill",
                   "result": (content or "技能不存在")[:300]}
            answer = (f"已通过 `load_skill` 加载技能 `weather-report` 的完整指令：\n\n"
                      f"```markdown\n{(content or '（技能不存在）')[:500]}\n```\n\n"
                      "（真实模式下：系统提示词只注入技能索引，模型判断任务匹配后才调用本工具拉取全文——"
                      "这就是「渐进式披露」，技能再多也不占上下文。）")
        else:
            yield {"type": "tool", "status": "start", "name": "list_skills_tool"}
            listing = await asyncio.to_thread(skills.list_skills)
            text = "\n".join(f"- **{s['name']}**：{s['description']}" for s in listing) or "（暂无技能）"
            yield {"type": "tool", "status": "end", "name": "list_skills_tool", "result": text}
            answer = (f"当前可用技能：\n\n{text}\n\n"
                      "可以继续试：「创建一个技能」体验制作流程，「加载 weather-report 技能」查看使用方式。")
    elif any(k in user_text for k in ("调研", "research", "多agent", "多智能体")):
        # 演示：orchestrator-worker 编排流程（Mock 下不真跑子图，只演示事件流）
        yield {"type": "tool", "status": "start", "name": "research_topic"}
        await asyncio.sleep(0.3)
        for city in ("北京", "上海", "成都"):
            tname, w = await _query_weather_any(city)
            if not tname:
                break
            yield {"type": "tool", "status": "start", "name": tname}
            yield {"type": "tool", "status": "end", "name": tname,
                   "result": w[:120]}
        demo_report = ("多 Agent 调研完成：orchestrator 拆分为 3 个子问题，"
                       "3 个 worker 并行调研后汇总。")
        yield {"type": "tool", "status": "end", "name": "research_topic",
               "result": demo_report}
        answer = (
            f"已通过 `research_topic` 触发**多 Agent 编排**（orchestrator-worker）：\n\n"
            f"**{demo_report}**\n\n"
            "流程：`plan`（orchestrator 拆题）→ `Send` 并行扇出 3 个 worker"
            "（各带安全工具子集，无危险能力）→ `aggregate`（汇总报告）。\n\n"
            "刚才流里 3 次天气工具调用就是 3 个 worker 各自查证的记录——"
            "Mock 模式为演示数据；真实模式下拆题、调研、汇总都由模型完成。")
    elif "工具" in user_text and any(k in user_text for k in ("创建", "新建", "制作", "写一个")):
        # 演示：对话式创建自定义工具（写可执行代码，属危险操作，先审批后落盘）
        demo_code = (
            'def dice_roll(sides: int = 6) -> str:\n'
            '    """掷一个 N 面骰子，返回 1 到 N 之间的随机结果。sides 为骰子面数，默认 6 面。"""\n'
            '    import random\n'
            '    n = max(2, int(sides))\n'
            '    return f"🎲 {n} 面骰子掷出了：{random.randint(1, n)}"\n')
        yield {"type": "tool", "status": "start", "name": "create_custom_tool"}
        aid = approval.create_approval(
            "create_custom_tool", {"name": "dice_roll", "code_preview": demo_code[:120]})
        yield {"type": "approval_required", "approval_id": aid,
               "tool": "create_custom_tool",
               "args": {"name": "dice_roll", "code_preview": demo_code[:120]}}
        decision = await approval.wait_decision(aid)
        if decision is True:
            result = await asyncio.to_thread(
                custom_tools.write_custom_tool, "dice_roll", demo_code)
            yield {"type": "tool", "status": "end",
                   "name": "create_custom_tool", "result": result}
            answer = (f"✅ 审批已通过，`create_custom_tool` 执行结果：{result}\n\n"
                      "自定义工具以 .py 文件保存在 `custom_tools/` 目录，"
                      "下一轮对话起模型就能直接调用它（免重启热加载）。\n"
                      "真实模式下由模型现场编写工具代码，Mock 模式这里用固定的骰子工具演示。")
        elif decision is False:
            yield {"type": "tool", "status": "end",
                   "name": "create_custom_tool", "result": "用户拒绝，未执行"}
            answer = ("❌ 你已**拒绝**创建该工具，未写入任何文件。\n\n"
                      "（写入可执行代码属于危险操作，fail-closed：拒绝/超时一律不落盘。）")
        else:
            answer = (f"⏱ 审批超时（{approval.APPROVAL_TIMEOUT} 秒未响应），"
                      "工具创建已自动取消，未写入任何文件。")
    elif any(k in user_text for k in ("天气", "weather", "气温", "下雨")):
        city = _extract_city(user_text)
        # 消息中带 "mcp" 字样时优先走 MCP 版工具（get_city_weather），否则优先走
        # weather-ops 领域包的 get_current_weather，方便在界面上对比两种扩展方式的调用链路
        prefer_mcp = "mcp" in user_text.lower()
        tool_name, weather = await _query_weather_any(city, prefer_mcp=prefer_mcp)
        if not tool_name:
            answer = (f"当前没有任何天气能力可查「{city}」：weather-ops 领域包未启用，"
                      "MCP 天气工具也不可用。\n\n"
                      "可以试试：「启用 weather-ops 包」（需审批）恢复天气能力——"
                      "这正是「通用内核 + 可插拔领域包」的能力边界演示。")
        else:
            yield {"type": "tool", "status": "start", "name": tool_name}
            with telemetry.trace_span(f"tool:{tool_name}", as_type="tool",
                                      input={"city": city}) as tspan:
                offline = weather.startswith("天气查询失败")
                if offline:
                    # 本机网络不通（内网演示环境）：降级为明确标注的离线演示数据
                    weather = (f"{city}当前天气：多云，气温 24°C，体感 26°C，湿度 65%，风速 12 km/h"
                               "（⚠️ 离线演示数据：本机无法访问天气接口，真实数据需配置可达的 "
                               "WEATHER_API_URL 或内部代理）")
                if tspan is not None:
                    try:
                        tspan.update(output=weather)
                    except Exception:
                        pass
            yield {"type": "tool", "status": "end", "name": tool_name, "result": weather}
            via = ("MCP Server 子进程（stdio）" if tool_name == "get_city_weather"
                   else "weather-ops 领域包（packs/ 目录，可整体禁用/替换）")
            note = ("Mock 模式演示，当前为**离线演示数据**（本机网络不通）。"
                    if offline else
                    "Mock 模式演示：工具返回的是真实 Open-Meteo 接口数据；接入 Qwen 后回答将由模型组织。")
            answer = (f"我调用了 `{tool_name}` 工具查询「{city}」，走的是**{via}**："
                      f"\n\n**{weather}**\n\n（{note}）")
    elif any(k in user_text for k in ("删除", "删掉", "delete", "remove")):
        # 危险操作演示：发起审批 → 等待前端决定 → 批准才执行（超时/拒绝一律不执行）
        filename = _extract_filename(user_text)
        aid = approval.create_approval("delete_workspace_file", {"path": filename})
        yield {"type": "approval_required", "approval_id": aid,
               "tool": "delete_workspace_file", "args": {"path": filename}}
        decision = await approval.wait_decision(aid)
        if decision is True:
            with telemetry.trace_span("tool:delete_workspace_file", as_type="tool",
                                      input={"path": filename}) as tspan:
                result = await asyncio.to_thread(do_delete, filename)
                if tspan is not None:
                    try:
                        tspan.update(output=result)
                    except Exception:
                        pass
            yield {"type": "tool", "status": "end",
                   "name": "delete_workspace_file", "result": result}
            answer = (f"✅ 审批已通过，`delete_workspace_file` 执行结果：{result}\n\n"
                      "（演示了「危险操作 → 人工审批 → 执行」完整链路。）")
        elif decision is False:
            answer = (f"❌ 你已**拒绝**删除「{filename}」，操作未执行。\n\n"
                      "（fail-closed：拒绝、超时、审批服务异常，一律不执行危险操作。）")
        else:
            answer = (f"⏱ 审批超时（{approval.APPROVAL_TIMEOUT} 秒未响应），"
                      f"删除「{filename}」已自动取消，未执行。")
    elif any(k in user_text for k in ("时间", "几点", "date", "time")):
        yield {"type": "tool", "status": "start", "name": "get_current_time"}
        await asyncio.sleep(0.4)
        # 时间工具自 v0.12.0 起来自平台包 core-utils（演示平台包与内核分层）
        if "get_current_time" in STATE.pack_tools:
            now = str(await asyncio.to_thread(
                STATE.pack_tools["get_current_time"].invoke, {}))
        else:
            from datetime import datetime
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "（平台包未加载，内核兜底）"
        yield {"type": "tool", "status": "end", "name": "get_current_time", "result": now}
        answer = (f"我调用了 `get_current_time` 工具，当前服务器时间是 **{now}**。\n\n"
                  "（该工具来自平台包 `core-utils`——跨领域通用能力；"
                  "Mock 模式的演示回复，接入真实模型后此回答将由 Qwen 生成。）")
    else:
        answer = (
            f"收到：「{user_text}」\n\n"
            "当前运行在 **Mock 模式**，尚未连接模型服务。你可以：\n"
            "1. 问我「现在几点」或「北京天气怎么样」查看工具调用链路；\n"
            "2. 输入「删除文件 test.txt」体验危险操作的人工审批流；\n"
            "3. 按 README 启动 vLLM 加载 Qwen 27B，把 `.env` 中 `MOCK_LLM` 改为 `false` 后重启，即可获得真实回答。"
        )
    for i in range(0, len(answer), 6):
        yield {"type": "token", "content": answer[i:i + 6]}
        await asyncio.sleep(0.015)
    yield {"type": "done"}


def _extract_filename(text: str) -> str:
    """Mock 模式简易文件名提取：去掉常见动词后剩下的即文件名，兜底 demo.txt。"""
    cleaned = text
    for w in ("删除", "删掉", "文件", "请", "帮我", "把", "了", "一下",
              "delete", "remove", "？", "?", "，", ",", "。", " ", "　"):
        cleaned = cleaned.replace(w, "")
    return cleaned or "demo.txt"


def _tool_result_text(raw) -> str:
    """兼容 MCP 工具返回的 content-block 列表与纯字符串。"""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, dict) and "text" in item:
                parts.append(item["text"])
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(raw)


def _extract_city(text: str) -> str:
    """Mock 模式的简易城市提取：去掉常见询问词后剩下的即城市名，兜底「北京」。
    （真实模式下城市参数由模型从对话中解析，不经过这里。）"""
    import re

    cleaned = text
    for w in ("查询", "一下", "现在", "今天", "目前", "当前", "天气", "气温",
              "怎么样", "如何", "下雨", "吗", "呢", "的", "weather",
              "MCP", "mcp", "使用",
              "？", "?", "，", ",", "。", " ", "　", "告诉我", "我想知道", "请", "帮我", "看看"):
        cleaned = cleaned.replace(w, "")
    cleaned = re.sub(r"^(在|于|给|查|问|用|走)+", "", cleaned)
    return cleaned or "北京"
