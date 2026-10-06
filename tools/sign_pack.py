"""管理员 CLI：为手工放入 packs/ 的领域包签名（离线审计后授予信任）。

用法：
    python tools/sign_pack.py <pack-name> [<pack-name>...]
    python tools/sign_pack.py --all        # 给全部未签名/签名失效的包签名

需要 PACK_SIGNING_KEY（从 .env 或环境变量读取）。
流程约定：先人工审计包内代码（tools/*.py 是可执行代码！），再运行本脚本。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import config, packsign  # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if not packsign.is_enforced():
        print("错误：未配置 PACK_SIGNING_KEY（.env 或环境变量），无法签名。")
        return 1

    packs_dir = config.PACKS_DIR
    if args == ["--all"]:
        names = [d.name for d in sorted(packs_dir.iterdir())
                 if d.is_dir() and not d.name.startswith((".", "_"))]
    else:
        names = args

    rc = 0
    for name in names:
        pack_dir = packs_dir / name.strip().lower()
        if not (pack_dir / "pack.yaml").is_file():
            print(f"[跳过] {name}: 不存在或缺少 pack.yaml")
            rc = 1
            continue
        before = packsign.trust_status(pack_dir, packsign.read_signature(pack_dir))
        sig = packsign.sign_pack_files(pack_dir)
        print(f"[已签名] {name}: {before} -> signed  ({sig[:40]}…)")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
