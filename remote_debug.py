# -*- coding: utf-8 -*-
"""远程调试：直连游戏客户端的文本 RPC 服务。

客户端协议（与 y11tool 的远程调试一致）：
  * 每条命令是一行 UTF-8 文本，以 \\n 结尾；
  * 每条命令新建一次 TCP 连接，发送后读取回包并断开；
  * for_caller.xxx(...)      协议调用，原样发送；
  * client:SomeUI.OnClick()  直接执行客户端语句（client: 只是本工具的前缀）；
  * 其他内容                 作为 GM 指令，包装成 rpc_run_gm_orders("...")；
  * 同步日志：rpc_start_sync_log() 启动后，每秒 rpc_do_sync_log()
    返回一个 JSON 数组（无新日志时为 "[]"），rpc_stop_sync_log() 停止。

「启动客户端并开监听」：启动或复用客户端窗口，按一次 F1 打开客户端自带的
调试控制台（debug/pf.lua，127.0.0.1:9527），再通过它执行
start_remote_debug(端口, "0.0.0.0")。F1 需要 SHOW_TEST_BTN 或测试环境。

界面不提供「等待回包」；需要该能力时直接用 RemoteDebugClient.wait_result()。

单独运行：
    python remote_debug.py

并入现有工具（main.py 中两行）：
    from remote_debug import RemoteDebugPage
    self.tabs.addTab(RemoteDebugPage(), "远程调试")

本模块只读写 data/remote_debug.json，不修改工具原有的数据文件。
"""

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

DEFAULT_PORT = 9301
PORT_CHOICES = ["9301", "9302", "9303", "9304", "9305"]
SETTINGS_NAME = "remote_debug.json"
HISTORY_LIMIT = 50
MAX_RESPONSE = 8 * 1024 * 1024
READ_IDLE_TIMEOUT = 0.5
OUTPUT_BLOCK_LIMIT = 8000

# 同步日志：活跃时快速轮询，空闲时逐步退避
SYNC_MIN_INTERVAL = 0.25
SYNC_MAX_INTERVAL = 2.0
SYNC_REQUEST_TIMEOUT = 2.0
SYNC_RETRY = 2
SYNC_RETRY_DELAY = 0.5
SYNC_FLUSH_INTERVAL = 0.1

# 客户端 qcinit.lua 的 rpc_stop_sync_log() 只删除 hook，没有清空 _g["logHookId"]，
# 而 rpc_start_sync_log() 又用 `if not _g["logHookId"]` 判断是否需要注册 hook，
# 于是「停止后再启动」不会重新注册，rpc_do_sync_log() 永远返回 []。启动前先重置该字段。
SYNC_RESET_HOOK = (
    "(function() "
    "if _g['logHookId'] then "
    "pcall(HOOK.delHook,HOOK.HOOK.HookType.ClientPrintReport,_g['logHookId']) "
    "end; "
    "_g['logHookId']=nil; "
    "return 'ok' "
    "end)()"
)

# F1 会执行 debug/pf.lua，它在 9527 端口开一个调试控制台：收到的每一行都交给
# loadstring 执行。比往游戏窗口里模拟打字可靠，只需按一次 F1。
PF_CONSOLE_PORT = 9527
# pf.lua 的 consoleLoop 在客户端断开后会在已关闭的 socket 上反复 select，
# 把整个 Lua 虚拟机卡死。它内部状态用的是脚本自己的局部 _g 表，注入的代码改不到，
# 只能发它自带的 stopListen 命令，让控制台自己关闭连接，工具再断开。
AUTO_LISTEN_TEMPLATE = (
    'if not start_remote_debug then '
    'pcall(dofile, "script/base/module/debug/qcinit.lua") end; '
    'start_remote_debug(%d, "0.0.0.0")'
)
AUTO_WINDOW_TIMEOUT = 90.0
AUTO_PORT_TIMEOUT = 6.0

COLORS = {
    "send": "#0969da",
    "recv": "#1a7f37",
    "error": "#cf222e",
    "warn": "#9a6700",
    "system": "#8250df",
    "info": "#57606a",
}

PAGE_QSS = """
QPlainTextEdit { border: 1px solid #c9ced6; border-radius: 6px; background: #ffffff; }
QTableWidget { border: 1px solid #c9ced6; border-radius: 6px; }
"""


# ---------------------------------------------------------------- 文件与工具
def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    d = app_dir() / "data"
    try:
        d.mkdir(exist_ok=True)
    except OSError:
        pass
    return d


def settings_file() -> Path:
    return data_dir() / SETTINGS_NAME


def load_settings() -> dict:
    path = settings_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    try:
        settings_file().write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def local_ip() -> str:
    try:
        return socket.gethostbyname(socket.getfqdn(socket.gethostname()))
    except Exception:
        return "127.0.0.1"


def parse_keywords(text: str):
    return [k for k in re.split(r"[\s,，;；]+", text.strip()) if k]


