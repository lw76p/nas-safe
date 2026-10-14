TS Safe · 完整版安装包（跨平台）
================================

这个包能让任意一台电脑/服务器变成一台【独立的 TS Safe 主机】：
装好后开机自动在后台运行，自带控制台（浏览器打开 http://localhost:8848），
迁移、快照、重复文件清理、磁盘清理、每日白话日报……全部原生可用，
不再依赖别的设备来「代管」这台设备。

【Windows】
⚠ 务必先把整个 NAS-Safe-Full.zip 解压到一个【不含中文、不含空格】的文件夹
  （例如 D:\TSafe），不要直接双击压缩包里面的文件。
1. 进入解压后的文件夹。
2. 右键 install_windows_service.bat →「以管理员身份运行」（必须管理员）。
   （必须管理员：服务要写 C:\ProgramData、放行防火墙 8848、注册开机自启）
   （install_windows.ps1 是真正的安装程序，必须和 bat 放在同一个文件夹里）
3. 脚本自动：建虚拟环境 → 装依赖 → 注册并启动 TSafeServer 服务。
4. 打开 http://localhost:8848 ；首次进入设一个管理员账号。
   同局域网其他设备访问 http://这台电脑的局域网IP:8848 。
   卸载：以管理员运行  install_windows_service.bat uninstall 。

【Linux（systemd，如 Ubuntu / Debian / 树莓派）】
  sudo bash install_service.sh
脚本自动建 venv、装依赖、写 /etc/systemd/system/tssafe.service 并 enable+start。
日志：journalctl -u tssafe -f ；状态目录默认 /opt/nas-safe/state 。
卸载： sudo bash install_service.sh uninstall 。

【macOS（launchd）】
  bash install_service.sh
脚本自动建 venv、装依赖、写 ~/Library/LaunchAgents/com.tssafe.server.plist 并 load。
状态目录默认 ~/Library/Application Support/NAS Safe/state 。
卸载： bash install_service.sh uninstall 。

【说明】
- 纯后台服务，不带界面、不弹窗；想看提醒可另行安装桌面小助手。
- 需要 Python 3.10+ 才能装依赖；若本机没装，脚本会提示去官网下载。
- 控制台端口均为 8848；若被占用，可在安装前设置环境变量 NASSAFE_PORT。
