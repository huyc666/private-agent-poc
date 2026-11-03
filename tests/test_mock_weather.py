"""Mock 演示剧本与天气模块：城市/文件名提取、各演示场景分支（技能/时间/
天气/领域包/工具审批）、Open-Meteo 解析、降级链与兜底数据源。
全部离线（monkeypatch HTTP 层与能力探测）。"""
import asyncio
import shutil

import pytest

from server import approval, config, mock, packs, weather


def _collect(agen):
    async def _run():
        return [ev async for ev in agen]
    return asyncio.run(_run())


# ---------------------------------------------------------------- 提取器

def test_extract_city():
    assert mock._extract_city("北京现在天气怎么样？") == "北京"
    assert mock._extract_city("上海今天下雨吗") == "上海"
    assert mock._extract_city("    ") == "北京"          # 全部被去除 → 兜底


def test_extract_filename():
    assert mock._extract_filename("帮我删除文件 demo.txt") == "demo.txt"
    assert mock._extract_filename("删除") == "demo.txt"   # 无残留 → 兜底


# ---------------------------------------------------------------- mock 剧本

def test_mock_default_branch():
    events = _collect(mock.mock_stream("你好"))
    types = [e["type"] for e in events]
    assert "token" in types
    assert types[-1] == "done"
    assert "Mock" in "".join(e["content"] for e in events if e["type"] == "token")


def _approve_when_pending(tool: str, approve: bool):
    """后台任务：轮询审批表，出现 pending 的目标审批立即决议（模拟前端决定）。"""
    async def _task():
        for _ in range(300):                             # 最多 ~15s
            recs = approval.list_recent(limit=10)
            pend = [r for r in recs
                    if r["tool"] == tool and r["status"] == "pending"]
            if pend:
                approval.resolve(pend[0]["aid"], approve, "boss", "approver")
                return
            await asyncio.sleep(0.05)
        raise AssertionError("审批记录未出现")
    return _task()


def test_mock_delete_approved_executes():
    """删除场景全链路：审批登记 → 批准 → do_delete 真实执行 → 文件消失。"""
    target = config.TOOL_WORKSPACE / "del_target.txt"
    target.write_text("将被删除", encoding="utf-8")

    async def scenario():
        events = []
        t = asyncio.create_task(_approve_when_pending("delete_workspace_file", True))
        try:
            async for ev in mock.mock_stream("删除文件 del_target.txt"):
                events.append(ev)
        finally:
            await t
        return events

    events = asyncio.run(scenario())
    types = [e["type"] for e in events]
    assert "approval_required" in types
    ends = [e for e in events if e["type"] == "tool" and e["status"] == "end"]
    assert ends and ends[0]["name"] == "delete_workspace_file"
    assert "已删除" in ends[0]["result"]
    assert not target.exists()                            # 危险操作已真实执行


def test_mock_delete_rejected_not_executed():
    """fail-closed：拒绝 → 不执行删除。"""
    target = config.TOOL_WORKSPACE / "keep_me.txt"
    target.write_text("保留", encoding="utf-8")

    async def scenario():
        events = []
        t = asyncio.create_task(_approve_when_pending("delete_workspace_file", False))
        try:
            async for ev in mock.mock_stream("删除文件 keep_me.txt"):
                events.append(ev)
        finally:
            await t
        return events

    events = asyncio.run(scenario())
    assert any(e["type"] == "approval_required" for e in events)
    assert target.exists()                                 # 未执行
    answer = "".join(e["content"] for e in events if e["type"] == "token")
    assert "拒绝" in answer


# ------------------------------------------------- mock 演示流代际栅栏（v0.17.41 AC6）

def test_mock_fenced_stale_generation_aborts():
    """AC6：mock 演示流绑定会话代际——会话已删（代际过期）时审批不创建、
    演示以「会话已被删除」中止（与真实模型路径同口径 fail-closed）。"""
    from server import sessions
    sid = "mock-fence-stale"
    sessions.mark_deleted(sid)
    before = approval.pending_count()
    events = _collect(mock.mock_stream("删除文件 stale.txt",
                                       session_id=sid, generation=0))
    assert any(e["type"] == "error" and "会话已被删除" in e["content"]
               for e in events)
    assert not any(e["type"] == "approval_required" for e in events)
    assert approval.pending_count() == before              # 未新增 pending