def shorten(text: str, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（已截断，共 %d 字符）" % len(text)


# ---------------------------------------------------------------- 客户端自动化
VK_F1 = 0x70
VK_MENU = 0x12
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002


class ClientAutomationError(RuntimeError):
    pass


class _Win32:
    """窗口查找、前台切换与键盘注入（仅 Windows）。"""

    _instance = None

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        import ctypes
        from ctypes import wintypes as wt

        self.ctypes = ctypes
        self.wt = wt
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        ULONG_PTR = ctypes.c_size_t

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wt.WORD),
                ("wScan", wt.WORD),
                ("dwFlags", wt.DWORD),
                ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wt.LONG),
                ("dy", wt.LONG),
                ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD),
                ("time", wt.DWORD),
                ("dwExtraInfo", ULONG_PTR),
            ]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [
                ("uMsg", wt.DWORD),
                ("wParamL", wt.WORD),
                ("wParamH", wt.WORD),
            ]

        class _INPUTUNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]

        self.KEYBDINPUT = KEYBDINPUT
        self.INPUT = INPUT
        self.WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        u, k = self.user32, self.kernel32
        u.EnumWindows.argtypes = [self.WNDENUMPROC, wt.LPARAM]
        u.EnumWindows.restype = wt.BOOL
        u.IsWindowVisible.argtypes = [wt.HWND]
        u.IsWindowVisible.restype = wt.BOOL
        u.IsIconic.argtypes = [wt.HWND]
        u.IsIconic.restype = wt.BOOL
        u.GetForegroundWindow.restype = wt.HWND
        u.SetForegroundWindow.argtypes = [wt.HWND]
        u.SetForegroundWindow.restype = wt.BOOL
        u.BringWindowToTop.argtypes = [wt.HWND]
        u.BringWindowToTop.restype = wt.BOOL
        u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        u.ShowWindow.restype = wt.BOOL
        u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        u.GetWindowThreadProcessId.restype = wt.DWORD
        u.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
        u.AttachThreadInput.restype = wt.BOOL
        u.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        u.GetClassNameW.restype = ctypes.c_int
        u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        u.GetWindowRect.restype = wt.BOOL
        u.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        u.SendInput.restype = wt.UINT
        u.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ULONG_PTR]
        u.keybd_event.restype = None
        k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k.OpenProcess.restype = wt.HANDLE
        k.QueryFullProcessImageNameW.argtypes = [
            wt.HANDLE,
            wt.DWORD,
            wt.LPWSTR,
            ctypes.POINTER(wt.DWORD),
        ]
        k.QueryFullProcessImageNameW.restype = wt.BOOL
        k.CloseHandle.argtypes = [wt.HANDLE]
        k.CloseHandle.restype = wt.BOOL
        k.GetCurrentThreadId.restype = wt.DWORD

    def _process_image(self, pid):
        handle = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            size = self.wt.DWORD(32768)
            buffer = self.ctypes.create_unicode_buffer(size.value)
            if self.kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, self.ctypes.byref(size)
            ):
                return buffer.value
            return ""
        finally:
            self.kernel32.CloseHandle(handle)

    def _class_name(self, hwnd):
        buffer = self.ctypes.create_unicode_buffer(256)
        if self.user32.GetClassNameW(hwnd, buffer, 256):
            return buffer.value
        return ""

    def _rect(self, hwnd):
        rect = self.wt.RECT()
        if self.user32.GetWindowRect(hwnd, self.ctypes.byref(rect)):
            return (rect.left, rect.top, rect.right, rect.bottom)
        return (0, 0, 0, 0)

    def find_window(self, exe_path=""):
        """按进程可执行文件路径查找游戏主窗口，返回 (hwnd, pid, image)。"""
        ctypes = self.ctypes
        wt = self.wt
        wanted_path = os.path.normcase(os.path.abspath(exe_path)) if exe_path else ""
        wanted_name = os.path.basename(wanted_path) if wanted_path else ""
        foreground = int(self.user32.GetForegroundWindow() or 0)
        found = []

        def callback(hwnd, _lparam):
            if not self.user32.IsWindowVisible(hwnd):
                return True
            pid = wt.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return True
            image = self._process_image(pid.value)
            if wanted_path:
                image_norm = os.path.normcase(image)
                if not image_norm or (
                    image_norm != wanted_path
                    and os.path.basename(image_norm) != wanted_name
                ):
                    return True
            class_name = self._class_name(hwnd)
            # 排除桌面、任务栏、控制台等非游戏窗口
            if class_name in (
                "Shell_TrayWnd",
                "Progman",
                "WorkerW",
                "ConsoleWindowClass",
            ):
                return True
            left, top, right, bottom = self._rect(hwnd)
            width, height = right - left, bottom - top
            if width < 320 or height < 200:
                return True
            score = width * height
            if int(hwnd) == foreground:
                score += 10 ** 12
            if class_name == "UnityWndClass":
                score += 10 ** 11
            found.append((score, hwnd, pid.value, image))
            return True

        self.user32.EnumWindows(self.WNDENUMPROC(callback), 0)
        if not found:
            return None
        _score, hwnd, pid, image = max(found)
        return hwnd, pid, image

    def focus(self, hwnd):
        """把窗口切到前台并确认成功。"""
        u = self.user32
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, SW_RESTORE)
        u.ShowWindow(hwnd, SW_RESTORE)
        if int(u.GetForegroundWindow() or 0) == int(hwnd):
            return True
        # 模拟一次 Alt 抬起，解除 Windows 的前台窗口限制
        u.keybd_event(VK_MENU, 0, 0, 0)
        u.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        u.SetForegroundWindow(hwnd)
        time.sleep(0.15)
        if int(u.GetForegroundWindow() or 0) == int(hwnd):
            return True

        fg = u.GetForegroundWindow()
        current = self.kernel32.GetCurrentThreadId()
        attached = []
        for thread_id in (
            u.GetWindowThreadProcessId(fg, None) if fg else 0,
            u.GetWindowThreadProcessId(hwnd, None),
        ):
            if (
                thread_id
                and thread_id != current
                and u.AttachThreadInput(current, thread_id, True)
            ):
                attached.append(thread_id)
        try:
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
            time.sleep(0.15)
        finally:
            for thread_id in attached:
                u.AttachThreadInput(current, thread_id, False)
        return int(u.GetForegroundWindow() or 0) == int(hwnd)

    def press(self, vk):
        items = []
        for flags in (0, KEYEVENTF_KEYUP):
            inp = self.INPUT()
            inp.type = INPUT_KEYBOARD
            inp.ki = self.KEYBDINPUT(vk, 0, flags, 0, 0)
            items.append(inp)
        array = (self.INPUT * len(items))(*items)
        self.user32.SendInput(len(items), array, self.ctypes.sizeof(self.INPUT))


