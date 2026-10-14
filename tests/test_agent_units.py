"""agent.py 单元：token 估算 / 切割成对完整性 / pre_model_hook 三分支 / 图装配去重。

Mock 模式（conftest 强制 MOCK_LLM=true）下 _rebuild_graph 在登记工具名后早退，
不依赖任何模型；压缩参数经 monkeypatch config 常量收缩到小数值便于构造。
"""
import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from server import agent, config, custom_tools, packs, tools_admin
from server.state import STATE


@pytest.fixture(autouse=True)
def _restore_state():
    """保存/恢复 STATE 单例相关字段与压缩缓存，测试间零串扰。"""
    keys = ("mcp_tool_list", "mcp_tools", "pack_tools", "tool_names",
            "custom_sig", "system_prompt_static", "agent", "llm")
    saved = {k: getattr(STATE, k, None) for k in keys}
    saved_cache = dict(agent._compact_cache)
    yield
    for k, v in saved.items():
        setattr(STATE, k, v)
    agent._compact_cache.clear()
    agent._compact_cache.update(saved_cache)


def _patch_ctx(monkeypatch, max_tokens=200, ratio=0.6, recent=30):
    """把压缩参数收缩到小数值：触发线 = 200×0.6 = 120 tokens，近窗 30。"""
    monkeypatch.setattr(config, "CONTEXT_MAX_TOKENS", max_tokens)
    monkeypatch.setattr(config, "CONTEXT_COMPACT_RATIO", ratio)
    monkeypatch.setattr(config, "CONTEXT_RECENT_TOKENS", recent)


# ---------------------------------------------------------------- _est_tokens

def test_est_tokens_cjk():
    # 100 个中日韩字符 ≈ 1 token/字 + 8 壳开销
    assert agent._est_tokens(HumanMessage("字" * 100)) == 108


def test_est_tokens_ascii():
    # 其余字符 ≈ 4 字符/token：100//4 + 8
    assert agent._est_tokens(HumanMessage("a" * 100)) == 33


def test_est_tokens_empty():
    assert agent._est_tokens(HumanMessage("")) == 8


def test_est_tokens_counts_reasoning_content():
    # 思维链（reasoning_content）同样占用窗口，必须一并计入（v0.17.7）
    msg = AIMessage("答", additional_kwargs={"reasoning_content": "思" * 10})
    assert agent._est_tokens(msg) == 11 + 8   # 11 个 CJK 字 + 壳


# ---------------------------------------------------------------- _find_cut

def _seq():
    """[H(48), AI(10), Tool(10), H(48)] tokens——覆盖切割点悬空场景。"""
    return [HumanMessage("字" * 40),
            AIMessage("字" * 2),
            ToolMessage("字" * 2, tool_call_id="t1"),
            HumanMessage("字" * 40)]


def test_find_cut_tool_pair_integrity():
    # budget=58：尾部累计 H2(48)+Tool(10)=58 装下，下一条 AI 会超 → 切割点
    # 落在 Tool 上 → 必须前移到 AI 之前（否则 ToolMessage 悬空导致 API 400）
    assert agent._find_cut(_seq(), 58) == 3


def test_find_cut_keeps_pair_when_possible():
    # budget=110：能装下 [AI, Tool, H2] 三条 → 切割点在 AI 上（非 tool），不前移
    assert agent._find_cut(_seq(), 110) == 1


def test_find_cut_all_fits():
    assert agent._find_cut(_seq(), 200) == 0


def test_find_cut_always_keeps_last():
    # budget 只够 0 条时也强制保留最后一条（i==len 分支不 break）
    assert agent._find_cut(_seq(), 1) == 3


# ---------------------------------------------------------------- _pre_model_hook

def _hook(msgs, thread_id="s1"):
    return asyncio.run(agent._pre_model_hook(
        {"messages": msgs}, {"configurable": {"thread_id": thread_id}}))["llm_input_messages"]


