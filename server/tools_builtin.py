"""内核内置工具：工作区文件读取与沙箱代码执行（运行时安全边界相关的最小集合）。

通用业务能力（时间/计算）已迁为平台包 packs/core-utils（v0.12.0 起）；
天气能力为领域包 packs/weather-ops（v0.8.0 起）。
内核只保留运行时管理与安全边界类工具，业务能力一律走「平台包/领域包」。

审批样板统一走 approval.require_approval（v0.13.0 起，原 7 处重复代码已收敛）。
"""
import json
import urllib.request

from . import approval, config


def read_text_file(path: str) -> str:
    """读取工作区内的文本文件内容（最大 20KB）。path 相对项目根目录。"""
    try:
        target = (config.TOOL_WORKSPACE / path).resolve()
        if not target.is_relative_to(config.TOOL_WORKSPACE):
            return "拒绝访问：路径越出工作区边界"
        if not target.is_file():
            return f"文件不存在: {path}"
        return target.read_text(encoding="utf-8", errors="replace")[:20_000]
    except Exception as e:
        return f"读取失败: {e}"


def run_python_code(code: str) -> str:
    """在隔离沙箱中运行 Python 代码并返回 stdout/stderr。仅在部署 Docker 沙箱服务并配置 SANDBOX_URL 后可用。"""
    if not config.SANDBOX_URL:
        return ("代码执行未启用：需先部署 Docker 沙箱服务（docker-compose.sandbox.yml）"
                "并配置 SANDBOX_URL。本框架不允许在未隔离环境中执行代码（fail-closed）。")
    try:
        headers = {"Content-Type": "application/json"}
        if config.SANDBOX_AUTH_TOKEN:
            headers["X-Sandbox-Token"] = config.SANDBOX_AUTH_TOKEN
        req = urllib.request.Request(
            config.SANDBOX_URL.rstrip("/") + "/run",
            data=json.dumps({"code": code}).encode("utf-8"),
            headers=headers)
        with urllib.request.urlopen(req, timeout=config.SANDBOX_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("output") or data.get("error") or "（无输出）"
    except Exception as e:
        return f"沙箱调用失败: {e}"


def do_delete(path: str) -> str:
    """删除的实际执行逻辑（审批通过后才会走到这里；Mock 演示剧本也复用）。"""
    try:
        target = (config.TOOL_WORKSPACE / path).resolve()
        if not target.is_relative_to(config.TOOL_WORKSPACE):
            return "拒绝访问：路径越出工作区边界"
        if not target.is_file():
            return f"文件不存在: {path}"
        target.unlink()
        return f"已删除文件: {path}"
    except Exception as e:
        return f"删除失败: {e}"


async def delete_workspace_file(path: str) -> str:
    """删除工作区内的指定文件（危险操作，执行前需用户批准）。path 相对项目根目录。"""
    return approval.require_approval(
        "delete_workspace_file", {"path": path}, "删除",
        lambda: do_delete(path))
