# -*- coding: utf-8 -*-
"""服务器指令工具

功能：维护服务器列表（名称 / 编号 / 机器），选中服务器后点击指令按钮，
通过 HTTP 调用服务端控制接口并等待返回结果，实时显示等待耗时。

当前内置更新 / 重启 / 清档 / 显示进程状态 / 服务器还原 / 服务器改动 /
切换赛季 / 切换群组 / 切换分支指令，后续新增指令只需在 COMMANDS 中增加一项。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from datetime import date, timedelta
from pathlib import Path

import requests
from PySide6.QtCore import QEvent, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QTextCharFormat, QTextCursor, QTextOption
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
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
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app_log import get_logger, setup_logging
from config_search import ConfigSearchPage
from gm_commands import GmCommandPage

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
    {"key": "viewlog", "label": "查看日志", "op": "log.sh", "check_proc": False,
     "dialog": "view_log"},
    {"key": "swseason", "label": "切换赛季", "op": "swSeason.sh", "check_proc": True, "param": "season", "param_name": "赛季"},
    {"key": "swgroup", "label": "切换群组", "op": "swSrvGroup.sh", "check_proc": False, "param": "group", "param_name": "群组"},
    {"key": "switchbranch", "label": "切换分支", "op": "switch.sh", "check_proc": True,
     "dialog": "switch_branch", "timeout": None, "cancelable": True},
    {"key": "status", "label": "服务器改动", "op": "status.sh", "check_proc": False, "more": True},
    {"key": "revert", "label": "服务器还原", "op": "revert.sh", "check_proc": False, "more": True},
    {"key": "swstage", "label": "服务器档位", "op": "swSrvStage.sh", "check_proc": False,
     "param": "stage", "param_name": "档位", "more": True},
    # 本地客户端指令（不走 HTTP）
    {"key": "clientcompile", "label": "客户端编译", "mode": "local", "no_config": True},
    {"key": "openclient", "label": "打开主干", "mode": "local"},
    {"key": "svnupdate", "label": "更新主干", "mode": "local", "svn": True},
    {"key": "openbranch", "label": "打开本周分支", "mode": "local"},
    {"key": "svnupdatebranch", "label": "更新本周分支", "mode": "local", "svn": True},
    {"key": "openbranch_last", "label": "打开上周分支", "mode": "local"},
    {"key": "svnupdatebranch_last", "label": "更新上周分支", "mode": "local", "svn": True},
]

# 「切换群组」右键「查看群组」弹窗中展示的群组对照表
GROUP_LIST_TEXT = (
    "99\t内服\n"
    "4\t大陆正式服\n"
    "5\t大陆彩蛋服\n"
    "6\t大陆周版本\n"
    "7\t台湾正式服\n"
    "8\t台湾彩蛋服\n"
    "9\t韩国彩蛋服\n"
    "10\t韩国正式服\n"
    "11\t东南亚正式服\n"
    "12\t东南亚彩蛋服\n"
    "13\t日本正式服\n"
    "14\t日本彩蛋服\n"
    "15\t海外混服正式服\n"
    "16\t海外混服彩蛋/周版本服"
)


def branch_options(count=5, today=None):
    """依次给出最近若干个周三的日期（含今天），并在最前面加上主干 main。"""
    today = today or date.today()
    offset = (today.weekday() - 2) % 7  # 2 = 周三
    latest = today - timedelta(days=offset)
    dates = [latest - timedelta(days=7 * i) for i in range(count)]
    return ["main"] + [d.strftime("%Y_%m_%d") for d in dates]


def ymd_no_zero(d=None):
    """返回不带前导零的 YYYY_M_D 格式日期，例如 2026_11_2。"""
    d = d or date.today()
    return "%d_%d_%d" % (d.year, d.month, d.day)


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


def autocode_relative_path(local_path):
    """从本地文件路径中截取 autocode 之后的相对路径。

    例如 E:\\y11_designer\\C-策划导表\\autocode\\ActReward\\Act1001.lua
    返回 \\ActReward\\Act1001.lua（保留原分隔符）。
    路径中不含 autocode 目录时返回 None。
    """
    norm = str(local_path).replace("/", "\\")
    idx = norm.lower().find("\\autocode\\")
    if idx < 0:
        return None
    return norm[idx + len("\\autocode"):]


def build_upload_file_path(local_path, srv):
    """按服务器信息拼出上传接口需要的服务端 file_path。

    返回 (file_path, None) 或 (None, 错误说明)。
    """
    if srv.get("machine") not in UPLOAD_PATH_PREFIX:
        return None, "服务器「%s」的机器号 %s 暂不支持配置替换" % (srv.get("name"), srv.get("machine"))
    sub = autocode_relative_path(local_path)
    if sub is None:
        return None, "文件路径必须位于 autocode 目录下，当前为：%s" % local_path
    prefix = UPLOAD_PATH_PREFIX[srv["machine"]]
    tail = UPLOAD_PATH_TAIL % (srv["number"], sub)
    return prefix + tail, None


# 「更新」成功后自动执行的进程状态检查指令，以及判定进程异常的响应关键字
PROC_OP = "proc.sh"
PROC_ABNORMAL_KEYWORD = "进程挂了"

# 配置替换：autocode 文件上传接口（端口与机器对应）
UPLOAD_BASE = {
    "241": "http://192.168.6.241:3999/upload_file",
    "243": "http://192.168.6.243:3999/upload_file",
}
UPLOAD_KEY = "tk$rBp9)5s&kj7"          # 上传接口固定 key
UPLOAD_PATH_PREFIX = {"241": "/home", "243": "/data"}  # 服务端路径前缀
UPLOAD_PATH_TAIL = "/qc_test/y11_qc_srv/game_%s/logic/autocode%s"  # %s=服务器编号, %s=autocode 后的相对路径

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
CUSTOM_COMMANDS_FILE = DATA_DIR / "custom_commands.json"


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


def load_custom_cmd_defs():
    """加载自定义指令定义（「更多」中右键新增的指令）。

    每项格式：{"op": 指令, "need_param": 是否需要数字框, "check_proc": 是否执行后检查进程}
    """
    _migrate_old_file(CUSTOM_COMMANDS_FILE)
    if not CUSTOM_COMMANDS_FILE.exists():
        return []
    try:
        data = json.loads(CUSTOM_COMMANDS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    result = []
    for item in data.get("commands") or []:
        op = str(item.get("op", "")).strip()
        if not op:
            continue
        name = str(item.get("name", "")).strip() or op
        result.append({
            "name": name,
            "op": op,
            "need_param": bool(item.get("need_param")),
            "check_proc": bool(item.get("check_proc")),
        })
    return result


def save_custom_cmd_defs(defs):
    CUSTOM_COMMANDS_FILE.write_text(
        json.dumps({"commands": defs}, ensure_ascii=False, indent=2),
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
      - config_dir: 配置目录（配置替换页签「打开配置目录」按钮使用）
    旧配置里的 svn_update_dir 已废弃，忽略即可。
    """
    _migrate_old_file(CLIENT_SETTINGS_FILE)
    if not CLIENT_SETTINGS_FILE.exists():
        return {"main_client_path": "", "branch_root_dir": "", "config_dir": ""}
    try:
        data = json.loads(CLIENT_SETTINGS_FILE.read_text(encoding="utf-8"))
        return {
            "main_client_path": str(data.get("main_client_path", "")),
            "branch_root_dir": str(data.get("branch_root_dir", "")),
            "config_dir": str(data.get("config_dir", "")),
        }
    except Exception:
        return {"main_client_path": "", "branch_root_dir": "", "config_dir": ""}


def save_client_settings(settings):
    CLIENT_SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def now_str() -> str:
    return time.strftime("%H:%M:%S")


def esc(text: str) -> str:
    return text.replace(chr(0x202F), " ")


def esc_multiline(text: str) -> str:
    return esc(text).replace("\n", "<br/>")


_BR_RE = re.compile(r"(?i)<br\s*/?>")


def clean_body(text: str) -> str:
    """服务端响应中常带字面 <br> 标签，展示时直接去掉。"""
    return _BR_RE.sub("", text)


