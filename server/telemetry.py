"""Langfuse 调用链观测接入。

设计原则（私有化内网环境）：
- 默认关闭（LANGFUSE_ENABLED=false），零侵入
- 开启后若 Langfuse 服务器不可达/认证失败，自动停用，绝不影响聊天主链路
- 真实模式走 LangChain CallbackHandler 自动埋点；Mock 模式手动埋点演示
"""
import os
from contextlib import contextmanager

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

DEFAULT_HOST = "http://localhost:3000"

_client = None
_checked = False


def enabled() -> bool:
    return os.environ.get("LANGFUSE_ENABLED", "false").lower() in ("1", "true", "yes")


def get_client():
    """返回 Langfuse 客户端；未启用或不可用返回 None（惰性初始化，只检查一次）。"""
    global _client, _checked
    if _checked:
        return _client
    _checked = True
    if not enabled():
        return None
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
            print("[telemetry] Langfuse 认证失败，观测已停用（不影响主链路）")
    except Exception as e:
        print(f"[telemetry] Langfuse 不可用（{type(e).__name__}: {e}），观测已停用（不影响主链路）")
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
