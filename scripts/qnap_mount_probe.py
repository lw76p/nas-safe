"""只读探测威联通 QTS 快照的挂载/浏览 CLI（不写任何东西到 NAS）。"""
import os
import paramiko

HOST = os.environ.get("NASSAFE_HOST", "192.168.8.62")
USER = os.environ.get("NASSAFE_USER", "lw76p")
PASS = os.environ.get("NASSAFE_PASS", "")


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, 22, USER, PASS, timeout=15,
              look_for_keys=False, allow_agent=False)

    def run(cmd, timeout=30):
        i, o, e = c.exec_command(cmd, timeout=timeout)
        out = o.read().decode(errors="replace")
        err = e.read().decode(errors="replace")
        return out, err

    # 登录拿到会话
    run(f"qcli -l user={USER} pw='{PASS}' saveauthsid=yes")

    cmds = [
        # 挂载参数语法（预期报错但展示用法）
        "qcli_volumesnapshot -m",
        "qcli_volumesnapshot --help",
        "qcli_volumesnapshot -h",
        # 现有快照浏览相关路径
        "ls -la /.share/snapshot/ 2>/dev/null",
        "ls -la /share/ 2>/dev/null | grep -i snap",
        "ls -la /share/ 2>/dev/null | head -40",
        # 其他快照工具帮助
        "snapshot_util --help 2>&1 | head -25",
        "qsnaputil --help 2>&1 | head -25",
        "qsnapman --help 2>&1 | head -25",
        # 配置
        "cat /etc/config/qsnapshot/snapshot.conf 2>/dev/null | head -30",
        # 现有逻辑卷（块级快照底层）
        "ls -la /dev/mapper/ 2>/dev/null | grep -i -E 'lv|tp|cachedev' | head -30",
        # 当前卷1无快照确认
        "qcli_volumesnapshot -l volumeID=1",
    ]
    for cmd in cmds:
        out, err = run(cmd)
        print("\n===== $", cmd, "=====")
        merged = (out + err).rstrip()
        print(merged if merged else "(no output)")

    c.close()


if __name__ == "__main__":
    main()