class RequestWorker(QThread):
    """在后台线程发起 HTTP 请求，避免阻塞界面。"""

    done = Signal(bool, object, str, float)  # 是否成功, HTTP 状态码(异常时 None), 响应内容, 耗时

    def __init__(self, url, timeout, truncate=True):
        super().__init__()
        self._url = url
        self._timeout = timeout
        self._truncate = truncate

    def run(self):
        start = time.monotonic()
        try:
            resp = requests.get(self._url, timeout=self._timeout)
            elapsed = time.monotonic() - start
            ok = 200 <= resp.status_code < 300
            self.done.emit(ok, resp.status_code, self._body(resp, self._truncate), elapsed)
        except Exception as exc:  # 网络异常 / 超时
            self.done.emit(False, None, str(exc), time.monotonic() - start)

    @staticmethod
    def _body(resp, truncate=True) -> str:
        encoding = resp.encoding
        if not encoding or encoding.lower() in ("iso-8859-1", "ascii"):
            resp.encoding = resp.apparent_encoding or "utf-8"
        text = clean_body(resp.text.strip())
        if truncate and len(text) > MAX_BODY_STORE:
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


class UploadWorker(QThread):
    """后台上传 autocode 文件到服务端（multipart/form-data）。"""

    done = Signal(bool, str, str)  # ok, 服务端 file_path, 结果说明

    def __init__(self, srv, local_path, remote_path, parent=None):
        super().__init__(parent)
        self._srv = srv
        self._local_path = local_path
        self._remote_path = remote_path

    def run(self):
        url = UPLOAD_BASE.get(self._srv.get("machine"))
        if not url:
            self.done.emit(False, self._remote_path, "机器 %s 没有对应的上传接口" % self._srv.get("machine"))
            return
        try:
            with open(self._local_path, "rb") as fh:
                resp = requests.post(
                    url,
                    data={"file_path": self._remote_path, "key": UPLOAD_KEY},
                    files={"upload": (Path(self._local_path).name, fh)},
                    timeout=120,
                )
            body = resp.text.strip()
            if len(body) > 1500:
                body = body[:1500] + "…"
            # 替换是否成功以响应内容为准：包含「成功替换」才算成功，否则失败
            ok = "成功替换" in body
            if ok or 200 <= resp.status_code < 300:
                self.done.emit(ok, self._remote_path, body or "（空响应）")
            else:
                self.done.emit(False, self._remote_path, "HTTP %s\n%s" % (resp.status_code, body or "（空响应）"))
        except FileNotFoundError:
            self.done.emit(False, self._remote_path, "本地文件不存在：%s" % self._local_path)
        except requests.exceptions.RequestException as exc:
            self.done.emit(False, self._remote_path, "请求失败：%s" % exc)
        except Exception as exc:
            self.done.emit(False, self._remote_path, "上传异常：%s" % exc)


class FileDropArea(QFrame):
    """可拖入文件、也可点击选择文件的区域。"""

    fileChosen = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(110)
        self._path = ""
        self._set_idle_style()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        self._label = QLabel("将 autocode 目录下的文件拖到这里\n或点击选择文件")
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setWordWrap(True)
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self._label)

    def _set_idle_style(self):
        self.setStyleSheet(
            "FileDropArea { border: 2px dashed #b9c4d0; border-radius: 8px; background: #fafbfc; }"
        )

    def _set_hover_style(self):
        self.setStyleSheet(
            "FileDropArea { border: 2px dashed #2f7ae5; border-radius: 8px; background: #eef4ff; }"
        )

    def path(self):
        return self._path

    def set_path(self, path):
        self._path = path or ""
        self._label.setText(self._path or "将 autocode 目录下的文件拖到这里\n或点击选择文件")
        self._label.setStyleSheet("color:#1f5bb5;" if self._path else "color:#7a8794;")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._choose_file()
        super().mousePressEvent(event)

    def _choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择要替换的文件", "", "所有文件 (*.*)")
        if path:
            self.set_path(path)
            self.fileChosen.emit(path)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_hover_style()

    def dragLeaveEvent(self, event):
        self._set_idle_style()

    def dropEvent(self, event):
        self._set_idle_style()
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if local:
                self.set_path(local)
                self.fileChosen.emit(local)
                event.acceptProposedAction()
                return


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


