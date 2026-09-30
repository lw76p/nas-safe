#!/usr/bin/env python3
"""NAS Safe 项目每日异地备份到阿里云服务器。

用法：
    python scripts/backup_to_ali.py

行为：
  1. 把项目源码（server/ web/ scripts/ agent/ docs/ miniprogram/ README.md 等）
     打包成带时间戳的 zip，排除 .git / __pycache__ / *.pyc / *.spec / 临时构建产物。
  2. 通过 SFTP 上传到阿里云服务器 /opt/backups/nas-safe/YYYYMMDD_HHMMSS.zip。
  3. 服务端只保留最近 7 份备份，自动清理更旧的。

凭据：
  从 ~/.workbuddy/cloud-secret.txt 读取 root 密码（单行文本）。
  如需改服务器地址，设置环境变量 ALI_BACKUP_HOST / ALI_BACKUP_USER。
"""

import os
import re
import sys
import time
import zipfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import paramiko

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
PROJECT_DIR = Path(__file__).resolve().parent.parent
LOCAL_TMP = Path(os.environ.get("TEMP", "/tmp")) / "nassafe_backup"
HOST = os.environ.get("ALI_BACKUP_HOST", "47.108.213.178")
USER = os.environ.get("ALI_BACKUP_USER", "root")
REMOTE_DIR = os.environ.get("ALI_BACKUP_DIR", "/opt/backups/nas-safe")
KEEP_DAYS = int(os.environ.get("ALI_BACKUP_KEEP_DAYS", "7"))

EXCLUDE_PATTERNS = [
    re.compile(r"(^|/)\.git(/|$)"),
    re.compile(r"(^|/)\.gitignore$"),
    re.compile(r"(^|/)__pycache__(/|$)"),
    re.compile(r"(^|/)\.pytest_cache(/|$)"),
    re.compile(r"(^|/)\.venv(/|$)"),
    re.compile(r"(^|/)venv(/|$)"),
    re.compile(r"(^|/)node_modules(/|$)"),
    re.compile(r"(^|/)dist(/|$)"),
    re.compile(r"(^|/)build(/|$)"),
    re.compile(r"\.pyc$"),
    re.compile(r"\.pyo$"),
    re.compile(r"\.spec$"),
    re.compile(r"desktop_agent\.spec$"),
]

# 默认只备份这些顶层目录/文件（按需增减）
INCLUDE_TOP = [
    "server",
    "web",
    "scripts",
    "agent",
    "docs",
    "miniprogram",
    "README.md",
    "docker-compose.yml",
    "docker-compose.qnap.yml",
    "Dockerfile",
    "requirements.txt",
    "PRODUCT.md",
]


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _read_secret() -> str:
    """从 ~/.workbuddy/cloud-secret.txt 读取 root 密码。"""
    p = Path.home() / ".workbuddy" / "cloud-secret.txt"
    if not p.exists():
        raise FileNotFoundError(f"找不到服务器密码文件：{p}")
    return p.read_text(encoding="utf-8").strip()


def _should_include(rel_path: str) -> bool:
    """排除垃圾/构建文件。"""
    for pat in EXCLUDE_PATTERNS:
        if pat.search(rel_path):
            return False
    return True


def _pack() -> Path:
    """打包项目为 zip，返回本地 zip 路径。"""
    LOCAL_TMP.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
    zip_path = LOCAL_TMP / f"nas-safe_{stamp}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for top in INCLUDE_TOP:
            src = PROJECT_DIR / top
            if not src.exists():
                continue
            if src.is_file():
                zf.write(src, arcname=top)
                continue
            for path in src.rglob("*"):
                rel = path.relative_to(PROJECT_DIR).as_posix()
                if not _should_include(rel):
                    continue
                if path.is_file():
                    zf.write(path, arcname=rel)

    return zip_path


def _ssh_connect() -> paramiko.SSHClient:
    """建立 SSH 连接。"""
    password = _read_secret()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(HOST, port=22, username=USER, password=password, timeout=15)
    return client


def _upload(local_zip: Path) -> str:
    """上传 zip 到阿里云并清理旧备份，返回服务端路径。"""
    client = _ssh_connect()
    try:
        # 创建远端目录
        stdin, stdout, stderr = client.exec_command(f"mkdir -p {REMOTE_DIR}", timeout=30)
        err = stderr.read().decode("utf-8", "replace").strip()
        if err:
            raise RuntimeError(f"创建远端目录失败：{err}")

        remote_name = local_zip.name
        remote_path = f"{REMOTE_DIR}/{remote_name}"

        sftp = client.open_sftp()
        try:
            print(f"[backup] uploading {local_zip} -> {HOST}:{remote_path}")
            sftp.put(str(local_zip), remote_path)
        finally:
            sftp.close()

        # 清理旧备份：保留最近 KEEP_DAYS 天
        cleanup_cmd = (
            f"find {REMOTE_DIR} -maxdepth 1 -name 'nas-safe_*.zip' "
            f"-mtime +{KEEP_DAYS} -print -delete"
        )
        stdin, stdout, stderr = client.exec_command(cleanup_cmd, timeout=60)
        deleted = stdout.read().decode("utf-8", "replace").strip()
        if deleted:
            print(f"[backup] cleaned old backups:\n{deleted}")

        return remote_path
    finally:
        client.close()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    print(f"[backup] project root: {PROJECT_DIR}")
    print(f"[backup] packing...")
    zip_path = _pack()
    print(f"[backup] packed: {zip_path} ({zip_path.stat().st_size / 1024 / 1024:.2f} MB)")

    remote_path = _upload(zip_path)
    print(f"[backup] saved on server: {remote_path}")

    # 本地 zip 保留一份当日即可，避免占满磁盘
    try:
        zip_path.unlink()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[backup] FAILED: {exc}")
        sys.exit(1)
