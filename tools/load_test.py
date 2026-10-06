#!/usr/bin/env python
"""并发压测工具（v0.16.1）：验证框架随并发量增长的扩展表现。

用法（在 agent-poc 目录下，服务已启动）：
    python tools/load_test.py [--host http://localhost:7100] [--key pak-xxx]
    python tools/load_test.py --scenario health   # 只测轻端点
    python tools/load_test.py --scenario chat     # 只测聊天流式

阶梯并发打 /api/health 与 /api/chat（Mock 流式），报告每级并发的
成功率、p50/p95/max 延迟、吞吐，用于判断瓶颈与扩展上限。
"""
import argparse
import asyncio
import statistics
import time

import httpx


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, int(len(sorted_vals) * p + 0.999) - 1))
    return sorted_vals[k]


def report(name: str, conc: int, lat: list[float], errors: int, wall: float) -> None:
    lat_s = sorted(lat)
    ok = len(lat_s)
    total = ok + errors
    print(f"  并发 {conc:<4} 请求 {total:<4} 成功 {ok:<4} 失败 {errors:<3} "
          f"p50 {_pct(lat_s, .5)*1000:>7.0f}ms  p95 {_pct(lat_s, .95)*1000:>7.0f}ms  "
          f"max {max(lat_s)*1000 if lat_s else 0:>7.0f}ms  "
          f"吞吐 {ok/wall:>6.1f} req/s")


async def one_health(client: httpx.AsyncClient, _idx: int = 0) -> float:
    t0 = time.perf_counter()
    r = await client.get("/api/health")
    r.raise_for_status()
    return time.perf_counter() - t0


async def one_chat(client: httpx.AsyncClient, session_prefix: str, idx: int) -> float:
    t0 = time.perf_counter()
    async with client.stream(
            "POST", "/api/chat",
            json={"message": "你好", "session_id": f"{session_prefix}-{idx}"}) as r:
        r.raise_for_status()
        async for _ in r.aiter_lines():
            pass
    return time.perf_counter() - t0


async def one_chat_same_session(client: httpx.AsyncClient, session_id: str,
                                _idx: int) -> float:
    """同一会话的并发请求：验证串行化保护（应全部成功且耗时近似线性叠加）。"""
    t0 = time.perf_counter()
    async with client.stream(
            "POST", "/api/chat",
            json={"message": "你好", "session_id": session_id}) as r:
        r.raise_for_status()
        async for _ in r.aiter_lines():
            pass
    return time.perf_counter() - t0


async def run_level(name: str, conc: int, total: int, worker, *args) -> None:
    sem = asyncio.Semaphore(conc)
    lat: list[float] = []
    errors = 0

    async def guarded(i: int) -> None:
        nonlocal errors
        async with sem:
            try:
                lat.append(await worker(*args, i))
            except Exception:
                errors += 1

    t0 = time.perf_counter()
    await asyncio.gather(*(guarded(i) for i in range(total)))
    wall = time.perf_counter() - t0
    report(name, conc, lat, errors, wall)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="http://localhost:7100")
    ap.add_argument("--key", default="", help="认证模式下的 Bearer 凭据")
    ap.add_argument("--scenario", choices=["all", "health", "chat", "session"],
                    default="all")
    args = ap.parse_args()

    headers = {"Authorization": f"Bearer {args.key}"} if args.key else {}
    limits = httpx.Limits(max_connections=500, max_keepalive_connections=500)
    timeout = httpx.Timeout(120.0)
    async with httpx.AsyncClient(base_url=args.host, headers=headers,
                                 limits=limits, timeout=timeout) as client:
        if args.scenario in ("all", "health"):
            print("\n[1] /api/health 轻端点阶梯（每级 3 倍请求数）")
            for conc in (10, 50, 100, 200):
                await run_level("health", conc, conc * 3, one_health, client)
        if args.scenario in ("all", "chat"):
            print("\n[2] /api/chat Mock 流式阶梯（每级 2 倍请求数，各自独立会话）")
            prefix = f"lt-{int(time.time())}"
            for conc in (5, 10, 25, 50):
                await run_level("chat", conc, conc * 2, one_chat, client, prefix)
        if args.scenario in ("all", "session"):
            print("\n[3] 同会话并发（串行化保护验证：同一 session_id 并行打 N 个请求，"
                  "应全部成功且 p50 近似随 N 线性增长）")
            same = f"lt-same-{int(time.time())}"
            for conc in (3, 6, 10):
                await run_level("same-session", conc, conc,
                                one_chat_same_session, client, same)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