def test_hook_below_trigger_keeps_all(monkeypatch):
    # 无摘要分支：总量 ≤ 触发线（120）时全量保留（8k~触发线区间不裁丢历史）
    _patch_ctx(monkeypatch)
    msgs = [HumanMessage("字" * 30) for _ in range(3)]      # 3×38=114 ≤ 120
    view = _hook(msgs)
    assert len(view) == 3
    assert [m.content for m in view] == [m.content for m in msgs]


def test_hook_above_trigger_trims_to_recent(monkeypatch):
    # 无摘要分支：总量 192 > 120 → 裁到近窗预算 30（至少保留最后一条）
    _patch_ctx(monkeypatch)
    msgs = [HumanMessage("字" * 40) for _ in range(4)]       # 4×48=192
    view = _hook(msgs)
    assert len(view) == 1
    assert view[0].content == msgs[-1].content


def test_hook_summary_branch_injects_summary(monkeypatch):
    # 摘要分支：裁掉已摘要前缀（count=2），注入摘要 SystemMessage；尾部不超触发线不裁
    _patch_ctx(monkeypatch)
    agent._compact_cache["s1"] = {"summary": "任务目标摘要A", "count": 2}
    msgs = [HumanMessage("字" * 5) for _ in range(6)]       # 每条 13 tokens
    view = _hook(msgs)
    assert isinstance(view[0], SystemMessage)
    assert "任务目标摘要A" in view[0].content
    assert len(view) == 1 + (6 - 2)                          # System + 尾部 4 条


def test_hook_summary_branch_trims_tail_by_trigger(monkeypatch):
    # 摘要分支：尾部超触发线时按触发线预算裁（尾部预算是触发线而非近窗——
    # 尾部是摘要未覆盖的新内容，误裁会丢）
    _patch_ctx(monkeypatch)
    agent._compact_cache["s1"] = {"summary": "旧摘要", "count": 2}
    msgs = [HumanMessage("字" * 40) for _ in range(6)]      # 尾部 4×48=192 > 120
    view = _hook(msgs)
    assert isinstance(view[0], SystemMessage)
    assert len(view) == 3                                    # System + 尾部 2 条（96≤120）


# ---------------------------------------------------------------- _rebuild_graph

class _FakeTool:
    def __init__(self, name):
        self.name = name


def test_rebuild_graph_tool_dedup(monkeypatch, capsys):
    """同名工具先挂载者优先（内置 > MCP > 自定义 > 领域包），后挂载者跳过并告警；
    被冲突跳过的 pack 工具不进 pack_tools 登记表。"""
    monkeypatch.setattr(tools_admin, "builtin_langchain_tools", lambda: [])
    monkeypatch.setattr(custom_tools, "load_langchain_tools",
                        lambda: [_FakeTool("clash")])
    monkeypatch.setattr(packs, "load_pack_tools",
                        lambda: [_FakeTool("clash"), _FakeTool("pack_only")])
    STATE.mcp_tool_list = [_FakeTool("clash")]               # MCP 先于自定义
    agent._rebuild_graph()
    assert STATE.tool_names.count("clash") == 1             # 三方同名只保留一份
    assert "pack_only" in STATE.tool_names
    assert "clash" not in STATE.pack_tools                   # pack 版 clash 被跳过
    assert "pack_only" in STATE.pack_tools
    out = capsys.readouterr().out
    assert "工具名冲突" in out and "clash" in out
    assert STATE.custom_sig is not None


def test_rebuild_graph_registers_state(monkeypatch):
    """正常装配：工具名清单与重载签名登记；mock 模式不创建 agent 图。"""
    monkeypatch.setattr(tools_admin, "builtin_langchain_tools",
                        lambda: [_FakeTool("builtin_tool")])
    monkeypatch.setattr(custom_tools, "load_langchain_tools", lambda: [])
    monkeypatch.setattr(packs, "load_pack_tools", lambda: [])
    STATE.mcp_tool_list = []
    agent._rebuild_graph()
    assert "builtin_tool" in STATE.tool_names
    assert STATE.pack_tools == {}
    # MOCK_LLM=true：签名登记后早退，不触碰 LangGraph 装配
    assert STATE.agent is None
