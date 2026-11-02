# -*- coding: utf-8 -*-
"""配置搜索：在 autocode 的 Lua 配置表里按中文注释 / 关键词做全文搜索。

单独运行：
    python config_search.py

并入现有工具（不改动原有代码，只需在某个页签里加一行）：
    from config_search import ConfigSearchPage
    self.tabs.addTab(ConfigSearchPage(), "配置搜索")

本文件只读取 data/client_settings.json 里的 config_dir，不修改其它内容。
"""

import html
import json
import os
import re
import subprocess
import sys
import time
from bisect import bisect_right
from itertools import accumulate
from pathlib import Path
from threading import Event

from PySide6.QtCore import QStringListModel, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

# 本地化副本后缀：默认忽略这些副本，只保留 CN/原表/Mod
LOCALE_SUFFIX = re.compile(r"(?:JP|KR|TW|TH|VI|Hybrid)$")
# 默认跳过的文件夹（可在页面上方右键配置）
DEFAULT_IGNORE_DIRS = ("tmp",)
# 表内结构标记
CONTENT_RE = re.compile(r"Content\s*=\s*\{")
SHEET_KEY_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*\{\s*$")
ID_RE = re.compile(r"^\s*\[\s*([^\]]+?)\s*\]\s*=\s*\{")
COMMENT_RE = re.compile(r"--\[\[.*?\]\]", re.S)
# 多语言表（Language/CN）：[ id ] = { Txt = [=[文本]=] }
LANG_ENTRY_RE = re.compile(r"\[\s*(\d+)\s*\]\s*=\s*\{\s*Txt\s*=\s*\[(=*)\[(.*?)\]\2\]", re.S)
# 多语言字段：字段名以 Name/Des/Text 等结尾，值是语言 id
ML_FIELD_RE = re.compile(
    r"^(\s*)(\w*(?:Name|Des|Desc|Txt|Text|Title|Tips?))\s*=\s*(\d+)\s*,?\s*$",
    re.M,
)
# 多语言候选值预筛：以 "=" 开头可触发快速子串查找，比逐 id 扫描或长交替正则快得多
ML_VALUE_RE = re.compile(r"=[ \t]*(\d{3,})[ \t]*,[ \t]*\n")

MAX_PER_FILE = 300
MAX_TOTAL = 5000

# svn update 输出状态码 -> 中文
SVN_STATUS_TEXT = {
    "U": "更新", "A": "新增", "D": "删除", "G": "合并",
    "C": "冲突", "E": "存在", "R": "替换",
}


# ---------------------------------------------------------------- 设置读写
def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def settings_file() -> Path:
    return app_dir() / "data" / "client_settings.json"


def load_settings() -> dict:
    try:
        return json.loads(settings_file().read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(settings: dict) -> None:
    p = settings_file()
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def search_settings_file() -> Path:
    return app_dir() / "data" / "config_search.json"


def load_search_settings() -> dict:
    try:
        data = json.loads(search_settings_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_search_settings(data: dict) -> None:
    p = search_settings_file()
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_ignore_dirs() -> list:
    dirs = load_search_settings().get("ignore_dirs", DEFAULT_IGNORE_DIRS)
    if not isinstance(dirs, list):
        return list(DEFAULT_IGNORE_DIRS)
    result = []
    for d in dirs:
        name = str(d).strip().strip("/").replace("\\", "/")
        if name and name not in result:
            result.append(name)
    return result


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="gb18030")
        except Exception:
            return path.read_bytes().decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _indent(line: str) -> int:
    n = 0
    for ch in line:
        if ch == "\t":
            n += 4
        elif ch == " ":
            n += 1
        else:
            break
    return n


def _scope_match(rel: str, name: str, scope: str) -> bool:
    """范围匹配：scope 为空=全部；可填文件夹（含子目录）、表名（Item 或 Item.lua）。"""
    s = scope.strip().replace("\\", "/").strip("/").lower()
    if not s:
        return True
    rel_l = rel.lower()
    if s.endswith(".lua"):
        return rel_l == s or rel_l.endswith("/" + s) or name.lower() == s
    rel_dir = os.path.dirname(rel_l)
    return (rel_dir == s or rel_dir.startswith(s + "/")
            or Path(name).stem.lower() == s)


def list_lua_files(root: Path, ignore_locale: bool, ignore_language: bool = False,
                   scope: str = "", ignore_dirs=()):
    ignored = {d.lower() for d in ignore_dirs}
    files = []
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs
                   if not (ignore_language and d.lower() == "language")
                   and d.lower() not in ignored]
        for name in names:
            if not name.lower().endswith(".lua"):
                continue
            if ignore_locale and LOCALE_SUFFIX.search(Path(name).stem):
                continue
            path = Path(dirpath) / name
            if scope:
                rel = os.path.relpath(str(path), str(root)).replace("\\", "/")
                if not _scope_match(rel, name, scope):
                    continue
            files.append(path)
    files.sort(key=lambda p: str(p).lower())
    return files


def load_language(root: Path):
    """加载 Language/CN 多语言表，返回 {语言id: 中文文本}。"""
    base = root / "Language" / "CN"
    if not base.is_dir():
        return {}
    table = {}
    for p in sorted(base.glob("*.lua"), key=lambda x: x.name):
        text = read_text(p)
        for m in LANG_ENTRY_RE.finditer(text):
            table[int(m.group(1))] = m.group(3)
    return table


# ---------------------------------------------------------------- 搜索逻辑
def scan_file(path: Path, root: Path, keyword: str, ignore_case: bool,
              max_per_file: int = MAX_PER_FILE, ml_hits=None):
    """扫描单个文件，返回匹配行列表。非标准 Lua 会降级为空 Sheet/ID。

    ml_hits 非空时，额外匹配「多语言字段 = 语言id」的行（如 Name/Des）。
    """
    if not keyword:
        return []
    text = read_text(path)
    if not text:
        return []

    needle = keyword.lower() if ignore_case else keyword
    # 纯中文关键词无需 lower 整份文件，省一次全表拷贝
    has_ascii_alpha = any(("a" <= c <= "z") or ("A" <= c <= "Z") for c in keyword)
    hay = text.lower() if (ignore_case and has_ascii_alpha) else text

    has_raw = needle in hay

    # 多语言字段匹配：先用候选 id 做 C 级预筛，命中后才跑字段正则
    ml_positions = []
    if ml_hits:
        ids = list(ml_hits.keys())
        if len(ids) <= 16:
            candidate = any(str(i) in text for i in ids)
        else:
            candidate = not {str(i) for i in ids}.isdisjoint(ML_VALUE_RE.findall(text))
        if candidate:
            for m in ML_FIELD_RE.finditer(text):
                resolved = ml_hits.get(int(m.group(3)))
                if resolved:
                    ml_positions.append((m.start(), m.group(2), resolved))

    if not has_raw and not ml_positions:
        return []

    raw_lines = text.splitlines(keepends=True)
    if not raw_lines:
        return []
    lines = [ln.rstrip("\r\n") for ln in raw_lines]
    ends = list(accumulate(len(ln) for ln in raw_lines))  # 累计结束偏移

    # 注释块所在行区间（0 基）
    comment_ranges = []
    if "--[[" in text:
        for m in COMMENT_RE.finditer(text):
            s = bisect_right(ends, m.start())
            e = bisect_right(ends, max(m.start(), m.end() - 1))
            comment_ranges.append((s, e))
    comment_starts = [r[0] for r in comment_ranges]

    def in_comment(li):
        j = bisect_right(comment_starts, li) - 1
        return j >= 0 and comment_ranges[j][0] <= li <= comment_ranges[j][1]

    # Sheet 标记：找到每个 Content 行上方缩进更小的 Key = { 行
    sheet_marks = []
    for m in CONTENT_RE.finditer(text):
        ci = bisect_right(ends, m.start())
        if ci >= len(lines):
            continue
        ci = ci if ci < len(lines) else len(lines) - 1
        c_indent = _indent(lines[ci])
        for j in range(ci - 1, max(-1, ci - 200), -1):
            mm = SHEET_KEY_RE.match(lines[j])
            if mm and _indent(lines[j]) < c_indent:
                sheet_marks.append((j, mm.group(1)))
                break
    sheet_marks.sort(key=lambda x: x[0])
    sheet_starts = [x[0] for x in sheet_marks]

    def sheet_of(li):
        j = bisect_right(sheet_starts, li) - 1
        return sheet_marks[j][1] if j >= 0 else ""

    def id_of(li):
        start = find_record_start(lines, li, in_comment)
        if start is None:
            return ""
        mm = ID_RE.match(lines[start])
        return mm.group(1) if mm else ""

    rel = os.path.relpath(str(path), str(root)).replace("\\", "/")
    seen = set()
    result = []
    pos = hay.find(needle) if has_raw else -1
    while pos != -1:
        li = bisect_right(ends, pos)
        if li >= len(lines):
            li = len(lines) - 1
        if li not in seen:
            seen.add(li)
            is_comment = in_comment(li)
            body = " ".join(lines[li].split())
            if len(body) > 400:
                body = body[:400] + "…"
            result.append({
                "path": str(path),
                "rel": rel,
                "sheet": sheet_of(li),
                "id": id_of(li),
                "line": li + 1,
                "text": body,
                "is_comment": is_comment,
            })
            if len(result) >= max_per_file:
                break
        pos = hay.find(needle, pos + 1)

    for m_start, field, resolved in ml_positions:
        li = bisect_right(ends, m_start)
        if li >= len(lines):
            li = len(lines) - 1
        if li in seen:
            continue
        seen.add(li)
        body = " ".join(lines[li].split())
        if len(body) > 400:
            body = body[:400] + "…"
        result.append({
            "path": str(path),
            "rel": rel,
            "sheet": sheet_of(li),
            "id": id_of(li),
            "line": li + 1,
            "text": body,
            "is_comment": False,
            "is_ml": True,
            "ml_field": field,
            "ml_text": resolved,
        })
        if len(result) >= max_per_file:
            break

    result.sort(key=lambda r: r["line"])
    return result


def find_record_start(lines, target_idx, in_comment=None, max_back=3000):
    """返回包含 target_idx 的记录起始行（[ id ] = { 所在行），找不到返回 None。

    不能简单地「向上找最近的 [ id ] = { 行」：记录内部可能有从行首开始的嵌套表
    （如 LevelItem = { 换行后 [1]={...}），会把它误判成记录 ID。这里按花括号深度
    向上找到真正包住该行的记录块；顶格的 [n]={...} 视为记录内的数组元素，向上归到
    所属记录。
    """
    own = ID_RE.match(lines[target_idx])
    if own and _indent(lines[target_idx]) > 0:
        return target_idx
    depth = 0
    for j in range(target_idx - 1, max(-1, target_idx - max_back), -1):
        if in_comment is not None and in_comment(j):
            continue
        depth += lines[j].count("}") - lines[j].count("{")
        if depth < 0:
            if ID_RE.match(lines[j]):
                return j
            depth = 0  # 不是记录块（如多行嵌套表），继续向上找外层记录
    return target_idx if own else None


def row_block_bounds(lines, target_idx):
    """返回 target_idx 所在行块的 (start, end) 0 基闭区间；找不到返回 None。"""
    start = find_record_start(lines, target_idx)
    if start is None:
        return None
    depth = 0
    for j in range(start, min(len(lines), start + 4000)):
        depth += lines[j].count("{") - lines[j].count("}")
        if depth <= 0:
            return start, j
    return start, min(len(lines) - 1, start + 4000)


class SearchWorker(QThread):
    progress = Signal(int, int)   # 已完成文件数, 总文件数
    batch = Signal(list)
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, root: Path, files, keyword, ignore_case,
                 ignore_locale, ml_hits=None, max_total=MAX_TOTAL, parent=None):
        super().__init__(parent)
        self.root = root
        self.files = files
        self.keyword = keyword
        self.ignore_case = ignore_case
        self.ignore_locale = ignore_locale
        self.ml_hits = ml_hits or {}
        self.max_total = max_total
        self._stop = Event()

    def stop(self):
        self._stop.set()

    def run(self):
        t0 = time.time()
        total = len(self.files)
        count = 0
        truncated = False
        try:
            for i, p in enumerate(self.files, 1):
                if self._stop.is_set():
                    break
                try:
                    rows = scan_file(p, self.root, self.keyword,
                                     self.ignore_case,
                                     ml_hits=self.ml_hits)
                except Exception:
                    rows = []
                if rows:
                    if count + len(rows) > self.max_total:
                        rows = rows[: self.max_total - count]
                        truncated = True
                    if rows:
                        self.batch.emit(rows)
                        count += len(rows)
                self.progress.emit(i, total)
                if truncated and count >= self.max_total:
                    break
        except Exception as exc:  # 防止线程内的意外异常吞掉界面
            self.failed.emit(str(exc))
            return
        self.done.emit({
            "count": count,
            "files": total,
            "seconds": time.time() - t0,
            "truncated": truncated,
            "stopped": self._stop.is_set(),
        })


