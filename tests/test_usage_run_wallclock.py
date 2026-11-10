"""6.8 run 墙钟时长（口径 B）：run_records 落账时机、终态、取消、迁移与视图。

用例编号对应 docs/specs/6.8-run-wallclock-duration.md §5（AC1–AC10、AC12）。
本文件只写测试，不实现 record_run / run_records；红态原因应与 §5 预期一致。
"""
import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from server import agent, approval, config, session_lock, usage
from server.state import STATE


# ---------------------------------------------------------------- 公共装置


@pytest.fixture(autouse=True)
def _restore_state():
    """STATE 单例与 agent 内存态测试间复位（沿用 test_agent_llm 先例）。"""
    saved = (STATE.llm, STATE.agent, STATE.checkpointer, STATE.checkpointer_cm)
    saved_locks = dict(agent._session_locks)
    saved_cache = dict(agent._compact_cache)
    yield
    (STATE.llm, STATE.agent, STATE.checkpointer, STATE.checkpointer_cm) = saved
    agent._session_locks.clear()
    agent._session_locks.update(saved_locks)
    agent._compact_cache.clear()
    agent._compact_cache.update(saved_cache)


class _StreamAgent:
    """按脚本回放 astream 事件；aget_state 无中断（同 test_agent_llm）。"""

    def __init__(self, events):
        self.events = events

    async def astream_events(self, payload, config=None, version="v2"):
        for ev in self.events:
            yield ev

    async def aget_state(self, cfg):
        return SimpleNamespace(tasks=[])


class _FailAgent:
    async def astream_events(self, payload, config=None, version="v2"):
        raise RuntimeError("C:\\abs\\path 与密钥细节")
        yield                                           # pragma: no cover

    async def aget_state(self, cfg):
        return SimpleNamespace(tasks=[])


class _IntrAgent:
    """第一轮跑完报审批中断；resume 后的第二轮正常结束（同 test_agent_llm）。"""

    def __init__(self, events1, events2):
        self.stage = 0
        self._events = [events1, events2]

    async def astream_events(self, payload, config=None, version="v2"):
        for ev in self._events[self.stage]:
            yield ev
        self.stage += 1

    async def aget_state(self, cfg):
        if self.stage == 1:
            intr = SimpleNamespace(value={"kind": "approval",
                                           "tool": "delete_workspace_file",
                                           "args": {"filename": "x.txt"}})
            return SimpleNamespace(tasks=[SimpleNamespace(interrupts=[intr])])
        return SimpleNamespace(tasks=[])


def _tok(text: str) -> dict:
    return {"event": "on_chat_model_stream", "tags": [],
            "data": {"chunk": AIMessageChunk(content=text)}}


def _install(monkeypatch, fake_agent):
    monkeypatch.setattr(config, "MOCK_LLM", False)
    STATE.llm = None
    STATE.agent = fake_agent
    agent._rebuild_graph()


def _collect(agen):
    async def _run():
        return [ev async for ev in agen]
    return asyncio.run(_run())


def _clock(values):
    """固定单调时钟序列；末值重复，避免实现多读一次时 StopIteration。"""
    seq = list(values)

    def _next():
        return seq.pop(0) if len(seq) > 1 else seq[0]
    return _next


def _rows(run_id: str) -> list[dict]:
    """直读 run_records；表不存在时返回空，让断言以「0 行」红掉。"""
    db = Path(config.USAGE_DB)
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM run_records WHERE run_id = ?", (run_id,))]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def _count_run_rows() -> int:
    conn = sqlite3.connect(str(config.USAGE_DB))
    try:
        return conn.execute("SELECT COUNT(*) FROM run_records").fetchone()[0]
    finally:
        conn.close()


def _count_run_rows_or_zero() -> int:
    """守护类用例（AC9）：表缺失等价于「没有行」，不得因此判红。"""
    try:
        return _count_run_rows()
    except sqlite3.OperationalError:
        return 0


def _usage_rows(run_id: str) -> list[dict]:
    return usage._query(
        "SELECT * FROM usage_records WHERE run_id = ? ORDER BY id", (run_id,))


def _wait_rows(run_id: str, timeout: float = 2.0) -> list[dict]:
    """取消/断连的落账在线程池内完成，允许短暂轮询。"""
    import time
    deadline = time.time() + timeout
    rows = _rows(run_id)
    while not rows and time.time() < deadline:
        time.sleep(0.02)
        rows = _rows(run_id)
    return rows


