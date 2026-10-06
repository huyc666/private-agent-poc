#!/usr/bin/env python
"""用户管理 CLI（v0.15.0）：签发 / 吊销 / 轮换 / 密码 / 列出 API Key 用户。

用法（在 agent-poc 目录下）：
    python tools/manage_users.py add <用户名> [--role user|approver|admin] [--days N]
    python tools/manage_users.py rotate <用户名> [--days N]
    python tools/manage_users.py passwd <用户名> [--password 明文]   # 不给则交互输入
    python tools/manage_users.py revoke <用户名>
    python tools/manage_users.py list

--days N：Key 在 N 天后过期（0 = 永不过期）。
rotate：为已有用户换发新 Key，旧 Key 立即失效。
passwd：设置/重置登录密码（账号密码登录用，只存 PBKDF2 加盐散列）；
        --password 会留在 shell 历史里，仅建议脚本化场景使用。
add/rotate 成功后明文 Key 只打印一次，请立即保存（库中只存 sha256）。
"""
import argparse
import getpass
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import auth  # noqa: E402


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "永不过期"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def main() -> int:
    parser = argparse.ArgumentParser(description="Private Agent POC 用户管理")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add", help="创建用户并签发一次性 API Key")
    p_add.add_argument("username")
    p_add.add_argument("--role", default="user", choices=auth.ROLES)
    p_add.add_argument("--days", type=int, default=0,
                       help="Key 有效期天数（0 = 永不过期）")
    p_rotate = sub.add_parser("rotate", help="换发新 Key（旧 Key 立即失效）")
    p_rotate.add_argument("username")
    p_rotate.add_argument("--days", type=int, default=0,
                          help="新 Key 有效期天数（0 = 永不过期）")
    p_passwd = sub.add_parser("passwd", help="设置/重置登录密码（账号密码登录用）")
    p_passwd.add_argument("username")
    p_passwd.add_argument("--password", default=None,
                          help="直接指定密码（会留在 shell 历史，建议省略后交互输入）")
    p_revoke = sub.add_parser("revoke", help="吊销用户（立即生效，保留审计记录）")
    p_revoke.add_argument("username")
    sub.add_parser("list", help="列出全部用户")
    args = parser.parse_args()

    if args.cmd == "add":
        key = auth.add_user(args.username, args.role, days=args.days)
        if key is None:
            print(f"创建失败：用户名「{args.username}」已存在或参数非法")
            return 1
        expiry = f"{args.days} 天后过期" if args.days > 0 else "永不过期"
        print(f"用户「{args.username}」（角色 {args.role}，{expiry}）已创建。")
        print(f"一次性 API Key（请立即保存，不再显示）: {key}")
        return 0
    if args.cmd == "rotate":
        key = auth.rotate_user(args.username, days=args.days)
        if key is None:
            print(f"轮换失败：用户「{args.username}」不存在")
            return 1
        expiry = f"{args.days} 天后过期" if args.days > 0 else "永不过期"
        print(f"用户「{args.username}」已换发新 Key（{expiry}），旧 Key 立即失效。")
        print(f"一次性 API Key（请立即保存，不再显示）: {key}")
        return 0
    if args.cmd == "passwd":
        password = args.password
        if password is None:
            password = getpass.getpass("新密码（输入不回显，至少 "
                                       f"{auth.MIN_PASSWORD_LEN} 位）: ")
            again = getpass.getpass("再输一次确认: ")
            if password != again:
                print("两次输入不一致，未修改")
                return 1
        if auth.set_password(args.username, password):
            print(f"用户「{args.username}」的登录密码已更新。")
            return 0
        print(f"设置失败：用户「{args.username}」不存在，"
              f"或密码少于 {auth.MIN_PASSWORD_LEN} 位")
        return 1
    if args.cmd == "revoke":
        if auth.revoke_user(args.username):
            print(f"用户「{args.username}」已吊销，其 Key 与登录令牌立即失效。")
            return 0
        print(f"用户「{args.username}」不存在或已吊销")
        return 1
    # list
    users = auth.list_users()
    if not users:
        print("（暂无用户；开放模式无需用户，认证模式用 add 创建）")
        return 0
    now = time.time()
    print(f"{'用户名':<20} {'角色':<10} {'状态':<8} {'密码':<6} {'过期时间':<22} 创建时间")
    for u in users:
        if u["revoked"]:
            status = "已吊销"
        elif u.get("expires_at") and u["expires_at"] <= now:
            status = "已过期"
        else:
            status = "正常"
        pwd = "已设置" if u.get("has_password") else "—"
        print(f"{u['username']:<20} {u['role']:<10} {status:<8} {pwd:<6} "
              f"{_fmt_ts(u.get('expires_at')):<22} {_fmt_ts(u['created'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