def _ensure_client_window(worker, exe_path):
    w32 = _Win32.get()
    found = w32.find_window(exe_path)
    if found is not None:
        hwnd, pid, image = found
        worker.log.emit(
            "复用已运行的客户端：%s（PID %d）" % (image or exe_path, pid), "system"
        )
        return hwnd

    path = Path(os.path.abspath(exe_path))
    worker.log.emit("启动客户端：%s" % path, "system")
    try:
        subprocess.Popen([str(path)], cwd=str(path.parent))
    except OSError as exc:
        raise ClientAutomationError("启动客户端失败：%s" % exc) from exc

    deadline = time.monotonic() + AUTO_WINDOW_TIMEOUT
    while time.monotonic() < deadline:
        if worker.stop_event.wait(0.5):
            return None
        found = w32.find_window(exe_path)
        if found is not None:
            hwnd, pid, _image = found
            worker.log.emit("客户端窗口已出现（PID %d），等待初始化…" % pid, "info")
            if worker.stop_event.wait(4.0):
                return None
            return hwnd
    raise ClientAutomationError(
        "等待客户端窗口超时（%d 秒），请确认程序能否正常启动" % AUTO_WINDOW_TIMEOUT
    )


def _wait_listen_port(worker, port, timeout):
    client = RemoteDebugClient("127.0.0.1", port, timeout=1.5)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if worker.stop_event.is_set():
            return False
        try:
            client.ping()
            return True
        except OSError:
            time.sleep(0.4)
    return False


def _pf_console_command(command, timeout=3.0):
    """连 F1 控制台发一行 Lua，再发 stopListen 让控制台自己断开。

    返回收到的回复。工具不能直接断开：pf.lua 的 consoleLoop 之后会在已关闭的
    socket 上死循环，卡死客户端。
    """
    with socket.create_connection(
        ("127.0.0.1", PF_CONSOLE_PORT), timeout=timeout
    ) as sock:
        sock.settimeout(0.3)
        sock.sendall((command + "\n").encode("utf-8"))
        chunks = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                continue
            if not data:
                break
            chunks.append(data)
            if b"waiting connect cli" in b"".join(chunks):
                break

        # stopListen 是控制台自带的 DEBUG_METHODS 命令，会关闭所有连接和监听。
        # 重复发送直到客户端关闭本连接，避免请求没被处理就提前断开。
        deadline = time.monotonic() + 2.0
        next_stop = time.monotonic()
        while time.monotonic() < deadline:
            if time.monotonic() >= next_stop:
                try:
                    sock.sendall(b"stopListen\n")
                except OSError:
                    break
                next_stop = time.monotonic() + 0.8
            try:
                data = sock.recv(65536)
            except socket.timeout:
                continue
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks).decode("utf-8", errors="replace").strip()


def auto_open_listen(worker, exe_path, port):
    """启动/复用客户端，按 F1 打开 pf.lua 调试控制台并开启远程调试端口。"""
    w32 = _Win32.get()
    hwnd = _ensure_client_window(worker, exe_path)
    if hwnd is None:
        return
    command = AUTO_LISTEN_TEMPLATE % port
    reply = ""
    last_error = None
    for attempt in range(1, 4):
        if worker.stop_event.is_set():
            return
        if not w32.focus(hwnd):
            raise ClientAutomationError(
                "无法切换到客户端窗口，请先手动点一下客户端再重试"
            )
        worker.log.emit("按 F1 打开客户端调试控制台（第 %d/3 次）…" % attempt, "info")
        w32.press(VK_F1)
        if worker.stop_event.wait(1.2):
            return
        try:
            reply = _pf_console_command(command)
        except OSError as exc:
            last_error = exc
            reply = ""
        if "waiting connect cli" in reply or "listen ok" in reply:
            break
        for line in reply.splitlines():
            if line.startswith("ERROR"):
                last_error = line.strip()
        reply = ""
        if worker.stop_event.wait(1.0):
            return

    if not reply:
        if _wait_listen_port(worker, port, 1.0):
            worker.result.emit("127.0.0.1:%d" % port)
            return
        raise ClientAutomationError(
            "无法通过客户端 F1 调试控制台开启监听（127.0.0.1:%d）：%s；"
            "F1 需要 SHOW_TEST_BTN=true 或处于测试环境，请确认客户端已进入登录/游戏界面"
            % (PF_CONSOLE_PORT, last_error or "控制台没有有效回复")
        )

    worker.log.emit("F1 控制台回复：%s" % shorten(reply, 300), "recv")
    if not _wait_listen_port(worker, port, AUTO_PORT_TIMEOUT):
        raise ClientAutomationError(
            "命令已执行，但 127.0.0.1:%d 仍不可达，请查看客户端日志" % port
        )
    worker.result.emit("127.0.0.1:%d" % port)


# ---------------------------------------------------------------- 协议客户端
class RemoteDebugError(RuntimeError):
    pass


