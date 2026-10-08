"""Langfuse 调用链观测接入。

设计原则（私有化内网环境）：
- 默认关闭（LANGFUSE_ENABLED=false），零侵入
- 开启后若 Langfuse 服务器不可达/认证失败，自动停用，绝不影响聊天主链路
- 真实模式走 LangChain CallbackHandler 自动埋点；Mock 模式手动埋点演示
"""
import os
import threading
import time
from contextlib import contextmanager

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DEFAULT_HOST = "http://localhost:3000"
RETRY_COOLDOWN = 60.0  # 秒：连接失败后的重试冷却（架构评审 P3：失败可恢复）

_client = None
_next_retry = 0.0  # 冷却期内不再探测；期满自动重试（此前只检查一次，永久停用）
_init_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("LANGFUSE_ENABLED", "false").lower() in ("1", "true", "yes")


def get_client():
    """返回 Langfuse 客户端；未启用或暂不可用返回 None。
    连接失败不再永久停用（架构评审 P3）：失败后进入 RETRY_COOLDOWN 冷却，
    期满自动重试——观测服务后启动/临时宕机恢复后，埋点无需重启即可接回。"""
    global _client, _next_retry
    if _client is not None:
        return _client
    if not enabled():
        return None
    now = time.monotonic()
    if now < _next_retry:
        return None
    # 冷却期满：抢占下一轮探测时间，避免并发请求同时打探测（auth_check 有超时）
    with _init_lock:
        now = time.monotonic()
        if _client is not None or now < _next_retry:
            return _client
        _next_retry = now + RETRY_COOLDOWN
        try:
            from langfuse import Langfuse

            client = Langfuse(
                public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", "pk-lf-local"),
                secret_key=os.environ.get("LANGFUSE_SECRET_KEY", "sk-lf-local"),
                host=os.environ.get("LANGFUSE_HOST", DEFAULT_HOST),
                timeout=3,
            )
            if client.auth_check():
                _client = client
                print(f"[telemetry] Langfuse 已连接: {os.environ.get('LANGFUSE_HOST', DEFAULT_HOST)}")
            else:
                print(f"[telemetry] Langfuse 认证失败，{RETRY_COOLDOWN:.0f}s 后重试（不影响主链路）")
        except Exception as e:
            print(f"[telemetry] Langfuse 不可用（{type(e).__name__}: {e}），"
                  f"{RETRY_COOLDOWN:.0f}s 后重试（不影响主链路）")
        return _client


def langchain_callbacks() -> list:
    """真实模式：返回 LangChain 回调处理器列表（不可用时为空列表）。"""
    if get_client() is None:
        return []
    try:
        from langfuse.langchain import CallbackHandler

        return [CallbackHandler()]
    except Exception as e:
        print(f"[telemetry] LangChain 回调创建失败（{e}），按无观测运行")
        return []


@contextmanager
def trace_span(name: str, as_type: str = "span", input=None):
    """手动埋点用。观测不可用时 yield None，业务代码照常执行。"""
    client = get_client()
    if client is None:
        yield None
        return
    try:
        with client.start_as_current_observation(name=name, as_type=as_type, input=input) as obs:
            yield obs
    except Exception as e:
        print(f"[telemetry] 埋点异常（{type(e).__name__}），已忽略")
        yield None


def set_trace_io(input=None, output=None):
    """设置当前 trace 的输入/输出（在 trace_span 上下文内调用）。"""
    client = get_client()
    if client is None:
        return
    try:
        client.set_current_trace_io(input=input, output=output)
    except Exception:
        pass


def flush():
    """应用关闭时冲刷缓冲的观测数据。"""
    if _client is not None:
        try:
            _client.flush()
        except Exception:
            pass
