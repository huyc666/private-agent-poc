"""conftest 环境默认值的门控语义（安全复审 M2）。

非门控运行必须强制隔离（SANDBOX_URL 清空）；只有 RUN_LLM_TESTS 存在时才
保留外部可达沙箱地址。用子进程执行 conftest，避免污染当前测试进程环境。
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONF = Path(__file__).resolve().parent / "conftest.py"

_PROBE = (
    "import runpy, sys;"
    "runpy.run_path(sys.argv[1]);"
    "from server import config;"
    "print(config.SANDBOX_URL)"
)


def _probe_sandbox_url(extra_env):
    env = {k: v for k, v in os.environ.items()
           if k not in ("RUN_LLM_TESTS", "SANDBOX_URL")}
    env.update(extra_env)
    proc = subprocess.run([sys.executable, "-c", _PROBE, str(CONF)],
                          cwd=ROOT, env=env, capture_output=True, text=True,
                          timeout=180)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_sandbox_url_retention_is_bound_to_llm_gate():
    url = "http://sbx.test:9001"
    # 非门控：即使外部提供了地址也必须清空，避免默认套件真连沙箱
    assert _probe_sandbox_url({"SANDBOX_URL": url}) == ""
    # 门控：保留外部可达沙箱地址
    assert _probe_sandbox_url({"SANDBOX_URL": url, "RUN_LLM_TESTS": "1"}) == url