# ---------------------------------------------------------------- AC1 正常终态


def test_ac1_record_committed_before_done(monkeypatch):
    """done 事件产出的那一刻 run_records 已提交；零 usage 行也能取到墙钟。"""
    _install(monkeypatch, _StreamAgent([_tok("答")]))

    async def scenario():
        seen = None
        async for ev in agent.stream_reply(
                [{"role": "user", "content": "你好"}],
                session_id="s-ac1", user_id="alice", run_id="r-ac1"):
            if ev["type"] == "done":
                seen = (ev, _rows("r-ac1"))
        return seen

    done, rows = asyncio.run(scenario())
    assert len(rows) == 1, "done 产出时 run_records 应已提交 1 行"
    row = rows[0]
    assert row["status"] == "ok"
    for key in ("started_at", "finished_at", "wall_ms"):
        assert row[key] is not None, f"{key} 不应为空"
    assert row["finished_at"] >= row["started_at"]
    agg = usage.run_usage("r-ac1")
    assert agg["calls"] == 0, "_StreamAgent 不触发 UsageCallback"
    assert agg["wall_ms"] is not None, "墙钟须独立取自 run_records"
    assert agg["wall_ms"] == row["wall_ms"]
    assert done["usage"]["run"]["wall_ms"] == row["wall_ms"]


# ---------------------------------------------------------------- AC2 失败终态


def test_ac2_record_committed_before_error(monkeypatch):
    """error 事件产出的那一刻已落 status=error；文案仍脱敏、无 done。"""
    _install(monkeypatch, _FailAgent())

    async def scenario():
        seen = None
        async for ev in agent.stream_reply(
                [{"role": "user", "content": "你好"}],
                session_id="s-ac2", user_id="alice", run_id="r-ac2"):
            if ev["type"] == "error":
                seen = (ev, _rows("r-ac2"))
        return seen

    err, rows = asyncio.run(scenario())
    assert "密钥细节" not in err["content"]
    assert len(rows) == 1, "error 产出时 run_records 应已提交 1 行"
    row = rows[0]
    assert row["status"] == "error"
    for key in ("started_at", "finished_at", "wall_ms"):
        assert row[key] is not None


class _Boom(BaseException):
    """非 GeneratorExit/CancelledError 的 BaseException：模拟未走终态捕获点的异常退出。"""


def test_ac2b_uncaught_base_exception_records_error(monkeypatch):
    """兜底分支：BaseException 落到错误终态并落 1 行 error，且原样上抛。"""
    async def boom(_session_id):
        raise _Boom("unexpected terminal")

    monkeypatch.setattr(agent, "prepare_compaction", boom)
    _install(monkeypatch, _StreamAgent([_tok("答")]))
    with pytest.raises(_Boom):
        _collect(agent.stream_reply(
            [{"role": "user", "content": "你好"}],
            session_id="s-ac2b", user_id="alice", run_id="r-ac2b"))
    rows = _rows("r-ac2b")
    assert len(rows) == 1, "未捕获异常只允许落 1 行，不得重复"
    assert rows[0]["status"] == "error"
    for key in ("started_at", "finished_at", "wall_ms"):
        assert rows[0][key] is not None


# ---------------------------------------------------------------- AC3 跨审批轮


def test_ac3_multi_round_single_record(monkeypatch):
    """两轮审批 resume 仍只有 1 行，墙钟覆盖两轮。"""
    _install(monkeypatch, _IntrAgent([_tok("准备删除")], [_tok("已执行")]))
    monkeypatch.setattr(usage, "_mono", _clock([100.0, 106.0]))

    async def fake_wait(aid, timeout=approval.APPROVAL_TIMEOUT):
        approval.resolve(aid, True, "boss", "approver")
        return True

    monkeypatch.setattr(approval, "wait_decision", fake_wait)
    out = _collect(agent.stream_reply(
        [{"role": "user", "content": "删除文件"}],
        session_id="s-ac3", user_id="alice", run_id="r-ac3"))
    assert out[-1]["type"] == "done"
    rows = _rows("r-ac3")
    assert len(rows) == 1, "同一 run 跨审批轮只允许 1 行"
    assert rows[0]["status"] == "ok"
    assert rows[0]["wall_ms"] == 6000, "墙钟须覆盖两轮，而不是只覆盖单轮"


