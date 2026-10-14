"""session_lock：STATE_DB 后端分布式租约锁（跨进程互斥的核心保障）。"""
import asyncio
import time

import pytest

from server import session_lock


def test_acquire_renew_release_cycle():
    assert session_lock.acquire("s1", "owner-a") is True
    assert session_lock.acquire("s1", "owner-b") is False   # 他人持有
    assert session_lock.renew("s1", "owner-a") is True     # 心跳续租
    assert session_lock.release("s1", "owner-a") is True
    assert session_lock.release("s1", "owner-a") is False  # 幂等
    assert session_lock.acquire("s1", "owner-b") is True   # 释放后可获取


def test_expired_lease_can_be_taken_over():
    """持有者崩溃（无心跳）→ 租约到期，他人可接管。
    注意：过期判定用接管方传入的 ttl（acquire 的 SQL 语义），故第二次也传小 ttl。"""
    assert session_lock.acquire("s2", "owner-a", ttl=0.05) is True
    time.sleep(0.1)
    assert session_lock.acquire("s2", "owner-b", ttl=0.05) is True


def test_renew_rejected_after_takeover():
    session_lock.acquire("s3", "owner-a", ttl=0.05)
    time.sleep(0.1)
    assert session_lock.acquire("s3", "owner-b", ttl=0.05) is True
    # 原持有者心跳续租失败 = 已被接管，调用方应中止本轮对话
    assert session_lock.renew("s3", "owner-a") is False


def test_session_lease_context_holds_and_releases():
    async def run():
        async with session_lock.SessionLease("s4", "w1"):
            assert session_lock.acquire("s4", "w2") is False
        assert session_lock.acquire("s4", "w2") is True    # 退出即释放
    asyncio.run(run())


def test_session_lease_timeout_is_bounded():
    """有界等待：他人长时间持有时抛 LeaseTimeout，而不是无限挂起。"""
    async def run():
        session_lock.acquire("s5", "holder")
        with pytest.raises(session_lock.LeaseTimeout):
            async with session_lock.SessionLease("s5", "w2", wait_timeout=0.2):
                pass
    asyncio.run(run())


def test_session_lease_heartbeat_detects_takeover(monkeypatch):
    """心跳发现租约被接管 → lost 标志置位（调用方据此中止本轮）。"""
    monkeypatch.setattr(session_lock, "HEARTBEAT_INTERVAL", 0.1)
    async def run():
        lease = session_lock.SessionLease("s6", "w1", ttl=60)
        await lease.__aenter__()
        # 模拟另一进程接管（ttl 极短的过期接管）
        session_lock.release("s6", "w1")
        session_lock.acquire("s6", "intruder", ttl=60)
        await asyncio.sleep(0.35)
        assert lease.lost is True
        await lease.__aexit__()
    asyncio.run(run())
