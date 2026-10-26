# -*- coding: utf-8 -*-
"""服务器指令工具

功能：维护服务器列表（名称 / 编号 / 机器），选中服务器后点击指令按钮，
通过 HTTP 调用服务端控制接口并等待返回结果，实时显示等待耗时。

当前内置更新 / 重启 / 清档 / 显示进程状态 / 服务器还原 / 服务器改动 /
切换赛季 / 切换群组 / 切换分支指令，后续新增指令只需在 COMMANDS 中增加一项。
"""
import json
import re
import subprocess
import sys
import time
import webbrowser
from datetime import date, timedelta
from pathlib import Path

import requests
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# 服务器机器 -> 指令接口前缀
MACHINE_PREFIX = {
    "241": "http://192.168.6.241:8627/wiz/ctlSrv?op=",
    "243": "http://192.168.6.243:8527/wiz/ctlSrv?op=",
}

# 指令表：新增指令在此追加一行，并在界面自动生成对应按钮
# check_proc=True 表示该指令执行成功后会自动执行一次进程状态检查
# param 表示该指令需要额外数字参数，param_name 为参数在界面与日志中的显示名
COMMANDS = [
    {"key": "update", "label": "更新", "op": "up.sh", "check_proc": True},
    {"key": "restart", "label": "重启", "op": "restart.sh", "check_proc": True},
    {"key": "drop", "label": "清档", "op": "drop.sh", "check_proc": False},
    {"key": "proc", "label": "显示进程状态", "op": "proc.sh", "check_proc": False},
    {"key": "revert", "label": "服务器还原", "op": "revert.sh", "check_proc": False},
    {"key": "swseason", "label": "切换赛季", "op": "swSeason.sh", "check_proc": True, "param": "season", "param_name": "赛季"},
    {"key": "swgroup", "label": "切换群组", "op": "swSrvGroup.sh", "check_proc": False, "param": "group", "param_name": "群组"},
    {"key": "switchbranch", "label": "切换分支", "op": "switch.sh", "check_proc": True,
     "dialog": "switch_branch", "timeout": None, "cancelable": True},
    {"key": "status", "label": "服务器改动", "op": "status.sh", "check_proc": False, "more": True},
    # 本地客户端指令（不走 HTTP）
    {"key": "clientcompile", "label": "客户端编译", "mode": "local", "no_config": True},
    {"key": "openclient", "label": "打开主干", "mode": "local"},
    {"key": "svnupdate", "label": "更新主干", "mode": "local", "svn": True},
    {"key": "openbranch", "label": "打开本周分支", "mode": "local"},
    {"key": "svnupdatebranch", "label": "更新本周分支", "mode": "local", "svn": True},
    {"key": "openbranch_last", "label": "打开上周分支", "mode": "local"},
    {"key": "svnupdatebranch_last", "label": "更新上周分支", "mode": "local", "svn": True},
]


def branch_options(count=5, today=None):
    """依次给出最近若干个周三的日期（含今天），并在最前面加上主干 main。"""
    today = today or date.today()
    offset = (today.weekday() - 2) % 7  # 2 = 周三
    latest = today - timedelta(days=offset)
    dates = [latest - timedelta(days=7 * i) for i in range(count)]
    return ["main"] + [d.strftime("%Y_%m_%d") for d in dates]


def latest_wednesday(today=None, weeks_ago=0):
    """返回从今天算起的最近一个周三（含今天）的 date 对象。

    weeks_ago: 0=本周三，1=上周三，2=上上周三，以此类推。
    """
    today = today or date.today()
    offset = (today.weekday() - 2) % 7
    wed = today - timedelta(days=offset)
    if weeks_ago:
        wed -= timedelta(days=7 * weeks_ago)
    return wed


def latest_wednesday_mmdd(today=None, weeks_ago=0):
    """最近一个周三的 MMDD 格式字符串，例如 '1022'。

    weeks_ago=1 时返回上周三。
    """
    return latest_wednesday(today, weeks_ago).strftime("%m%d")

# 「更新」成功后自动执行的进程状态检查指令，以及判定进程异常的响应关键字
PROC_OP = "proc.sh"
PROC_ABNORMAL_KEYWORD = "进程挂了"

# 切换分支日志接口和监控主进程报错日志接口
LOG_OP = "log.sh"
LOG_INTERVAL_MS = 1500  # 监控轮询间隔（毫秒）

# 可监控的报错日志类型：每种日志一个开关按钮，按「服务器 × 日志类型」独立监控
MONITOR_LOGS = [
    {"key": "main", "label": "主进程", "file": "main_error.log"},
    {"key": "fight", "label": "同步信息", "file": "fight_error.log"},
    {"key": "agent", "label": "agent进程", "file": "agent_error.log"},
    {"key": "battle", "label": "战斗", "file": "battle_error.log"},
    {"key": "worker", "label": "worker信息", "file": "worker_error.log"},
]

# 报错块起始标记：形如 [2026-10-21 10:34:54]traceback:
ERROR_BLOCK_RE = re.compile(r"(\[\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\]traceback:)", re.IGNORECASE)
ERROR_TIME_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]traceback:", re.IGNORECASE)
# 包含分支关键字时形如 y11_server_branch/2026_10_14，否则为 -s 参数标识的主干赛季
BRANCH_KEYWORD = "y11_server_branch"
BRANCH_RE = re.compile(r"y11_server_branch[/\\]+([^\s'\"]+)")
SEASON_ARG_RE = re.compile(r"(?<![\w-])-s\s*(\d+)")

REQUEST_TIMEOUT = 180  # 等待服务器响应的超时时间（秒）；切换分支不受此限制（timeout=None）
INLINE_BODY_LIMIT = 200  # 日志中直接内联展示的响应长度上限，超出则折叠为「展开响应」链接
MAX_BODY_STORE = 200000  # 弹窗中展示的响应原文上限

SERVER_BTN_QSS = """
QPushButton, QToolButton {
    text-align: left;
    padding: 8px 12px;
    border: 1px solid #c9ced6;
    border-radius: 6px;
    background: #f7f8fa;
    color: #1f2329;
}
QPushButton:hover, QToolButton:hover { background: #eef2f7; }
QPushButton:checked, QToolButton:checked {
    background: #d6e7ff;
    border: 1px solid #2f7ded;
    color: #10418f;
    font-weight: bold;
}
"""

MULTI_BTN_QSS = """
QPushButton:checked {
    background: #2f7ded;
    border: 1px solid #2f7ded;
    color: #ffffff;
    font-weight: bold;
}
"""

APP_QSS = """
QWidget { font-size: 13px; font-family: "Microsoft YaHei", "Segoe UI", sans-serif; }
QLabel#sectionTitle { font-weight: bold; color: #1f2329; padding: 2px 0; }
QPushButton { padding: 6px 12px; border: 1px solid #c9ced6; border-radius: 6px; background: #f7f8fa; }
QPushButton:hover { background: #eef2f7; }
QPushButton:disabled { color: #a0a4ab; background: #f0f1f3; }
QPlainTextEdit {
    border: 1px solid #c9ced6;
    border-radius: 6px;
    background: #ffffff;
    font-family: Consolas, "Microsoft YaHei", monospace;
}
"""


