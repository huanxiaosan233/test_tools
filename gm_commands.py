# -*- coding: utf-8 -*-
"""GM 指令页签：搜索导出的 wizDump.txt，并维护可一键复制的自定义指令。

单独运行：
    python gm_commands.py

并入现有工具（main.py 中两行）：
    from gm_commands import GmCommandPage
    self.tabs.addTab(GmCommandPage(), "GM 指令")

本模块只读写 data/gm_commands.json，不修改工具原有的数据文件。
"""

import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

DUMP_NAME = "wizDump.txt"
CUSTOM_FILE_NAME = "gm_commands.json"
DUMP_LINE_RE = re.compile(r"^\s*cmd:\s*(\S+)\s+desc:\s*(.*?)\s*$")
CUSTOM_COLUMNS = 3
CMD_COLUMN_SCALE = 1.2

# 与「配置搜索」页签保持一致的输入框 / 表头样式（表头为静态样式，无点击感）
PAGE_QSS = """
QLineEdit { padding: 6px 8px; border: 1px solid #c9ced6; border-radius: 6px; background: #ffffff; }
QHeaderView::section { background: #f3f5f8; border: 0; padding: 6px; border-bottom: 1px solid #e0e3e8; }
"""


# ---------------------------------------------------------------- 文件读写
def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def custom_file() -> Path:
    d = app_dir() / "data"
    d.mkdir(exist_ok=True)
    return d / CUSTOM_FILE_NAME


def dump_file() -> Path:
    base = app_dir()
    for path in (base / "data" / DUMP_NAME, base / DUMP_NAME):
        if path.exists():
            return path
    return base / "data" / DUMP_NAME


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


def parse_dump(text: str):
    """把 `cmd:xxx  desc:yyy` 每行解析成 (指令, 说明)，忽略格式不符的行。"""
    items = []
    for line in text.splitlines():
        match = DUMP_LINE_RE.match(line)
        if match:
            items.append((match.group(1), match.group(2)))
    return items


def load_custom_commands():
    path = custom_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    result = []
    for item in data.get("commands") or []:
        cmd = str(item.get("cmd", "")).strip()
        if not cmd:
            continue
        name = str(item.get("name", "")).strip() or cmd
        entry = {"name": name, "cmd": cmd}
        desc = str(item.get("desc", "")).strip()
        if desc:
            entry["desc"] = desc
        result.append(entry)
    return result


