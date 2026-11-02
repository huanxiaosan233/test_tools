"""全局日志模块：把运行记录和报错写入 data/app.log（自动轮转）。

用法：
    from app_log import setup_logging, get_logger, log_exception

    setup_logging(DATA_DIR, "测试工具")   # 程序启动时调用一次
    get_logger().warning("日志刷新失败：%s", reason)
    try:
        ...
    except Exception as exc:
        log_exception("上传失败", exc)

setup_logging 会接管以下报错来源：
  * 主线程未捕获异常（sys.excepthook）
  * 子线程未捕获异常（threading.excepthook）
  * 无法抛出的异常（sys.unraisablehook）
  * Qt 的警告 / 错误（qInstallMessageHandler）
  * 弹窗提示 QMessageBox.warning / critical 的文字
  * 进程级崩溃（faulthandler，写入 data/crash_native.log）
"""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import platform
import sys
import threading
import traceback
from pathlib import Path

LOGGER_NAME = "app"
LOG_FILENAME = "app.log"
NATIVE_LOG_FILENAME = "crash_native.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 5

_configured = False
_native_log_file = None


def get_logger() -> logging.Logger:
    """返回应用日志记录器；setup_logging 之前调用也安全（只是暂无输出）。"""
    return logging.getLogger(LOGGER_NAME)


def log_exception(message: str, exc: BaseException | None = None) -> None:
    """在 except 块中记录异常；传入 exc 时会带上完整堆栈。"""
    logger = get_logger()
    if exc is None:
        logger.error("%s", message, exc_info=True)
    else:
        logger.error("%s: %s", message, exc, exc_info=exc)


def default_data_dir() -> Path:
    """与 main.app_dir()/data_dir() 一致的 data 目录，供独立运行时使用。"""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent
    return base / "data"


def setup_logging(data_dir=None, app_name: str = "") -> logging.Logger:
    """初始化日志文件并安装全局报错钩子，可重复调用。"""
    global _configured, _native_log_file

    logger = get_logger()
    if _configured:
        return logger

    try:
        directory = Path(data_dir) if data_dir else default_data_dir()
        directory.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            directory / LOG_FILENAME,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(handler)
    except Exception:
        # 日志文件不可写时不能影响程序本身
        logger.addHandler(logging.NullHandler())
        _configured = True
        return logger

    _configured = True
    _install_exception_hooks(logger)
    _install_qt_message_handler(logger)
    _install_dialog_logging(logger)
    _install_stderr_fallback(logger)
    _enable_faulthandler(directory)

    logger.info("=" * 60)
    logger.info(
        "启动 %s | Python %s | 打包运行=%s | 程序=%s",
        app_name or "应用",
        platform.python_version(),
        bool(getattr(sys, "frozen", False)),
        sys.executable,
    )
    return logger


def _install_exception_hooks(logger: logging.Logger) -> None:
    previous_excepthook = sys.excepthook

    def _excepthook(exc_type, exc_value, exc_tb):
        try:
            detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            logger.critical("未捕获异常：\n%s", detail)
        except Exception:
            pass
        try:
            # 继续执行原有钩子（例如 main.py 里写 crash.log 的那一个）
            if previous_excepthook is not None:
                previous_excepthook(exc_type, exc_value, exc_tb)
        except Exception:
            pass

    sys.excepthook = _excepthook

    previous_thread_hook = threading.excepthook

    def _thread_excepthook(args):
        try:
            if args.exc_type is SystemExit:
                return
            thread_name = args.thread.name if args.thread is not None else "未知线程"
            detail = "".join(
                traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
            )
            logger.critical("线程「%s」未捕获异常：\n%s", thread_name, detail)
        except Exception:
            pass
        try:
            previous_thread_hook(args)
        except Exception:
            pass

    threading.excepthook = _thread_excepthook

    if hasattr(sys, "unraisablehook"):
        previous_unraisable_hook = sys.unraisablehook

        def _unraisablehook(args):
            try:
                logger.error(
                    "无法抛出的异常（%s）：%s",
                    args.err_msg or "无附加信息",
                    args.exc_value,
                    exc_info=args.exc_value,
                )
            except Exception:
                pass
            try:
                previous_unraisable_hook(args)
            except Exception:
                pass

        sys.unraisablehook = _unraisablehook


def _install_qt_message_handler(logger: logging.Logger) -> None:
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:
        return

    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def _handler(mode, context, message):
        try:
            level = levels.get(mode, logging.INFO)
            if level == logging.DEBUG:
                return
            logger.log(level, "[Qt] %s", message)
        except Exception:
            pass

    qInstallMessageHandler(_handler)


def _install_dialog_logging(logger: logging.Logger) -> None:
    """记录 QMessageBox 弹窗文字，覆盖已被 except 捕获、只弹窗提示的报错。"""
    try:
        from PySide6.QtWidgets import QMessageBox
    except Exception:
        return

    def _wrap(method_name: str, level: int) -> None:
        original = getattr(QMessageBox, method_name, None)
        if original is None:
            return

        def _logged(parent, title, text, *args, **kwargs):
            try:
                logger.log(level, "弹窗[%s] %s：%s", method_name, title, text)
            except Exception:
                pass
            return original(parent, title, text, *args, **kwargs)

        try:
            setattr(QMessageBox, method_name, staticmethod(_logged))
        except Exception:
            pass

    _wrap("warning", logging.WARNING)
    _wrap("critical", logging.ERROR)


def _install_stderr_fallback(logger: logging.Logger) -> None:
    """无控制台打包运行时 sys.stderr 为 None，把写入重定向到日志。"""
    if sys.stderr is not None:
        return

    class _StreamToLogger:
        def __init__(self):
            self._busy = False

        def write(self, text):
            text = (text or "").rstrip()
            if not text or self._busy:
                return
            self._busy = True
            try:
                logger.warning("[stderr] %s", text)
            except Exception:
                pass
            finally:
                self._busy = False

        def flush(self):
            pass

    sys.stderr = _StreamToLogger()


def _enable_faulthandler(directory: Path) -> None:
    """记录段错误等进程级崩溃堆栈到 crash_native.log。"""
    global _native_log_file
    try:
        _native_log_file = open(directory / NATIVE_LOG_FILENAME, "a", encoding="utf-8")
        faulthandler.enable(file=_native_log_file, all_threads=True)
    except Exception:
        _native_log_file = None
