"""真实 LLM（DeepSeek）链路集成测试。

默认跳过；启用条件（.env 配置可用的 DeepSeek 密钥）：
    $env:RUN_LLM_TESTS="1"
    & .venv\\Scripts\\python.exe -B -m pytest tests/test_integration_llm.py -q

覆盖离线 mock 无法触达的真实链路：
- 真实模式 build_agent：_ReasoningChatOpenAI 装配 + Agent 图 + MemorySaver
- DeepSeek 推理模型 reasoning_content 透传（reasoning 事件，agent.py 私有转换器）
- 真实对话流式事件契约（token → done.usage 任务/会话双口径计量）
- 真实工具调用（模型自主调 read_text_file 读工作区文件并引用其内容）
"""
import asyncio
import os
import uuid

import pytest

from server import agent, config
from server.state import STATE

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LLM_TESTS") != "1",
    reason="真实 LLM 集成测试：设 RUN_LLM_TESTS=1 且 .env 配置可用的模型密钥后运行",
)


@pytest.fixture(scope="module", autouse=True)
def real_llm_agent():
    """模块级：真实模式装配 Agent（仅构造对象与图，不发起 API 调用）。"""
    if not config.LLM_API_KEY:
        pytest.skip("config.LLM_API_KEY 为空：.env 未配置模型密钥")
    config.MOCK_LLM = False
    asyncio.run(agent.build_agent())
    assert STATE.agent is not None and STATE.llm is not None
    yield
    # 还原 mock 模式与空 STATE（与离线测试的默认假设一致，不污染同进程后续用例）
    config.MOCK_LLM = True
    STATE.llm = STATE.agent = None
    STATE.checkpointer = None
    STATE.checkpointer_cm = None
    if hasattr(STATE, "system_prompt_static"):
        delattr(STATE, "system_prompt_static")


def _chat(content: str, session_id: str, run_id: str = "") -> list[dict]:
    """跑一轮真实对话并收集全部 SSE 事件。"""
    async def _run():
        return [ev async for ev in agent.stream_reply(
            [{"role": "user", "content": content}],
            session_id=session_id, user_id="it-llm", run_id=run_id)]
    return asyncio.run(_run())


def test_real_chat_stream_and_session_usage():
    """真实对话：token 流式透出，done.usage 会话口径有真实 token 计量。"""
    sid = f"it-chat-{uuid.uuid4().hex[:8]}"
    events = _chat("只回答两个字：好的", sid)
    kinds = [e["type"] for e in events]
    assert "token" in kinds and "error" not in kinds
    assert kinds[-1] == "done"
    session = events[-1]["usage"]["session"]
    assert session["calls"] >= 1 and session["total_tokens"] > 0


def test_real_run_usage_scope():
    """带 run_id 的调用：done.usage 的任务（run）口径来自 DeepSeek 真实用量。"""
    rid = f"run-{uuid.uuid4().hex[:8]}"
    events = _chat("只回答三个字：知道了", f"it-run-{uuid.uuid4().hex[:8]}", run_id=rid)
    run = events[-1]["usage"]["run"]
    assert run["calls"] >= 1 and run["total_tokens"] > 0


def test_real_reasoning_passthrough():
    """DeepSeek 推理模型：reasoning_content 经 _ReasoningChatOpenAI 透传为
    reasoning 事件（思维链不进 token 流，agent.py 146-154 的离线盲区）。"""
    events = _chat("9.11 和 9.9 哪个大？一句话说明理由。",
                   f"it-rs-{uuid.uuid4().hex[:8]}")
    kinds = [e["type"] for e in events]
    assert "reasoning" in kinds, "推理模型应返回思维链（reasoning 事件缺失）"
    assert all(e["content"] for e in events if e["type"] == "reasoning")
    assert "token" in kinds and kinds[-1] == "done"


def test_real_tool_call_read_text_file():
    """真实工具调用：模型经 read_text_file 读工作区文件并引用其内容。"""
    marker = f"IT-MARK-{uuid.uuid4().hex[:8]}"
    (config.TOOL_WORKSPACE / "integration_note.txt").write_text(
        f"验证码：{marker}", encoding="utf-8")
    events = _chat(
        "请用 read_text_file 工具读取 integration_note.txt，"
        "然后原样告诉我文件里的验证码。",
        f"it-tool-{uuid.uuid4().hex[:8]}")
    tool_evs = [(e["name"], e["status"]) for e in events if e["type"] == "tool"]
    assert ("read_text_file", "start") in tool_evs
    assert ("read_text_file", "end") in tool_evs
    tokens = "".join(e["content"] for e in events if e["type"] == "token")
    assert marker in tokens, "模型应把工具读到的验证码带回答案"
    assert [e["type"] for e in events][-1] == "done"
