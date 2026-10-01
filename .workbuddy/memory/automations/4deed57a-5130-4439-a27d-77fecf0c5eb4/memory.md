# 自动化执行记录：NAS Safe 每日异地备份

> 任务 ID：4deed57a-5130-4439-a27d-77fecf0c5eb4
> 脚本：scripts/backup_to_ali.py（打包 → SFTP 上传阿里云 /opt/backups/nas-safe/ → 保留近 7 天）

## 执行摘要

- **首次执行**（2026-10-01 21:00 触发）：成功。
- 运行环境：托管 Python venv（paramiko 5.0.0，C:\Users\aa\.workbuddy\binaries\python\envs\default）。
- 打包：项目源码/文档，21.89 MB。
- 上传：`nas-safe_20261001_210046.zip` → `47.108.213.178:/opt/backups/nas-safe/`（SSH/SFTP 走 AutoAddPolicy，密码取自 ~/.workbuddy/cloud-secret.txt）。
- 清理：find -mtime +7 未报告删除项（无超过 7 天的旧备份）。
- 本地临时 zip 已按脚本逻辑自动删除。
- 注意：脚本含外部网络 (SSH 47.108.213.178:22)，运行时需关闭沙箱（dangerouslyDisableSandbox），否则连接超时失败。

## 后续观察点

- 下次执行若失败，优先排查：服务器可达性、cloud-secret.txt 密码是否过期、paramiko 依赖是否在 venv 内。
- 备份频率按自动化计划，无需手动触发。