def test_mock_fenced_active_generation_creates_pending():
    """AC6：代际匹配（活跃会话）→ 正常登记 pending 并走完审批演示。"""
    sid = "mock-fence-live"

    async def scenario():
        events = []
        t = asyncio.create_task(_approve_when_pending("delete_workspace_file", False))
        try:
            async for ev in mock.mock_stream("删除文件 live.txt",
                                             session_id=sid, generation=0):
                events.append(ev)
        finally:
            await t
        return events

    events = asyncio.run(scenario())
    assert any(e["type"] == "approval_required" for e in events)
    answer = "".join(e["content"] for e in events if e["type"] == "token")
    assert "拒绝" in answer


def test_mock_fenced_post_decision_no_execution():
    """评审 C1 防线：批准决议与删除竞争——决议落库后终局再校验命中代际过期，
    即使决议为批准也绝不执行（与真实路径 spec 3.4.3 同口径）。"""
    from server import sessions
    sid = "mock-fence-race"
    target = config.TOOL_WORKSPACE / "race.txt"
    target.write_text("不可删", encoding="utf-8")

    async def _delete_then_approve():
        for _ in range(300):
            pend = [r for r in approval.list_recent(limit=10)
                    if r["tool"] == "delete_workspace_file"
                    and r["status"] == "pending"]
            if pend:
                sessions.mark_deleted(sid)                 # 删除先于决议落库
                approval.resolve(pend[0]["aid"], True, "boss", "approver")
                return
            await asyncio.sleep(0.05)
        raise AssertionError("审批记录未出现")

    async def scenario():
        events = []
        t = asyncio.create_task(_delete_then_approve())
        try:
            async for ev in mock.mock_stream("删除文件 race.txt",
                                             session_id=sid, generation=0):
                events.append(ev)
        finally:
            await t
        return events

    events = asyncio.run(scenario())
    assert any(e["type"] == "approval_required" for e in events)
    assert any(e["type"] == "error" and "会话已被删除" in e["content"]
               for e in events)
    assert target.exists()                                 # 批准了也绝不执行


# ---------------------------------------------------------------- weather：Open-Meteo

_GEO = {"results": [{"name": "北京", "latitude": 39.9, "longitude": 116.4,
                     "admin1": "北京市", "country": "中国"}]}
_FORECAST = {"current": {"temperature_2m": 25.1, "relative_humidity_2m": 40,
                         "apparent_temperature": 26.0, "weather_code": 0,
                         "wind_speed_10m": 10.5}}


def test_via_open_meteo_ok(monkeypatch):
    # 两段调用（geocoding → forecast）都带 params，按 URL 区分返回内容
    monkeypatch.setattr(weather, "_http_get_json",
                        lambda url, params=None:
                        _FORECAST if "forecast" in url else _GEO)
    text = weather._via_open_meteo("北京")
    assert "Open-Meteo" in text
    assert "晴" in text                                   # weather_code 0
    assert "25.1" in text


def test_via_open_meteo_city_not_found(monkeypatch):
    monkeypatch.setattr(weather, "_http_get_json", lambda url, params=None: {"results": []})
    with pytest.raises(LookupError):
        weather._via_open_meteo("不存在的城市")


# ---------------------------------------------------------------- weather：itboy 兜底

_CITY_TREE = {"北京市": {"AREAID": "101010"},
              "广东省": {"广州市": {"AREAID": "101280"}}}


def test_find_area_id():
    assert weather._find_area_id(_CITY_TREE, "北京") == "101010"   # 市名直接命中
    assert weather._find_area_id(_CITY_TREE, "广州市") == "101280"
    assert weather._find_area_id(_CITY_TREE, "深圳") is None


def test_via_itboy_ok(monkeypatch):
    monkeypatch.setattr(weather, "_load_city_tree", lambda: _CITY_TREE)
    monkeypatch.setattr(
        weather, "_http_get_json",
        lambda url, params=None: {"status": 200, "data": {
            "city": "北京", "wendu": "22", "shidu": "50%", "quality": "优",
            "forecast": [{"type": "晴", "fx": "北风", "fl": "3级",
                          "low": "18℃", "high": "28℃"}]}})
    text = weather._via_itboy("北京")
    assert "itboy" in text and "22" in text