class SvnUpdateWorker(QThread):
    """后台对配置目录执行 svn update。"""

    done = Signal(bool, int, str, str, str)  # ok, returncode, stdout, stderr, errmsg

    def __init__(self, svn_dir, parent=None):
        super().__init__(parent)
        self._svn_dir = str(svn_dir)

    def run(self):
        try:
            result = subprocess.run(
                ["svn", "update"],
                cwd=self._svn_dir,
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=600,
            )
            self.done.emit(True, result.returncode, result.stdout, result.stderr, "")
        except subprocess.TimeoutExpired:
            self.done.emit(False, -1, "", "", "svn update 超时（10 分钟）")
        except FileNotFoundError:
            self.done.emit(False, -1, "", "", "未找到 svn 命令，请确认已安装并加入 PATH")
        except Exception as exc:
            self.done.emit(False, -1, "", "", str(exc))


# ---------------------------------------------------------------- 界面
PAGE_QSS = """
QLineEdit { padding: 6px 8px; border: 1px solid #c9ced6; border-radius: 6px; background: #ffffff; }
QTableWidget { border: 1px solid #c9ced6; border-radius: 6px; gridline-color: #eceef1; }
QHeaderView::section { background: #f3f5f8; border: 0; padding: 6px; border-bottom: 1px solid #e0e3e8; }
QTextBrowser { border: 1px solid #c9ced6; border-radius: 6px; background: #ffffff; }
"""


