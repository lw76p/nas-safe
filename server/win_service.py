"""
TS Safe — Windows 服务外壳

以「子进程」方式拉起 server/app.py，使完整 TS Safe 引擎在 Windows 上
开机自启、后台运行、可停可重启；子进程崩溃时自动自愈重启。

设计要点：
  - 本文件只依赖 pywin32（仅在「安装/作为服务运行」时需要）；
    app.py 本体零第三方依赖（仅 rsa / pdfminer.six / python-docx，
    由随附 venv 提供），不需要 pywin32。
  - 服务通过环境变量把状态目录 / 前端目录 / 端口 / 绑定地址注入子进程，
    因此 app.py 在不同机器/盘符上都能正确落地数据。
  - 状态目录默认 C:\\ProgramData\\NAS Safe\\state（所有用户可读写、
    不会随仓库移动而丢失）。
  - 日志：service.log（服务自身）+ app.log（app.py 输出），均在状态
    目录的 logs/ 下，便于排障。

用法（管理员 CMD / PowerShell）：
  python win_service.py install     注册服务（自动启动）
  python win_service.py start       启动
  python win_service.py stop        停止
  python win_service.py remove      卸载
也可直接由 install_windows_service.bat 完成全部步骤。
"""

from __future__ import annotations

import os
import sys
import time
import logging
import subprocess
import traceback

try:
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager
except ImportError:  # 允许在非 Windows / 未装 pywin32 时被 import（便于 lint）
    win32serviceutil = None
    win32service = None
    win32event = None
    servicemanager = None


# ---- 路径与配置 ----------------------------------------------------------
SERVER_DIR = os.path.dirname(os.path.abspath(__file__))      # .../server
INSTALL_ROOT = os.path.dirname(SERVER_DIR)                   # .../nassafe
DEFAULT_STATE_DIR = r"C:\ProgramData\NAS Safe\state"

STATE_DIR = os.environ.get("NASSAFE_STATE_DIR", DEFAULT_STATE_DIR)
WEB_DIR = os.environ.get("NASSAFE_WEB_DIR") or os.path.join(INSTALL_ROOT, "web")
PORT = os.environ.get("NASSAFE_PORT", "8848")
BIND_HOST = os.environ.get("NASSAFE_BIND_HOST", "0.0.0.0")

LOG_DIR = os.path.join(STATE_DIR, "logs")
SERVICE_LOG = os.path.join(LOG_DIR, "service.log")
APP_LOG = os.path.join(LOG_DIR, "app.log")
STARTUP_ERROR_LOG = os.path.join(STATE_DIR, "logs", "startup_error.log")


def _resolve_python() -> str:
    """优先用与安装配套的 venv python，否则退回 PATH 中的 python。

    服务进程（pythonservice.exe）的 sys.executable 不是常规 python.exe，
    因此这里显式定位 venv 的 python，保证 rsa/pdfminer/python-docx 可用。
    """
    venv_py = os.path.join(INSTALL_ROOT, "venv", "Scripts", "python.exe")
    if os.path.exists(venv_py):
        return venv_py
    return "python"


def _configure_logging() -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:
        pass
    try:
        logging.basicConfig(
            filename=SERVICE_LOG,
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            encoding="utf-8",
        )
    except OSError:
        # 如果连日志文件都写不了，至少别让服务崩溃
        logging.basicConfig(
            stream=sys.stdout,
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
        )


def _write_startup_error(msg: str) -> None:
    """把启动错误写到显眼位置，便于 install bat 读取并弹窗。"""
    try:
        os.makedirs(os.path.dirname(STARTUP_ERROR_LOG), exist_ok=True)
        with open(STARTUP_ERROR_LOG, "w", encoding="utf-8") as f:
            f.write(msg)
            f.write("\n")
    except OSError:
        pass


def _read_app_log_tail(lines: int = 30) -> str:
    try:
        if not os.path.exists(APP_LOG):
            return ""
        with open(APP_LOG, "r", encoding="utf-8", errors="ignore") as f:
            return "".join(f.readlines()[-lines:])
    except Exception:
        return ""