def test_via_itboy_bad_status(monkeypatch):
    monkeypatch.setattr(weather, "_load_city_tree", lambda: _CITY_TREE)
    monkeypatch.setattr(weather, "_http_get_json",
                        lambda url, params=None: {"status": 500, "message": "oops"})
    with pytest.raises(RuntimeError):
        weather._via_itboy("北京")


# ---------------------------------------------------------------- weather：降级链

def test_query_weather_fallback_chain(monkeypatch):
    """Open-Meteo 失败 → 自动降级 itboy 兜底。"""
    def _raise(city):
        raise ConnectionError("api down")
    monkeypatch.setattr(weather, "_via_open_meteo", _raise)
    monkeypatch.setattr(weather, "_via_itboy", lambda city: "兜底天气：晴 20°C")
    assert weather.query_weather("北京") == "兜底天气：晴 20°C"


def test_query_weather_not_found_prefers_hint(monkeypatch):
    """主源未找到城市 + 兜底源也没有 → 返回「未找到」而非含混的查询失败。"""
    def _lookup(city):
        raise LookupError("未找到城市")
    monkeypatch.setattr(weather, "_via_open_meteo", _raise_lookup := _lookup)
    monkeypatch.setattr(weather, "_via_itboy", _lookup)
    result = weather.query_weather("火星")
    assert "未找到城市" in result
    assert "天气查询失败" not in result


def test_query_weather_all_sources_fail(monkeypatch):
    def _raise(city):
        raise ConnectionError("api down")
    monkeypatch.setattr(weather, "_via_open_meteo", _raise)
    monkeypatch.setattr(weather, "_via_itboy", _raise)
    result = weather.query_weather("北京")
    assert "天气查询失败" in result
    assert "WEATHER_GEOCODING_URL" in result               # 带私有化配置指引


def test_query_weather_itboy_disabled(monkeypatch):
    """WEATHER_ITBOY_ENABLED=false：兜底源被跳过（含 not_found 时文案不带降级失败细节）。"""
    monkeypatch.setattr(weather, "ITBOY_ENABLED", False)
    def _raise(city):
        raise ConnectionError("api down")
    monkeypatch.setattr(weather, "_via_open_meteo", _raise)
    result = weather.query_weather("北京")
    assert "天气查询失败" in result


# ---------------------------------------------------------------- mock 其余场景分支

def test_mock_skill_create_branch():
    """技能制作场景：v0.17.41 起共享技能与领域包同口径——先审批后落盘。"""
    from server import skills

    async def scenario():
        events = []
        t = asyncio.create_task(_approve_when_pending("create_skill", True))
        try:
            async for ev in mock.mock_stream("帮我创建一个技能"):
                events.append(ev)
        finally:
            await t
        return events

    events = asyncio.run(scenario())
    assert any(e["type"] == "approval_required" and e["tool"] == "create_skill"
               for e in events)
    ends = [e for e in events if e["type"] == "tool" and e["status"] == "end"]
    assert ends[0]["name"] == "create_skill"
    answer = "".join(e["content"] for e in events if e["type"] == "token")
    assert "daily-brief" in answer and "审批已通过" in answer
    assert (config.SKILLS_DIR / "daily-brief" / "SKILL.md").is_file()
    assert any(s["name"] == "daily-brief" for s in skills.list_skills())


def test_mock_skill_create_rejected_not_written():
    """技能制作场景拒绝 → 不落盘（fail-closed 口径与真实行为一致）。"""
    async def scenario():
        events = []
        t = asyncio.create_task(_approve_when_pending("create_skill", False))
        try:
            async for ev in mock.mock_stream("帮我创建一个技能"):
                events.append(ev)
        finally:
            await t
        return events

    events = asyncio.run(scenario())
    answer = "".join(e["content"] for e in events if e["type"] == "token")
    assert "拒绝" in answer
    assert not (config.SKILLS_DIR / "daily-brief").exists()


def test_mock_skill_list_branch():
    events = _collect(mock.mock_stream("有哪些技能"))
    ends = [e for e in events if e["type"] == "tool" and e["status"] == "end"]
    assert ends[0]["name"] == "list_skills_tool"
    answer = "".join(e["content"] for e in events if e["type"] == "token")
    assert "当前可用技能" in answer


