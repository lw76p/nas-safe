#!/bin/sh
# NAS Safe — 系统探测脚本
#
# 用途：在目标 NAS 上运行，检测是否支持快照功能。
#       纯只读操作，不修改任何数据。
#
# 用法（在 NAS 的 SSH 里执行）：
#   sh probe.sh
#   或
#   curl -fsSL <你的地址>/probe.sh | sh
#
# 输出：一份可读的检测报告。把结果发回来即可判断适配可行性。

set -u

echo "=============================================================="
echo "  NAS Safe — 系统探测报告"
echo "  生成时间：$(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================================="
echo ""

# ---------------------------------------------------------------
echo "【1】系统识别"
echo "--------------------------------------------------------------"
if [ -f /etc/os-release ]; then
  . /etc/os-release 2>/dev/null || true
  echo "  NAME        : ${NAME:-未知}"
  echo "  PRETTY_NAME : ${PRETTY_NAME:-未知}"
  echo "  ID          : ${ID:-未知}"
  echo "  ID_LIKE     : ${ID_LIKE:-无}"
  echo "  VERSION     : ${VERSION:-未知}"
else
  echo "  未找到 /etc/os-release"
fi
echo "  内核        : $(uname -r)"
echo "  架构        : $(uname -m)"
echo "  主机名      : $(hostname 2>/dev/null || echo 未知)"

# 品牌特征文件
echo ""
echo "  品牌特征文件检查："
for f in /etc/fnos-release /etc/ugos-release /etc/synoinfo.conf \
         /etc/config/uLinux.conf /etc/openmediavault/config.xml \
         /etc.defaults/VERSION /etc/unraid-version ; do
  if [ -f "$f" ]; then
    echo "    [有] $f"
  fi
done

# ---------------------------------------------------------------
echo ""
echo "【2】运行环境"
echo "--------------------------------------------------------------"
if [ -f /.dockerenv ]; then
  echo "  容器内      : 是 (发现 /.dockerenv)"
else
  echo "  容器内      : 否"
fi
if [ -d /run/systemd/system ]; then
  echo "  systemd     : 可用"
else
  echo "  systemd     : 不可用"
fi
echo "  当前用户    : $(id -un 2>/dev/null || echo 未知) (uid=$(id -u 2>/dev/null || echo '?'))"
echo "  Docker      : $(command -v docker >/dev/null 2>&1 && docker --version 2>/dev/null || echo '未安装')"

# ---------------------------------------------------------------
echo ""
echo "【3】关键命令检测"
echo "--------------------------------------------------------------"
for cmd in btrfs zfs httm rsync python3 curl; do
  if command -v "$cmd" >/dev/null 2>&1; then
    echo "  $cmd : [有] $(command -v $cmd)"
  else
    echo "  $cmd : [无]"
  fi
done

# ---------------------------------------------------------------
echo ""
echo "【4】挂载点与文件系统（关键）"
echo "--------------------------------------------------------------"
printf "  %-32s %-8s %s\n" "挂载点" "类型" "设备"
echo "  --------------------------------------------------------------"

if [ -r /proc/mounts ]; then
  # 只列出常见的数据文件系统，过滤掉系统伪文件系统
  awk '$3 ~ /^(btrfs|zfs|ext4|ext3|ext2|xfs|f2fs)$/ {printf "  %-32s %-8s %s\n", $2, $3, $1}' /proc/mounts
else
  echo "  无法读取 /proc/mounts"
fi

echo ""
echo "  统计："
if [ -r /proc/mounts ]; then
  btrfs_count=$(awk '$3=="btrfs"' /proc/mounts | wc -l | tr -d ' ')
  zfs_count=$(awk '$3=="zfs"' /proc/mounts | wc -l | tr -d ' ')
  ext4_count=$(awk '$3=="ext4"' /proc/mounts | wc -l | tr -d ' ')
  echo "    btrfs 挂载点 : $btrfs_count"
  echo "    zfs   挂载点 : $zfs_count"
  echo "    ext4  挂载点 : $ext4_count"
fi