class RemoteDebugClient:
    """游戏客户端远程调试接口，不依赖 Qt，可直接在脚本里使用。

    示例：
        d = RemoteDebugClient("192.168.1.10", 9301)
        d.gm_order("super")
        print(d.call("for_caller.s_req_union_list()"))
        info = d.wait_result("for_caller.s_req_union_list()", "c_req_union_list")
    """

    def __init__(self, host="127.0.0.1", port=DEFAULT_PORT, timeout=5.0):
        self.host = host
        self.port = int(port)
        self.timeout = timeout

    def send(self, text: str) -> str:
        """发送一行命令，返回客户端回包文本。"""
        payload = ("%s\n" % (text or "").strip()).encode("utf-8")
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as sock:
            sock.sendall(payload)
            chunks = []
            total = 0
            first = True
            while total < MAX_RESPONSE:
                try:
                    data = sock.recv(65536)
                except socket.timeout:
                    break
                if not data:
                    break
                chunks.append(data)
                total += len(data)
                if first:
                    # 客户端一般回完就断开；若保持连接，最多再等一小段收尾
                    sock.settimeout(READ_IDLE_TIMEOUT)
                    first = False
        return b"".join(chunks).decode("utf-8", errors="replace").strip()

    def ping(self) -> bool:
        with socket.create_connection((self.host, self.port), timeout=self.timeout):
            return True

    def gm_order(self, order: str) -> str:
        return self.send('rpc_run_gm_orders("%s")' % order)

    def call(self, expression: str) -> str:
        result = self.send(expression)
        if result == "error":
            raise RemoteDebugError("协议返回 error，需要检查调用")
        return result

    def run_client_code(self, code: str) -> str:
        code = (code or "").strip()
        code = code.replace("\r\n", "#").replace("\r", "#").replace("\n", "#")
        code = code.replace("'", "\\'")
        result = self.send("rpc_run_code('%s')" % code)
        if result == "error":
            raise RemoteDebugError("客户端执行报错，注意查看日志")
        return result

    def wait_result(self, caller: str, maker_name: str, timeout=10):
        """等待指定 maker 回包，返回解析后的 JSON（失败则返回原文）。"""
        caller = (caller or "").strip().replace("'", "\\'")
        self.call("rpc_call_and_wait_result('%s')" % caller)
        time.sleep(0.2)
        rounds = max(1, int(timeout * 2))
        for _ in range(rounds):
            raw = self.send("rpc_get_maker_result()")
            for part in raw.split("Maker."):
                if part.startswith(maker_name + "("):
                    start = part.find("(") + 1
                    end = part.rfind(")")
                    if end > start:
                        data = part[start:end]
                        try:
                            return json.loads(data)
                        except json.JSONDecodeError:
                            pass
                        try:
                            return json.loads(data.replace('\\"', '"'))
                        except json.JSONDecodeError:
                            return data
            time.sleep(0.5)
        raise RemoteDebugError("等待 %s 回包超时（%ss）" % (maker_name, timeout))

    def dump_ui_tree(self):
        raw = self.send("rpc_dump_ui_tree()")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RemoteDebugError("UI 树解析失败：%s" % raw[:200]) from exc

    def ui_rows(self):
        """把 UI 树展开成 [(对象路径, 文本)]。"""
        rows = []
        tree = self.dump_ui_tree()
        if not isinstance(tree, dict):
            raise RemoteDebugError("UI 树格式异常：%r" % (tree,))
        for objs in tree.values():
            for item in objs or []:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                *path, text = item
                rows.append(("/".join(str(p) for p in path), str(text)))
        return rows

    @staticmethod
    def parse_path(path: str) -> str:
        parts = []
        for piece in str(path).split("/"):
            parts.append(piece if piece.isdigit() else '"%s"' % piece)
        return "{" + ",".join(parts) + "}"

    def exists(self, obj_path: str) -> bool:
        return self.send("rpc_exists_obj(%s)" % self.parse_path(obj_path)) == "yes"

    def click(self, obj_path: str) -> None:
        result = self.send("rpc_click_obj(%s)" % self.parse_path(obj_path))
        if result != "yes":
            raise RemoteDebugError("%s 不存在" % obj_path)

    def highlight(self, obj_path: str, fade_time=1) -> None:
        result = self.send(
            "rpc_hightlight_obj(%s,%s)" % (self.parse_path(obj_path), fade_time)
        )
        if result != "yes":
            raise RemoteDebugError("%s 不存在" % obj_path)


# ---------------------------------------------------------------- 后台线程
class _Worker(QThread):
    log = Signal(str, str)
    result = Signal(object)
    done = Signal()

    def __init__(self, fn):
        super().__init__()
        self._fn = fn
        self.stop_event = threading.Event()

    def run(self):
        try:
            self._fn(self)
        except Exception as exc:
            self.log.emit("%s: %s" % (type(exc).__name__, exc), "error")
        finally:
            self.done.emit()


