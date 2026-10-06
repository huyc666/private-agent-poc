"""领域包可信签名：HMAC-SHA256 内容签名，防篡改与可信来源校验（v0.13.0）。

信任模型：
- 部署方在 .env 配置 PACK_SIGNING_KEY（HMAC 密钥）→ 强制模式：
  所有包必须带有效签名才加载；未签名 / 签名无效 / 内容被篡改 → 整包
  unavailable（fail-closed），原因在 list_packs / /api/health 可见。
- 未配置密钥 → 开放模式（POC 默认）：加载行为与旧版一致，但 list_packs
  展示信任状态（signed / unsigned / invalid），便于平滑过渡到强制模式。

信任链（强制模式下包如何获得签名）：
1. 对话式 create_pack：内容已过人工审批 → 落盘时自动签名（可信路径）；
2. 手工放入 packs/ 的包：管理员用 sign_pack 工具（挂审批）或
   CLI `python tools/sign_pack.py <name>` 签名；
3. 签名后任何内容修改都会使签名失效 → 热重载时整包不可用（篡改即失效）。

签名覆盖（所有可执行/可注入内容）：
- pack.yaml（剔除 signature: 行，保证重签幂等）
- tools/**、skills/** 全部文件、prompt.md
canonical 形式：逐文件 "相对路径\\0内容sha256hex\\n" 拼接后做 HMAC-SHA256。
"""
import hashlib
import hmac
import re
from pathlib import Path

from . import config

SIG_PREFIX = "hmac-sha256:"
_SIG_LINE = re.compile(r"^\s*signature\s*:", re.MULTILINE)


def _key() -> bytes | None:
    k = config.PACK_SIGNING_KEY
    return k.encode("utf-8") if k else None


def is_enforced() -> bool:
    """是否处于强制模式（配置了签名密钥）。"""
    return _key() is not None


def _canonical_parts(pack_dir: Path) -> list[tuple[str, bytes]]:
    """收集签名载荷：[(相对路径, 内容字节)]，路径用 POSIX 形式保证跨平台稳定。"""
    parts: list[tuple[str, bytes]] = []
    manifest = pack_dir / "pack.yaml"
    if manifest.is_file():
        text = manifest.read_text(encoding="utf-8", errors="replace")
        stripped = "\n".join(l for l in text.splitlines()
                             if not l.strip().startswith("signature:"))
        parts.append(("pack.yaml", stripped.encode("utf-8")))
    for sub in ("tools", "skills"):
        d = pack_dir / sub
        if d.is_dir():
            for p in sorted(d.rglob("*")):
                if p.is_file():
                    rel = p.relative_to(pack_dir).as_posix()
                    parts.append((rel, p.read_bytes()))
    prompt = pack_dir / "prompt.md"
    if prompt.is_file():
        parts.append(("prompt.md", prompt.read_bytes()))
    return parts


def _payload(pack_dir: Path) -> bytes:
    lines = []
    for rel, content in _canonical_parts(pack_dir):
        lines.append(f"{rel}\0{hashlib.sha256(content).hexdigest()}\n")
    return "".join(lines).encode("utf-8")


def compute(pack_dir: Path, key: bytes | None = None) -> str | None:
    """计算包目录当前内容的签名；无密钥返回 None。"""
    k = key or _key()
    if k is None:
        return None
    return SIG_PREFIX + hmac.new(k, _payload(pack_dir), hashlib.sha256).hexdigest()


def verify(pack_dir: Path, signature: str, key: bytes | None = None) -> bool:
    """校验签名与包当前内容是否一致（常数时间比较，防时序侧信道）。"""
    if not signature or not signature.startswith(SIG_PREFIX):
        return False
    expected = compute(pack_dir, key)
    if expected is None:
        return False
    return hmac.compare_digest(expected, signature)


def read_signature(pack_dir: Path) -> str:
    """从 pack.yaml 读取 signature 字段（简单行解析，不依赖 yaml 模块）。"""
    manifest = pack_dir / "pack.yaml"
    if not manifest.is_file():
        return ""
    for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("signature:"):
            return s.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


def sign_pack_files(pack_dir: Path) -> str:
    """把当前内容的签名写入 pack.yaml（覆盖旧 signature 行，幂等）。
    返回签名串；无密钥时返回空串（调用方负责提示）。"""
    sig = compute(pack_dir)
    if sig is None:
        return ""
    manifest = pack_dir / "pack.yaml"
    text = manifest.read_text(encoding="utf-8", errors="replace")
    kept = [l for l in text.splitlines() if not l.strip().startswith("signature:")]
    kept.append(f'signature: "{sig}"')
    manifest.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return sig


def trust_status(pack_dir: Path, signature: str) -> str:
    """管理视图用的信任状态：enforced 模式下由调用方先做准入判断。
    signed-unverified：包带签名但本机未配置密钥，无法校验（仅声明，不代表可信）。"""
    if not signature:
        return "unsigned"
    if _key() is None:
        return "signed-unverified"
    return "signed" if verify(pack_dir, signature) else "invalid"