class ViewLogDialog(QDialog):
    """查看日志：输入时间（默认今天，格式 YYYY_MM_DD）与日志名（自动补 .log 后缀）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("查看日志")
        self.setMinimumWidth(360)

        self.date_edit = QLineEdit(ymd_no_zero())
        self.date_edit.setPlaceholderText("时间，例如 2026_11_2")
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("日志名，例如 fight.log 或 fight")

        form = QFormLayout(self)
        form.addRow("时间", self.date_edit)
        form.addRow("日志名", self.name_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _on_accept(self):
        if not self.date_edit.text().strip():
            QMessageBox.information(self, "提示", "请输入时间")
            return
        if not self.name_edit.text().strip():
            QMessageBox.information(self, "提示", "请输入日志名")
            return
        self.accept()

    def values(self):
        date_str = self.date_edit.text().strip()
        log_name = self.name_edit.text().strip()
        if not log_name.lower().endswith(".log"):
            log_name += ".log"
        return date_str, log_name


def split_log_fields(line):
    """把一条日志行拆成展示行：时间戳单独一行，其余遇到「,」换行（一个字段一项）。"""
    line = line.rstrip()
    parts = []
    m = re.match(r"^(\[[^\]]*\])\s*(.*)$", line)
    rest = line
    if m:
        parts.append(m.group(1))
        rest = m.group(2)
    parts.extend(seg.strip() for seg in rest.split(",") if seg.strip())
    return parts or [line]


# Qt 自动换行只能在「断行机会」处断开；按 Unicode 断行规则，逗号后紧跟字母/数字不算断点
# （这样 "1,000"、"a,b" 才不会被拆开）。日志正文是「,」分隔的字段、没有空格，
# 整行唯一的断点就是时间戳里的空格——这就是时间戳总被从中间拆开的原因；
# 但若只把时间戳空格保护起来，整行又变成零断点，超宽时完全不换行。
# 因此显示前同时做两步处理：
#   1. 时间戳 [日期 时间] 里的空格换成窄不换行空格（U+202F），时间戳永远完整；
#      （不能用 U+00A0，QPlainTextEdit 会把它转成普通空格）
#   2. 每个逗号后插入零宽空格（U+200B）作为断点，长行在字段边界处折行。
# 复制 / 搜索 / 行详情时再做逆操作还原为原始文本。
LOG_ZWSP = "\u200b"
TS_NNBSP = chr(0x202F)
TS_SPACE_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\]")


def prepare_log_text(text):
    """日志显示前预处理：逗号后加零宽空格断点，时间戳空格换成不换行空格。"""
    text = text.replace(",", "," + LOG_ZWSP)
    return TS_SPACE_RE.sub(lambda m: "[" + m.group(1) + TS_NNBSP + m.group(2) + "]", text)


def restore_log_text(text):
    """prepare_log_text 的逆操作：还原零宽空格与不换行空格。"""
    return text.replace(LOG_ZWSP, "").replace(TS_NNBSP, " ")


# 日志查看弹窗「刷新」用的后台请求线程登记表：
# 刷新期间弹窗可能被关闭，登记可防止运行中的线程对象被回收
_refresh_workers = []


def _cleanup_refresh_worker(worker):
    """刷新线程结束后从登记表移除并销毁。"""
    if worker in _refresh_workers:
        _refresh_workers.remove(worker)
    worker.deleteLater()


class LogViewerDialog(QDialog):
    """日志查看窗口：显示日志全文，支持关键字搜索（高亮 + 上一个/下一个跳转）与按行筛选。"""

    _MATCH_CAP = 10000  # 高亮匹配数上限，避免超大日志全量高亮卡顿

    def __init__(self, title, text, parent=None, url=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1280, 720)
        self._full_text = text
        self._url = url      # 原始日志请求地址，用于「刷新」重新拉取最新内容
        self._worker = None  # 进行中的刷新线程
        self._matches = []      # 当前显示内容中的全部匹配光标
        self._current_idx = -1  # 当前选中的匹配下标
        self._capped = False    # 匹配数是否达到高亮上限

        layout = QVBoxLayout(self)

        # 搜索行：高亮所有匹配并支持跳转
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("搜索："))
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("输入关键字，回车查找下一个")
        self._search_edit.textChanged.connect(self._highlight_matches)
        self._search_edit.returnPressed.connect(self.find_next)
        search_row.addWidget(self._search_edit, 1)
        prev_btn = QPushButton("上一个")
        prev_btn.clicked.connect(self.find_prev)
        next_btn = QPushButton("下一个")
        next_btn.clicked.connect(self.find_next)
        search_row.addWidget(prev_btn)
        search_row.addWidget(next_btn)
        self._match_label = QLabel("")
        search_row.addWidget(self._match_label)
        layout.addLayout(search_row)

        # 筛选行：只显示包含关键字的行
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("筛选："))
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText("只显示包含关键字的行，回车生效")
        self._filter_edit.returnPressed.connect(self.apply_filter)
        filter_row.addWidget(self._filter_edit, 1)
        filter_btn = QPushButton("筛选")
        filter_btn.clicked.connect(self.apply_filter)
        reset_btn = QPushButton("重置")
        reset_btn.clicked.connect(self.reset_filter)
        filter_row.addWidget(filter_btn)
        filter_row.addWidget(reset_btn)
        self._filter_label = QLabel("")
        filter_row.addWidget(self._filter_label)
        layout.addLayout(filter_row)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        # 全局样式表 QWidget{font-size:13px} 会覆盖 setFont 的字号，字号需用样式表指定；
        # 选中色默认深灰，改为深蓝底白字更醒目
        self._view.setFont(QFont("Consolas", 12))
        self._view.setStyleSheet(
            "font-size: 15px;"
            "selection-background-color: #1565c0; selection-color: #ffffff;"
        )
        # 自动换行：正文逗号后已插入零宽空格作为断点（长行在字段边界处折行），
        # 时间戳空格已换成不换行空格（不会被拆）；完整内容可双击该行逐字段查看
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._view.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self._view.setPlainText(prepare_log_text(text))
        self._view.viewport().installEventFilter(self)  # 双击某行弹出字段详情
        layout.addWidget(self._view, 1)

        # 底部一行：左侧双击提示，右侧「刷新 / 复制全部 / 关闭」
        hint = QLabel("提示：双击任意一行日志，可打开整条日志详情（时间戳与各字段逐行展示）")
        hint.setStyleSheet("color:#999; font-size:12px;")
        row = QHBoxLayout()
        row.addWidget(hint)
        row.addStretch(1)
        self._browser_btn = QPushButton("浏览器打开")
        self._browser_btn.setEnabled(bool(url))
        self._browser_btn.clicked.connect(self._open_in_browser)
        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.clicked.connect(self._refresh_log)
        copy_btn = QPushButton("复制全部")
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(restore_log_text(self._view.toPlainText())))
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        row.addWidget(self._browser_btn)
        row.addWidget(self._refresh_btn)
        row.addWidget(copy_btn)
        row.addWidget(close_btn)
        layout.addLayout(row)

    # ---------- 搜索 ----------
    def _highlight_matches(self):
        """收集当前显示内容中的全部搜索匹配，刷新高亮与计数显示。"""
        keyword = self._search_edit.text()
        self._matches = []
        self._capped = False
        if keyword:
            # 显示文本含零宽空格（逗号后断点）与不换行空格（时间戳内）：
            # 查找时去掉前者、把后者当作普通空格，命中后再映射回原文位置
            raw = self._view.toPlainText()
            cleaned = []
            pos_map = []  # cleaned 下标 -> raw 下标
            for i, ch in enumerate(raw):
                if ch == LOG_ZWSP:
                    continue
                cleaned.append(" " if ch == TS_NNBSP else ch)
                pos_map.append(i)
            haystack = "".join(cleaned)
            needle = keyword.replace(LOG_ZWSP, "").replace(TS_NNBSP, " ")
            start = 0
            while needle:
                idx = haystack.find(needle, start)
                if idx < 0:
                    break
                if len(self._matches) >= self._MATCH_CAP:
                    self._capped = True
                    break
                cursor = QTextCursor(self._view.document())
                cursor.setPosition(pos_map[idx])
                cursor.setPosition(pos_map[idx + len(needle) - 1] + 1,
                                   QTextCursor.MoveMode.KeepAnchor)
                self._matches.append(cursor)
                start = idx + len(needle)
        self._current_idx = -1
        self._apply_match_highlight()

    def _apply_match_highlight(self):
        """重绘高亮：普通匹配黄色，当前选中匹配橙色；并更新「第 N / 共 M 处」。"""
        yellow = QTextCharFormat()
        yellow.setBackground(QColor("#fff59d"))
        orange = QTextCharFormat()
        orange.setBackground(QColor("#ff9800"))
        extras = []
        for i, cursor in enumerate(self._matches):
            sel = QTextEdit.ExtraSelection()
            sel.format = orange if i == self._current_idx else yellow
            sel.cursor = cursor
            extras.append(sel)
        self._view.setExtraSelections(extras)

        if not self._search_edit.text():
            self._match_label.setText("")
            return
        total = "%d+" % len(self._matches) if self._capped else "%d" % len(self._matches)
        if self._current_idx >= 0:
            self._match_label.setText("第 %d / %s 处" % (self._current_idx + 1, total))
        else:
            self._match_label.setText("共 %s 处" % total)

    def _goto_match(self, idx):
        """跳转到指定匹配：移动光标、滚到可见位置并刷新高亮。

        不做文本选中——编辑器默认选中色（深灰）会盖住橙色高亮，
        当前匹配完全由橙色 ExtraSelection 标识。
        """
        self._current_idx = idx
        cursor = QTextCursor(self._matches[idx])
        cursor.clearSelection()
        self._view.setTextCursor(cursor)
        self._view.ensureCursorVisible()
        self._apply_match_highlight()

    def find_next(self):
        if not self._matches:
            return
        pos = self._view.textCursor().position()
        # 光标位置（含当前选中的匹配）之后的第一个匹配，超出则回绕到开头
        idx = 0
        while idx < len(self._matches) and self._matches[idx].position() <= pos:
            idx += 1
        self._goto_match(idx if idx < len(self._matches) else 0)

    def find_prev(self):
        if not self._matches:
            return
        pos = self._view.textCursor().position()
        # 光标位置（含当前选中的匹配）之前的最后一个匹配，超出则回绕到末尾
        idx = len(self._matches) - 1
        while idx >= 0 and self._matches[idx].position() >= pos:
            idx -= 1
        self._goto_match(idx if idx >= 0 else len(self._matches) - 1)

    # ---------- 筛选 ----------
    def apply_filter(self):
        keyword = self._filter_edit.text()
        if not keyword:
            self.reset_filter()
            return
        lines = self._full_text.splitlines()
        matched = [ln for ln in lines if keyword in ln]
        self._view.setPlainText(prepare_log_text("\n".join(matched)))
        self._filter_label.setText("%d / %d 行" % (len(matched), len(lines)))
        self._highlight_matches()

    def reset_filter(self):
        self._filter_edit.clear()
        self._view.setPlainText(prepare_log_text(self._full_text))
        self._filter_label.setText("")
        self._highlight_matches()

    # ---------- 行详情 ----------
    def eventFilter(self, obj, event):
        """双击日志区某行：弹出该行字段详情（一个字段一行），并拦截默认取词选中。"""
        if obj is self._view.viewport() and event.type() == QEvent.Type.MouseButtonDblClick:
            cursor = self._view.cursorForPosition(event.position().toPoint())
            line = cursor.block().text()
            if line.strip():
                self._show_line_detail(line)
                return True
        return super().eventFilter(obj, event)

    def _show_line_detail(self, line):
        """弹窗展示一条日志：时间戳与各字段一行一个，超长值在弹窗内自动换行。"""
        line = restore_log_text(line)  # 详情里还原特殊空格，复制出来不带特殊字符
        dlg = QDialog(self)
        dlg.setWindowTitle("日志详情")
        dlg.resize(640, 560)
        lay = QVBoxLayout(dlg)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setFont(QFont("Consolas", 12))
        # 字号与选中色样式需在控件级指定，避免被全局样式表覆盖
        view.setStyleSheet(
            "font-size: 14px;"
            "selection-background-color: #1565c0; selection-color: #ffffff;"
        )
        view.setPlainText("\n".join(split_log_fields(line)))
        lay.addWidget(view, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        copy_btn = QPushButton("复制整行")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(line))
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dlg.accept)
        row.addWidget(copy_btn)
        row.addWidget(close_btn)
        lay.addLayout(row)
        dlg.exec()

    # ---------- 浏览器打开 ----------
    def _open_in_browser(self):
        """用系统默认浏览器打开当前日志的请求 URL。"""
        if not self._url:
            return
        webbrowser.open(self._url)

    # ---------- 刷新 ----------
    def _refresh_log(self):
        """重新请求该条日志的最新内容并更新显示。"""
        if not self._url or self._worker is not None:
            return
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText("刷新中…")
        worker = RequestWorker(self._url, REQUEST_TIMEOUT, truncate=False)
        _refresh_workers.append(worker)
        worker.finished.connect(lambda w=worker: _cleanup_refresh_worker(w))
        self._worker = worker
        worker.done.connect(self._on_refresh_done)
        worker.start()

    def _on_refresh_done(self, ok, status, body, elapsed):
        """刷新完成：成功则替换为最新内容（保留搜索/筛选状态），失败则弹窗提示。"""
        self._worker = None
        self._refresh_btn.setEnabled(True)
        self._refresh_btn.setText("刷新")
        if not ok:
            reason = body if status is None else 'HTTP %s' % status
            QMessageBox.warning(self, "查看日志", "日志刷新失败：%s" % reason)
            return
        self._full_text = body
        if self._filter_edit.text():
            self.apply_filter()
        else:
            self._view.setPlainText(prepare_log_text(body))
            self._highlight_matches()


class MoreCommandsDialog(QDialog):
    """「更多」指令弹窗：按钮排布与外层服务器指令区一致（每行 3 个，可带数字输入框）。

    底部「新增」按钮用于新增自定义指令；自定义指令按钮支持右键「编辑 / 删除」。
    """

    def __init__(self, more_cmds, add_callback=None, edit_callback=None,
                 delete_callback=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("更多指令")
        self.setModal(True)
        self.setMinimumWidth(460)
        self.chosen = None
        self.param_value = None
        self.param_widgets = {}  # cmd_key -> QSpinBox
        self._more_cmds = list(more_cmds)
        self._add_callback = add_callback
        self._edit_callback = edit_callback
        self._delete_callback = delete_callback

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        self._grid = QGridLayout()
        self._grid.setSpacing(6)
        layout.addLayout(self._grid)
        layout.addStretch(1)

        row = QHBoxLayout()
        row.addStretch(1)
        add_btn = QPushButton("新增")
        add_btn.clicked.connect(self._on_add)
        row.addWidget(add_btn)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.reject)
        row.addWidget(close_btn)
        layout.addLayout(row)

        self._rebuild()

    def _rebuild(self):
        """根据 self._more_cmds 重建按钮网格。"""
        self.param_widgets = {}
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for idx, cmd in enumerate(self._more_cmds):
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

            # 自定义指令：右键编辑 / 删除
            if cmd.get("custom"):
                btn.setToolTip("右键：编辑 / 删除")
                btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                btn.customContextMenuRequested.connect(
                    lambda pos, c=cmd, b=btn: self._on_cmd_context_menu(pos, c, b)
                )

            r, c = divmod(idx, 3)
            self._grid.addWidget(cell, r, c)
        for c in range(3):
            self._grid.setColumnStretch(c, 1)

    def _on_add(self):
        if not self._add_callback:
            return
        new_list = self._add_callback()
        if new_list is not None:
            self._more_cmds = list(new_list)
            self._rebuild()

    def _on_cmd_context_menu(self, pos, cmd, btn):
        menu = QMenu(self)
        edit_act = menu.addAction("编辑")
        del_act = menu.addAction("删除")
        chosen = menu.exec(btn.mapToGlobal(pos))
        if chosen == edit_act and self._edit_callback:
            new_list = self._edit_callback(cmd["key"])
            if new_list is not None:
                self._more_cmds = list(new_list)
                self._rebuild()
        elif chosen == del_act and self._delete_callback:
            new_list = self._delete_callback(cmd["key"])
            if new_list is not None:
                self._more_cmds = list(new_list)
                self._rebuild()

    def _choose(self, cmd):
        """记录选中的指令及其参数值，然后关闭弹窗。"""
        self.chosen = cmd
        spin = self.param_widgets.get(cmd["key"])
        self.param_value = str(spin.value()) if spin is not None else None
        self.accept()


class AddCommandDialog(QDialog):
    """「新增/编辑指令」弹窗：输入名字与指令，并勾选是否需要数字框、是否执行后检查进程。"""

    def __init__(self, initial=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑指令" if initial else "新增指令")
        self.setMinimumWidth(440)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如 服务器档位")
        self.op_edit = QLineEdit()
        self.op_edit.setPlaceholderText("例如 swSrvStage.sh")
        self.param_check = QCheckBox("需要数字框（数字跟在服务器编号后面）")
        self.proc_check = QCheckBox("执行后自动检查进程")

        if initial:
            self.name_edit.setText(initial.get("name", ""))
            self.op_edit.setText(initial.get("op", ""))
            self.param_check.setChecked(bool(initial.get("need_param")))
            self.proc_check.setChecked(bool(initial.get("check_proc")))

        form = QFormLayout(self)
        form.addRow("名字：", self.name_edit)
        form.addRow("指令：", self.op_edit)
        form.addRow("", self.param_check)
        form.addRow("", self.proc_check)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        form.addRow(buttons)

        def accept():
            if not self.name_edit.text().strip():
                QMessageBox.information(self, "提示", "请输入名字")
                return
            if not self.op_edit.text().strip():
                QMessageBox.information(self, "提示", "请输入指令")
                return
            self.accept()

        buttons.accepted.connect(accept)
        buttons.rejected.connect(self.reject)

    def values(self):
        return (
            self.name_edit.text().strip(),
            self.op_edit.text().strip(),
            self.param_check.isChecked(),
            self.proc_check.isChecked(),
        )


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
        self.resize(1120, 740)

        self.servers = load_servers()
        self._custom_cmd_defs = load_custom_cmd_defs()  # 「更多」中新增的自定义指令
        # 服务器选中状态按页签独立保存在 self._server_lists 各 entry 中
        # （selected / multi / multi_selected），页签之间互不同步
        self._flows = []       # 进行中的指令流程（支持多台服务器并发操作）
        self._zombies = []     # 已取消但后台线程尚未结束的请求，结束后自动回收
        self._log_workers = []  # 查看日志：进行中的日志拉取请求
        self._log_viewers = []  # 查看日志：已打开的日志查看窗口
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

        # 多个页签共用的服务器列表（数据共用，UI 各自渲染，选中状态各自独立）
        self._server_lists = []   # [{"grid", "group", "multi_btn", "selected", "multi", "multi_selected"}]

        # ---- Tab 1：服务器指令 ----
        self.tabs.addTab(self._build_server_tab(), "服务器指令")

        # ---- Tab 2：配置替换 ----
        self.tabs.addTab(self._build_replace_tab(), "配置替换")

        # ---- Tab 3：配置搜索 ----
        self.config_search_page = ConfigSearchPage(
            load_settings_fn=load_client_settings,
            save_settings_fn=save_client_settings,
        )
        self.tabs.addTab(self.config_search_page, "配置搜索")

        # ---- Tab 4：GM 指令 ----
        self.gm_page = GmCommandPage()
        self.tabs.addTab(self.gm_page, "GM 指令")

        self.tabs.currentChanged.connect(self._on_tab_changed)

    def _on_tab_changed(self, _index):
        """切到配置搜索页时，若配置目录在其它页签改过就刷新搜索范围列表。"""
        if self.tabs.currentWidget() is self.config_search_page:
            self.config_search_page.refresh_if_dir_changed()

    def _build_server_section(self):
        """服务器模块：标题 + 添加/多选 + 服务器列表。

        多个页签共用同一份服务器数据，但选中状态（selected / multi / multi_selected）
        保存在各自 entry 中，页签之间互不同步。
        """
        box = QWidget()
        vbox = QVBoxLayout(box)
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(8)

        vbox.addWidget(self._title("服务器"))
        tools = QHBoxLayout()
        add_btn = QPushButton("添加服务器")
        multi_btn = QPushButton("多选")
        multi_btn.setCheckable(True)
        multi_btn.setStyleSheet(MULTI_BTN_QSS)
        multi_btn.setToolTip("启用后可同时选中多台服务器，指令会发送给所有选中的服务器")
        tools.addWidget(add_btn)
        tools.addWidget(multi_btn)
        vbox.addLayout(tools)

        area = QScrollArea()
        area.setWidgetResizable(True)
        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(2, 2, 2, 2)
        grid.setSpacing(6)
        area.setWidget(host)
        vbox.addWidget(area, 1)

        group = QButtonGroup(self)
        group.setExclusive(True)
        entry = {
            "grid": grid,
            "group": group,
            "multi_btn": multi_btn,
            "selected": None,      # 单选模式下的当前服务器下标
            "multi": False,        # 是否启用多选
            "multi_selected": set(),  # 多选模式下选中的服务器下标
        }
        self._server_lists.append(entry)

        add_btn.clicked.connect(lambda _=False, e=entry: self._on_add(e))
        multi_btn.toggled.connect(lambda checked, e=entry: self._on_toggle_multi(e, checked))

        # 第一个列表保持原有属性名，兼容旧代码
        if not hasattr(self, "server_grid"):
            self.server_grid = grid
            self.button_group = group
            self.server_area = area
            self.multi_btn = multi_btn
            self.add_btn = add_btn
        return box

    def _build_replace_tab(self):
        """配置替换页签：共用服务器列表 + 上传 autocode 文件替换服务端配置。"""
        page = QWidget()
        root = QHBoxLayout(page)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(12)

        # 左列：服务器 + 替换配置
        left = QVBoxLayout()
        left.setSpacing(8)

        left.addWidget(self._build_server_section())
        self._replace_entry = self._server_lists[-1]

        left.addWidget(self._title("替换 autocode 配置"))
        self.drop_area = FileDropArea()
        left.addWidget(self.drop_area)

        # 按钮：一行三个，固定大小
        def _make_replace_btn(text):
            b = QPushButton(text)
            b.setFixedSize(150, 40)
            return b

        self.replace_srv_btn = _make_replace_btn("替换服务端")
        self.replace_srv_btn.clicked.connect(self._on_replace_server)
        self.replace_cli_btn = _make_replace_btn("替换客户端")
        self.replace_cli_btn.clicked.connect(self._on_replace_client)
        self.replace_branch_btn = _make_replace_btn("替换本周分支")
        self.replace_branch_btn.clicked.connect(lambda: self._on_replace_branch_client(0))
        self.replace_branch_last_btn = _make_replace_btn("替换上周分支")
        self.replace_branch_last_btn.clicked.connect(lambda: self._on_replace_branch_client(1))
        self.open_config_btn = _make_replace_btn("打开配置目录")
        self.open_config_btn.setToolTip("点击打开配置目录，右键可设置配置目录")
        self.open_config_btn.clicked.connect(self._on_open_config_dir)
        self.open_config_btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.open_config_btn.customContextMenuRequested.connect(self._on_config_dir_menu)

        btn_row1 = QHBoxLayout()
        btn_row1.setSpacing(8)
        for b in (self.replace_srv_btn, self.replace_cli_btn, self.replace_branch_btn):
            btn_row1.addWidget(b)
        btn_row1.addStretch(1)
        left.addLayout(btn_row1)

        btn_row2 = QHBoxLayout()
        btn_row2.setSpacing(8)
        for b in (self.replace_branch_last_btn, self.open_config_btn):
            btn_row2.addWidget(b)
        btn_row2.addStretch(1)
        left.addLayout(btn_row2)

        # 自定义路径替换：输入框默认自动拼接服务端路径（不含本地文件相对路径部分）
        self.custom_path_edit = QLineEdit()
        self.custom_path_edit.setPlaceholderText("自定义服务端文件路径（默认自动拼接，可修改）")
        self.custom_path_edit.setFixedHeight(int(self.custom_path_edit.sizeHint().height() * 1.3))
        left.addWidget(self.custom_path_edit)
        self.replace_custom_btn = QPushButton("自定义路径替换")
        self.replace_custom_btn.setFixedSize(150, 40)
        self.replace_custom_btn.clicked.connect(self._on_replace_custom)
        custom_row = QHBoxLayout()
        custom_row.addWidget(self.replace_custom_btn)
        custom_row.addStretch(1)
        left.addLayout(custom_row)
        self._refresh_custom_path_default()

        left.addStretch(1)

        left_widget = QWidget()
        left_widget.setLayout(left)
        left_widget.setMinimumWidth(500)
        left_widget.setMaximumWidth(500)
        root.addWidget(left_widget)

        # 右列：执行日志（独立 QTextBrowser，与服务器指令页签互不影响）
        right = QVBoxLayout()
        right.setSpacing(8)
        right.addWidget(self._title("执行日志"))
        self.replace_log = QTextBrowser()
        self.replace_log.setOpenLinks(False)
        self.replace_log.setOpenExternalLinks(False)
        self.replace_log.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.replace_log.customContextMenuRequested.connect(self._on_log_context_menu)
        right.addWidget(self.replace_log, 1)
        root.addLayout(right, 1)
        return page

    def _on_replace_server(self):
        path = self.drop_area.path()
        if not path:
            QMessageBox.information(self, "提示", "请先拖入或选择要替换的文件")
            return
        targets = [self.servers[i] for i in self._target_indexes(self._replace_entry)]
        if not targets:
            QMessageBox.information(self, "提示", "请先在上方选中一台（或多台）服务器")
            return

        jobs = []
        errors = []
        for srv in targets:
            remote, err = build_upload_file_path(path, srv)
            if err:
                errors.append(err)
            else:
                jobs.append((srv, remote))
        if errors:
            QMessageBox.warning(self, "无法替换", "\n".join(errors))
            if not jobs:
                return
        if not jobs:
            return

        self._start_replace_uploads(path, jobs)

    def _on_replace_custom(self):
        """自定义路径替换：与「替换服务端」相同，但 file_path 取输入框内容。"""
        path = self.drop_area.path()
        if not path:
            QMessageBox.information(self, "提示", "请先拖入或选择要替换的文件")
            return
        remote = self.custom_path_edit.text().strip()
        if not remote:
            QMessageBox.information(self, "提示", "请先填写自定义的服务端文件路径")
            return
        targets = [self.servers[i] for i in self._target_indexes(self._replace_entry)]
        if not targets:
            QMessageBox.information(self, "提示", "请先在上方选中一台（或多台）服务器")
            return

        jobs = []
        errors = []
        for srv in targets:
            if srv.get("machine") not in UPLOAD_BASE:
                errors.append("服务器「%s」的机器号 %s 暂不支持配置替换"
                              % (srv.get("name"), srv.get("machine")))
            else:
                jobs.append((srv, remote))
        if errors:
            QMessageBox.warning(self, "无法替换", "\n".join(errors))
            if not jobs:
                return
        if not jobs:
            return

        self._start_replace_uploads(path, jobs)

    def _start_replace_uploads(self, path, jobs):
        """启动上传任务：jobs = [(srv, remote_file_path), ...]"""
        self._replace_workers = []
        self.replace_srv_btn.setEnabled(False)
        self.replace_custom_btn.setEnabled(False)
        self._append_to_log('[%s] 正在上传到 %d 台服务器…' % (now_str(), len(jobs)), self.replace_log)
        self._replace_pending = len(jobs)

        for srv, remote in jobs:
            worker = UploadWorker(srv, path, remote)
            worker.done.connect(lambda ok, rp, msg, s=srv: self._on_replace_done(s, ok, rp, msg))
            self._replace_workers.append(worker)
            worker.start()

    def _on_replace_done(self, srv, ok, remote_path, message):
        name = srv["name"]
        # 先打印响应内容，再根据响应内容给出判断结果
        self._append_to_log('[%s] %s <span style="color:#666">响应内容：</span>%s'
                            % (now_str(), esc(name), esc_multiline(message)), self.replace_log)
        if ok:
            self._append_to_log('[%s] <span style="color:#1e7e34">替换成功：%s</span>'
                                % (now_str(), esc(name)), self.replace_log)
        else:
            self._append_to_log('[%s] <span style="color:#c62828">替换失败：%s</span>'
                                % (now_str(), esc(name)), self.replace_log)
            get_logger().warning(
                "文件替换失败 | 服务器=%s | 目标=%s | %s", name, remote_path, message
            )
        self._replace_pending -= 1
        if self._replace_pending <= 0:
            self.replace_srv_btn.setEnabled(True)
            self._update_custom_widgets_enabled()

    # ---------- 自定义路径默认值 ----------
    def _auto_custom_path(self):
        """自动拼接的服务端路径（不含从本地文件截取的 autocode 相对路径部分）。"""
        indexes = self._target_indexes(self._replace_entry)
        if not indexes:
            return ""
        srv = self.servers[indexes[0]]
        prefix = UPLOAD_PATH_PREFIX.get(srv.get("machine"))
        if prefix is None:
            return ""
        return prefix + (UPLOAD_PATH_TAIL % (srv["number"], ""))

    def _refresh_custom_path_default(self):
        """选中服务器变化时，实时刷新为最新默认路径。"""
        if not hasattr(self, "custom_path_edit"):
            return
        self.custom_path_edit.setText(self._auto_custom_path())

    def _update_custom_widgets_enabled(self):
        """多选模式下禁用自定义路径输入框和替换按钮（上传中也保持禁用）。"""
        if not hasattr(self, "custom_path_edit"):
            return
        multi = self._replace_entry["multi"]
        uploading = getattr(self, "_replace_pending", 0) > 0
        self.custom_path_edit.setEnabled(not multi)
        self.replace_custom_btn.setEnabled(not multi and not uploading)

    def _on_replace_client(self):
        """替换主干客户端：把文件复制到主干客户端的 autocode 脚本目录。"""
        settings, ok = self._ensure_client_settings(("need_main",))
        if not ok:
            return
        self._replace_client_local("主干客户端", settings["main_client_path"])

    def _on_replace_branch_client(self, weeks_ago):
        """替换本周/上周分支客户端：把文件复制到分支客户端的 autocode 脚本目录。"""
        settings, ok = self._ensure_client_settings(("need_branch",))
        if not ok:
            return
        wed = latest_wednesday_mmdd(weeks_ago=weeks_ago)
        exe_path = str(Path(settings["branch_root_dir"]) / wed / "game" / "game.exe")
        which = "本周分支客户端" if weeks_ago == 0 else "上周分支客户端"
        self._replace_client_local("%s（%s）" % (which, wed), exe_path)

    def _replace_client_local(self, label, exe_path):
        """把已选文件复制到本地客户端 autocode 脚本目录（由 exe 路径推导）。

        例如 exe 为 E:/y11_designer/game/game.exe，
        目标目录为 E:/y11_designer/game/game_Data/StreamingAssets/script/autocode，
        再拼接上传文件 autocode 之后的相对路径。
        """
        path = self.drop_area.path()
        if not path:
            QMessageBox.information(self, "提示", "请先拖入或选择要替换的文件")
            return
        sub = autocode_relative_path(path)
        if sub is None:
            QMessageBox.warning(self, "无法替换", "文件路径必须位于 autocode 目录下：\n%s" % path)
            return
        exe = Path(exe_path)
        autocode_dir = exe.parent / (exe.stem + "_Data") / "StreamingAssets" / "script" / "autocode"
        dest = Path(str(autocode_dir) + sub)
        if not autocode_dir.is_dir():
            QMessageBox.warning(
                self, "错误",
                "%s的 autocode 目录不存在：\n%s\n请确认客户端已正确安装/构建。" % (label, autocode_dir),
            )
            self._append_to_log('[%s] <span style="color:#c62828">替换%s失败：目录不存在 %s</span>'
                                % (now_str(), esc(label), esc(str(autocode_dir))), self.replace_log)
            return
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
        except OSError as exc:
            self._append_to_log('[%s] <span style="color:#c62828">替换%s失败：%s</span>'
                                % (now_str(), esc(label), esc(str(exc))), self.replace_log)
            return
        self._append_to_log('[%s] <span style="color:#1e7e34">替换%s成功</span>'
                            '<br><span style="color:#666">目标路径：</span>%s'
                            % (now_str(), esc(label), esc(str(dest))), self.replace_log)

    # ---------- 配置目录 ----------
    def _on_config_dir_menu(self, pos):
        """「打开配置目录」按钮右键菜单：设置配置目录。"""
        menu = QMenu(self)
        act = menu.addAction("设置配置目录…")
        chosen = menu.exec(self.open_config_btn.mapToGlobal(pos))
        if chosen == act:
            self._open_config_dir_dialog()

    def _open_config_dir_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("设置配置目录")
        dlg.setMinimumWidth(500)

        edit = QLineEdit()
        edit.setPlaceholderText("配置目录，例如 E:/y11_designer/C-策划导表/autocode")
        edit.setText(load_client_settings().get("config_dir", ""))

        def pick():
            path = QFileDialog.getExistingDirectory(dlg, "选择配置目录")
            if path:
                edit.setText(path)

        form = QFormLayout(dlg)
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        pick_btn = QPushButton("选择…")
        pick_btn.clicked.connect(pick)
        row.addWidget(pick_btn)
        wrap = QWidget()
        wrap.setLayout(row)
        form.addRow("配置目录：", wrap)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        form.addRow(buttons)

        def accept():
            settings = load_client_settings()
            settings["config_dir"] = edit.text().strip()
            save_client_settings(settings)
            dlg.accept()

        buttons.accepted.connect(accept)
        buttons.rejected.connect(dlg.reject)
        dlg.exec()

    def _on_open_config_dir(self):
        """打开配置目录（未配置时引导设置，右键按钮也可设置）。"""
        config_dir = load_client_settings().get("config_dir", "")
        if not config_dir:
            answer = QMessageBox.question(
                self, "配置目录未配置",
                "尚未设置配置目录（右键「打开配置目录」按钮可随时设置）。\n\n是否现在设置？",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._open_config_dir_dialog()
            config_dir = load_client_settings().get("config_dir", "")
            if not config_dir:
                return
        if not Path(config_dir).is_dir():
            QMessageBox.warning(
                self, "错误",
                "配置目录不存在：\n%s\n请右键「打开配置目录」按钮重新设置。" % config_dir,
            )
            return
        try:
            if sys.platform == "win32":
                os.startfile(config_dir)
            else:
                webbrowser.open(Path(config_dir).as_uri())
        except Exception as exc:
            QMessageBox.warning(self, "错误", "打开配置目录失败：\n%s" % exc)

    def _build_server_tab(self):
        """服务器指令页签，包含服务器列表、报错监控、指令、日志。"""
        page = QWidget()
        root = QHBoxLayout(page)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(12)

        # 左列：服务器 + 指令
        left = QVBoxLayout()
        left.setSpacing(8)

        left.addWidget(self._build_server_section())
        self._server_entry = self._server_lists[-1]

        # 报错监控：每种日志一个开关，作用于当前选中的服务器；可多服务器同时开
        left.addWidget(self._title("报错监控（选中服务器生效，可多服务器同时开）"))
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
                    more_btn.setToolTip("点击打开更多指令窗口；在窗口中可新增、编辑、删除自定义指令")
                    more_btn.clicked.connect(lambda _=False: self._show_more_dialog())
                    self.cmd_buttons.append(more_btn)
                    row_h.addWidget(more_btn, 1)
                    continue

                cmd = item
                btn = QPushButton(cmd["label"])
                btn.setMinimumHeight(40)
                btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                btn.clicked.connect(lambda _=False, c=cmd: self._on_run(c))
                if cmd["key"] == "swgroup":
                    btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                    btn.customContextMenuRequested.connect(
                        lambda pos, b=btn: self._show_group_view_menu(b)
                    )
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
        left_widget.setMinimumWidth(480)
        left_widget.setMaximumWidth(480)
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
        # 清空所有页签里的服务器列表
        for entry in self._server_lists:
            group, grid = entry["group"], entry["grid"]
            for btn in list(group.buttons()):
                group.removeButton(btn)
            while grid.count():
                item = grid.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.setParent(None)
                    widget.deleteLater()

        for entry in self._server_lists:
            group, grid = entry["group"], entry["grid"]
            # 越界下标修正（服务器被删除/移动后）
            sel = entry["selected"]
            if sel is not None and not (0 <= sel < len(self.servers)):
                entry["selected"] = None
            entry["multi_selected"] = {i for i in entry["multi_selected"] if 0 <= i < len(self.servers)}

            group.setExclusive(not entry["multi"])
            for idx, srv in enumerate(self.servers):
                btn = QPushButton()
                btn._full_text = '%s  #%s' % (srv["name"], srv["number"])
                btn.setText(btn._full_text)
                btn.setCheckable(True)
                if entry["multi"]:
                    btn.setChecked(idx in entry["multi_selected"])
                else:
                    btn.setChecked(idx == entry["selected"])
                btn.setMinimumHeight(38)
                btn.setStyleSheet(SERVER_BTN_QSS)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setToolTip('名称：%s\n编号：%s\n机器：%s' % (srv["name"], srv["number"], srv["machine"]))
                btn.clicked.connect(lambda _=False, e=entry, i=idx, b=btn: self._on_select(e, i, b))
                btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                btn.customContextMenuRequested.connect(
                    lambda pos, i=idx, b=btn: self._on_server_menu(i, b, pos)
                )
                group.addButton(btn, idx)
                row, col = divmod(idx, 3)
                grid.addWidget(btn, row, col)
            # 三列等宽
            for c in range(3):
                grid.setColumnStretch(c, 1)
            grid.setRowStretch(grid.rowCount(), 1)
        self._refresh_monitor_buttons()
        self._refresh_custom_path_default()
        # 延迟一帧后计算省略文本（此时按钮已有实际宽度）
        QTimer.singleShot(0, self._update_server_btn_elide)

    def _update_server_btn_elide(self):
        """根据当前按钮实际宽度，重新计算省略文本。"""
        metrics = self.fontMetrics()
        for entry in self._server_lists:
            for btn in list(entry["group"].buttons()):
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
    def _on_add(self, entry=None):
        dlg = ServerDialog("添加服务器", parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, number, machine = dlg.values()
        if any(s["name"] == name and s["number"] == number and s["machine"] == machine for s in self.servers):
            QMessageBox.information(self, "提示", "该服务器已存在，无需重复添加")
            return
        self.servers.append({"name": name, "number": number, "machine": machine})
        save_servers(self.servers)
        # 只选中发起添加的那个页签里的新服务器
        if entry is not None:
            new_idx = len(self.servers) - 1
            if entry["multi"]:
                entry["multi_selected"].add(new_idx)
            else:
                entry["selected"] = new_idx
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
        """移动服务器，同步修复各页签选中下标与监控 mkey 的映射。"""
        if from_idx == to_idx or not (0 <= to_idx < len(self.servers)):
            return
        srv = self.servers.pop(from_idx)
        self.servers.insert(to_idx, srv)
        save_servers(self.servers)
        for entry in self._server_lists:
            # 修复单选下标
            sel = entry["selected"]
            if sel is not None:
                if sel == from_idx:
                    entry["selected"] = to_idx
                elif from_idx < sel <= to_idx:
                    entry["selected"] = sel - 1
                elif to_idx <= sel < from_idx:
                    entry["selected"] = sel + 1
            # 修复多选下标
            new_multi = set()
            for i in entry["multi_selected"]:
                if i == from_idx:
                    new_multi.add(to_idx)
                elif from_idx < i <= to_idx:
                    new_multi.add(i - 1)
                elif to_idx <= i < from_idx:
                    new_multi.add(i + 1)
                else:
                    new_multi.add(i)
            entry["multi_selected"] = new_multi
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
        for entry in self._server_lists:
            entry["multi_selected"] = {
                i if i < idx else i - 1 for i in entry["multi_selected"] if i != idx
            }
            sel = entry["selected"]
            if sel == idx:
                entry["selected"] = None
            elif sel is not None and sel > idx:
                entry["selected"] = sel - 1
        self._refresh_server_buttons()
        self._append('已删除服务器：%s' % esc(srv["name"]))

    def _on_toggle_multi(self, entry, checked):
        """切换某个页签的多选模式（各页签互不影响）。"""
        entry["multi"] = checked
        entry["group"].setExclusive(not checked)
        if checked:
            # 进入多选时，把当前单选结果带过来
            if entry["selected"] is not None:
                entry["multi_selected"] = {entry["selected"]}
            entry["selected"] = None
        else:
            entry["multi_selected"].clear()
        entry["multi_btn"].setText("已启用多选" if checked else "多选")
        self._refresh_server_buttons()
        self._refresh_monitor_buttons()
        if entry is getattr(self, "_replace_entry", None):
            self._update_custom_widgets_enabled()

    def _on_select(self, entry, idx, btn=None):
        if entry["multi"]:
            target = btn if btn is not None else entry["group"].button(idx)
            if target is not None and target.isChecked():
                entry["multi_selected"].add(idx)
            else:
                entry["multi_selected"].discard(idx)
        else:
            entry["selected"] = idx
        if entry is getattr(self, "_replace_entry", None):
            self._refresh_custom_path_default()
        self._refresh_monitor_buttons()

    def _current_index(self, entry=None):
        entry = entry or getattr(self, "_server_entry", None)
        if entry is None:
            return None
        sel = entry["selected"]
        if sel is not None and 0 <= sel < len(self.servers):
            return sel
        return None

    def _target_indexes(self, entry=None):
        """本次指令要执行的所有服务器下标（默认取「服务器指令」页签的选中状态）。"""
        entry = entry or getattr(self, "_server_entry", None)
        if entry is None:
            return []
        if entry["multi"]:
            return [i for i in sorted(entry["multi_selected"]) if 0 <= i < len(self.servers)]
        idx = self._current_index(entry)
        return [] if idx is None else [idx]

    def _collect_selected_indices(self):
        """别名：返回当前选中范围的所有服务器下标（多选 / 单选通用）。"""
        return self._target_indexes()

    # ---------- 群组查看 ----------
    def _show_group_view_menu(self, btn):
        """「切换群组」按钮右键菜单：提供「查看群组」入口。"""
        menu = QMenu(self)
        act = menu.addAction("查看群组")
        act.triggered.connect(lambda _=False: self._show_group_list_dialog())
        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _show_group_list_dialog(self):
        """弹出群组对照表（编号 ↔ 群组名称）。"""
        dlg = QDialog(self)
        dlg.setWindowTitle("群组列表")
        dlg.resize(360, 430)
        layout = QVBoxLayout(dlg)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(GROUP_LIST_TEXT)
        layout.addWidget(view)
        row = QHBoxLayout()
        row.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(dlg.accept)
        row.addWidget(close_btn)
        layout.addLayout(row)
        dlg.exec()

    # ---------- 更多指令弹窗 ----------
    def _custom_commands(self):
        """把自定义指令定义转换成与 COMMANDS 同结构的指令项。"""
        cmds = []
        for idx, d in enumerate(self._custom_cmd_defs):
            cmd = {
                "key": "custom_%d" % idx,
                "label": d["name"],
                "op": d["op"],
                "check_proc": d["check_proc"],
                "more": True,
                "custom": True,
            }
            if d.get("need_param"):
                cmd["param"] = "arg"
                cmd["param_name"] = "参数"
            cmds.append(cmd)
        return cmds

    def _all_more_commands(self):
        """「更多」窗口中的全部指令：内置的 more 指令 + 自定义指令（内置的保持原样）。"""
        return [c for c in COMMANDS if c.get("more")] + self._custom_commands()

    def _show_more_dialog(self):
        """点击「更多」按钮时，弹出独立窗口列出更多指令（按钮格式与外层一致）。"""
        dlg = MoreCommandsDialog(
            self._all_more_commands(),
            add_callback=self._add_custom_and_get_list,
            edit_callback=self._edit_custom_command,
            delete_callback=self._delete_custom_command,
            parent=self,
        )
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        chosen = dlg.chosen
        value = dlg.param_value
        dlg.deleteLater()
        if accepted and chosen is not None:
            self._on_run(chosen, param_override=value)

    def _add_custom_and_get_list(self):
        """在「更多」弹窗里新增指令后，返回更新后的指令列表供弹窗刷新。"""
        self._on_add_custom_command()
        return self._all_more_commands()

    @staticmethod
    def _custom_idx_from_key(key):
        """把 custom_N 形式的 key 还原成 _custom_cmd_defs 的下标。"""
        try:
            return int(str(key).split("_", 1)[1])
        except (ValueError, IndexError):
            return -1

    def _edit_custom_command(self, key):
        idx = self._custom_idx_from_key(key)
        if idx < 0 or idx >= len(self._custom_cmd_defs):
            return self._all_more_commands()
        dlg = AddCommandDialog(initial=self._custom_cmd_defs[idx], parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return self._all_more_commands()
        name, op, need_param, check_proc = dlg.values()
        if any(i != idx and d["op"].lower() == op.lower()
               for i, d in enumerate(self._custom_cmd_defs)):
            QMessageBox.information(self, "提示", "该指令已存在：%s" % op)
            return self._all_more_commands()
        self._custom_cmd_defs[idx] = {
            "name": name, "op": op,
            "need_param": need_param, "check_proc": check_proc,
        }
        save_custom_cmd_defs(self._custom_cmd_defs)
        self._append('[%s] 已编辑指令：<b>%s</b>' % (now_str(), esc(name)))
        return self._all_more_commands()

    def _delete_custom_command(self, key):
        idx = self._custom_idx_from_key(key)
        if idx < 0 or idx >= len(self._custom_cmd_defs):
            return self._all_more_commands()
        d = self._custom_cmd_defs[idx]
        answer = QMessageBox.question(
            self, "确认删除", '确定删除自定义指令「%s」吗？' % d["name"]
        )
        if answer != QMessageBox.StandardButton.Yes:
            return self._all_more_commands()
        del self._custom_cmd_defs[idx]
        save_custom_cmd_defs(self._custom_cmd_defs)
        self._append('[%s] 已删除指令：<b>%s</b>' % (now_str(), esc(d["name"])))
        return self._all_more_commands()

    def _on_add_custom_command(self):
        dlg = AddCommandDialog(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, op, need_param, check_proc = dlg.values()
        if any(d["op"].lower() == op.lower() for d in self._custom_cmd_defs):
            QMessageBox.information(self, "提示", "该指令已存在：%s" % op)
            return
        self._custom_cmd_defs.append({
            "name": name,
            "op": op,
            "need_param": need_param,
            "check_proc": check_proc,
        })
        save_custom_cmd_defs(self._custom_cmd_defs)
        self._append(
            '[%s] 已新增指令：<b>%s</b>（%s，%s，%s）'
            % (now_str(), esc(name),
               "带数字框" if need_param else "无参数",
               "执行后检查进程" if check_proc else "不检查进程",
               "指令 %s" % op)
        )

    # ---------- 查看日志 ----------
    def _view_logs(self):
        """查看日志：输入时间与日志名，按选中服务器拉取日志并弹出查看窗口。"""
        targets = self._target_indexes()
        if not targets:
            QMessageBox.information(self, "提示", "请先在上方选中一个服务器")
            return
        dlg = ViewLogDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        date_str, log_name = dlg.values()

        for idx in targets:
            srv = self.servers[idx]
            if not MACHINE_PREFIX.get(srv["machine"]):
                QMessageBox.warning(self, "错误", '未知的服务器机器：%s' % srv["machine"])
                continue
            url = '%s%s %s %s/%s' % (
                MACHINE_PREFIX[srv["machine"]], LOG_OP, srv["number"], date_str, log_name,
            )
            self._append(
                '[%s] [%s] 执行「查看日志（%s/%s）」'
                % (now_str(), esc(srv["name"]), esc(date_str), esc(log_name))
            )
            worker = RequestWorker(url, REQUEST_TIMEOUT, truncate=False)
            self._log_workers.append(worker)
            worker.done.connect(
                lambda ok, status, body, elapsed, s=srv, w=worker, u=url:
                    self._on_log_fetched(s, w, ok, status, body, elapsed, date_str, log_name, u)
            )
            worker.start()

    def _on_log_fetched(self, srv, worker, ok, status, body, elapsed, date_str, log_name, url):
        """日志拉取完成：成功则弹出日志查看窗口，失败则输出红色日志并弹窗提示。"""
        if worker in self._log_workers:
            self._log_workers.remove(worker)
        worker.deleteLater()
        if not ok:
            reason = body if status is None else 'HTTP %s' % status
            self._append(
                '[%s] [%s] <span style="color:#c62828">查看日志失败：%s</span>'
                % (now_str(), esc(srv["name"]), esc(reason))
            )
            QMessageBox.warning(self, "查看日志", '「%s」日志拉取失败：%s' % (srv["name"], reason))
            return
        self._append(
            '[%s] [%s] 日志拉取成功（%.1f 秒），共 %d 行'
            % (now_str(), esc(srv["name"]), elapsed, len(body.splitlines()))
        )
        # 不传父窗口：日志窗口是独立顶层窗口，可移到主窗口后面、单独在任务栏切换
        viewer = LogViewerDialog(
            '查看日志 - %s / %s/%s' % (srv["name"], date_str, log_name), body, url=url,
        )
        self._log_viewers.append(viewer)
        viewer.finished.connect(lambda _=0, v=viewer: self._on_viewer_closed(v))
        viewer.show()

    def _on_viewer_closed(self, viewer):
        if viewer in self._log_viewers:
            self._log_viewers.remove(viewer)
        viewer.deleteLater()

    # ---------- 执行指令 ----------
    def _on_run(self, cmd, param_override=None):
        if cmd.get("mode") == "local":
            self._run_local(cmd)
            return
        if cmd.get("dialog") == "view_log":
            self._view_logs()
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
            settings = load_client_settings()
            save_client_settings({
                "main_client_path": main_edit.text().strip(),
                "branch_root_dir": branch_edit.text().strip(),
                "config_dir": settings.get("config_dir", ""),
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
        to_check_history = []  # 本次新开启监控的 (mkey, srv, log_def) 列表
        today_ymd = ymd_no_zero()
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
                    "poll_date": today_ymd,
                    "fail": 0,
                }
                self._append('[%s] <b>开始监控「%s报错」</b>' % (esc(srv["name"]), esc(log_def["label"])))
                any_changed = True
                to_check_history.append((mkey, srv, log_def))
            else:
                state = self._monitors.pop(mkey, None)
                if state is None:
                    continue
                self._append('[%s] <b>停止监控「%s报错」</b>' % (esc(srv["name"]), esc(log_def["label"])))
                any_changed = True
        if any_changed:
            self._sync_monitor_timer()
        # 开启监控后，异步检查是否有历史报错
        if to_check_history:
            jobs = []
            for mkey, srv, ld in to_check_history:
                url = '%s%s %s %s/%s' % (
                    MACHINE_PREFIX[srv["machine"]], LOG_OP, srv["number"], today_ymd, ld["file"],
                )
                jobs.append((mkey, url))
            poller = MonitorPoller(self)
            poller.set_jobs(jobs)
            poller.polled.connect(
                lambda results, srvs={id(srv): srv for _, srv, _ in to_check_history},
                logs={ld["key"]: ld for _, _, ld in to_check_history}:
                self._on_initial_history_checked(results, srvs, logs)
            )
            poller.finished.connect(lambda p=poller: p.deleteLater())
            poller.start()

    def _on_initial_history_checked(self, results, srv_map, log_map):
        """开启监控后的历史报错检查回调；有报错时弹窗提示。"""
        has_errors = []  # [(srv_name, log_label, blocks)]
        for mkey, ok, body, error in results:
            srv = srv_map.get(mkey[0])
            ld = log_map.get(mkey[1])
            if srv is None or ld is None or not ok or not body:
                continue
            blocks = self._split_error_blocks(body)
            if blocks:
                has_errors.append((srv["name"], ld["label"], blocks))
        if not has_errors:
            return
        # 有历史报错，弹窗展示
        self._show_initial_history_dialog(has_errors)

    def _show_initial_history_dialog(self, errors):
        """开启监控后发现历史报错时的弹窗。errors: [(srv_name, log_label, [(ts, content), ...]), ...]"""
        all_blocks = []
        for srv_name, log_label, blocks in errors:
            header = '<span style="color:#c62828;font-weight:bold">[服务器：%s / %s报错]</span>' % (
                esc(srv_name), esc(log_label)
            )
            rendered_parts = []
            for _, content in blocks:
                html = esc(content)
                html = ERROR_TIME_RE.sub(
                    lambda m: '<span style="color:#2f7ded;font-size:16px;font-weight:bold">[%s]traceback:</span>'
                              % m.group(1),
                    html,
                )
                rendered_parts.append(html.replace("\n", "<br/>"))
            all_blocks.append(header + "<br/>" + ("<hr/>".join(rendered_parts)))
        combined = "<hr/><hr/>".join(all_blocks)
        title = "存在历史报错信息（%d 台服务器共 %d 类日志）" % (
            len({s for s, _, _ in errors}), len(errors)
        )
        self._show_combined_errors(title, combined)

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
        today_ymd = ymd_no_zero()
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
        today_ymd = ymd_no_zero()
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
            get_logger().warning(
                "监控拉取失败 | 服务器=%s | %s报错 | 连续失败 %d 次 | %s",
                srv["name"], log_def["label"], state["fail"], error,
            )
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
        if not ok:
            get_logger().warning(
                "指令执行失败 | 服务器=%s | 步骤=%s | HTTP=%s | 耗时=%.2f秒 | %s",
                srv["name"],
                step.get("label", ""),
                status if status is not None else "无响应",
                elapsed,
                (body or "")[:500],
            )
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
        self._append_to_log(html, self.log)

    def _append_to_log(self, html, log_widget):
        doc = log_widget.document()
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not doc.isEmpty():
            cursor.insertBlock()
            cursor.setCharFormat(QTextCharFormat())
        cursor.insertHtml(html)
        bar = log_widget.verticalScrollBar()
        bar.setValue(bar.maximum())

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

    setup_logging(DATA_DIR, "测试工具")

    app = QApplication(sys.argv)
    app.setApplicationName("测试工具")
    app.setStyleSheet(APP_QSS)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