# ---------------------------------------------------------------- 弹窗
class CodeDialog(QDialog):
    """运行代码片段（rpc_run_code）。"""

    def __init__(self, parent, execute):
        super().__init__(parent)
        self._execute = execute
        self.setWindowTitle("运行代码片段")
        self.resize(760, 460)

        root = QVBoxLayout(self)
        tip = QLabel("发送 rpc_run_code(...)；换行会被替换成 #，因此只能写单行代码。")
        tip.setWordWrap(True)
        root.addWidget(tip)

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("例如：print(DATA_CENTER.getUserId())")
        self.editor.setFont(QFont("Consolas", 10))
        root.addWidget(self.editor, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        run_btn = QPushButton("执行")
        run_btn.clicked.connect(self._on_run)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        row.addWidget(run_btn)
        row.addWidget(close_btn)
        root.addLayout(row)

    def _on_run(self):
        code = self.editor.toPlainText().strip()
        if code:
            self._execute(code)


class UITreeDialog(QDialog):
    """显示 rpc_dump_ui_tree() 结果，双击复制点击代码。"""

    def __init__(self, parent, rows):
        super().__init__(parent)
        self.setWindowTitle("UI 树")
        self.resize(900, 640)
        self.rows = rows

        root = QVBoxLayout(self)
        top = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("输入路径或文本过滤")
        top.addWidget(self.filter_edit, 1)
        self.state_label = QLabel("")
        top.addWidget(self.state_label)
        root.addLayout(top)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["对象路径", "文本"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.doubleClicked.connect(lambda _index: self.copy_selected())
        root.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        bottom.addWidget(QLabel("双击一行复制 d.click(...) 代码"))
        bottom.addStretch(1)
        copy_btn = QPushButton("复制代码")
        copy_btn.clicked.connect(self.copy_selected)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(copy_btn)
        bottom.addWidget(close_btn)
        root.addLayout(bottom)

        self.filter_edit.textChanged.connect(self._fill)
        self._fill()

    def _fill(self):
        key = self.filter_edit.text().strip().lower()
        rows = [
            row
            for row in self.rows
            if not key or key in row[0].lower() or key in row[1].lower()
        ]
        self.table.setRowCount(len(rows))
        for index, (path, text) in enumerate(rows):
            self.table.setItem(index, 0, QTableWidgetItem(path))
            self.table.setItem(index, 1, QTableWidgetItem(text))
        self.state_label.setText("%d / %d" % (len(rows), len(self.rows)))

    def copy_selected(self):
        row = self.table.currentRow()
        if row < 0:
            return
        path = self.table.item(row, 0).text()
        code = 'd.click("%s")' % path
        QGuiApplication.clipboard().setText(code)
        self.state_label.setText("已复制：%s" % code)


# ---------------------------------------------------------------- 页签
class RemoteDebugPage(QWidget):
    def __init__(self):
        super().__init__()
        self._settings = load_settings()
        self._workers = []
        self._send_worker = None
        self._sync_worker = None
        self._test_worker = None
        self._tree_worker = None
        self._auto_worker = None
        self._tree_dialog = None
        self._code_dialog = None

        self.setWindowTitle("远程调试")
        self.setStyleSheet(PAGE_QSS)
        self._build_ui()
        self._restore_settings()
        self._append(
            "就绪。自动模式下：for_caller → 协议调用，client: → 客户端语句，其他 → GM 指令。",
            "system",
        )

    # ---------- 界面 ----------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        conn_row = QHBoxLayout()
        conn_row.setSpacing(8)
        conn_row.addWidget(QLabel("客户端 IP"))
        self.host_edit = QLineEdit(local_ip())
        self.host_edit.setFixedWidth(150)
        conn_row.addWidget(self.host_edit)
        conn_row.addWidget(QLabel("端口"))
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.addItems(PORT_CHOICES)
        self.port_combo.setCurrentText(str(DEFAULT_PORT))
        self.port_combo.setFixedWidth(90)
        conn_row.addWidget(self.port_combo)
        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self._on_test)
        conn_row.addWidget(self.test_btn)
        self.status_label = QLabel("未连接")
        self.status_label.setStyleSheet("color:#57606a")
        conn_row.addWidget(self.status_label)
        conn_row.addStretch(1)
        root.addLayout(conn_row)

        auto_row = QHBoxLayout()
        auto_row.setSpacing(8)
        auto_row.addWidget(QLabel("客户端程序"))
        self.client_exe_edit = QLineEdit()
        self.client_exe_edit.setPlaceholderText(
            "游戏客户端 exe；留空时点击按钮再选择"
        )
        auto_row.addWidget(self.client_exe_edit, 1)
        self.client_browse_btn = QPushButton("浏览…")
        self.client_browse_btn.clicked.connect(self._on_browse_client)
        auto_row.addWidget(self.client_browse_btn)
        self.auto_btn = QPushButton("启动客户端并开监听")
        self.auto_btn.setToolTip(
            "启动客户端或复用已运行窗口，按 F1 打开客户端调试控制台后执行 "
            'start_remote_debug(端口, "0.0.0.0")'
        )
        self.auto_btn.clicked.connect(self._on_auto_listen)
        auto_row.addWidget(self.auto_btn)
        root.addLayout(auto_row)

        cmd_row = QHBoxLayout()
        cmd_row.setSpacing(8)
        self.mode_combo = QComboBox()
        for label, key in (
            ("自动", "auto"),
            ("协议调用", "caller"),
            ("客户端语句", "client"),
            ("GM 指令", "gm"),
        ):
            self.mode_combo.addItem(label, key)
        cmd_row.addWidget(self.mode_combo)

        self.command = QComboBox()
        self.command.setEditable(True)
        self.command.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.command.setMinimumWidth(420)
        self.command.lineEdit().setPlaceholderText(
            "for_caller.xxx() / client:xxx() / GM 指令；多条用 ### 分隔，回车发送"
        )
        self.command.lineEdit().returnPressed.connect(self._on_send)
        cmd_row.addWidget(self.command, 1)

        cmd_row.addWidget(QLabel("重复"))
        self.repeat_spin = QSpinBox()
        self.repeat_spin.setRange(1, 999)
        self.repeat_spin.setValue(1)
        self.repeat_spin.setFixedWidth(70)
        cmd_row.addWidget(self.repeat_spin)

        self.send_btn = QPushButton("发送")
        self.send_btn.setFixedWidth(90)
        self.send_btn.clicked.connect(self._on_send)
        cmd_row.addWidget(self.send_btn)
        root.addLayout(cmd_row)

        tool_row = QHBoxLayout()
        tool_row.setSpacing(8)
        self.code_btn = QPushButton("运行代码")
        self.code_btn.clicked.connect(self._on_run_code)
        tool_row.addWidget(self.code_btn)
        self.tree_btn = QPushButton("UI 树")
        self.tree_btn.clicked.connect(self._on_dump_tree)
        tool_row.addWidget(self.tree_btn)
        self.sync_btn = QPushButton("同步日志")
        self.sync_btn.clicked.connect(self._on_sync)
        tool_row.addWidget(self.sync_btn)
        self.stop_sync_btn = QPushButton("停止同步")
        self.stop_sync_btn.setEnabled(False)
        self.stop_sync_btn.clicked.connect(self._on_stop_sync)
        tool_row.addWidget(self.stop_sync_btn)
        self.clear_btn = QPushButton("清空输出")
        self.clear_btn.clicked.connect(self.output_clear)
        tool_row.addWidget(self.clear_btn)
        self.copy_btn = QPushButton("复制输出")
        self.copy_btn.clicked.connect(self._on_copy_output)
        tool_row.addWidget(self.copy_btn)
        tool_row.addSpacing(10)
        tool_row.addWidget(QLabel("过滤关键字"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("空格分隔，留空显示全部（仅同步日志）")
        tool_row.addWidget(self.filter_edit, 1)
        root.addLayout(tool_row)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFont("Consolas", 10))
        self.output.setMaximumBlockCount(OUTPUT_BLOCK_LIMIT)
        root.addWidget(self.output, 1)

    # ---------- 设置 ----------
    def _restore_settings(self):
        s = self._settings
        self.host_edit.setText(str(s.get("host") or local_ip()))
        self.port_combo.setCurrentText(str(s.get("port", DEFAULT_PORT)))
        self.client_exe_edit.setText(str(s.get("client_exe") or ""))
        history = s.get("history")
        if not isinstance(history, list):
            history = []
        for item in history[:HISTORY_LIMIT]:
            if item:
                self.command.addItem(str(item))
        if self.command.count():
            self.command.setCurrentIndex(0)
        index = self.mode_combo.findData(s.get("mode", "auto"))
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)

    def _persist(self):
        save_settings(
            {
                "host": self.host_edit.text().strip(),
                "port": self._port() or DEFAULT_PORT,
                "history": [
                    self.command.itemText(i) for i in range(self.command.count())
                ][:HISTORY_LIMIT],
                "mode": self.mode_combo.currentData(),
                "client_exe": self.client_exe_edit.text().strip(),
            }
        )

    # ---------- 输出 ----------
    def _append(self, text: str, level: str = "info"):
        color = COLORS.get(level, COLORS["info"])
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor.insertText("[%s] %s\n" % (time.strftime("%H:%M:%S"), text), fmt)
        self.output.setTextCursor(cursor)
        self.output.ensureCursorVisible()

    def output_clear(self):
        self.output.clear()

    def _on_copy_output(self):
        QGuiApplication.clipboard().setText(self.output.toPlainText())
        self._append("已复制输出到剪贴板", "system")

    # ---------- 后台任务 ----------
    def _start_worker(self, fn, on_done=None, on_result=None):
        worker = _Worker(fn)
        worker.log.connect(self._append)
        if on_result is not None:
            worker.result.connect(on_result)
        worker.done.connect(lambda w=worker: self._finish_worker(w, on_done))
        worker.finished.connect(worker.deleteLater)
        self._workers.append(worker)
        worker.start()
        return worker

    def _finish_worker(self, worker, callback):
        """线程结束后清理引用；不能在别处留已删除的 QThread 包装对象。"""
        try:
            if callback is not None:
                callback()
        finally:
            for attr in (
                "_send_worker",
                "_sync_worker",
                "_test_worker",
                "_tree_worker",
                "_auto_worker",
            ):
                if getattr(self, attr, None) is worker:
                    setattr(self, attr, None)
            if worker in self._workers:
                self._workers.remove(worker)

    # ---------- 连接 ----------
    def _port(self):
        try:
            port = int(self.port_combo.currentText().strip())
        except (TypeError, ValueError):
            return None
        return port if 1 <= port <= 65535 else None

    def _target(self):
        port = self._port()
        if port is None:
            self._append("端口无效：%s" % self.port_combo.currentText().strip(), "warn")
            return None
        return (self.host_edit.text().strip() or local_ip(), port)

    def _on_test(self):
        if self._test_worker is not None and self._test_worker.isRunning():
            return
        target = self._target()
        if target is None:
            return
        host, port = target
        client = RemoteDebugClient(host, port, timeout=3)
        self.test_btn.setEnabled(False)
        self.status_label.setText("连接中 %s:%s …" % (host, port))
        self.status_label.setStyleSheet("color:#9a6700")

        def work(worker):
            client.ping()
            worker.result.emit("%s:%s" % (host, port))

        self._test_worker = self._start_worker(
            work, on_result=self._on_test_ok, on_done=self._on_test_done
        )

    def _on_test_ok(self, target):
        self.status_label.setText("已连接 %s" % target)
        self.status_label.setStyleSheet("color:#1a7f37")

    def _on_test_done(self):
        self.test_btn.setEnabled(True)
        if self.status_label.text().startswith("连接中"):
            self.status_label.setText("连接失败")
            self.status_label.setStyleSheet("color:#cf222e")

    # ---------- 自动开启监听 ----------
    def _on_browse_client(self):
        current = self.client_exe_edit.text().strip().strip('"')
        start_dir = str(Path(current).parent) if current else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择游戏客户端", start_dir, "可执行文件 (*.exe);;所有文件 (*)"
        )
        if path:
            self.client_exe_edit.setText(os.path.normpath(path))
            self._persist()

    def _on_auto_listen(self):
        if self._auto_worker is not None and self._auto_worker.isRunning():
            return
        if sys.platform != "win32":
            self._append("自动开启监听仅支持 Windows", "warn")
            return
        port = self._port()
        if port is None:
            self._append("端口无效：%s" % self.port_combo.currentText().strip(), "warn")
            return
        exe = self.client_exe_edit.text().strip().strip('"')
        if not exe:
            self._on_browse_client()
            exe = self.client_exe_edit.text().strip().strip('"')
        if not exe:
            return
        if not Path(exe).is_file():
            self._append("客户端不存在：%s" % exe, "error")
            return
        self._persist()
        self.auto_btn.setEnabled(False)
        self.auto_btn.setText("启动中…")
        self.status_label.setText("正在启动客户端…")
        self.status_label.setStyleSheet("color:#9a6700")

        def work(worker):
            auto_open_listen(worker, exe, port)

        self._auto_worker = self._start_worker(
            work, on_result=self._on_auto_ok, on_done=self._on_auto_done
        )

    def _on_auto_ok(self, target):
        self.status_label.setText("监听已开启 %s" % target)
        self.status_label.setStyleSheet("color:#1a7f37")
        self._append("客户端监听已开启：%s" % target, "system")

    def _on_auto_done(self):
        self.auto_btn.setEnabled(True)
        self.auto_btn.setText("启动客户端并开监听")
        if self.status_label.text() == "正在启动客户端…":
            self.status_label.setText("启动失败")
            self.status_label.setStyleSheet("color:#cf222e")

    # ---------- 发送 ----------
    def _remember(self, text: str):
        index = self.command.findText(text)
        if index >= 0:
            self.command.removeItem(index)
        self.command.insertItem(0, text)
        self.command.setCurrentIndex(0)
        while self.command.count() > HISTORY_LIMIT:
            self.command.removeItem(self.command.count() - 1)

    def _on_send(self):
        if self._send_worker is not None and self._send_worker.isRunning():
            return
        cmd = self.command.currentText().strip()
        if not cmd:
            self._append("命令为空", "warn")
            return
        mode = self.mode_combo.currentData()
        jobs = [part.strip() for part in re.split(r"###|\r?\n", cmd) if part.strip()]
        if not jobs:
            return

        target = self._target()
        if target is None:
            return
        host, port = target
        repeat = self.repeat_spin.value()
        client = RemoteDebugClient(host, port)

        self._remember("###".join(jobs))
        self._persist()
        self.send_btn.setEnabled(False)
        self.send_btn.setText("发送中…")

        def work(worker):
            for round_index in range(repeat):
                if worker.stop_event.is_set():
                    return
                if repeat > 1:
                    worker.log.emit("—— 第 %d/%d 轮 ——" % (round_index + 1, repeat), "info")
                for job in jobs:
                    if worker.stop_event.is_set():
                        return
                    try:
                        self._run_one(worker, client, job, mode)
                    except Exception as exc:
                        worker.log.emit("%s: %s" % (type(exc).__name__, exc), "error")

        self._send_worker = self._start_worker(work, on_done=self._on_send_done)

    def _run_one(self, worker, client, job, mode):
        lower = job.lower()
        if mode == "gm":
            kind, text = "gm", job
        elif mode == "client":
            kind = "call"
            text = job.split(":", 1)[1].strip() if lower.startswith("client:") else job
        elif mode == "caller":
            kind, text = "call", job
        else:
            if lower.startswith("client:"):
                kind, text = "call", job.split(":", 1)[1].strip()
            elif lower.startswith("for_caller") or lower.startswith("rpc_"):
                kind, text = "call", job
            else:
                kind, text = "gm", job

        if kind == "gm":
            worker.log.emit('→ rpc_run_gm_orders("%s")' % text, "send")
            result = client.gm_order(text)
        else:
            worker.log.emit("→ %s" % text, "send")
            result = client.call(text)
        worker.log.emit("← " + (shorten(result) if result else "(空)"), "recv")

    def _on_send_done(self):
        self.send_btn.setEnabled(True)
        self.send_btn.setText("发送")

    # ---------- 代码片段 ----------
    def _on_run_code(self):
        if self._code_dialog is None:
            self._code_dialog = CodeDialog(self, self._execute_code)
        self._code_dialog.show()
        self._code_dialog.raise_()

    def _execute_code(self, code):
        target = self._target()
        if target is None:
            return
        host, port = target
        client = RemoteDebugClient(host, port)

        def work(worker):
            worker.log.emit("→ rpc_run_code(%r)" % code, "send")
            result = client.run_client_code(code)
            worker.log.emit("← " + (shorten(result) if result else "(空)"), "recv")

        self._start_worker(work)

    # ---------- UI 树 ----------
    def _on_dump_tree(self):
        if self._tree_worker is not None and self._tree_worker.isRunning():
            return
        target = self._target()
        if target is None:
            return
        host, port = target
        client = RemoteDebugClient(host, port, timeout=15)
        self.tree_btn.setEnabled(False)

        def work(worker):
            worker.result.emit(client.ui_rows())

        self._tree_worker = self._start_worker(
            work, on_result=self._show_tree, on_done=self._on_tree_done
        )

    def _show_tree(self, rows):
        if not rows:
            self._append("UI 树为空", "warn")
            return
        if self._tree_dialog is not None:
            self._tree_dialog.close()
        self._tree_dialog = UITreeDialog(self, rows)
        self._tree_dialog.show()
        self._tree_dialog.raise_()

    def _on_tree_done(self):
        self.tree_btn.setEnabled(True)

    # ---------- 同步日志 ----------
    def _on_sync(self):
        if self._sync_worker is not None and self._sync_worker.isRunning():
            return
        target = self._target()
        if target is None:
            return
        host, port = target
        keywords = parse_keywords(self.filter_edit.text())
        client = RemoteDebugClient(host, port, timeout=SYNC_REQUEST_TIMEOUT)
        self.sync_btn.setEnabled(False)
        self.stop_sync_btn.setEnabled(True)

        def work(worker):
            worker.log.emit(
                "开始同步调试信息"
                + ("（过滤：%s）" % " ".join(keywords) if keywords else "（显示全部）"),
                "system",
            )
            try:
                try:
                    client.call(SYNC_RESET_HOOK)
                except Exception as exc:
                    worker.log.emit(
                        "重置客户端日志钩子失败（继续启动）：%s" % exc, "warn"
                    )
                client.call("rpc_start_sync_log()")
            except Exception as exc:
                worker.log.emit("启动同步失败：%s" % exc, "error")
                return

            def poll():
                """拉一次日志；失败重试 SYNC_RETRY 次，返回 (原文, 最后一次异常)。"""
                error = None
                for attempt in range(SYNC_RETRY + 1):
                    try:
                        return client.call("rpc_do_sync_log()"), None
                    except Exception as exc:
                        error = exc
                        if attempt < SYNC_RETRY:
                            worker.log.emit(
                                "拉取日志失败，重试 %d/%d：%s"
                                % (attempt + 1, SYNC_RETRY, exc),
                                "warn",
                            )
                            if worker.stop_event.wait(SYNC_RETRY_DELAY):
                                return None, None
                return None, error

            buffer = []
            interval = SYNC_MIN_INTERVAL
            idle_rounds = 0
            last_flush = time.monotonic()
            try:
                while not worker.stop_event.is_set():
                    raw, error = poll()
                    if worker.stop_event.is_set():
                        break
                    if error is not None:
                        worker.log.emit(
                            "连续 %d 次拉取失败，停止同步：%s"
                            % (SYNC_RETRY + 1, error),
                            "error",
                        )
                        break

                    if raw and raw != "[]":
                        buffer.extend(self._parse_sync_lines(raw, keywords))
                        idle_rounds = 0
                        interval = SYNC_MIN_INTERVAL
                    else:
                        idle_rounds = min(idle_rounds + 1, 4)
                        interval = min(
                            SYNC_MAX_INTERVAL, SYNC_MIN_INTERVAL * (2 ** idle_rounds)
                        )

                    if buffer and time.monotonic() - last_flush >= SYNC_FLUSH_INTERVAL:
                        self._flush_sync_buffer(worker, buffer)
                        last_flush = time.monotonic()

                    if worker.stop_event.wait(interval):
                        break
            finally:
                self._flush_sync_buffer(worker, buffer)
                try:
                    client.call("rpc_stop_sync_log()")
                except Exception:
                    pass
                worker.log.emit("停止同步", "system")

        self._sync_worker = self._start_worker(work, on_done=self._on_sync_done)

    @staticmethod
    def _parse_sync_lines(raw, keywords):
        try:
            lines = json.loads(raw)
            if not isinstance(lines, list):
                lines = [str(lines)]
        except Exception:
            lines = raw.splitlines()
        result = []
        for line in lines:
            line = str(line).rstrip()
            if not line:
                continue
            if keywords and not any(key in line for key in keywords):
                continue
            result.append(("error" if keywords else "recv", line))
        return result

    @staticmethod
    def _flush_sync_buffer(worker, buffer):
        """一次信号输出整批日志，避免高流量时逐行刷新卡住界面。"""
        if not buffer:
            return
        level, line = buffer[0]
        lines = [line]
        for next_level, next_line in buffer[1:]:
            if next_level == level:
                lines.append(next_line)
            else:
                worker.log.emit("\n".join(lines), level)
                level, lines = next_level, [next_line]
        worker.log.emit("\n".join(lines), level)
        buffer.clear()

    def _on_stop_sync(self):
        if self._sync_worker is not None:
            self._sync_worker.stop_event.set()
        self.stop_sync_btn.setEnabled(False)

    def _on_sync_done(self):
        self.sync_btn.setEnabled(True)
        self.stop_sync_btn.setEnabled(False)

    # ---------- 退出 ----------
    def shutdown(self):
        """程序退出前调用：停止后台线程，避免 Qt 销毁运行中的 QThread。"""
        for worker in list(self._workers):
            worker.stop_event.set()
        for worker in list(self._workers):
            if worker.isRunning() and not worker.wait(6000):
                # 网络请求最长 5 秒，仍不退出说明卡在系统调用里，退出前强制终止
                worker.terminate()
                worker.wait(1000)
        self._persist()


def main():
    try:
        from app_log import setup_logging

        setup_logging(data_dir(), "远程调试")
    except Exception:
        pass

    app = QApplication(sys.argv)
    page = RemoteDebugPage()
    page.resize(1120, 720)
    page.show()
    app.aboutToQuit.connect(page.shutdown)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