class TSafeServer(win32serviceutil.ServiceFramework):
    _svc_name_ = "TSafeServer"
    _svc_display_name_ = "TS Safe 防勒索快照服务"
    _svc_description_ = (
        "在 Windows 上后台运行 TS Safe 完整引擎"
        "（快照 / 重复文件 / 磁盘清理 / 迁移 / 日报 / 告警），并提供 Web 控制台。"
    )
    _svc_deps_ = ["Tcpip"]

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.proc = None
        self._stopping = False

    # ---- 控制指令 ----
    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self._stopping = True
        win32event.SetEvent(self.stop_event)
        self._terminate_child()

    SvcShutdown = SvcStop  # 系统关机时同样清理

    def _terminate_child(self):
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception as exc:  # noqa: BLE001
                logging.warning("终止子进程失败: %s", exc)
                try:
                    self.proc.kill()
                except Exception:
                    pass

    # ---- 启动子进程 ----
    def _launch(self):
        py = _resolve_python()
        env = dict(os.environ)
        env["NASSAFE_STATE_DIR"] = STATE_DIR
        env["NASSAFE_WEB_DIR"] = WEB_DIR
        env["NASSAFE_PORT"] = str(PORT)
        env["NASSAFE_BIND_HOST"] = BIND_HOST
        env["NASSAFE_SERVICE"] = "1"  # 提示 app.py 运行在服务模式
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
        except OSError as exc:
            logging.warning("创建状态目录失败: %s", exc)

        try:
            out = open(APP_LOG, "a", encoding="utf-8", buffering=1)
        except OSError:
            out = subprocess.DEVNULL

        logging.info(
            "启动子进程: %s app.py (PORT=%s BIND=%s STATE=%s WEB=%s)",
            py, PORT, BIND_HOST, STATE_DIR, WEB_DIR,
        )
        try:
            self.proc = subprocess.Popen(
                [py, "app.py"],
                cwd=SERVER_DIR,
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
            )
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc()
            logging.error("创建子进程失败: %s\n%s", exc, tb)
            _write_startup_error(f"创建子进程失败: {exc}\n{tb}")
            raise
        logging.info("子进程 PID=%s", self.proc.pid)

    def _check_child_healthy(self, seconds: int = 5) -> bool:
        """启动后等待几秒，若子进程已退出则视为启动失败。"""
        for _ in range(seconds):
            if self._stopping:
                return False
            if self.proc.poll() is not None:
                tail = _read_app_log_tail(20)
                msg = (
                    f"子进程启动后立即退出，返回码={self.proc.returncode}。\n"
                    f"常见原因：端口 {PORT} 被占用 / app.py 初始化失败 / 依赖缺失。\n"
                    f"--- app.log 最后 20 行 ---\n{tail}"
                )
                logging.error(msg)
                _write_startup_error(msg)
                return False
            time.sleep(1)
        return True

    # ---- 主循环 ----
    def SvcDoRun(self):
        _configure_logging()
        logging.info("TSafeServer 启动 (INSTALL_ROOT=%s)", INSTALL_ROOT)

        # 关键：立即报告服务已运行，让 SCM 不再等待，避免 2186
        try:
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
        except Exception as exc:  # noqa: BLE001
            logging.warning("ReportServiceStatus(RUNNING) 失败: %s", exc)

        try:
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
        except Exception:  # noqa: BLE001
            pass

        restart_delay = 3
        max_restarts = 10
        restarts = 0
        while not self._stopping:
            self._launch()
            # 首次启动做健康检查，秒崩则停止服务（而不是无限重启导致 SCM 判死）
            if not self._check_child_healthy(seconds=5):
                self._terminate_child()
                logging.error("子进程启动失败，服务停止")
                # 让 SCM 知道我们主动退出（StartServiceCtrlDispatcher 会返回）
                break

            # 等子进程退出或服务被停止
            while not self._stopping:
                rc = win32event.WaitForSingleObject(self.stop_event, 1000)
                if rc == win32event.WAIT_OBJECT_0:
                    break
                if self.proc.poll() is not None:
                    logging.warning("子进程异常退出 code=%s", self.proc.returncode)
                    restarts += 1
                    if restarts > max_restarts:
                        logging.error("子进程连续重启 %d 次，停止自愈", max_restarts)
                        self._stopping = True
                        break
                    logging.info(
                        "%ds 后重启子进程 (%d/%d)", restart_delay, restarts, max_restarts
                    )
                    time.sleep(restart_delay)
                    if self._stopping:
                        break
                    break  # 回到外层循环重新拉起

        self._terminate_child()
        logging.info("TSafeServer 停止")


if __name__ == "__main__":
    if win32serviceutil is None:
        print("错误：未安装 pywin32，无法作为 Windows 服务运行。")
        print("请以管理员身份运行 install_windows_service.bat 完成安装。")
        sys.exit(1)
    win32serviceutil.HandleCommandLine(TSafeServer)