def test_mock_time_branch():
    """时间场景：平台包工具未挂载时内核兜底（v0.12.0 平台包分层演示）。"""
    events = _collect(mock.mock_stream("现在几点"))
    ends = [e for e in events if e["type"] == "tool" and e["status"] == "end"]
    assert ends[0]["name"] == "get_current_time"
    answer = "".join(e["content"] for e in events if e["type"] == "token")
    assert "服务器时间" in answer


def test_mock_weather_with_pack_capability(monkeypatch):
    """天气场景：有领域包能力时透出工具调用事件与结果。"""
    async def _fake(city, prefer_mcp=False):
        return "get_current_weather", f"{city}当前天气：晴，24°C"
    monkeypatch.setattr(mock, "_query_weather_any", _fake)
    events = _collect(mock.mock_stream("北京天气怎么样"))
    ends = [e for e in events if e["type"] == "tool" and e["status"] == "end"]
    assert ends[0]["name"] == "get_current_weather"
    answer = "".join(e["content"] for e in events if e["type"] == "token")
    assert "晴" in answer and "领域包" in answer


def test_mock_weather_no_capability(monkeypatch):
    """无任何天气能力：给出能力边界说明（领域包可插拔的演示点）。"""
    async def _fake(city, prefer_mcp=False):
        return "", "天气查询失败：当前没有任何天气能力"
    monkeypatch.setattr(mock, "_query_weather_any", _fake)
    events = _collect(mock.mock_stream("北京天气怎么样"))
    assert not any(e.get("name") == "get_current_weather" for e in events)
    answer = "".join(e["content"] for e in events if e["type"] == "token")
    assert "没有任何天气能力" in answer


def test_mock_pack_create_approved():
    """领域包创建场景：审批登记 → 批准 → create_pack 落盘（危险操作先审后执）。"""
    from server.state import STATE
    saved = dict(STATE.pack_tools)
    try:
        async def scenario():
            events = []
            t = asyncio.create_task(_approve_when_pending("create_pack", True))
            try:
                async for ev in mock.mock_stream("帮我创建一个领域包"):
                    events.append(ev)
            finally:
                await t
            return events
        events = asyncio.run(scenario())
        assert any(e["type"] == "approval_required" and e["tool"] == "create_pack"
                   for e in events)
        ends = [e for e in events if e["type"] == "tool" and e["status"] == "end"]
        assert ends[0]["name"] == "create_pack"
        answer = "".join(e["content"] for e in events if e["type"] == "token")
        assert "审批已通过" in answer
        assert (config.PACKS_DIR / "office-ops" / "pack.yaml").is_file()
    finally:
        shutil.rmtree(config.PACKS_DIR / "office-ops", ignore_errors=True)
        packs.invalidate_scan_cache()
        STATE.pack_tools = saved                        # 撤销图重建的挂载登记


def test_mock_tool_create_rejected():
    """自定义工具创建场景：拒绝 → 不落盘任何可执行代码（fail-closed）。"""
    from server import custom_tools
    async def scenario():
        events = []
        t = asyncio.create_task(_approve_when_pending("create_custom_tool", False))
        try:
            async for ev in mock.mock_stream("帮我创建一个工具"):
                events.append(ev)
        finally:
            await t
        return events
    events = asyncio.run(scenario())
    answer = "".join(e["content"] for e in events if e["type"] == "token")
    assert "拒绝" in answer
    assert not (config.CUSTOM_TOOLS_DIR / "dice_roll.py").exists()
    assert "dice_roll" not in [t["name"] for t in custom_tools.list_custom_tools()]


def test_s4_query_weather_error_sanitized(monkeypatch):
    """S4(b)：全部数据源失败时对外文案不拼异常原文——网络异常文本常含
    内部代理/解析地址（经工具返回透传到用户/模型通道），细节只落本机
    日志。"""
    def _raise(city):
        raise ConnectionError("MARKER_INTERNAL_URL http://proxy.internal:8080")
    monkeypatch.setattr(weather, "_via_open_meteo", _raise)
    monkeypatch.setattr(weather, "_via_itboy", _raise)
    result = weather.query_weather("北京")
    assert "天气查询失败" in result
    assert "MARKER_INTERNAL_URL" not in result
    assert "proxy.internal" not in result
    assert "ConnectionError" not in result
