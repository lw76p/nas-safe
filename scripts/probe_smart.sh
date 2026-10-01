#!/bin/sh
# NAS Safe SMART 采集通道探测（100% 只读，不写任何数据、不改任何配置）
# 用法：把本文件拷到目标 NAS（如 /tmp/），执行：
#   sh probe_smart.sh          # 普通用户
#   sudo sh probe_smart.sh     # 建议加 sudo（部分品牌 SMART 需要 root）
# 把全部输出发回来即可，用于判定该品牌走哪条采集通道。

echo "===== 1. 系统 ====="
head -3 /etc/os-release 2>/dev/null || head -2 /etc/issue 2>/dev/null
echo "arch: $(uname -m)"
echo "kernel: $(uname -r)"

echo "===== 2. smartctl 可用性（通用通道 A）====="
SMARTCTL=""
for p in "$(command -v smartctl 2>/dev/null)" /usr/sbin/smartctl /usr/bin/smartctl /usr/local/bin/smartctl /run/smartctl/smartctl; do
  if [ -n "$p" ] && [ -x "$p" ]; then SMARTCTL="$p"; break; fi
done
if [ -n "$SMARTCTL" ]; then
  echo "smartctl: $SMARTCTL"
  "$SMARTCTL" --version 2>/dev/null | head -1
else
  echo "smartctl: 未找到"
fi

echo "===== 3. 磁盘设备 ====="
ls /dev/sd? 2>/dev/null
ls /dev/nvme? 2>/dev/null
ls /dev/nvme*n1 2>/dev/null | grep -v 'p[0-9]'

echo "===== 4. QTS Drive Analyzer 包（威联通专用通道 B）====="
ls /tmp/smart/disk_data_pkg_* 2>/dev/null || echo "无 /tmp/smart/disk_data_pkg_*"

echo "===== 5. 群晖/其他品牌特征 ====="
[ -f /etc/VERSION ] && echo "DSM_VERSION: $(cat /etc/VERSION 2>/dev/null | tr '\n' ' ')"
ls /usr/syno 2>/dev/null | head -2
ls /run/smartd 2>/dev/null | head -3
grep -l . /etc/defaults/ugos* /etc/ugos* 2>/dev/null | head -2
grep -i 'ugreen\|ugos' /proc/version 2>/dev/null

echo "===== 6. 试读第一块盘 SMART（通道 A 实测）====="
FIRST="$(ls /dev/sda 2>/dev/null; ls /dev/nvme0 2>/dev/null | head -1)"
FIRST="$(echo "$FIRST" | head -1)"
if [ -n "$SMARTCTL" ] && [ -n "$FIRST" ]; then
  echo "--- \$SMARTCTL -H -A $FIRST ---"
  "$SMARTCTL" -H -A "$FIRST" 2>&1 | head -30
  if [ $? -ne 0 ] || [ "$(id -u)" != "0" ]; then
    echo "--- 加 sudo 重试 ---"
    sudo -n "$SMARTCTL" -H -A "$FIRST" 2>&1 | head -30
  fi
else
  echo "跳过（无 smartctl 或无磁盘设备）"
fi

echo "===== 7. 无 sudo 时 SMART 是否可读（判断普通用户权限）====="
[ -n "$FIRST" ] && dd if="$FIRST" of=/dev/null bs=512 count=1 2>&1 | tail -1

echo "===== 探测结束（本脚本全程只读）====="