class ElidedLabel(QLabel):
    """单行标签：内容过长时末尾省略，悬停显示完整内容。"""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setFullText(text)

    def setFullText(self, text):
        self._full = text or ""
        self.setToolTip(self._full)
        self._apply()

    def fullText(self):
        return self._full

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply()

    def _apply(self):
        width = max(0, self.width() - 2)
        self.setText(self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideRight, width))


class ConfigSearchPage(QWidget):
    """配置搜索页签（可单独使用，也可嵌入 QTabWidget）。"""

    def __init__(self, load_settings_fn=None, save_settings_fn=None, parent=None):
        super().__init__(parent)
        self._load = load_settings_fn or load_settings
        self._save = save_settings_fn or save_settings
        self._worker = None
        self._svn_worker = None
        self._svn_t0 = 0.0
        self._rows = []
        self._keyword = ""
        self._lang = {}
        self._ignore_dirs = load_ignore_dirs()
        self.setStyleSheet(PAGE_QSS)
        self._build_ui()
        self._refresh_dir()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    # ---------- 构建界面 ----------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        top_area = QWidget()
        top_box = QVBoxLayout(top_area)
        top_box.setContentsMargins(0, 0, 0, 0)
        top_box.setSpacing(8)

        title = QLabel("配置搜索")
        title.setObjectName("sectionTitle")
        top_box.addWidget(title)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(QLabel("关键词"))
        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("输入中文注释或关键词，回车搜索")
        self.keyword_edit.returnPressed.connect(self._start_search)
        bar.addWidget(self.keyword_edit, 1)

        self.search_btn = QPushButton("搜索")
        self.search_btn.clicked.connect(self._start_search)
        bar.addWidget(self.search_btn)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_search)
        bar.addWidget(self.stop_btn)

        self.locale_check = QCheckBox("忽略本地化副本")
        self.locale_check.setChecked(True)
        self.locale_check.setToolTip("忽略以 JP/KR/TW/TH/VI/Hybrid 结尾的副本")
        bar.addWidget(self.locale_check)
        self.lang_check = QCheckBox("忽略多语言表")
        self.lang_check.setChecked(True)
        self.lang_check.setToolTip("跳过 Language 目录下的多语言文本表")
        bar.addWidget(self.lang_check)
        self.ml_check = QCheckBox("多语言名称匹配")
        self.ml_check.setChecked(True)
        self.ml_check.setToolTip("Name/Des 等字段存的是多语言 id，按 Language/CN 解析后参与匹配")
        bar.addWidget(self.ml_check)
        top_box.addLayout(bar)

        scope_row = QHBoxLayout()
        scope_row.setSpacing(8)
        scope_row.addWidget(QLabel("搜索范围"))
        self.scope_edit = QLineEdit()
        self.scope_edit.setPlaceholderText(
            "留空=全部；可填文件夹（如 City）或表名（如 Item 或 Item.lua），回车搜索")
        self.scope_edit.setClearButtonEnabled(True)
        self.scope_edit.returnPressed.connect(self._start_search)
        scope_row.addWidget(self.scope_edit, 1)
        self.svn_btn = QPushButton("更新配置表")
        self.svn_btn.setToolTip("对配置目录执行 svn update")
        self.svn_btn.clicked.connect(self._update_config)
        scope_row.addWidget(self.svn_btn)
        self.choose_btn = QPushButton("更改配置表目录…")
        self.choose_btn.clicked.connect(self._choose_dir)
        scope_row.addWidget(self.choose_btn)
        top_box.addLayout(scope_row)

        info_row = QHBoxLayout()
        info_row.setSpacing(8)
        info_row.addWidget(QLabel("选中信息"))
        self.sel_info = ElidedLabel("未选中")
        self.sel_info.setStyleSheet("color:#555;")
        info_row.addWidget(self.sel_info, 1)
        top_box.addLayout(info_row)

        top_area.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        top_area.customContextMenuRequested.connect(
            lambda pos, w=top_area: self._top_menu(w, pos))
        top_area.setToolTip("右键可配置忽略的文件夹")
        root.addWidget(top_area)

        split = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 0, 0)
        left_box.setSpacing(6)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["文件", "Sheet", "ID", "匹配内容"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        hdr = self.table.horizontalHeader()
        for col, mode in enumerate([
            QHeaderView.ResizeMode.Interactive,
            QHeaderView.ResizeMode.Interactive,
            QHeaderView.ResizeMode.Interactive,
            QHeaderView.ResizeMode.Stretch,
        ]):
            hdr.setSectionResizeMode(col, mode)
        self.table.setColumnWidth(0, 180)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(2, 70)
        self.table.itemSelectionChanged.connect(self._on_select)
        self.table.currentCellChanged.connect(self._on_cell_changed)
        self.table.itemDoubleClicked.connect(lambda *_: self._open_file())
        left_box.addWidget(self.table, 1)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        open_file_btn = QPushButton("打开文件")
        open_file_btn.clicked.connect(self._open_file)
        tools.addWidget(open_file_btn)
        open_loc_btn = QPushButton("打开所在目录")
        open_loc_btn.clicked.connect(self._open_locate)
        tools.addWidget(open_loc_btn)
        copy_btn = QPushButton("复制位置")
        copy_btn.clicked.connect(self._copy_location)
        tools.addWidget(copy_btn)
        copy_id_btn = QPushButton("复制 ID")
        copy_id_btn.clicked.connect(self._copy_id)
        tools.addWidget(copy_id_btn)
        tools.addStretch(1)
        left_box.addLayout(tools)
        split.addWidget(left)

        self.preview = QTextBrowser()
        self.preview.setFont(QFont("Consolas", 9))
        split.addWidget(self.preview)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([600, 460])
        root.addWidget(split, 1)

        self.status = QLabel("就绪。右键上方空白处可配置忽略的文件夹。")
        self.status.setStyleSheet("color:#666;")
        root.addWidget(self.status)

    # ---------- 目录 ----------
    def _config_dir(self) -> str:
        try:
            return str((self._load() or {}).get("config_dir", "") or "")
        except Exception:
            return ""

    def _refresh_dir(self):
        d = self._config_dir()
        self._loaded_dir = d
        self.choose_btn.setToolTip(d or "未设置配置目录")
        self._populate_scope()

    def refresh_if_dir_changed(self) -> bool:
        """配置目录在别处被修改时刷新范围补全，返回是否刷新过。"""
        if self._config_dir() == getattr(self, "_loaded_dir", None):
            return False
        self._refresh_dir()
        return True

    # ---------- 更新配置表（svn update） ----------
    def _update_config(self):
        if self._svn_worker is not None and self._svn_worker.isRunning():
            return
        root = Path(self._config_dir())
        if not root.is_dir():
            QMessageBox.warning(self, "提示", "配置目录不存在：\n%s" % root)
            return
        self.svn_btn.setEnabled(False)
        self._svn_t0 = time.time()
        self.status.setText("正在更新配置表（svn update）…")
        self._svn_worker = SvnUpdateWorker(root, self)
        self._svn_worker.done.connect(self._on_svn_done)
        self._svn_worker.start()

    def _on_svn_done(self, ok, returncode, stdout, stderr, errmsg):
        self.svn_btn.setEnabled(True)
        elapsed = time.time() - self._svn_t0
        if not ok:
            self.status.setText("配置表更新失败")
            QMessageBox.warning(self, "更新失败", errmsg)
            return
        if returncode != 0:
            self.status.setText("配置表更新失败（退出码 %d）" % returncode)
            QMessageBox.warning(
                self, "更新失败",
                "退出码 %d\n\n%s\n%s" % (
                    returncode,
                    (stdout or "")[-2000:] or "(空)",
                    (stderr or "")[-2000:] or "(空)"))
            return
        counts, conflicts = self._svn_update_stats(stdout)
        self._refresh_dir()
        parts = ["%s %d" % (SVN_STATUS_TEXT.get(k, k), v)
                 for k, v in sorted(counts.items())]
        detail = "，".join(parts) if parts else "已是最新版本"
        rev = re.search(r"(?:Updated to|At) revision (\d+)", stdout or "")
        if rev:
            detail += "，r%s" % rev.group(1)
        self.status.setText("配置表更新完成：%s（用时 %.1f 秒）" % (detail, elapsed))
        if conflicts:
            shown = "\n".join(conflicts[:30])
            if len(conflicts) > 30:
                shown += "\n… 等共 %d 个" % len(conflicts)
            QMessageBox.warning(
                self, "更新完成但有冲突",
                "发现 %d 个冲突，请先解决再搜索：\n\n%s" % (len(conflicts), shown))

    @staticmethod
    def _svn_update_stats(stdout):
        counts = {}
        conflicts = []
        for line in (stdout or "").splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("Tree conflict") or re.match(r"^C\s{2,}\S", s):
                conflicts.append(s)
                continue
            if len(s) > 1 and s[0] in "ADUGCER" and s[1] in " \t":
                counts[s[0]] = counts.get(s[0], 0) + 1
        return counts, conflicts

    def _populate_scope(self):
        root = Path(self._config_dir())
        items = set()
        ignored = {d.lower() for d in self._ignore_dirs}
        if root.is_dir():
            for dirpath, dirs, names in os.walk(root):
                dirs[:] = [d for d in dirs if d.lower() not in ignored]
                rel = os.path.relpath(dirpath, root).replace("\\", "/")
                if rel != ".":
                    items.add(rel)
                for name in names:
                    if name.lower().endswith(".lua"):
                        items.add(os.path.relpath(os.path.join(dirpath, name), root)
                                  .replace("\\", "/"))
        completer = QCompleter(QStringListModel(sorted(items, key=str.lower), self), self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setMaxVisibleItems(14)
        self.scope_edit.setCompleter(completer)

    # ---------- 忽略文件夹（上方空白处右键） ----------
    def _build_top_menu(self):
        menu = QMenu(self)
        act_cfg = menu.addAction("配置忽略的文件夹…")
        act_tmp = menu.addAction("忽略 tmp 文件夹")
        act_tmp.setCheckable(True)
        act_tmp.setChecked(any(d.lower() == "tmp" for d in self._ignore_dirs))
        menu.addSeparator()
        info = menu.addAction("当前忽略：%s" % (
            "、".join(self._ignore_dirs) if self._ignore_dirs else "无"))
        info.setEnabled(False)
        act_cfg.triggered.connect(self._edit_ignore_dirs)
        act_tmp.toggled.connect(self._toggle_tmp_ignore)
        return menu

    def _top_menu(self, widget, pos):
        self._build_top_menu().exec(widget.mapToGlobal(pos))

    def _toggle_tmp_ignore(self, checked):
        dirs = [d for d in self._ignore_dirs if d.lower() != "tmp"]
        if checked:
            dirs.append("tmp")
        self._set_ignore_dirs(dirs)

    def _edit_ignore_dirs(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("忽略的文件夹")
        box = QVBoxLayout(dlg)
        box.addWidget(QLabel("每行一个文件夹名（不含路径），搜索时跳过这些文件夹："))
        edit = QPlainTextEdit("\n".join(self._ignore_dirs))
        edit.setPlaceholderText("tmp")
        box.addWidget(edit, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        box.addWidget(buttons)
        dlg.resize(380, 300)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        dirs = []
        for line in edit.toPlainText().splitlines():
            name = line.strip().strip("/")
            if name and name not in dirs:
                dirs.append(name)
        self._set_ignore_dirs(dirs)

    def _set_ignore_dirs(self, dirs):
        self._ignore_dirs = dirs
        try:
            data = load_search_settings()
            data["ignore_dirs"] = dirs
            save_search_settings(data)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
        self._populate_scope()
        self.status.setText("已保存忽略文件夹：%s（重新搜索生效）" % (
            "、".join(dirs) if dirs else "无"))

    def _choose_dir(self):
        start = self._config_dir() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "选择配置目录", start)
        if not path:
            return
        settings = self._load() or {}
        settings["config_dir"] = path
        try:
            self._save(settings)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return
        self._rows.clear()
        self.table.setRowCount(0)
        self.preview.clear()
        self.sel_info.setFullText("未选中")
        self._refresh_dir()

    # ---------- 搜索流程 ----------
    def _start_search(self):
        if self._worker is not None and self._worker.isRunning():
            return
        keyword = self.keyword_edit.text().strip()
        if not keyword:
            QMessageBox.information(self, "提示", "请输入关键词")
            return
        root = Path(self._config_dir())
        if not root.is_dir():
            QMessageBox.warning(self, "提示", "配置目录不存在：\n%s\n请先点「更改配置目录…」设置。" % root)
            return

        scope = self.scope_edit.text().strip()
        files = list_lua_files(root, self.locale_check.isChecked(),
                               self.lang_check.isChecked(), scope,
                               self._ignore_dirs)
        if not files:
            tip = "该范围（%s）下没有找到 lua 文件" % scope if scope else "该目录下没有找到 lua 文件"
            QMessageBox.information(self, "提示", tip)
            return

        self._keyword = keyword
        self._lang = load_language(root)
        ml_hits = {}
        if self.ml_check.isChecked() and self._lang:
            kl = keyword.lower()
            ml_hits = {i: t for i, t in self._lang.items() if kl in t.lower()}
        self._rows.clear()
        self.table.setRowCount(0)
        self.preview.clear()
        self.sel_info.setFullText("未选中")
        self.search_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText("扫描中… 0/%d" % len(files))

        self._worker = SearchWorker(
            root=root,
            files=files,
            keyword=keyword,
            ignore_case=True,
            ignore_locale=self.locale_check.isChecked(),
            ml_hits=ml_hits,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.batch.connect(self._on_batch)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _stop_search(self):
        if self._worker is not None:
            self._worker.stop()
        self.stop_btn.setEnabled(False)
        self.status.setText("正在停止…")

    def _on_progress(self, done, total):
        self.status.setText("扫描中… %d/%d，已命中 %d 行" % (done, total, len(self._rows)))

    def _on_batch(self, rows):
        start = self.table.rowCount()
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(start + len(rows))
            for k, r in enumerate(rows):
                i = start + k
                values = [r["rel"], r["sheet"], r["id"], self._row_body(r)]
                for c, val in enumerate(values):
                    self.table.setItem(i, c, QTableWidgetItem(val))
                self._rows.append(r)
        finally:
            self.table.setUpdatesEnabled(True)

    @staticmethod
    def _row_body(r):
        if r.get("is_ml"):
            return "【名】%s  →  %s" % (r["text"], r.get("ml_text", ""))
        if r["is_comment"]:
            return "【注】" + r["text"]
        return r["text"]

    def _on_done(self, summary):
        self.search_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        tag = "已停止" if summary.get("stopped") else "完成"
        extra = "（结果过多已截断）" if summary.get("truncated") else ""
        self.status.setText("%s：命中 %d 行，扫描 %d 个文件，用时 %.2f 秒%s" % (
            tag, summary.get("count", 0), summary.get("files", 0),
            summary.get("seconds", 0.0), extra))
        if summary.get("count", 0) == 0:
            self.preview.setHtml("<p style='color:#888'>没有匹配结果。</p>")

    def _on_failed(self, message):
        self.search_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status.setText("搜索失败")
        QMessageBox.warning(self, "搜索失败", message)

    # ---------- 结果交互 ----------
    def _current_row(self):
        idx = self.table.currentRow()
        if 0 <= idx < len(self._rows):
            return self._rows[idx]
        return None

    def _on_select(self):
        row = self._current_row()
        if not row:
            self.sel_info.setFullText("未选中")
            return
        self._show_preview(row)

    def _on_cell_changed(self, row, col, _prev_row, _prev_col):
        item = self.table.item(row, col) if row >= 0 else None
        self.sel_info.setFullText(item.text() if item else "未选中")

    def _show_preview(self, row):
        if not row:
            return
        text = read_text(Path(row["path"]))
        if not text:
            self.preview.setHtml("<p style='color:#888'>无法读取文件。</p>")
            return
        lines = text.splitlines()
        target = row["line"] - 1
        bounds = row_block_bounds(lines, target)
        if bounds and bounds[0] <= target <= bounds[1]:
            lo, hi = bounds[0], bounds[1] + 1
            if hi - lo > 400:
                lo = max(lo, target - 200)
                hi = min(hi, target + 200)
        else:
            lo = max(0, target - 8)
            hi = min(len(lines), target + 9)
        parts = ['<pre style="font-family:Consolas,monospace; margin:0;">']
        for i in range(lo, hi):
            raw = lines[i]
            for _ in range(2):
                if raw.startswith("\t"):
                    raw = raw[1:]
                else:
                    break
            body = self._highlight(raw)
            note = self._ml_note(raw)
            num = "%5d" % (i + 1)
            if i == target:
                parts.append('<span style="background:#fff3a3">%s  %s</span>%s' % (num, body, note))
            else:
                parts.append('<span style="color:#999">%s</span>  %s%s' % (num, body, note))
            parts.append("\n")
        parts.append("</pre>")
        self.preview.setHtml("".join(parts))

    def _highlight(self, raw: str) -> str:
        kw = self._keyword
        if not kw:
            return html.escape(raw)
        low = raw.lower()
        kl = kw.lower()
        out = []
        i = 0
        while True:
            j = low.find(kl, i)
            if j < 0:
                out.append(html.escape(raw[i:]))
                break
            out.append(html.escape(raw[i:j]))
            out.append('<span style="background:#ffe08a;color:#000">%s</span>'
                       % html.escape(raw[j:j + len(kw)]))
            i = j + len(kw)
        return "".join(out)

    def _ml_note(self, raw: str) -> str:
        if not self._lang:
            return ""
        m = ML_FIELD_RE.match(raw)
        if not m:
            return ""
        resolved = self._lang.get(int(m.group(3)))
        if not resolved:
            return ""
        return '  <span style="color:#1a7f37">→ %s</span>' % html.escape(resolved)

    def _table_menu(self, pos):
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        self.table.selectRow(index.row())
        row = self._current_row()
        if not row:
            return
        menu = QMenu(self)
        act_id = menu.addAction("复制 ID")
        act_text = menu.addAction("复制匹配内容")
        act_file = menu.addAction("复制文件名")
        act_loc = menu.addAction("复制位置（文件:行）")
        menu.addSeparator()
        act_open = menu.addAction("打开文件")
        act_dir = menu.addAction("打开所在目录")
        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen == act_id:
            self._copy_id()
        elif chosen == act_text:
            QApplication.clipboard().setText(row["text"])
            self.status.setText("已复制匹配内容")
        elif chosen == act_file:
            self._copy_file()
        elif chosen == act_loc:
            self._copy_location()
        elif chosen == act_open:
            self._open_file()
        elif chosen == act_dir:
            self._open_locate()

    def _open_file(self):
        row = self._current_row()
        if row:
            QDesktopServices.openUrl(QUrl.fromLocalFile(row["path"]))

    def _open_locate(self):
        row = self._current_row()
        target = Path(row["path"]).parent if row else Path(self._config_dir())
        if target.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _copy_file(self):
        row = self._current_row()
        if not row:
            self.status.setText("请先选中一行")
            return
        QApplication.clipboard().setText(row["rel"])
        self.status.setText("已复制文件名：%s" % row["rel"])

    def _copy_location(self):
        row = self._current_row()
        if row:
            QApplication.clipboard().setText("%s:%d" % (row["path"], row["line"]))
            self.status.setText("已复制：%s:%d" % (row["rel"], row["line"]))

    def _copy_id(self):
        row = self._current_row()
        if not row:
            self.status.setText("请先选中一行")
            return
        if row["id"]:
            QApplication.clipboard().setText(row["id"])
            self.status.setText("已复制 ID：%s" % row["id"])
        else:
            self.status.setText("该行没有 ID")

    # ---------- 生命周期 ----------
    def shutdown(self):
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(2000)
        if self._svn_worker is not None and self._svn_worker.isRunning():
            self._svn_worker.wait(3000)

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)


APP_QSS = """
QWidget { font-size: 13px; font-family: "Microsoft YaHei", "Segoe UI", sans-serif; }
QLabel#sectionTitle { font-weight: bold; color: #1f2329; padding: 2px 0; }
QPushButton { padding: 6px 12px; border: 1px solid #c9ced6; border-radius: 6px; background: #f7f8fa; }
QPushButton:hover { background: #eef2f7; }
QPushButton:disabled { color: #a0a4ab; background: #f0f1f3; }
"""


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("配置搜索")
    app.setStyleSheet(APP_QSS)
    window = QMainWindow()
    window.setWindowTitle("配置搜索 - autocode")
    window.setCentralWidget(ConfigSearchPage())
    window.resize(1180, 760)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
