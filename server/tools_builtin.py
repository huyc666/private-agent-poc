"""内核内置工具：工作区文件读取与沙箱代码执行（运行时安全边界相关的最小集合）。

通用业务能力（时间/计算）已迁为平台包 packs/core-utils（v0.12.0 起）；
天气能力为领域包 packs/weather-ops（v0.8.0 起）。
内核只保留运行时管理与安全边界类工具，业务能力一律走「平台包/领域包」。

审批样板统一走 approval.require_approval（v0.13.0 起，原 7 处重复代码已收敛）。
"""
import json
import re
import urllib.request

from . import approval, config

# 敏感文件判定（v0.17.4 安全审计 F1）：.env 及变体（含部署密钥）与运行时
# SQLite 库（state.db/usage.db 及 WAL/SHM）。默认工作区=项目根目录时，
# 根目录的 .env 会落在工作区内——若不做拦截，对话可直接把它读出外泄。
_SENSITIVE_FILENAME_RE = re.compile(
    r"^(\.env($|\..*)|.*\.db($|-wal$|-shm$))$", re.IGNORECASE)
# 行级密钥掩码：识别 "KEY = value" / "KEY: value"（键名可带引号，兼容 JSON）
_KEYVALUE_RE = re.compile(r'^["\']?([A-Za-z_][A-Za-z0-9_]*)["\']?(\s*[=:]\s*)(.+)$')
_SECRET_KEY_RE = re.compile(
    r"(TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)


def _is_sensitive_file(target) -> bool:
    """敏感文件判定：返回 True 则拒绝读取。"""
    return bool(_SENSITIVE_FILENAME_RE.match(target.name))


def _redact_secrets(text: str) -> str:
    """按行掩码密钥值：键名含 token/key/secret/password/credential 等敏感词的
    键值行，值统一替换为占位符（工具代码/配置文件里的硬编码密钥不外泄到对话）。"""
    out = []
    for line in text.splitlines():
        m = _KEYVALUE_RE.match(line)
        if m and _SECRET_KEY_RE.search(m.group(1)):
            out.append(f"{m.group(1)}{m.group(2)}***（已脱敏）")
        else:
            out.append(line)
    return "\n".join(out)


def read_text_file(path: str) -> str:
    """读取工作区内的文本文件内容（最大 20KB）。path 相对项目根目录。
    v0.17.4：敏感文件（.env 及运行时 DB）拒绝读取；其余内容按行掩码密钥值。"""
    try:
        target = (config.TOOL_WORKSPACE / path).resolve()
        if not target.is_relative_to(config.TOOL_WORKSPACE):
            return "拒绝访问：路径越出工作区边界"
        if _is_sensitive_file(target):
            return f"拒绝访问：{target.name} 为敏感文件（部署密钥/运行时数据），不予读取"
        if not target.is_file():
            return f"文件不存在: {path}"
        return _redact_secrets(
            target.read_text(encoding="utf-8", errors="replace"))[:20_000]
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
