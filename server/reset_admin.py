#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理员密码重置逃生门（忘密码自救）。

适用：忘记密码、又没在注册时留找回邮箱（或邮件通道未配置）的情况。
在 TS Safe 所在机器上运行（需要在程序根目录下，即和 server/ 平级）：

    python3 server/reset_admin.py              # 交互式：列出账号，按提示重置
    python3 server/reset_admin.py 用户名 新密码   # 一条命令直接重置

说明：能登录这台机器的人本来就能改程序数据，所以此脚本不做额外的身份校验；
它只是把「能登录机器」的能力转化成「能重置网页密码」，与群晖/QNAP 的重置按钮同理。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
os.chdir(_ROOT)          # 与 app.py 的 state_dir()=cwd/state 保持一致
sys.path.insert(0, _HERE)

import auth  # noqa: E402


def main() -> None:
    args = sys.argv[1:]
    if len(args) >= 2:
        username, pw = args[0].strip(), args[1]
    else:
        print("TS Safe 管理员密码重置")
        users = auth.list_users()
        if not users:
            print("还没有任何账号，直接打开网页会进入注册页。")
            return
        for u in users:
            print("  - %s（%s）" % (u.get("username"), u.get("role")))
        username = input("要重置哪个账号: ").strip()
        pw = input("新密码（至少 6 位）: ").strip()
    try:
        auth.reset_password(username, pw)
    except ValueError as exc:
        print("❌ 重置失败：%s" % exc)
        raise SystemExit(1)
    print("✅ 已重置 %s 的密码。回到网页用新密码登录即可。" % username)


if __name__ == "__main__":
    main()