def test_ac3b_second_terminal_write_deduped(monkeypatch):
    """ok 已落账后 done 负载再抛错：error 兜底被 recorded 去重，仍只有 1 行 ok。"""
    _install(monkeypatch, _StreamAgent([_tok("答")]))

    def boom(_run_id):
        raise RuntimeError("aggregate failed")

    monkeypatch.setattr(usage, "run_usage", boom)
    with pytest.raises(RuntimeError):
        _collect(agent.stream_reply(
            [{"role": "user", "content": "你好"}],
            session_id="s-ac3b", user_id="alice", run_id="r-ac3b"))
    rows = _rows("r-ac3b")
    assert len(rows) == 1, "第二次终态落账须被 recorded 去重，不得产生第二行"
    assert rows[0]["status"] == "ok", "首个终态生效，后到的 error 不得覆盖"


# ---------------------------------------------------------------- AC4 编排子调用


def test_ac4_orchestration_children_not_separate_runs(monkeypatch):
    """父 run 内执行编排：子调用 run_id 均为父 run，run_records 仍只有父一行。"""
    from langgraph import prebuilt

    definition = {
        "name": "ac4-demo", "version": "1",
        "roles": [{"id": "r", "prompt": "执行", "tools": []}],
        "steps": [{"id": "s", "role": "r", "mode": "single"}],
    }

    class FakeAgent:
        def __init__(self, llm, tools):
            self.tools = list(tools)

        async def ainvoke(self, payload, config=None):
            msg = AIMessage(content="ok")
            msg.usage_metadata = {"input_tokens": 3, "output_tokens": 2,
                                  "total_tokens": 5}
            msg.response_metadata = {"model_name": "fake-model"}
            usage.UsageCallback().handler.on_llm_end(
                SimpleNamespace(generations=[[SimpleNamespace(message=msg)]]),
                run_id="orch-step",
                tags=list((config or {}).get("tags") or []))
            return {"messages": [msg]}

    monkeypatch.setattr(prebuilt, "create_react_agent", FakeAgent)
    monkeypatch.setattr(config, "CUSTOM_TOOLS_EXEC", "local")  # 真实模式默认 sandbox，测试显式本地

    class _FakeLLM:
        """聚合阶段直调 llm.ainvoke；同样按当前上下文落 usage 行。"""

        async def ainvoke(self, messages, config=None, **kw):
            message = AIMessage(content="汇总")
            message.usage_metadata = {"input_tokens": 2, "output_tokens": 1,
                                      "total_tokens": 3}
            message.response_metadata = {"model_name": "fake-model"}
            usage.UsageCallback().handler.on_llm_end(
                SimpleNamespace(generations=[[SimpleNamespace(
                    message=message)]]),
                run_id="orch-agg",
                tags=list((config or {}).get("tags") or []))
            return AIMessage(content="汇总")

    class _OrchAgent:
        async def astream_events(self, payload, config=None, version="v2"):
            from server import orchestration as orch
            identity = usage.Identity(
                username=usage.current_user.get(), role="user",
                department="", auth_source="test")
            result = await orch.execute(
                definition, "任务", identity=identity,
                session_id=usage.current_session.get(),
                run_id=usage.current_run.get(), llm=_FakeLLM())
            assert result["ok"] is True, result.get("error")
            yield _tok("编排完成")

        async def aget_state(self, cfg):
            return SimpleNamespace(tasks=[])

    _install(monkeypatch, _OrchAgent())
    out = _collect(agent.stream_reply(
        [{"role": "user", "content": "跑编排"}],
        session_id="s-ac4", user_id="alice", run_id="r-ac4"))
    assert out[-1]["type"] == "done"
    usage_rows = _usage_rows("r-ac4")
    assert usage_rows, "编排子调用应产生 usage 行"
    assert {r["run_id"] for r in usage_rows} == {"r-ac4"}
    rows = _rows("r-ac4")
    assert len(rows) == 1
    assert rows[0]["run_id"] == "r-ac4"
    assert rows[0]["wall_ms"] is not None


# ---------------------------------------------------------------- AC5 老库迁移


_LEGACY_SCHEMA = """
CREATE TABLE usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL, date TEXT NOT NULL, user_id TEXT NOT NULL,
    session_id TEXT NOT NULL, run_id TEXT NOT NULL, model TEXT NOT NULL,
    source TEXT NOT NULL, prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL, total_tokens INTEGER NOT NULL,
    prompt_cost REAL NOT NULL DEFAULT 0,
    completion_cost REAL NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok')
"""