def app_dir() -> Path:
    """程序所在目录（打包后为 exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """数据 / 配置目录：app_dir() 下的 data 子目录（自动创建）。"""
    d = app_dir() / "data"
    d.mkdir(exist_ok=True)
    return d


DATA_DIR = data_dir()
DATA_FILE = DATA_DIR / "servers.json"
CLIENT_SETTINGS_FILE = DATA_DIR / "client_settings.json"


def _migrate_old_file(new_path: Path) -> None:
    """把 app_dir() 下同名旧文件迁移到 data 目录（一次性）。"""
    if new_path.exists():
        return
    old = app_dir() / new_path.name
    if old.exists():
        try:
            new_path.write_bytes(old.read_bytes())
        except Exception:
            pass


def load_servers():
    _migrate_old_file(DATA_FILE)
    if not DATA_FILE.exists():
        return []
    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    servers = data.get("servers") or []
    result = []
    for item in servers:
        name = str(item.get("name", "")).strip()
        number = str(item.get("number", "")).strip()
        machine = str(item.get("machine", "")).strip()
        if name and number and machine in MACHINE_PREFIX:
            result.append({"name": name, "number": number, "machine": machine})
    return result


def save_servers(servers):
    DATA_FILE.write_text(
        json.dumps({"servers": servers}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_client_settings():
    """加载客户端配置。

    配置项：
      - main_client_path: 主干客户端 exe 完整路径（如 D:/Game/Client/game/game.exe）
        主干 SVN 更新目录 = Path(main_client_path).parent（自动推导）
      - branch_root_dir: 分支版本根目录（如 E:/分支版本）
        本周分支路径 = branch_root_dir/MMDD/game/game.exe
        本周分支 SVN 更新目录 = branch_root_dir/MMDD/game
    旧配置里的 svn_update_dir 已废弃，忽略即可。
    """
    _migrate_old_file(CLIENT_SETTINGS_FILE)
    if not CLIENT_SETTINGS_FILE.exists():
        return {"main_client_path": "", "branch_root_dir": ""}
    try:
        data = json.loads(CLIENT_SETTINGS_FILE.read_text(encoding="utf-8"))
        return {
            "main_client_path": str(data.get("main_client_path", "")),
            "branch_root_dir": str(data.get("branch_root_dir", "")),
        }
    except Exception:
        return {"main_client_path": "", "branch_root_dir": ""}


def save_client_settings(settings):
    CLIENT_SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def now_str() -> str:
    return time.strftime("%H:%M:%S")


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def esc_multiline(text: str) -> str:
    return esc(text).replace("\n", "<br/>")


_BR_RE = re.compile(r"(?i)<br\s*/?>")


def clean_body(text: str) -> str:
    """服务端响应中常带字面 <br> 标签，展示时直接去掉。"""
    return _BR_RE.sub("", text)


class RequestWorker(QThread):
    """在后台线程发起 HTTP 请求，避免阻塞界面。"""

    done = Signal(bool, object, str, float)  # 是否成功, HTTP 状态码(异常时 None), 响应内容, 耗时

    def __init__(self, url, timeout):
        super().__init__()
        self._url = url
        self._timeout = timeout

    def run(self):
        start = time.monotonic()
        try:
            resp = requests.get(self._url, timeout=self._timeout)
            elapsed = time.monotonic() - start
            ok = 200 <= resp.status_code < 300
            self.done.emit(ok, resp.status_code, self._body(resp), elapsed)
        except Exception as exc:  # 网络异常 / 超时
            self.done.emit(False, None, str(exc), time.monotonic() - start)

    @staticmethod
    def _body(resp) -> str:
        encoding = resp.encoding
        if not encoding or encoding.lower() in ("iso-8859-1", "ascii"):
            resp.encoding = resp.apparent_encoding or "utf-8"
        text = clean_body(resp.text.strip())
        if len(text) > MAX_BODY_STORE:
            text = text[:MAX_BODY_STORE] + "\n……（内容过长，已截断）"
        return text


class MonitorPoller(QThread):
    """后台并发拉取所有监控目标的报错日志，避免多监控时阻塞界面。"""

    polled = Signal(list)  # [(mkey, ok, body, error_msg), ...]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._jobs = []  # [(mkey, url), ...]

    def set_jobs(self, jobs):
        self._jobs = jobs

    def run(self):
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(self._fetch, self._jobs))
        self.polled.emit(results)

    @staticmethod
    def _fetch(job):
        mkey, url = job
        try:
            resp = requests.get(url, timeout=3)
            if resp.status_code != 200:
                return mkey, False, "", "HTTP %s" % resp.status_code
            return mkey, True, clean_body(resp.text.strip()), ""
        except Exception as exc:
            return mkey, False, "", str(exc)


class _SVNWorker(QThread):
    """后台执行 svn update，完成后通过信号把结果送回 UI 线程。"""

    done = Signal(bool, int, str, str, str)  # ok, returncode, stdout, stderr, errmsg

    def __init__(self, svn_dir, parent=None):
        super().__init__(parent)
        self._svn_dir = svn_dir

    def run(self):
        try:
            result = subprocess.run(
                ["svn", "update"],
                cwd=self._svn_dir,
                capture_output=True, text=True, encoding="utf-8",
                timeout=600,
            )
            self.done.emit(True, result.returncode, result.stdout, result.stderr, "")
        except subprocess.TimeoutExpired:
            self.done.emit(False, -1, "", "", "svn update 超时（10 分钟）")
        except FileNotFoundError:
            self.done.emit(False, -1, "", "", "未找到 svn 命令，请确认已安装并加入 PATH")
        except Exception as exc:
            self.done.emit(False, -1, "", "", str(exc))


class ServerDialog(QDialog):
    """添加 / 编辑服务器的弹窗。"""

    def __init__(self, title, name="", number="", machine="241", parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(320)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：测试服 1")
        self.name_edit.setText(name)
        self.number_edit = QLineEdit()
        self.number_edit.setPlaceholderText("例如：1015")
        self.number_edit.setText(number)
        self.machine_box = QComboBox()
        self.machine_box.addItems(["241", "243"])
        self.machine_box.setCurrentText(machine)

        form = QFormLayout(self)
        form.addRow("服务器名称", self.name_edit)
        form.addRow("服务器编号", self.number_edit)
        form.addRow("服务器机器", self.machine_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _on_ok(self):
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "提示", "请填写服务器名称")
            return
        if not self.number_edit.text().strip():
            QMessageBox.warning(self, "提示", "请填写服务器编号")
            return
        self.accept()

    def values(self):
        return (
            self.name_edit.text().strip(),
            self.number_edit.text().strip(),
            self.machine_box.currentText(),
        )


class SwitchBranchDialog(QDialog):
    """切换分支：选择分支（main 或最近几个周三）并填写赛季数字。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("切换分支")
        self.setMinimumWidth(320)

        self.branch_box = QComboBox()
        self.branch_box.addItems(branch_options())
        self.season_spin = QSpinBox()
        self.season_spin.setRange(1, 9999)
        self.season_spin.setValue(1)
        self.season_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)

        form = QFormLayout(self)
        form.addRow("分支", self.branch_box)
        form.addRow("赛季数字", self.season_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return self.branch_box.currentText(), str(self.season_spin.value())


class MonitorErrorDialog(QDialog):
    """展示监控到的报错：标题行蓝色加大显示，弹窗高度自适应内容，置顶显示。"""

    def __init__(self, title, errors, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        blocks = errors if isinstance(errors, list) else [errors]
        parts = []
        for block in blocks:
            html = esc(block)
            # 报错标题行（[时间]traceback:）加大一号并标蓝
            html = ERROR_TIME_RE.sub(
                lambda m: '<span style="color:#2f7ded;font-size:16px;font-weight:bold">[%s]traceback:</span>'
                          % m.group(1),
                html,
            )
            parts.append(html.replace("\n", "<br/>"))

        view = QTextBrowser()
        view.setOpenExternalLinks(False)
        view.setHtml("<hr/>".join(parts))

        # 高度自适应文本内容，最长拉伸到整个屏幕可用高度
        doc = view.document()
        doc.setTextWidth(660)
        content_h = int(doc.size().height())
        screen_h = QApplication.primaryScreen().availableGeometry().height()
        self.resize(720, max(220, min(content_h + 110, screen_h - 40)))

        layout = QVBoxLayout(self)
        layout.addWidget(view)
        buttons = QDialogButtonBox()
        copy_btn = buttons.addButton("复制全部", QDialogButtonBox.ButtonRole.ActionRole)
        shot_btn = buttons.addButton("截图", QDialogButtonBox.ButtonRole.ActionRole)
        close_btn = buttons.addButton("关闭", QDialogButtonBox.ButtonRole.RejectRole)
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(view.toPlainText()))
        shot_btn.clicked.connect(lambda: self._shot_to_clipboard(view))
        close_btn.clicked.connect(self.accept)
        layout.addWidget(buttons)

    def _shot_to_clipboard(self, view):
        """把完整报错内容（含未显示的滚动部分）渲染成图片并复制到剪贴板。"""
        doc = view.document().clone()  # 克隆避免改动界面显示
        doc.setTextWidth(660)
        width = int(doc.textWidth()) + 20
        height = int(doc.size().height()) + 20
        image = QImage(width, height, QImage.Format.Format_RGB32)
        image.fill(QColor("white"))
        painter = QPainter(image)
        painter.translate(10, 10)
        doc.drawContents(painter)
        painter.end()
        QApplication.clipboard().setImage(image)
        QMessageBox.information(self, "截图", "报错截图已复制到剪贴板，可直接粘贴使用。")


class ResponseDialog(QDialog):
    """展示完整响应内容的弹窗。"""

    def __init__(self, title, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 520)
        self._text = text

        layout = QVBoxLayout(self)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text)
        layout.addWidget(view, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        copy_btn = QPushButton("复制全部")
        copy_btn.clicked.connect(self._copy)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        row.addWidget(copy_btn)
        row.addWidget(close_btn)
        layout.addLayout(row)

    def _copy(self):
        QApplication.clipboard().setText(self._text)


class MoreCommandsDialog(QDialog):
    """「更多」指令弹窗：按钮排布与外层服务器指令区一致（每行 3 个，可带数字输入框）。"""

    def __init__(self, more_cmds, parent=None):
        super().__init__(parent)
        self.setWindowTitle("更多指令")
        self.setModal(True)
        self.setMinimumWidth(460)
        self.chosen = None
        self.param_value = None
        self.param_widgets = {}  # cmd_key -> QSpinBox

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        grid = QGridLayout()
        grid.setSpacing(6)
        for idx, cmd in enumerate(more_cmds):
            btn = QPushButton(cmd["label"])
            btn.setMinimumHeight(40)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda _=False, c=cmd: self._choose(c))

            cell = QWidget()
            cell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            cell_layout = QHBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setSpacing(6)
            cell_layout.addWidget(btn, 1)
            if cmd.get("param"):
                spin = QSpinBox()
                spin.setRange(1, 9999)
                spin.setValue(1)
                spin.setMinimumHeight(40)
                spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
                spin.setToolTip("输入要切换到的%s数字" % cmd.get("param_name", "参数"))
                cell_layout.addWidget(spin)
                self.param_widgets[cmd["key"]] = spin

            row, col = divmod(idx, 3)
            grid.addWidget(cell, row, col)
        for c in range(3):
            grid.setColumnStretch(c, 1)
        layout.addLayout(grid)
        layout.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.reject)
        row.addWidget(close_btn)
        layout.addLayout(row)

    def _choose(self, cmd):
        """记录选中的指令及其参数值，然后关闭弹窗。"""
        self.chosen = cmd
        spin = self.param_widgets.get(cmd["key"])
        self.param_value = str(spin.value()) if spin is not None else None
        self.accept()


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("测试工具")
        # 加载图标：优先 data 目录，其次 exe / 脚本所在目录
        icon_path = data_dir() / "图标.ico"
        if not icon_path.exists():
            icon_path = app_dir() / "图标.ico"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1096, 720)

        self.servers = load_servers()
        self._selected = None          # 单选模式下的当前服务器下标
        self._multi = False            # 是否启用多选
        self._multi_selected = set()   # 多选模式下选中的服务器下标
        self._flows = []       # 进行中的指令流程（支持多台服务器并发操作）
        self._zombies = []     # 已取消但后台线程尚未结束的请求，结束后自动回收
        self._running_svn = set()   # 正在执行中的 SVN 更新指令 key（防止重复触发）
        self._bodies = {}      # 「展开响应」：id -> 完整响应内容
        self._body_seq = 0
        self._monitors = {}    # (id(服务器), 日志key) -> 监控状态，支持多服务器 × 多日志类型并发
        self._monitor_history = {}  # (id(服务器), 日志key) -> 历史报错列表（停止监控后仍可右键查看）
        self._poller = None    # 后台拉取线程（MonitorPoller），None 表示空闲
        self._monitor_timer = QTimer(self)
        self._monitor_timer.setInterval(LOG_INTERVAL_MS)
        self._monitor_timer.timeout.connect(self._monitor_tick)

        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self._build_ui()
        self._refresh_server_buttons()
        self._append('<span style="color:#666">就绪。请「添加服务器」并在左侧选中一个服务器，然后点击下方指令按钮。</span>')

    # ---------- 界面 ----------
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)

        # ---- Tab 1：服务器指令 ----
        self.tabs.addTab(self._build_server_tab(), "服务器指令")

        # ---- Tab 2：GM 指令（占位，后续扩展） ----
        self.tabs.addTab(self._build_gm_tab(), "GM 指令")

    def _build_server_tab(self):
        """服务器指令页签，包含服务器列表、报错监控、指令、日志。"""
        page = QWidget()
        root = QHBoxLayout(page)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(12)

        # 左列：服务器 + 指令
        left = QVBoxLayout()
        left.setSpacing(8)

        left.addWidget(self._title("服务器"))
        tools = QHBoxLayout()
        self.add_btn = QPushButton("添加服务器")
        self.add_btn.clicked.connect(self._on_add)
        self.multi_btn = QPushButton("多选")
        self.multi_btn.setCheckable(True)
        self.multi_btn.setStyleSheet(MULTI_BTN_QSS)
        self.multi_btn.setToolTip("启用后可同时选中多台服务器，指令会发送给所有选中的服务器")
        self.multi_btn.toggled.connect(self._on_toggle_multi)
        tools.addWidget(self.add_btn)
        tools.addWidget(self.multi_btn)
        left.addLayout(tools)

        self.server_area = QScrollArea()
        self.server_area.setWidgetResizable(True)
        self.server_host = QWidget()
        self.server_grid = QGridLayout(self.server_host)
        self.server_grid.setContentsMargins(2, 2, 2, 2)
        self.server_grid.setSpacing(6)
        self.server_area.setWidget(self.server_host)
        left.addWidget(self.server_area, 1)

        self.button_group = QButtonGroup(self)
        self.button_group.setExclusive(True)

        # 报错监控：每种日志一个开关，作用于当前选中的服务器；可多服务器同时开
        left.addWidget(self._title("报错监控（对当前选中服务器生效）"))
        mon_grid = QGridLayout()
        mon_grid.setSpacing(4)
        self._monitor_buttons = {}
        for i, log_def in enumerate(MONITOR_LOGS):
            b = QPushButton(log_def["label"])
            b.setCheckable(True)
            b.setStyleSheet(MULTI_BTN_QSS)
            b.setMinimumHeight(30)
            b.setToolTip('监控选中服务器的「%s报错」\n日志文件：%s\n右键：查看历史报错'
                         % (log_def["label"], log_def["file"]))
            b.toggled.connect(lambda checked, d=log_def: self._on_toggle_log_monitor(d, checked))
            b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            b.customContextMenuRequested.connect(
                lambda pos, d=log_def, btn=b: self._on_monitor_menu(d, btn, pos)
            )
            self._monitor_buttons[log_def["key"]] = b
            row, col = divmod(i, 3)
            mon_grid.addWidget(b, row, col)
        left.addLayout(mon_grid)

        left.addWidget(self._title("指令"))
        self.cmd_buttons = []
        self._param_inputs = {}  # cmd_key -> 参数输入控件
        server_cmds = [c for c in COMMANDS if c.get("mode") != "local"]
        primary_cmds = [c for c in server_cmds if not c.get("more")]
        more_cmds = [c for c in server_cmds if c.get("more")]
        # 可见按钮 = 主指令 + 末尾追加一个"更多"按钮占位符
        visible_items = list(primary_cmds)
        if more_cmds:
            visible_items.append({"__more__": True})

        rows_v = QVBoxLayout()
        rows_v.setSpacing(6)
        for row_idx in range(0, len(visible_items), 3):
            row_items = visible_items[row_idx:row_idx + 3]
            row_h = QHBoxLayout()
            row_h.setSpacing(6)
            for item in row_items:
                if item.get("__more__"):
                    more_btn = QPushButton("更多…")
                    more_btn.setMinimumHeight(40)
                    more_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                    more_btn.setToolTip("点击打开更多指令窗口")
                    more_btn.clicked.connect(
                        lambda _=False, cmds=more_cmds: self._show_more_dialog(cmds)
                    )
                    self.cmd_buttons.append(more_btn)
                    row_h.addWidget(more_btn, 1)
                    continue

                cmd = item
                btn = QPushButton(cmd["label"])
                btn.setMinimumHeight(40)
                btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                btn.clicked.connect(lambda _=False, c=cmd: self._on_run(c))
                self.cmd_buttons.append(btn)

                cell = QWidget()
                cell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                cell_layout = QHBoxLayout(cell)
                cell_layout.setContentsMargins(0, 0, 0, 0)
                cell_layout.setSpacing(6)
                cell_layout.addWidget(btn, 1)
                if cmd.get("param"):
                    spin = QSpinBox()
                    spin.setRange(1, 9999)
                    spin.setValue(1)
                    spin.setMinimumHeight(40)
                    spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    spin.setToolTip("输入要切换到的%s数字" % cmd.get("param_name", "参数"))
                    cell_layout.addWidget(spin)
                    self._param_inputs[cmd["key"]] = spin

                row_h.addWidget(cell, 1)
            # 不满 3 列时，最后补一个占位 spacer 让最后一列也能撑开
            while row_items.__len__() < 3:
                spacer = QWidget()
                spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                row_h.addWidget(spacer, 1)
                row_items.append(None)
            rows_v.addLayout(row_h)
        left.addLayout(rows_v)

        # 客户端指令（本地指令）
        local_cmds = [c for c in COMMANDS if c.get("mode") == "local"]
        if local_cmds:
            left.addWidget(self._title("客户端指令"))
            client_v = QVBoxLayout()
            client_v.setSpacing(6)
            for row_idx in range(0, len(local_cmds), 3):
                row_cmds = local_cmds[row_idx:row_idx + 3]
                row_h = QHBoxLayout()
                row_h.setSpacing(6)
                for cmd in row_cmds:
                    btn = QPushButton(cmd["label"])
                    btn.setMinimumHeight(40)
                    btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                    if not cmd.get("no_config"):
                        btn.setToolTip("右键：修改客户端路径配置")
                        btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                        btn.customContextMenuRequested.connect(
                            lambda pos, b=btn: self._on_client_cmd_menu(b, pos)
                        )
                    btn.clicked.connect(lambda _=False, c=cmd: self._on_run(c))
                    self.cmd_buttons.append(btn)
                    row_h.addWidget(btn, 1)
                while row_cmds.__len__() < 3:
                    spacer = QWidget()
                    spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                    row_h.addWidget(spacer, 1)
                    row_cmds.append(None)
                client_v.addLayout(row_h)
            left.addLayout(client_v)

        left_widget = QWidget()
        left_widget.setLayout(left)
        left_widget.setMinimumWidth(460)
        left_widget.setMaximumWidth(460)
        root.addWidget(left_widget)

        # 右列：执行日志
        right = QVBoxLayout()
        right.setSpacing(8)
        right.addWidget(self._title("执行日志"))
        self.log = QTextBrowser()
        self.log.setOpenLinks(False)
        self.log.setOpenExternalLinks(False)
        self.log.anchorClicked.connect(self._on_anchor_clicked)
        self.log.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.log.customContextMenuRequested.connect(self._on_log_context_menu)
        right.addWidget(self.log, 1)
        root.addLayout(right, 1)

        return page

    def _build_gm_tab(self):
        """GM 指令页签（占位，后续扩展具体功能）。"""
        page = QWidget()
        vbox = QVBoxLayout(page)
        vbox.addStretch(1)
        hint = QLabel("GM 指令功能待开发…")
        hint.setStyleSheet("color:#999; font-size:14px;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vbox.addWidget(hint)
        vbox.addStretch(1)
        return page

    @staticmethod
    def _title(text) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionTitle")
        return label

    def _refresh_server_buttons(self):
        for btn in list(self.button_group.buttons()):
            self.button_group.removeButton(btn)
        while self.server_grid.count():
            item = self.server_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        if self._selected is not None and not (0 <= self._selected < len(self.servers)):
            self._selected = None
        self._multi_selected = {i for i in self._multi_selected if 0 <= i < len(self.servers)}

        for idx, srv in enumerate(self.servers):
            btn = QPushButton()
            btn._full_text = '%s  #%s' % (srv["name"], srv["number"])
            btn.setText(btn._full_text)
            btn.setCheckable(True)
            if self._multi:
                btn.setChecked(idx in self._multi_selected)
            else:
                btn.setChecked(idx == self._selected)
            btn.setMinimumHeight(38)
            btn.setStyleSheet(SERVER_BTN_QSS)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip('名称：%s\n编号：%s\n机器：%s' % (srv["name"], srv["number"], srv["machine"]))
            btn.clicked.connect(lambda _=False, i=idx: self._on_select(i))
            btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda pos, i=idx, b=btn: self._on_server_menu(i, b, pos)
            )
            self.button_group.addButton(btn, idx)
            row, col = divmod(idx, 3)
            self.server_grid.addWidget(btn, row, col)
        # 三列等宽
        for c in range(3):
            self.server_grid.setColumnStretch(c, 1)
        self.server_grid.setRowStretch(self.server_grid.rowCount(), 1)
        self._refresh_monitor_buttons()
        # 延迟一帧后计算省略文本（此时按钮已有实际宽度）
        QTimer.singleShot(0, self._update_server_btn_elide)

    def _update_server_btn_elide(self):
        """根据当前按钮实际宽度，重新计算省略文本。"""
        metrics = self.fontMetrics()
        for btn in list(self.button_group.buttons()):
            full = getattr(btn, "_full_text", btn.text())
            available = btn.width() - 28  # 减去左右 padding + 边框
            if available <= 0:
                continue
            btn.setText(metrics.elidedText(full, Qt.TextElideMode.ElideRight, available))

    def resizeEvent(self, event):
        """窗口尺寸变化时，重新计算服务器按钮的省略文本。"""
        super().resizeEvent(event)
        QTimer.singleShot(0, self._update_server_btn_elide)

    # ---------- 服务器管理 ----------
    def _on_add(self):
        dlg = ServerDialog("添加服务器", parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, number, machine = dlg.values()
        if any(s["name"] == name and s["number"] == number and s["machine"] == machine for s in self.servers):
            QMessageBox.information(self, "提示", "该服务器已存在，无需重复添加")
            return
        self.servers.append({"name": name, "number": number, "machine": machine})
        save_servers(self.servers)
        self._selected = len(self.servers) - 1
        self._refresh_server_buttons()
        self._append('已添加服务器：<b>%s</b>（编号 %s，机器 %s）' % (esc(name), esc(number), esc(machine)))

    def _on_edit(self, idx):
        srv = self.servers[idx]
        dlg = ServerDialog("编辑服务器", srv["name"], srv["number"], srv["machine"], self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, number, machine = dlg.values()
        if any(
            s is not srv and s["name"] == name and s["number"] == number and s["machine"] == machine
            for s in self.servers
        ):
            QMessageBox.information(self, "提示", "编辑后的服务器信息与其他服务器重复")
            return
        srv["name"] = name
        srv["number"] = number
        srv["machine"] = machine
        save_servers(self.servers)
        self._refresh_server_buttons()
        self._append('已编辑服务器：<b>%s</b>（编号 %s，机器 %s）' % (esc(name), esc(number), esc(machine)))

    def _on_server_menu(self, idx, btn, pos):
        menu = QMenu(self)
        edit_action = menu.addAction("编辑")
        delete_action = menu.addAction("删除")
        menu.addSeparator()
        up_action = menu.addAction("上移")
        down_action = menu.addAction("下移")
        move_top_action = menu.addAction("移到最前")
        move_bottom_action = menu.addAction("移到最后")
        # 只有一个服务器时禁用移动项
        single = len(self.servers) <= 1
        for a in (up_action, down_action, move_top_action, move_bottom_action):
            a.setEnabled(not single)
        # 边界禁用
        up_action.setEnabled(up_action.isEnabled() and idx > 0)
        down_action.setEnabled(down_action.isEnabled() and idx < len(self.servers) - 1)
        move_top_action.setEnabled(move_top_action.isEnabled() and idx > 0)
        move_bottom_action.setEnabled(move_bottom_action.isEnabled() and idx < len(self.servers) - 1)

        action = menu.exec(btn.mapToGlobal(pos))
        if action == edit_action:
            self._on_edit(idx)
        elif action == delete_action:
            self._on_delete(idx)
        elif action == up_action:
            self._move_server(idx, idx - 1)
        elif action == down_action:
            self._move_server(idx, idx + 1)
        elif action == move_top_action:
            self._move_server(idx, 0)
        elif action == move_bottom_action:
            self._move_server(idx, len(self.servers) - 1)

    def _move_server(self, from_idx, to_idx):
        """移动服务器，同步修复 _selected / _multi_selected / 监控 mkey 的映射。"""
        if from_idx == to_idx or not (0 <= to_idx < len(self.servers)):
            return
        srv = self.servers.pop(from_idx)
        self.servers.insert(to_idx, srv)
        save_servers(self.servers)
        # 修复单选下标
        if self._selected is not None:
            if self._selected == from_idx:
                self._selected = to_idx
            elif from_idx < self._selected <= to_idx:
                self._selected -= 1
            elif to_idx <= self._selected < from_idx:
                self._selected += 1
        # 修复多选下标
        new_multi = set()
        for i in self._multi_selected:
            if i == from_idx:
                new_multi.add(to_idx)
            elif from_idx < i <= to_idx:
                new_multi.add(i - 1)
            elif to_idx <= i < from_idx:
                new_multi.add(i + 1)
            else:
                new_multi.add(i)
        self._multi_selected = new_multi
        self._refresh_server_buttons()
        self._append('已移动服务器「%s」：#%d → #%d'
                     % (esc(srv["name"]), from_idx + 1, to_idx + 1))

    def _on_delete(self, idx):
        if not (0 <= idx < len(self.servers)):
            return
        srv = self.servers[idx]
        answer = QMessageBox.question(
            self, "确认删除", '确定删除服务器「%s」吗？' % srv["name"]
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._stop_server_monitors(srv)
        self.servers.pop(idx)
        save_servers(self.servers)
        self._multi_selected = {
            i if i < idx else i - 1 for i in self._multi_selected if i != idx
        }
        if self._selected == idx:
            self._selected = None
        elif self._selected is not None and self._selected > idx:
            self._selected -= 1
        self._refresh_server_buttons()
        self._append('已删除服务器：%s' % esc(srv["name"]))

    def _on_toggle_multi(self, checked):
        self._multi = checked
        self.multi_btn.setText("已启用多选" if checked else "多选")
        self.button_group.setExclusive(not checked)
        if checked:
            # 进入多选时，把当前单选结果带过来
            if self._selected is not None:
                self._multi_selected = {self._selected}
            self._selected = None
        else:
            self._multi_selected.clear()
        self._refresh_server_buttons()
        self._refresh_monitor_buttons()

    def _on_select(self, idx):
        if self._multi:
            btn = self.button_group.button(idx)
            if btn is not None and btn.isChecked():
                self._multi_selected.add(idx)
            else:
                self._multi_selected.discard(idx)
            return
        self._selected = idx
        self._refresh_monitor_buttons()

    def _current_index(self):
        if self._selected is not None and 0 <= self._selected < len(self.servers):
            return self._selected
        return None

    def _target_indexes(self):
        """本次指令要执行的所有服务器下标。"""
        if self._multi:
            return [i for i in sorted(self._multi_selected) if 0 <= i < len(self.servers)]
        idx = self._current_index()
        return [] if idx is None else [idx]

    def _collect_selected_indices(self):
        """别名：返回当前选中范围的所有服务器下标（多选 / 单选通用）。"""
        return self._target_indexes()

    # ---------- 更多指令弹窗 ----------
    def _show_more_dialog(self, more_cmds):
        """点击「更多」按钮时，弹出独立窗口列出 more_cmds（按钮格式与外层一致）。"""
        dlg = MoreCommandsDialog(more_cmds, self)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        chosen = dlg.chosen
        value = dlg.param_value
        dlg.deleteLater()
        if accepted and chosen is not None:
            self._on_run(chosen, param_override=value)

    # ---------- 执行指令 ----------
    def _on_run(self, cmd, param_override=None):
        if cmd.get("mode") == "local":
            self._run_local(cmd)
            return

        targets = self._target_indexes()
        if not targets:
            QMessageBox.information(self, "提示", "请先在上方选中一个服务器")
            return

        params = []
        label = cmd["label"]
        if cmd.get("cancelable"):
            # 可取消的指令：等待期间再次点击 = 取消等待，不再发起新请求
            targets_srvs = [self.servers[i] for i in targets]
            waiting = [f for f in self._flows
                       if f.get("cmd_key") == cmd["key"]
                       and any(f["srv"] is s for s in targets_srvs)]
            if waiting:
                names = "、".join(flow["srv"]["name"] for flow in waiting)
                answer = QMessageBox.question(
                    self,
                    "确认取消",
                    '「%s」正在等待服务器响应，确定取消等待吗？\n服务器：%s'
                    % (cmd["label"], names),
                )
                if answer != QMessageBox.StandardButton.Yes:
                    return
                for flow in waiting:
                    self._cancel_flow(flow)
                return
        input_widget = self._param_inputs.get(cmd["key"])
        if param_override is not None:
            # 来自「更多」弹窗：参数值由弹窗内的数字框提供
            params.append(param_override)
            label = "%s（%s %s）" % (label, cmd.get("param_name", "参数"), param_override)
        elif input_widget is not None:
            value = str(input_widget.value())
            params.append(value)
            label = "%s（%s %s）" % (label, cmd.get("param_name", "参数"), value)
        elif cmd.get("dialog") == "switch_branch":
            dlg = SwitchBranchDialog(self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            branch, season = dlg.values()
            params.extend([branch, season])
            label = "%s（%s，赛季 %s）" % (label, branch, season)

        steps = [{"label": label, "op": cmd["op"], "show_body": True, "params": params}]
        if cmd.get("check_proc"):
            # 执行成功后自动检查进程状态；自动检查只输出判定结果，不打印响应内容
            steps.append({
                "label": "检查进程状态",
                "op": PROC_OP,
                "show_body": False,
            })

        started = 0
        for idx in targets:
            srv = self.servers[idx]
            if not MACHINE_PREFIX.get(srv["machine"]):
                QMessageBox.warning(self, "错误", '未知的服务器机器：%s' % srv["machine"])
                continue
            if any(
                flow["srv"] is srv and flow.get("cmd_key") == cmd["key"]
                for flow in self._flows
            ):
                self._append(
                    '[%s] [%s] <span style="color:#b26a00">该指令正在执行中，已跳过</span>'
                    % (now_str(), esc(srv["name"]))
                )
                continue

            flow = {
                "srv": srv,
                "cmd_key": cmd["key"],
                "timeout": cmd.get("timeout", REQUEST_TIMEOUT),
                "steps": [dict(step) for step in steps],
                "step": None,
                "worker": None,
                "live_no": None,
                "start_ts": 0.0,
            }
            self._flows.append(flow)
            self._run_next_step(flow)
            started += 1

        if started == 0:
            QMessageBox.information(self, "提示", "选中的服务器都在执行该指令，请等待完成")

    def _run_next_step(self, flow):
        if not flow["steps"]:
            self._finish_flow(flow)
            return
        step = flow["steps"].pop(0)
        flow["step"] = step
        srv = flow["srv"]
        url = '%s%s %s' % (MACHINE_PREFIX[srv["machine"]], step["op"], srv["number"])
        for extra in step.get("params") or []:
            url += ' %s' % extra
        self._append('[%s] [%s] 执行「%s」' % (now_str(), esc(srv["name"]), esc(step["label"])))

        self._start_live_line(flow)
        flow["start_ts"] = time.monotonic()

        worker = RequestWorker(url, flow.get("timeout", REQUEST_TIMEOUT))
        flow["worker"] = worker
        worker.done.connect(lambda ok, status, body, elapsed, f=flow, w=worker: self._on_result(f, w, ok, status, body, elapsed))
        worker.start()

    def _finish_flow(self, flow):
        self._release_worker(flow)
        flow["step"] = None
        if flow in self._flows:
            self._flows.remove(flow)

    def _cancel_flow(self, flow):
        """取消等待：停止接收结果，后台线程结束后自动回收。"""
        elapsed = time.monotonic() - flow.get("start_ts", time.monotonic())
        self._finish_live(
            flow,
            '[%s] [%s] <span style="color:#b26a00">已取消等待</span>　耗时 %.1f 秒'
            % (now_str(), esc(flow["srv"]["name"]), elapsed),
        )
        worker = flow.get("worker")
        if worker is not None:
            try:
                worker.done.disconnect()
            except Exception:
                pass
            # 线程可能仍在等待服务器响应，挂到回收列表，结束后自动销毁
            self._zombies.append(worker)
            worker.finished.connect(lambda w=worker: self._reap_zombie(w))
            flow["worker"] = None
        flow["steps"] = []
        flow["step"] = None
        if flow in self._flows:
            self._flows.remove(flow)

    def _reap_zombie(self, worker):
        if worker in self._zombies:
            self._zombies.remove(worker)
        worker.deleteLater()

    # ---------- 本地客户端指令 ----------
    def _ensure_client_settings(self, need_fields=None):
        """确保指定的客户端配置项已填写；未配置则弹窗选择，返回 (settings, ok)。

        need_fields: 需要的字段名列表，None 表示全部字段。
          - need_main: 主干客户端 exe 路径
          - need_branch: 分支版本根目录
        """
        settings = load_client_settings()
        if need_fields is None:
            need_fields = ("need_main", "need_branch")
        need_main = "need_main" in need_fields
        need_branch = "need_branch" in need_fields

        missing = []
        if need_main and not settings["main_client_path"]:
            missing.append("主干客户端 exe 路径")
        if need_branch and not settings["branch_root_dir"]:
            missing.append("分支版本根目录")

        if missing:
            answer = QMessageBox.question(
                self, "客户端未配置",
                "当前缺少以下配置：\n  · " + "\n  · ".join(missing)
                + "\n\n是否现在配置？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return settings, False
            self._open_client_settings_dialog()
            settings = load_client_settings()
            # 保存后再检查一次
            if need_main and not settings["main_client_path"]:
                return settings, False
            if need_branch and not settings["branch_root_dir"]:
                return settings, False
        return settings, True

    def _open_client_settings_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("客户端设置")
        dlg.setMinimumWidth(500)

        main_edit = QLineEdit()
        main_edit.setPlaceholderText("客户端 exe 完整路径，例如 D:/Client/game/game.exe")
        branch_edit = QLineEdit()
        branch_edit.setPlaceholderText("分支版本根目录，例如 E:/分支版本")

        settings = load_client_settings()
        main_edit.setText(settings.get("main_client_path", ""))
        branch_edit.setText(settings.get("branch_root_dir", ""))

        def pick_exe():
            path, _ = QFileDialog.getOpenFileName(
                dlg, "选择主干客户端 exe", "", "可执行文件 (*.exe);;所有文件 (*)"
            )
            if path:
                main_edit.setText(path)

        def pick_branch_dir():
            path = QFileDialog.getExistingDirectory(dlg, "选择分支版本根目录")
            if path:
                branch_edit.setText(path)

        form = QFormLayout(dlg)
        main_row = QHBoxLayout()
        main_row.addWidget(main_edit, 1)
        main_pick = QPushButton("选择…")
        main_pick.clicked.connect(pick_exe)
        main_row.addWidget(main_pick)
        main_wrap = QWidget()
        main_wrap.setLayout(main_row)
        form.addRow("主干客户端 exe：", main_wrap)

        branch_row = QHBoxLayout()
        branch_row.addWidget(branch_edit, 1)
        branch_pick = QPushButton("选择…")
        branch_pick.clicked.connect(pick_branch_dir)
        branch_row.addWidget(branch_pick)
        branch_wrap = QWidget()
        branch_wrap.setLayout(branch_row)
        form.addRow("分支版本根目录：", branch_wrap)

        hint = QLabel(
            '说明： SVN 更新目录自动取 exe 所在文件夹；\n'
            '分支路径自动拼接「分支根目录/MMDD/game/game.exe」，'
            '其中 MMDD 为本周三的月日。'
        )
        hint.setStyleSheet("color:#888; font-size:12px;")
        hint.setWordWrap(True)
        form.addRow(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        form.addRow(buttons)

        def accept():
            save_client_settings({
                "main_client_path": main_edit.text().strip(),
                "branch_root_dir": branch_edit.text().strip(),
            })
            dlg.accept()

        buttons.accepted.connect(accept)
        buttons.rejected.connect(dlg.reject)
        dlg.exec()

    def _on_client_cmd_menu(self, btn, pos):
        """右键客户端指令按钮：打开客户端设置对话框。"""
        menu = QMenu(self)
        config_action = menu.addAction("修改客户端路径配置")
        action = menu.exec(btn.mapToGlobal(pos))
        if action == config_action:
            self._open_client_settings_dialog()

    def _run_local(self, cmd):
        key = cmd["key"]
        label = cmd["label"]
        # SVN 指令拦截：正在执行中的 SVN 指令（同 key），直接打印拦截提示
        if cmd.get("svn") and key in self._running_svn:
            self._append(
                '[%s] <span style="color:#b26a00">该 SVN 更新正在执行中，已拦截重复触发</span>'
                % now_str()
            )
            return

        self._append('[%s] <b>%s</b>' % (now_str(), esc(label)))

        if key == "clientcompile":
            url = "http://192.168.5.137:8092/"
            try:
                webbrowser.open(url)
                self._append('[%s] <span style="color:#1e7e34">已在浏览器中打开客户端编译页面</span>' % now_str())
            except Exception as exc:
                QMessageBox.warning(self, "错误", '打开浏览器失败：\n%s' % str(exc))
                self._append('[%s] <span style="color:#c62828">打开失败：%s</span>'
                             % (now_str(), esc(str(exc))))

        elif key == "openclient":
            settings, ok = self._ensure_client_settings(("need_main",))
            if not ok:
                return
            exe_path = settings["main_client_path"]
            if not Path(exe_path).exists():
                QMessageBox.warning(
                    self, "错误",
                    '主干客户端路径不存在：\n%s\n请在"客户端设置"中重新配置。' % exe_path,
                )
                return
            self._launch_exe(exe_path, "主干客户端")

        elif key == "svnupdate":
            settings, ok = self._ensure_client_settings(("need_main",))
            if not ok:
                return
            svn_dir = str(Path(settings["main_client_path"]).parent)
            if not Path(svn_dir).is_dir():
                QMessageBox.warning(
                    self, "错误",
                    'SVN 更新目录不存在（由主干 exe 自动推导）：\n%s\n请检查 exe 路径是否正确。' % svn_dir,
                )
                return
            self._start_svn_worker(key, "更新主干", svn_dir)

        elif key in ("openbranch", "openbranch_last"):
            settings, ok = self._ensure_client_settings(("need_branch",))
            if not ok:
                return
            weeks_ago = 0 if key == "openbranch" else 1
            wed = latest_wednesday_mmdd(weeks_ago=weeks_ago)
            exe_path = str(Path(settings["branch_root_dir"]) / wed / "game" / "game.exe")
            if not Path(exe_path).exists():
                which = "本周" if weeks_ago == 0 else "上周"
                QMessageBox.warning(
                    self, "错误",
                    '%s分支客户端路径不存在：\n%s\n请确认分支版本根目录是否正确，'
                    '或该 MMDD (%s) 的分支是否已构建。' % (which, exe_path, wed),
                )
                return
            which = "本周分支" if weeks_ago == 0 else "上周分支"
            self._launch_exe(exe_path, "%s（%s）" % (which, wed))

        elif key in ("svnupdatebranch", "svnupdatebranch_last"):
            settings, ok = self._ensure_client_settings(("need_branch",))
            if not ok:
                return
            weeks_ago = 0 if key == "svnupdatebranch" else 1
            wed = latest_wednesday_mmdd(weeks_ago=weeks_ago)
            svn_dir = str(Path(settings["branch_root_dir"]) / wed / "game")
            if not Path(svn_dir).is_dir():
                which = "本周" if weeks_ago == 0 else "上周"
                QMessageBox.warning(
                    self, "错误",
                    '%s分支 SVN 更新目录不存在：\n%s\n请确认分支版本根目录是否正确，'
                    '或该 MMDD (%s) 的分支目录是否已存在。' % (which, svn_dir, wed),
                )
                return
            which = "本周" if weeks_ago == 0 else "上周"
            self._start_svn_worker(key, "更新%s分支（%s）" % (which, wed), svn_dir)

    def _start_svn_worker(self, key, desc, svn_dir):
        """启动一个 SVN 后台 worker，登记到 _running_svn。"""
        self._running_svn.add(key)
        self._append('[%s] 正在%s（svn update %s）…' % (now_str(), esc(desc), esc(svn_dir)))
        worker = _SVNWorker(svn_dir, self)
        worker.done.connect(lambda *args, k=key: self._svn_done(k, *args))
        worker.start()

    def _launch_exe(self, exe_path, label):
        """启动一个 exe（Windows 下通过 cmd /c start 创建独立控制台窗口，标题设为 exe 完整路径）。"""
        try:
            if sys.platform == "win32":
                subprocess.Popen('cmd /c start "%s" "%s"' % (exe_path, exe_path), shell=True)
            else:
                subprocess.Popen([exe_path])
            self._append('[%s] <span style="color:#1e7e34">已启动%s</span>'
                         % (now_str(), esc(label)))
        except Exception as exc:
            QMessageBox.warning(self, "错误", '启动%s失败：\n%s' % (label, str(exc)))
            self._append('[%s] <span style="color:#c62828">启动失败：%s</span>'
                         % (now_str(), esc(str(exc))))

    def _svn_done(self, key, ok, returncode, stdout, stderr, errmsg):
        """SVN worker 完成回调；先清理 _running_svn，再输出结果。"""
        self._running_svn.discard(key)
        if not ok:
            self._append('[%s] <span style="color:#c62828">SVN 更新失败：%s</span>'
                         % (now_str(), esc(errmsg)))
            QMessageBox.warning(self, "错误", 'SVN 更新失败：\n%s' % errmsg)
            return
        if returncode == 0:
            # svn update 退出码为 0 时仍可能存在冲突（状态前缀为 C 的行），需单独检测并提醒
            conflict_lines = []
            tree_conflict_lines = []
            for line in stdout.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                # 文本冲突：C 开头后跟若干空格 + 路径
                if re.match(r"^C\s{2,}\S+", stripped):
                    conflict_lines.append(stripped)
                # 树冲突：行首为状态码，且树冲突列（第 7 个字符位置）为 C；
                # 这里按列位置判断，避免把其他列的 C 误判为树冲突
                elif stripped[0] in "ADGURM" and len(stripped) > 6 and stripped[6] == "C":
                    tree_conflict_lines.append(stripped)
                elif stripped.startswith("Tree conflict"):
                    tree_conflict_lines.append(stripped)
            if conflict_lines or tree_conflict_lines:
                all_conflicts = conflict_lines + tree_conflict_lines
                count = len(all_conflicts)
                detail = "\n".join(all_conflicts[:50])
                if count > 50:
                    detail += "\n... 等共 %d 个" % count
                self._append(
                    '[%s] <span style="color:#e65100">⚠ SVN 更新完成，但发现 %d 个冲突，请尽快解决！</span>'
                    % (now_str(), count)
                )
                msg = QMessageBox(self)
                msg.setIcon(QMessageBox.Icon.Warning)
                msg.setWindowTitle("SVN 更新冲突")
                msg.setText("检测到 %d 个冲突，SVN 已标记为冲突状态：\n\n%s" % (count, detail))
                msg.setInformativeText("建议先解决冲突再继续操作，否则服务器更新可能异常。")
                msg.setStandardButtons(QMessageBox.StandardButton.Ok)
                msg.exec()
            else:
                self._append('[%s] <span style="color:#1e7e34">SVN 更新完成</span>' % now_str())
        else:
            self._append('[%s] <span style="color:#c62828">SVN 更新失败（退出码 %d）</span>'
                         % (now_str(), returncode))
            QMessageBox.warning(
                self, "SVN 更新失败",
                '退出码 %d\n\nstdout:\n%s\n\nstderr:\n%s' % (
                    returncode, stdout[:2000] or "(空)", stderr[:2000] or "(空)"
                ),
            )

    # ---------- 监控服务端报错 ----------
    def _on_toggle_log_monitor(self, log_def, checked):
        """开关某一种报错日志的监控。多选模式下作用于所有选中的服务器，单选模式下作用于当前选中的服务器。"""
        btn = self._monitor_buttons[log_def["key"]]
        targets = self._collect_selected_indices()
        if not targets:
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
            QMessageBox.information(self, "提示", "请先选中一台服务器")
            return
        any_changed = False
        for idx in targets:
            srv = self.servers[idx]
            mkey = (id(srv), log_def["key"])
            if checked:
                if mkey in self._monitors:
                    continue
                self._monitors[mkey] = {
                    "srv": srv,
                    "log": log_def,
                    "last_ts": None,
                    "baseline_done": False,
                    "poll_date": date.today().strftime("%Y_%m_%d"),
                    "fail": 0,
                }
                self._append('[%s] <b>开始监控「%s报错」</b>' % (esc(srv["name"]), esc(log_def["label"])))
                any_changed = True
            else:
                state = self._monitors.pop(mkey, None)
                if state is None:
                    continue
                self._append('[%s] <b>停止监控「%s报错」</b>' % (esc(srv["name"]), esc(log_def["label"])))
                any_changed = True
        if any_changed:
            self._sync_monitor_timer()

    def _on_monitor_menu(self, log_def, btn, pos):
        """监控按钮右键：查看该日志类型在当前选中服务器上的历史报错。"""
        menu = QMenu(self)
        history_action = menu.addAction("查看历史报错")
        action = menu.exec(btn.mapToGlobal(pos))
        if action == history_action:
            self._show_monitor_history(log_def)

    def _show_monitor_history(self, log_def):
        """批量查看当前选中服务器的今日报错历史（多选/单选通用）。"""
        targets = self._collect_selected_indices()
        if not targets:
            QMessageBox.information(self, "提示", "请先选中一台服务器")
            return
        today_ymd = date.today().strftime("%Y_%m_%d")
        errors_per_srv = []  # [(srv_name, [content, ...]), ...]
        failed = []
        empty = []
        for idx in targets:
            srv = self.servers[idx]
            url = '%s%s %s %s/%s' % (
                MACHINE_PREFIX[srv["machine"]], LOG_OP, srv["number"], today_ymd, log_def["file"],
            )
            try:
                resp = requests.get(url, timeout=5)
                if resp.status_code != 200:
                    raise RuntimeError("HTTP %s" % resp.status_code)
                body = clean_body(resp.text.strip())
            except Exception as exc:
                failed.append((srv["name"], str(exc)))
                continue
            blocks = self._split_error_blocks(body)
            if blocks:
                errors_per_srv.append((srv["name"], [content for _, content in blocks]))
            else:
                empty.append(srv["name"])

        # 把每个服务器的错误用分隔线拼一起显示
        if errors_per_srv:
            title = "历史报错 / %s（共 %d 台有报错）" % (log_def["label"], len(errors_per_srv))
            all_blocks = []
            for srv_name, contents in errors_per_srv:
                header = '<span style="color:#c62828;font-weight:bold">[服务器：%s]</span>' % esc(srv_name)
                rendered_parts = []
                for content in contents:
                    html = esc(content)
                    html = ERROR_TIME_RE.sub(
                        lambda m: '<span style="color:#2f7ded;font-size:16px;font-weight:bold">[%s]traceback:</span>'
                                  % m.group(1),
                        html,
                    )
                    rendered_parts.append(html.replace("\n", "<br/>"))
                all_blocks.append(header + "<br/>" + ("<hr/>".join(rendered_parts)))
            combined = "<hr/><hr/>".join(all_blocks)
            self._show_combined_errors(title, combined)
        messages = []
        if failed:
            messages.append("拉取失败：" + "、".join(n for n, _ in failed))
        if empty:
            messages.append("无报错：" + "、".join(empty))
        if messages:
            QMessageBox.information(self, "历史报错", "\n".join(messages))

    def _show_combined_errors(self, title, html_body):
        """展示已拼接好的 HTML 错误内容。"""
        dlg = MonitorErrorDialog(title, ["<empty>"], self)
        # MonitorErrorDialog 会自动用 esc 处理 blocks，这里用 setHtml 覆盖
        # 直接取 view 控件替换内容
        from PySide6.QtWidgets import QTextBrowser
        view = dlg.findChildren(QTextBrowser)[0]
        view.setHtml(html_body)
        doc = view.document()
        doc.setTextWidth(660)
        content_h = int(doc.size().height())
        screen_h = QApplication.primaryScreen().availableGeometry().height()
        dlg.resize(720, max(220, min(content_h + 110, screen_h - 40)))
        dlg.exec()

    def _sync_monitor_timer(self):
        if self._monitors and not self._monitor_timer.isActive():
            self._monitor_timer.start()
        elif not self._monitors and self._monitor_timer.isActive():
            self._monitor_timer.stop()

    def _refresh_monitor_buttons(self):
        """切换选中服务器后，按钮高亮状态跟随当前选中范围已开启的监控。
        多选模式：有任意一个选中服务器开启了该类型监控就高亮（混合态用选中表示）。
        单选模式：仅当前选中服务器开启时高亮。"""
        targets = self._collect_selected_indices()
        for key, btn in self._monitor_buttons.items():
            active = any(
                (id(self.servers[i]), key) in self._monitors for i in targets
            ) if targets else False
            btn.blockSignals(True)
            btn.setChecked(active)
            btn.blockSignals(False)

    def _stop_server_monitors(self, srv, announce=True):
        """停止某台服务器的全部监控（删除服务器时调用）。"""
        doomed = [k for k, st in self._monitors.items() if st["srv"] is srv]
        for k in doomed:
            st = self._monitors.pop(k)
            if announce:
                self._append('[%s] <b>停止监控「%s报错」</b>'
                             % (esc(srv["name"]), esc(st["log"]["label"])))
        if doomed:
            self._sync_monitor_timer()

    def _monitor_tick(self):
        if not self._monitors:
            return
        today_ymd = date.today().strftime("%Y_%m_%d")
        # 跨天：日期变更后所有监控一起停止并提示
        stale = [k for k, st in self._monitors.items() if st["poll_date"] != today_ymd]
        if stale:
            names = sorted({self._monitors[k]["srv"]["name"] for k in stale})
            for k in stale:
                st = self._monitors.pop(k)
                self._append(
                    '[%s] <span style="color:#b26a00">日期已变更为 %s，自动停止监控「%s报错」（原监控日期 %s）</span>'
                    % (esc(st["srv"]["name"]), today_ymd, esc(st["log"]["label"]), esc(st["poll_date"]))
                )
            self._sync_monitor_timer()
            self._refresh_monitor_buttons()
            QMessageBox.information(
                self, "监控停止",
                '日期已变更为 %s，以下服务器的报错监控已自动停止：\n%s\n如需继续监控请重新开启。'
                % (today_ymd, "、".join(names)),
            )
            return
        # 上一轮还在拉取就跳过本次，避免任务堆积
        if self._poller is not None and self._poller.isRunning():
            return
        jobs = []
        for mkey, st in self._monitors.items():
            srv, log_def = st["srv"], st["log"]
            url = '%s%s %s %s/%s' % (
                MACHINE_PREFIX[srv["machine"]], LOG_OP, srv["number"], today_ymd, log_def["file"],
            )
            jobs.append((mkey, url))
        self._poller = MonitorPoller(self)
        self._poller.set_jobs(jobs)
        self._poller.polled.connect(self._on_polled)
        self._poller.finished.connect(lambda p=self._poller: self._on_poller_finished(p))
        self._poller.start()

    def _on_poller_finished(self, poller):
        # 只清空当前引用，避免排队信号误伤已创建的下一代 poller
        if self._poller is poller:
            self._poller = None
        poller.deleteLater()

    def _on_polled(self, results):
        for mkey, ok, body, error in results:
            state = self._monitors.get(mkey)
            if state is None:
                continue  # 拉取期间已被停止
            self._apply_monitor_result(mkey, state, ok, body, error)

    def _apply_monitor_result(self, mkey, state, ok, body, error):
        srv, log_def = state["srv"], state["log"]
        if not ok:
            state["fail"] += 1
            if state["fail"] >= 3:
                self._monitors.pop(mkey, None)
                self._append(
                    '[%s] <span style="color:#c62828">监控「%s报错」连续 3 次拉取失败：%s，停止监控</span>'
                    % (esc(srv["name"]), esc(log_def["label"]), esc(error))
                )
                self._sync_monitor_timer()
                self._refresh_monitor_buttons()
                QMessageBox.warning(
                    self, "监控停止",
                    '监控「%s」的「%s报错」时连续拉取失败：\n%s\n已自动停止监控。'
                    % (srv["name"], log_def["label"], error),
                )
            return
        state["fail"] = 0

        blocks = self._split_error_blocks(body)
        if not blocks:
            state["baseline_done"] = True
            return
        last_ts = state["last_ts"]
        new_blocks = []
        for ts, content in blocks:
            if last_ts is None or ts > last_ts:
                new_blocks.append(content)
                last_ts = ts
        state["last_ts"] = last_ts

        if not state["baseline_done"]:
            # 首轮只建立时间基准，不弹窗（已有内容视为历史）
            state["baseline_done"] = True
            return
        if new_blocks:
            self._monitor_history.setdefault(mkey, []).extend(new_blocks)
            title = "新报错 - %s / %s（共 %d 条）" % (srv["name"], log_def["label"], len(new_blocks))
            MonitorErrorDialog(title, new_blocks, self).exec()

    @staticmethod
    def _split_error_blocks(body):
        """按 [时间]traceback: 切分主进程报错块，返回 [(时间串, 完整块文本), ...]。"""
        if not body:
            return []
        matches = list(ERROR_BLOCK_RE.finditer(body))
        if not matches:
            return []
        result = []
        for i, m in enumerate(matches):
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            time_m = ERROR_TIME_RE.match(body, start)
            ts = time_m.group(1) if time_m else ""
            content = body[start:end].strip()
            result.append((ts, content))
        return result

    def _release_worker(self, flow):
        """等后台线程真正结束后再释放，避免线程仍在运行时被回收。"""
        worker = flow.get("worker")
        if worker is None:
            return
        try:
            worker.done.disconnect()
        except Exception:
            pass
        worker.wait(2000)
        flow["worker"] = None

    def _on_result(self, flow, worker, ok, status, body, elapsed):
        step = flow.get("step") or {}
        srv = flow["srv"]

        if ok:
            head = '<span style="color:#12833b;font-weight:bold">✔ 执行成功</span>'
        elif status is None:
            head = '<span style="color:#c62828;font-weight:bold">✘ 执行失败</span>'
        else:
            head = '<span style="color:#c62828;font-weight:bold">✘ 执行失败（HTTP %s）</span>' % status
        self._finish_live(flow, '[%s] [%s] %s　耗时 %.2f 秒' % (now_str(), esc(srv["name"]), head, elapsed))

        if status is None:
            if body:
                self._append('<span style="color:#666">错误信息：</span>%s' % esc_multiline(body))
        elif step.get("show_body", True):
            self._log_body(body, srv["name"])

        if step.get("op") == PROC_OP and ok:
            self._report_proc_status(body)

        if not ok:
            flow["steps"] = []  # 请求失败，终止后续步骤

        self._run_next_step(flow)

    # ---------- 响应内容展示 ----------
    def _log_body(self, body, srv_name=""):
        if not body:
            self._append('<span style="color:#666">响应内容：（空）</span>')
            return
        if len(body) <= INLINE_BODY_LIMIT:
            self._append('<span style="color:#666">响应内容：</span>%s' % esc_multiline(body))
            return
        self._body_seq += 1
        body_id = str(self._body_seq)
        self._bodies[body_id] = (srv_name, body)
        self._append(
            '<span style="color:#666">响应内容：</span>'
            '<a href="#expand-%s" style="color:#2f7ded">展开响应</a>' % body_id
        )

    def _report_proc_status(self, body):
        if PROC_ABNORMAL_KEYWORD in body:
            self._append(
                '<span style="color:#c62828;font-weight:bold">进程异常，有进程挂了</span>'
            )
            return

        # 无异常时进一步识别版本信息
        season_match = SEASON_ARG_RE.search(body)
        season = "S%s" % season_match.group(1) if season_match else ""

        if BRANCH_KEYWORD in body:
            branch_match = BRANCH_RE.search(body)
            date = branch_match.group(1) if branch_match else ""
            text = ("%s分支 %s" % (date, season)).strip() if season else "%s分支" % date
            self._append('<span style="color:#12833b;font-weight:bold">%s</span>' % esc(text))
        elif season:
            self._append('<span style="color:#12833b;font-weight:bold">主干%s</span>' % esc(season))
        else:
            self._append('<span style="color:#12833b;font-weight:bold">✔ 进程无异常</span>')

    # ---------- 日志 ----------
    def _append(self, html):
        doc = self.log.document()
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not doc.isEmpty():
            cursor.insertBlock()
            cursor.setCharFormat(QTextCharFormat())
        cursor.insertHtml(html)
        self._scroll_to_bottom()

    def _start_live_line(self, flow):
        """新增一个空块作为实时刷新的「等待中」状态行。"""
        doc = self.log.document()
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not doc.isEmpty():
            cursor.insertBlock()
            cursor.setCharFormat(QTextCharFormat())
        flow["live_no"] = doc.blockCount() - 1
        self._scroll_to_bottom()

    def _finish_live(self, flow, html):
        if not self._replace_live(flow, html):
            # 等待行已被清空等情况：直接追加结果，避免丢失
            self._append(html)
        flow["live_no"] = None

    def _on_anchor_clicked(self, url):
        target = url.toString()
        marker = "expand-"
        pos = target.find(marker)
        if pos < 0:
            return
        body_id = target[pos + len(marker):].strip("/")
        entry = self._bodies.get(body_id)
        if entry is not None:
            srv_name, body = entry
            ResponseDialog("响应内容 - %s" % srv_name, body, self).exec()

    def _on_log_context_menu(self, pos):
        menu = QMenu(self)
        copy_action = menu.addAction("复制")
        copy_action.setEnabled(self.log.textCursor().hasSelection())
        copy_action.triggered.connect(self.log.copy)
        select_all_action = menu.addAction("选择全部")
        select_all_action.triggered.connect(self.log.selectAll)
        menu.addSeparator()
        clear_action = menu.addAction("清空打印")
        clear_action.triggered.connect(self._clear_log)
        menu.exec(self.log.mapToGlobal(pos))

    def _clear_log(self):
        self.log.clear()
        self._bodies.clear()
        for flow in self._flows:
            flow["live_no"] = None

    def _tick(self):
        for flow in list(self._flows):
            live_no = flow.get("live_no")
            if live_no is None:
                continue
            elapsed = time.monotonic() - flow["start_ts"]
            self._replace_live(
                flow,
                '[%s] [%s] <span style="color:#b26a00">等待服务器响应…</span>　已耗时 %.1f 秒'
                % (now_str(), esc(flow["srv"]["name"]), elapsed),
            )

    def _replace_live(self, flow, html):
        """替换等待行内容；目标块不存在（如日志被清空）时返回 False。"""
        live_no = flow.get("live_no")
        if live_no is None:
            return False
        block = self.log.document().findBlockByNumber(live_no)
        if not block.isValid():
            flow["live_no"] = None
            return False
        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        cursor.movePosition(
            QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor
        )
        cursor.insertHtml(html)
        self._scroll_to_bottom()
        return True

    def _scroll_to_bottom(self):
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ---------- 关闭 ----------
    def closeEvent(self, event):
        running = [f for f in self._flows if f.get("worker") is not None and f["worker"].isRunning()]
        if running:
            answer = QMessageBox.question(
                self, "确认退出", "有 %d 个操作正在等待服务器响应，确定要退出吗？" % len(running)
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            for flow in running:
                worker = flow["worker"]
                try:
                    worker.done.disconnect()
                except Exception:
                    pass
                worker.terminate()
                worker.wait(1000)
                flow["worker"] = None
        event.accept()


def main():
    import traceback

    crash_log = DATA_DIR / "crash.log"

    def _excepthook(exc_type, exc_value, exc_tb):
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            from datetime import datetime
            with open(crash_log, "a", encoding="utf-8") as f:
                f.write("=" * 60 + "\n")
                f.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "\n")
                f.write(detail + "\n")
        except Exception:
            pass
        print(detail, file=sys.stderr)

    sys.excepthook = _excepthook

    app = QApplication(sys.argv)
    app.setApplicationName("测试工具")
    app.setStyleSheet(APP_QSS)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
