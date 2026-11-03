"""API 时延基线测量脚本（docs/ENGINEERING-METRICS.md U2 条目的测量手段）。

用法：
    .venv\\Scripts\\python.exe tests\\perf_baseline.py

在独立临时目录建库（不影响真实数据），mock 模式 + 强制认证启动 FastAPI
TestClient（进程内测量，不含网络与模型推理），对 5 个基础端点各测 50 轮，
输出 min/median/p95/max（毫秒）。模型首 token 时延不在本脚本范围内。
"""

import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

# 独立临时状态（须在 import server 之前设置；config 值在调用时读取）
_TMP = Path(tempfile.mkdtemp(prefix="perf-baseline-"))
os.environ.setdefault("MOCK_LLM", "true")
os.environ["AUTH_ENABLED"] = "true"
os.environ["STATE_DB"] = str(_TMP / "state.db")
os.environ["USAGE_DB"] = str(_TMP / "usage.db")
os.environ["TOOL_WORKSPACE"] = str(_TMP / "ws")
os.environ["UPLOADS_DIR"] = str(_TMP / "ws" / "uploads")
os.environ["OUTPUTS_DIR"] = str(_TMP / "ws" / "outputs")
os.environ["SKILLS_DIR"] = str(_TMP / "skills")
os.environ["PERSONAL_SKILLS_DIR"] = str(_TMP / "skills_personal")
os.environ["CUSTOM_TOOLS_DIR"] = str(_TMP / "custom_tools")
os.environ["PERSONAL_CUSTOM_TOOLS_DIR"] = str(_TMP / "custom_tools_personal")
os.environ["PACKS_DIR"] = str(_TMP / "packs")
os.environ["LANGFUSE_ENABLED"] = "false"
os.environ["SANDBOX_URL"] = ""
os.environ["PACK_SIGNING_KEY"] = ""
os.environ["AUTH_BOOTSTRAP_ADMIN"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from server import auth  # noqa: E402
from server.main import app  # noqa: E402

ROUNDS = 50


def bench(name, fn):
    fn()  # 热身：JIT/连接池/首查缓存不计入
    samples = []
    for _ in range(ROUNDS):
        t0 = time.perf_counter()
        resp = fn()
        samples.append((time.perf_counter() - t0) * 1000.0)
        assert resp.status_code < 400, f"{name} 返回 {resp.status_code}"
    samples.sort()
    p95 = samples[max(0, int(len(samples) * 0.95) - 1)]
    median = statistics.median(samples)
    print(f"{name:<28} min={samples[0]:7.1f}  median={median:7.1f}  "
          f"p95={p95:7.1f}  max={samples[-1]:7.1f}  (ms, n={len(samples)})")
    return p95


def main():
    assert auth.add_user("perfuser", role="user"), "建用户失败"
    assert auth.set_password("perfuser", "perf-pass-123"), "设密码失败"

    results = {}
    with TestClient(app) as client:
        login = client.post("/api/auth/login",
                            json={"username": "perfuser",
                                  "password": "perf-pass-123"})
        assert login.status_code == 200, login.text
        token = login.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        results["POST /api/auth/login"] = bench(
            "POST /api/auth/login",
            lambda: client.post("/api/auth/login",
                                json={"username": "perfuser",
                                      "password": "perf-pass-123"}))
        results["GET /api/health"] = bench(
            "GET /api/health", lambda: client.get("/api/health"))
        results["GET /api/sessions"] = bench(
            "GET /api/sessions",
            lambda: client.get("/api/sessions", headers=headers))
        results["GET /api/uploads"] = bench(
            "GET /api/uploads",
            lambda: client.get("/api/uploads", headers=headers))
        results["GET /api/usage"] = bench(
            "GET /api/usage",
            lambda: client.get("/api/usage", headers=headers))

    worst = max(results.values())
    print(f"\n全部端点 P95 最差值：{worst:.1f} ms（阈值判定以此为准）")
    return 0 if worst <= 500.0 else 1


if __name__ == "__main__":
    sys.exit(main())
