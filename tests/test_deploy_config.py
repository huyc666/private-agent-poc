"""SEC-5/SEC-6（spec v0.17.32 AC5/AC6）：部署配置基线——
- LiteLLM 主模型地址 env 化（容器内 127.0.0.1 指向自身，fallback 掩盖主链路错误）；
- 上下文压缩阈值与 vLLM --max-model-len 基线对齐（128000 默认 vs 32768 实际窗口）。

读受控配置文件断言，防未来单侧改动回退。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------- AC5 网关地址

def test_ac5_litellm_primary_model_base_uses_env_reference():
    text = _read("gateway/litellm_config.yaml")
    assert "api_base: os.environ/VLLM_API_BASE" in text
    # 主模型段不再硬编码 127.0.0.1（deepseek fallback 段无 api_base，不受限）
    assert "api_base: http://127.0.0.1:8000" not in text
    # YAML 纯 ASCII 约束：litellm 以 OS 默认编码读，zh-CN Windows GBK 下非 ASCII 会炸
    assert all(ord(c) < 128 for c in text)


def test_ac5_gateway_compose_injects_default_and_extra_hosts():
    text = _read("docker/docker-compose.gateway.yml")
    assert "VLLM_API_BASE" in text
    assert "host.docker.internal:8000" in text    # 默认指向宿主映射端口
    assert "host-gateway" in text                  # Linux 宿主网关解析


def test_ac5_docs_cover_vllm_api_base():
    for rel in ("docs/DEPLOYMENT.md", "docs/DEPLOY_RUNBOOK.md",
                "docs/PRODUCTION.md"):
        assert "VLLM_API_BASE" in _read(rel), f"{rel} 缺少 VLLM_API_BASE 说明"


# ---------------------------------------------------------------- AC6 压缩基线

def test_ac6_context_tokens_baseline_aligned_with_vllm_window():
    compose = _read("docker/docker-compose.yml")
    assert "--max-model-len 32768" in compose
    # .env.example 压缩段与 docker-compose vLLM 窗口同值提示（防单侧改动）
    assert "32768" in _read(".env.example")
    assert "32768" in _read("README.md")
    # SEC-6 方案第 2 条（评审修复 C5）：compose vllm command 上方注释互指
    assert "CONTEXT_MAX_TOKENS" in compose