def save_custom_commands(items) -> None:
    custom_file().write_text(
        json.dumps({"commands": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------- 添加 / 编辑弹窗
class GmCommandDialog(QDialog):
    """添加或编辑一条自定义 GM 指令。"""

    def __init__(self, initial=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑指令" if initial else "添加指令")
        self.setMinimumWidth(480)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("按钮上显示的名称，例如 添加铜钱")
        self.cmd_edit = QLineEdit()
        self.cmd_edit.setPlaceholderText("点击按钮后复制的内容，例如 add_coin 1000")
        self.desc_edit = QLineEdit()
        self.desc_edit.setPlaceholderText("可选，点击按钮时打印到输出记录")

        if initial:
            self.name_edit.setText(initial.get("name", ""))
            self.cmd_edit.setText(initial.get("cmd", ""))
            self.desc_edit.setText(initial.get("desc", ""))

        form = QFormLayout(self)
        form.addRow("指令名称：", self.name_edit)
        form.addRow("指令内容：", self.cmd_edit)
        form.addRow("指令描述：", self.desc_edit)
        hint = QLabel("点击按钮会复制「指令内容」，并输出到下方的记录区。")
        hint.setStyleSheet("color:#888;")
        form.addRow("", hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        form.addRow(buttons)

        def accept():
            if not self.name_edit.text().strip():
                QMessageBox.information(self, "提示", "请输入指令名称")
                return
            if not self.cmd_edit.text().strip():
                QMessageBox.information(self, "提示", "请输入指令内容")
                return
            self.accept()

        buttons.accepted.connect(accept)
        buttons.rejected.connect(self.reject)

    def values(self):
        return (
            self.name_edit.text().strip(),
            self.cmd_edit.text().strip(),
            self.desc_edit.text().strip(),
        )


# ---------------------------------------------------------------- 页签
class GmCommandPage(QWidget):
    """GM 指令页签：左侧自定义指令按钮 + 输出记录，右侧 wizDump 指令搜索。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet(PAGE_QSS)
        self._custom = load_custom_commands()
        self._dump = []
        self._dump_ok = False
        self._build_ui()
        self._reload_dump()
        self._rebuild_custom()

    # ---------- 界面 ----------
    @staticmethod
    def _title(text) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sectionTitle")
        return label

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(12)

        # ---- 左列：自定义指令 + 输出记录 ----
        left = QVBoxLayout()
        left.setSpacing(8)

        head = QHBoxLayout()
        head.addWidget(self._title("自定义指令"))
        head.addStretch(1)
        add_btn = QPushButton("添加指令")
        add_btn.clicked.connect(self._on_add)
        head.addWidget(add_btn)
        left.addLayout(head)

        self.cmd_area = QScrollArea()
        self.cmd_area.setWidgetResizable(True)
        host = QWidget()
        self.cmd_grid = QGridLayout(host)
        self.cmd_grid.setContentsMargins(2, 2, 2, 2)
        self.cmd_grid.setSpacing(6)
        self.cmd_grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.cmd_area.setWidget(host)
        left.addWidget(self.cmd_area, 2)

        out_head = QHBoxLayout()
        out_head.addWidget(self._title("输出记录"))
        out_head.addStretch(1)
        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(lambda: self.output.clear())
        out_head.addWidget(clear_btn)
        left.addLayout(out_head)

        self.output = QTextBrowser()
        self.output.setMinimumHeight(150)
        left.addWidget(self.output, 1)

        left_widget = QWidget()
        left_widget.setLayout(left)
        left_widget.setMinimumWidth(456)
        left_widget.setMaximumWidth(600)
        root.addWidget(left_widget)

        # ---- 右列：指令搜索 ----
        right = QVBoxLayout()
        right.setSpacing(8)

        search_head = QHBoxLayout()
        search_head.addWidget(self._title("指令搜索"))
        search_head.addStretch(1)
        reload_btn = QPushButton("重新加载")
        reload_btn.setToolTip("重新读取 wizDump.txt")
        reload_btn.clicked.connect(lambda: self._reload_dump(manual=True))
        search_head.addWidget(reload_btn)
        right.addLayout(search_head)

        self.search = QLineEdit()
        self.search.setPlaceholderText("输入指令名或说明关键词，回车复制第一条")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._apply_filter)
        self.search.returnPressed.connect(self._copy_first)
        right.addWidget(self.search)

        info = QHBoxLayout()
        self.count_label = QLabel()
        self.count_label.setStyleSheet("color:#888;")
        info.addWidget(self.count_label)
        info.addStretch(1)
        self.copy_label = QLabel()
        self.copy_label.setStyleSheet("color:#1a7f37;")
        info.addWidget(self.copy_label)
        right.addLayout(info)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["指令", "说明", "复制"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionsClickable(False)
        header.setHighlightSections(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(2, 78)
        self.table.cellDoubleClicked.connect(self._on_row_double_clicked)
        right.addWidget(self.table, 1)

        root.addLayout(right, 1)

        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.timeout.connect(self.copy_label.clear)

    # ---------- 搜索 ----------
    def _reload_dump(self, manual=False):
        path = dump_file()
        self._dump_ok = path.exists()
        if self._dump_ok:
            text = read_text(path)
            self._dump = sorted(parse_dump(text), key=lambda it: it[0].lower())
        else:
            self._dump = []
            self.count_label.setText("未找到 wizDump.txt，请放到 data 目录后点「重新加载」")
            self.count_label.setToolTip(str(path))
            self._apply_filter()
            if manual:
                QMessageBox.warning(self, "提示", "未找到指令文件：\n%s" % path)
            return
        self.count_label.setToolTip(str(path))
        self._apply_filter()

    def _apply_filter(self):
        keyword = self.search.text().strip().lower()
        rows = [
            (cmd, desc)
            for cmd, desc in self._dump
            if not keyword or keyword in cmd.lower() or keyword in desc.lower()
        ]

        self.table.setUpdatesEnabled(False)
        self.table.clearContents()
        self.table.setRowCount(len(rows))
        for row, (cmd, desc) in enumerate(rows):
            cmd_item = QTableWidgetItem(cmd)
            cmd_item.setFont(QFont("Consolas", 10))
            cmd_item.setToolTip(cmd)
            self.table.setItem(row, 0, cmd_item)

            desc_item = QTableWidgetItem(desc)
            desc_item.setToolTip(desc)
            self.table.setItem(row, 1, desc_item)

            copy_btn = QPushButton("复制")
            copy_btn.setFixedHeight(24)
            copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            copy_btn.setStyleSheet("QPushButton { padding: 2px 10px; }")
            copy_btn.clicked.connect(lambda _=False, c=cmd: self._copy(c))
            self.table.setCellWidget(row, 2, copy_btn)
        self.table.setUpdatesEnabled(True)
        self.table.resizeColumnToContents(0)
        self.table.setColumnWidth(
            0, max(1, int(self.table.columnWidth(0) * CMD_COLUMN_SCALE))
        )

        if self._dump_ok:
            if keyword:
                self.count_label.setText(
                    "匹配 %d 条 / 共 %d 条" % (len(rows), len(self._dump))
                )
            else:
                self.count_label.setText("共 %d 条指令" % len(self._dump))

    def _copy(self, text):
        QApplication.clipboard().setText(text)
        self.copy_label.setText("已复制：%s" % text)
        self._copy_timer.start(2200)

    def _copy_first(self):
        if self.table.rowCount() > 0:
            item = self.table.item(0, 0)
            if item is not None:
                self._copy(item.text())

    def _on_row_double_clicked(self, row, _column):
        item = self.table.item(row, 0)
        if item is not None:
            self._copy(item.text())

    # ---------- 自定义指令 ----------
    def _rebuild_custom(self):
        while self.cmd_grid.count():
            item = self.cmd_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        for col in range(CUSTOM_COLUMNS):
            self.cmd_grid.setColumnStretch(col, 1)

        if not self._custom:
            hint = QLabel("暂无自定义指令，点击右上角「添加指令」创建")
            hint.setStyleSheet("color:#999;")
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.cmd_grid.addWidget(hint, 0, 0, 1, CUSTOM_COLUMNS)
            return

        for idx, item in enumerate(self._custom):
            btn = QPushButton(item["name"])
            btn.setMinimumHeight(38)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip("%s\n左键：复制并输出　右键：编辑 / 删除" % item["cmd"])
            btn.clicked.connect(lambda _=False, it=item: self._run_custom(it))
            btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda pos, b=btn, i=idx: self._show_custom_menu(b, pos, i)
            )
            self.cmd_grid.addWidget(btn, idx // CUSTOM_COLUMNS, idx % CUSTOM_COLUMNS)

    def _run_custom(self, item):
        cmd = item["cmd"]
        desc = item.get("desc", "")
        QApplication.clipboard().setText(cmd)
        stamp = datetime.now().strftime("%H:%M:%S")
        line = '<span style="color:#8a8f99">[%s]</span> %s 已复制 → <b>%s</b>' % (
            stamp, html.escape(item["name"]), html.escape(cmd)
        )
        if desc:
            line += '<br><span style="color:#666">描述：%s</span>' % html.escape(desc)
        self.output.append(line)
        console = "[%s] %s 已复制: %s" % (stamp, item["name"], cmd)
        if desc:
            console += " | 描述: %s" % desc
        print(console, flush=True)

    def _show_custom_menu(self, btn, pos, idx):
        if not (0 <= idx < len(self._custom)):
            return
        menu = QMenu(self)
        edit_act = menu.addAction("编辑")
        delete_act = menu.addAction("删除")
        action = menu.exec(btn.mapToGlobal(pos))
        if action is edit_act:
            self._edit_custom(idx)
        elif action is delete_act:
            self._delete_custom(idx)

    def _on_add(self):
        dlg = GmCommandDialog(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, cmd, desc = dlg.values()
        entry = {"name": name, "cmd": cmd}
        if desc:
            entry["desc"] = desc
        self._custom.append(entry)
        self._persist()
        self._rebuild_custom()

    def _edit_custom(self, idx):
        dlg = GmCommandDialog(initial=self._custom[idx], parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, cmd, desc = dlg.values()
        entry = {"name": name, "cmd": cmd}
        if desc:
            entry["desc"] = desc
        self._custom[idx] = entry
        self._persist()
        self._rebuild_custom()

    def _delete_custom(self, idx):
        name = self._custom[idx]["name"]
        choice = QMessageBox.question(self, "删除指令", "确定删除「%s」？" % name)
        if choice != QMessageBox.StandardButton.Yes:
            return
        del self._custom[idx]
        self._persist()
        self._rebuild_custom()

    def _persist(self):
        try:
            save_custom_commands(self._custom)
        except Exception as exc:
            QMessageBox.warning(self, "保存失败", str(exc))


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(
        'QWidget { font-size: 13px; font-family: "Microsoft YaHei", "Segoe UI", sans-serif; }'
        "QLabel#sectionTitle { font-weight: bold; color: #1f2329; padding: 2px 0; }"
        "QPushButton { padding: 6px 12px; border: 1px solid #c9ced6; border-radius: 6px;"
        " background: #f7f8fa; }"
        "QPushButton:hover { background: #eef2f7; }"
    )
    page = GmCommandPage()
    page.setWindowTitle("GM 指令 - 测试工具")
    page.resize(1120, 740)
    page.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