def test_ac5_legacy_db_migration_and_null_wall():
    """老库自动建 run_records 且幂等；老 run 的墙钟四键为 None。"""
    db = Path(config.USAGE_DB)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute(_LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO usage_records (ts, date, user_id, session_id, run_id,"
        " model, source, prompt_tokens, completion_tokens, total_tokens)"
        " VALUES (0, '2026-10-01', 'old', 'olds', 'oldr', 'oldm', 'main',"
        " 100, 200, 300)")
    conn.commit()
    conn.close()

    usage._query("SELECT 1")          # 触发建表迁移
    usage._query("SELECT 1")          # 幂等：重复执行不报错
    cols = {r["name"] for r in usage._query(
        "PRAGMA table_info(run_records)")}
    assert {"run_id", "user_id", "session_id", "started_at", "finished_at",
            "wall_ms", "status"} <= cols

    agg = usage.run_usage("oldr")
    assert agg["calls"] == 1
    assert agg["total_tokens"] == 300
    for key in ("wall_ms", "started_at", "finished_at", "status"):
        assert key in agg, f"run_usage 缺键 {key}"
        assert agg[key] is None, f"老 run 的 {key} 应为 None"

    by_run = [r for r in usage.summary()["by_run"] if r["run_id"] == "oldr"]
    assert len(by_run) == 1
    assert "wall_ms" in by_run[0] and by_run[0]["wall_ms"] is None
    assert by_run[0]["total_tokens"] == 300


# ---------------------------------------------------------------- AC6 by_run SQL


def test_ac6_by_run_alias_and_filters():
    """四种过滤分支均不报歧义；新 run 有墙钟、老 run 为 None，聚合口径不变。"""
    usage.record("m", {"input_tokens": 10, "output_tokens": 5},
                 duration_ms=300, source="main")
    usage.current_run.set("new-run")
    usage.record("m", {"input_tokens": 10, "output_tokens": 5},
                 duration_ms=300, source="main")
    usage.record_run("new-run", "alice", "s-new", 100.0, 102.5, 2500, "ok")

    usage.current_run.set("old-run")
    usage.record("m", {"input_tokens": 1, "output_tokens": 1},
                 duration_ms=100, source="main")

    for kwargs in ({}, {"user_id": "alice"}, {"session_id": "s-new"},
                   {"days": 1}):
        summary = usage.summary(**kwargs)          # 不得抛 ambiguous column name
        assert "by_run" in summary

    rows = {r["run_id"]: r for r in usage.summary()["by_run"]}
    assert rows["new-run"]["wall_ms"] == 2500
    assert rows["old-run"]["wall_ms"] is None
    assert rows["new-run"]["duration_ms"] == 300
    assert rows["new-run"]["failed_calls"] == 0
    assert rows["new-run"]["calls"] == 1


# ---------------------------------------------------------------- AC7 写库红线


def test_ac7_record_failure_never_blocks_stream(monkeypatch, caplog):
    """record_run 抛错不得影响 done；日志只含类型名；恰好尝试 1 次。"""
    calls = []

    def boom(*a, **kw):
        calls.append(1)
        raise RuntimeError("db down secret-path-/tmp/x")

    monkeypatch.setattr(usage, "record_run", boom, raising=False)
    _install(monkeypatch, _StreamAgent([_tok("答")]))
    with caplog.at_level(logging.WARNING, logger="server.agent"):
        out = _collect(agent.stream_reply(
            [{"role": "user", "content": "你好"}],
            session_id="s-ac7", user_id="alice", run_id="r-ac7"))
    assert out[-1]["type"] == "done"
    assert len(calls) == 1, "写失败后恰好尝试 1 次，不得在 finally 重试"
    messages = [r.getMessage() for r in caplog.records]
    assert any("RuntimeError" in m for m in messages), "落账失败须记异常类型名"
    assert all("secret-path" not in m and "db down" not in m for m in messages), \
        "日志不得回显异常文本"
    assert all("secret-path" not in e.get("content", "") for e in out)


# ---------------------------------------------------------------- AC8 取消


class _SlowAgent:
    """首个 token 后挂起，留出断连窗口。"""

    async def astream_events(self, payload, config=None, version="v2"):
        yield _tok("首")
        await asyncio.sleep(0.3)
        yield _tok("尾")

    async def aget_state(self, cfg):
        return SimpleNamespace(tasks=[])


def _asgi_scope(headers: list[tuple[bytes, bytes]]) -> dict:
    return {
        "type": "http", "asgi": {"version": "3.0"},
        "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/api/chat", "raw_path": b"/api/chat", "query_string": b"",
        "root_path": "", "headers": headers,
        "client": ("testclient", 50000), "server": ("testserver", 80),
        "state": {},
    }


def _sse_run_id(chunks: list[bytes]) -> str:
    for body in chunks:
        for line in body.decode("utf-8", "ignore").splitlines():
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except ValueError:
                continue
            if ev.get("type") == "session":
                return ev.get("run_id") or ""
    return ""


def test_ac8a_chat_disconnect_cancelled(make_user, monkeypatch):
    """端点级断连：首个 stream_reply 事件后断开，落 1 行 cancelled。

    说明：下载的 TestClient/httpx ASGITransport 会缓冲整包响应（见
    test_session_delete_concurrency 的注释），无法在流中途关闭；这里直接调用
    ASGI app 并在首个 token/tool/approval_required 响应块后回 http.disconnect，
    仍走完整的 /api/chat 路由、认证中间件与 StreamingResponse 取消链路。
    """
    from server.main import app

    key = make_user("alice")
    _install(monkeypatch, _SlowAgent())
    # AC8 的 `wall_ms>0` 由 usage._mono 替身保证（spec §4 AC8）：真实时钟下
    # 断连可能落在同一毫秒，int 截断后为 0，属计时分辨率而非实现缺陷。
    monkeypatch.setattr(usage, "_mono", _clock([100.000, 100.500]))
    body = json.dumps({"message": "你好"}).encode()
    headers = [(b"host", b"testserver"),
               (b"content-type", b"application/json"),
               (b"content-length", str(len(body)).encode()),
               (b"authorization", f"Bearer {key}".encode())]

    async def scenario():
        chunks: list[bytes] = []
        first_event = asyncio.Event()
        disconnect = asyncio.Event()
        request_sent = False

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body,
                        "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                payload = message.get("body", b"")
                chunks.append(payload)
                if any(f'"type": "{t}"' in payload.decode("utf-8", "ignore")
                       for t in ("token", "tool", "approval_required")):
                    first_event.set()
                    disconnect.set()

        await asyncio.wait_for(
            app(_asgi_scope(headers), receive, send), timeout=10)
        return chunks, first_event.is_set()

    chunks, saw_event = asyncio.run(scenario())
    assert saw_event, "未观测到 stream_reply 产生的首个事件"
    run_id = _sse_run_id(chunks)
    assert run_id, "session 帧未带 run_id"
    rows = _wait_rows(run_id)
    assert len(rows) == 1, "断连应落且仅落 1 行"
    assert rows[0]["status"] == "cancelled"
    for k in ("started_at", "finished_at", "wall_ms"):
        assert rows[0][k] is not None
    assert rows[0]["wall_ms"] > 0
    ok_rows = [r for r in _rows(run_id) if r["status"] == "ok"]
    assert ok_rows == []


class _TakeoverLease:
    """第一次检查 lost=False（放行首事件），之后恒 True（租约被接管）。"""

    def __init__(self, session_id, owner, **kw):
        self.session_id = session_id
        self._checks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def lost(self):
        self._checks += 1
        return self._checks > 1


def test_ac8b_lease_takeover_cancelled(client_factory, make_user, monkeypatch):
    """租约接管：路由中止后仍落 1 行 cancelled，不产生 ok 行。"""
    key = make_user("alice")
    _install(monkeypatch, _StreamAgent([_tok("一"), _tok("二")]))
    monkeypatch.setattr(usage, "_mono", _clock([100.000, 100.500]))
    monkeypatch.setattr(session_lock, "SessionLease", _TakeoverLease)
    with client_factory() as api:
        r = api.post("/api/chat", headers={"Authorization": f"Bearer {key}"},
                     json={"message": "你好"})
    assert r.status_code == 200
    blocks = [line.encode() for line in r.text.splitlines(True)
              if line.startswith("data: ")]
    run_id = _sse_run_id(blocks)
    assert run_id
    assert any('"type": "error"' in b.decode() for b in blocks)
    rows = _wait_rows(run_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "cancelled"
    assert rows[0]["wall_ms"] > 0
    assert [x for x in rows if x["status"] == "ok"] == []


def test_ac8c_direct_aclose(monkeypatch):
    """补充：直接 aclose 内层生成器也须落 cancelled。"""
    _install(monkeypatch, _StreamAgent([_tok("一"), _tok("二")]))

    async def scenario():
        agen = agent.stream_reply(
            [{"role": "user", "content": "你好"}],
            session_id="s-ac8c", user_id="alice", run_id="r-ac8c")
        first = await agen.__anext__()
        assert first["type"] == "token"
        await agen.aclose()

    asyncio.run(scenario())
    rows = _wait_rows("r-ac8c")
    assert len(rows) == 1
    assert rows[0]["status"] == "cancelled"


# ---------------------------------------------------------------- AC9 Mock 守卫


def test_ac9_mock_mode_no_run_record(monkeypatch):
    """Mock 流不写 run_records、不伪造墙钟（实现前即应绿：守护断言）。"""
    monkeypatch.setattr(config, "MOCK_LLM", True)
    out = _collect(agent.stream_reply(
        [{"role": "user", "content": "你好"}],
        session_id="s-ac9", user_id="alice", run_id="r-ac9"))
    assert out[-1]["type"] == "done"
    assert _count_run_rows_or_zero() == 0
    assert _usage_rows("r-ac9") == []
    assert not out[-1].get("usage", {}).get("run", {}).get("wall_ms")


# ---------------------------------------------------------------- AC10 审批等待


def test_ac10_approval_wait_exact_wallclock(monkeypatch):
    """固定时钟 [100.000, 101.250] → wall_ms=1250（int 截断），模型段仍 200。"""
    _install(monkeypatch, _IntrAgent([_tok("准备")], [_tok("完成")]))
    monkeypatch.setattr(usage, "_mono", _clock([100.000, 101.250]))

    async def fake_wait(aid, timeout=approval.APPROVAL_TIMEOUT):
        approval.resolve(aid, True, "boss", "approver")
        return True

    monkeypatch.setattr(approval, "wait_decision", fake_wait)
    usage.current_user.set("alice")
    usage.current_session.set("s-ac10")
    usage.current_run.set("r-ac10")
    usage.record("m", {"input_tokens": 10, "output_tokens": 5},
                 duration_ms=200)

    out = _collect(agent.stream_reply(
        [{"role": "user", "content": "删除文件"}],
        session_id="s-ac10", user_id="alice", run_id="r-ac10"))
    assert out[-1]["type"] == "done"
    rows = _rows("r-ac10")
    assert len(rows) == 1
    assert rows[0]["wall_ms"] == 1250, "审批等待须计入，且沿用 int() 截断口径"
    assert usage.run_usage("r-ac10")["duration_ms"] == 200


# ---------------------------------------------------------------- AC12 空 run_id


def test_ac12_empty_run_id_skipped(monkeypatch):
    """空 run_id 不得以任何键写 run_records；done 仍产出。"""
    _install(monkeypatch, _StreamAgent([_tok("答")]))
    usage._query("SELECT 1")           # 确保建表路径已执行（实现后含 run_records）
    try:
        before = _count_run_rows()
    except sqlite3.OperationalError:
        pytest.fail("run_records 表不存在：实现缺失（结构性红）")
    out = _collect(agent.stream_reply(
        [{"role": "user", "content": "你好"}],
        session_id="s-ac12", user_id="alice", run_id=""))
    assert out[-1]["type"] == "done"
    assert out[-1]["usage"]["run"] == {}
    assert _count_run_rows() == before == 0


def test_ac12b_record_run_empty_run_id_skips_insert():
    """record_run 的空 run_id 二次防御：直接调用不落行；非空 run_id 作对照。"""
    usage._query("SELECT 1")           # 触发建表
    assert _count_run_rows() == 0
    usage.record_run("", "alice", "s-empty", 100.0, 100.5, 500, "ok")
    assert _count_run_rows() == 0, "空 run_id 不得写入任何行"
    usage.record_run("r-ctrl", "alice", "s-empty", 100.0, 100.5, 500, "ok")
    rows = _rows("r-ctrl")
    assert len(rows) == 1, "对照组：非空 run_id 应正常落 1 行"