# ---------------------------------------------------------------
echo ""
echo "【5】btrfs 详情"
echo "--------------------------------------------------------------"
if command -v btrfs >/dev/null 2>&1; then
  echo "  btrfs 版本  : $(btrfs --version 2>/dev/null | head -1)"
  echo ""
  echo "  文件系统概览："
  btrfs filesystem show 2>/dev/null | sed 's/^/    /' || echo "    (读取失败，可能需要 root)"
  echo ""
  echo "  子卷列表（各挂载点）："
  if [ -r /proc/mounts ]; then
    awk '$3=="btrfs" {print $2}' /proc/mounts | while read -r mp; do
      echo "    --- $mp ---"
      btrfs subvolume list -o "$mp" 2>/dev/null | head -20 | sed 's/^/      /' \
        || echo "      (无权限或无子卷)"
      echo "    --- $mp 的快照 ---"
      btrfs subvolume list -s -o "$mp" 2>/dev/null | head -10 | sed 's/^/      /' \
        || echo "      (无)"
    done
  fi
else
  echo "  未安装 btrfs 命令"
fi

# ---------------------------------------------------------------
echo ""
echo "【6】ZFS 详情"
echo "--------------------------------------------------------------"
if command -v zfs >/dev/null 2>&1; then
  echo "  ZFS 版本    : $(zfs --version 2>/dev/null | head -1)"
  echo ""
  echo "  数据集列表："
  zfs list -H -o name,mountpoint,used 2>/dev/null | head -20 | sed 's/^/    /' \
    || echo "    (读取失败，可能需要 root)"
  echo ""
  echo "  快照数量  : $(zfs list -H -t snapshot 2>/dev/null | wc -l | tr -d ' ')"
else
  echo "  未安装 zfs 命令"
fi

# ---------------------------------------------------------------
echo ""
echo "【7】厂商快照路径探索"
echo "--------------------------------------------------------------"
echo "  常见的厂商快照目录："
for d in /volume1/@snapshot /volume2/@snapshot \
         /share/ZFS*/@snapshot \
         /mnt/@snapshots /mnt/snapshots ; do
  if [ -d "$d" ] 2>/dev/null; then
    echo "    [存在] $d"
    ls -la "$d" 2>/dev/null | head -5 | sed 's/^/        /'
  fi
done

echo ""
echo "  各挂载点下的隐藏快照目录："
if [ -r /proc/mounts ]; then
  awk '$3 ~ /^(btrfs|zfs)$/ {print $2}' /proc/mounts | while read -r mp; do
    for sub in .snapshots @snapshots .nassafe "#recycle" ; do
      if [ -d "$mp/$sub" ]; then
        echo "    [有] $mp/$sub"
      fi
    done
  done
fi

# ---------------------------------------------------------------
echo ""
echo "【8】权限与能力测试"
echo "--------------------------------------------------------------"
echo "  sudo 可用   : $(command -v sudo >/dev/null 2>&1 && echo 是 || echo 否)"

if [ "$(id -u 2>/dev/null)" = "0" ]; then
  echo "  当前是 root : 是（可以直接执行快照操作）"
else
  echo "  当前是 root : 否（快照操作可能需要 sudo）"
fi

# 只读测试：尝试列出子卷（不创建任何东西）
if command -v btrfs >/dev/null 2>&1; then
  first_btrfs=$(awk '$3=="btrfs" {print $2; exit}' /proc/mounts 2>/dev/null)
  if [ -n "${first_btrfs:-}" ]; then
    echo ""
    echo "  只读测试（对 $first_btrfs 执行 list，不做任何修改）："
    if btrfs subvolume list -o "$first_btrfs" >/dev/null 2>&1; then
      echo "    结果：具备读取权限"
    else
      echo "    结果：读取失败（可能需要 root 或 sudo）"
    fi
  fi
fi

# ---------------------------------------------------------------
echo ""
echo "【9】存储空间"
echo "--------------------------------------------------------------"
df -h 2>/dev/null | awk 'NR==1 || $1 ~ /^\/dev/' | sed 's/^/  /'

# ---------------------------------------------------------------
echo ""
echo "=============================================================="
echo "  探测完成"
echo "=============================================================="
echo ""
echo "  如果看到 btrfs 或 zfs 挂载点 —— 说明你的 NAS 支持快照功能。"
echo "  如果只有 ext4 —— 需要重建存储池为 btrfs 才能使用快照。"
echo ""
