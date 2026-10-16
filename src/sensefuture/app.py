from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import pyqtgraph as pg
from PyQt6 import QtCore, QtGui
from PyQt6 import QtWidgets as W
from serial.tools.list_ports import comports

from .catalog import BY_COMMAND, PARAMETERS, decode_errors
from .worker import DeviceWorker

COLORS = ("#57b7ff", "#e7b65b")
LINK_COMMANDS = {"RESET", "ADDRESS", "BOUNDTABLEONE", "BOUNDTABLETWO"}
CSV_FIELDS = [
    "timestamp_utc",
    "monotonic_s",
    "channel",
    "temperature_C",
    "target_C",
    "error_C",
    "output_percent",
    "voltage_V",
    "resistance_ohm",
    "enabled",
    "error_code",
]


def button(text, callback):
    b = W.QPushButton(text)
    b.clicked.connect(callback)
    return b


def number(lo, hi, val, decimals=2, suffix=""):
    w = W.QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setDecimals(decimals)
    w.setValue(val)
    w.setSuffix(suffix)
    w.setKeyboardTracking(False)
    return w


def valid_temperature(raw):
    return -40000000 <= raw <= 100000000


class Window(W.QMainWindow):
    def __init__(self):
        super().__init__()
        self.worker = None
        self.latest = {}
        self.loaded_editors = set()
        self.history = deque(maxlen=30000)
        self.rows = deque(maxlen=60000)
        self.last_sample = 0
        self.record_file = self.record_writer = None
        self.closing_after_stop = False
        self.stage_index = None
        self.stage_since = None
        self.stage_waiting = None
        self.stage_deadline = None
        self.active_plan = []
        self.settings = QtCore.QSettings("SenseFuture", "Workbench")
        self.setWindowTitle("SenseFuture · 温控工作台")
        self.resize(1440, 950)
        self.setMinimumSize(1100, 760)
        self.default_font_size = max(
            14,
            QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.GeneralFont).pointSize(),
        )
        self.build()
        self.set_ui_font_size(
            int(self.settings.value("font_size", self.default_font_size)), persist=False
        )
        self.refresh_ports()
        self.restoreGeometry(self.settings.value("geometry", QtCore.QByteArray()))
        self.watchdog = QtCore.QTimer(self)
        self.watchdog.timeout.connect(self.check_stale)
        self.watchdog.start(500)

    def build(self):
        root = W.QWidget()
        layout = W.QVBoxLayout(root)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        title = W.QLabel("SenseFuture  /  温控工作台")
        title.setObjectName("title")
        heading = W.QHBoxLayout()
        heading.addWidget(title)
        heading.addStretch()
        heading.addWidget(W.QLabel("字号"))
        self.font_size = W.QSpinBox()
        self.font_size.setRange(10, 22)
        self.font_size.setSuffix(" pt")
        self.font_size.setToolTip("界面与图表字号；Ctrl + 加号 / 减号调整，Ctrl+0 恢复默认")
        self.font_size.valueChanged.connect(self.set_ui_font_size)
        heading.addWidget(button("A−", lambda: self.font_size.stepDown()))
        heading.addWidget(self.font_size)
        heading.addWidget(button("A+", lambda: self.font_size.stepUp()))
        layout.addLayout(heading)
        for sequences, delta in ((("Ctrl++", "Ctrl+="), 1), (("Ctrl+-",), -1), (("Ctrl+0",), 0)):
            action = QtGui.QAction(self)
            action.setShortcuts([QtGui.QKeySequence(key) for key in sequences])
            action.triggered.connect(
                lambda _, delta=delta: self.font_size.setValue(
                    self.font_size.value() + delta if delta else self.default_font_size
                )
            )
            self.addAction(action)
        row = W.QHBoxLayout()
        self.port = W.QComboBox()
        self.port.setEditable(True)
        self.port.setMinimumWidth(195)
        row.addWidget(W.QLabel("串口"))
        row.addWidget(self.port)
        self.scan = button("刷新", self.refresh_ports)
        row.addWidget(self.scan)
        self.baud = W.QComboBox()
        self.baud.addItems(
            [str(v) for v in (4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800)]
        )
        self.baud.setCurrentText(str(self.settings.value("baud", "38400")))
        row.addWidget(self.baud)
        self.protocol = W.QComboBox()
        self.protocol.addItems(["ASCII", "Modbus RTU"])
        row.addWidget(self.protocol)
        row.addStretch()
        connection_box = W.QWidget()
        connections = W.QVBoxLayout(connection_box)
        connections.setContentsMargins(0, 0, 0, 0)
        connections.addLayout(row)
        row = W.QHBoxLayout()
        row.addWidget(W.QLabel("站号"))
        self.station = W.QSpinBox()
        self.station.setRange(1, 247)
        row.addWidget(self.station)
        self.channels = W.QComboBox()
        self.channels.addItems(["1 通道", "2 通道"])
        self.channels.setCurrentIndex(1)
        row.addWidget(self.channels)
        self.connect_btn = button("连接设备", self.toggle_connection)
        row.addWidget(self.connect_btn)
        row.addStretch()
        self.stop_btn = button("关闭全部输出", self.stop_outputs)
        self.stop_btn.setObjectName("stop")
        self.stop_btn.setEnabled(False)
        row.addWidget(self.stop_btn)
        connections.addLayout(row)
        layout.addWidget(connection_box)
        self.state = W.QLabel("未连接 · 请选择串口，或选择模拟设备预览")
        self.state.setObjectName("muted")
        layout.addWidget(self.state)
        self.tabs = W.QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.build_monitor()
        self.build_parameters()
        self.build_plan()
        self.build_diagnostics()
        self.setCentralWidget(root)
        self.statusBar().showMessage(
            "通信分辨率 0.00001 °C · 0.01 °C 稳定性需实测 · 连接不会主动开启输出"
        )
        self.base_stylesheet = """
            QMainWindow, QWidget { background: #20252d; color: #e4e9f1; font-size: BASEpt; }
            QLabel#title { font-size: TITLEpt; font-weight: 600; padding-bottom: 5px; }
            QLabel#muted { color: #a2afc2; }
            QLabel#readout { font-size: READOUTpt; font-weight: 600; }
            QGroupBox { border: 1px solid #404b5b; border-radius: 7px; margin-top: 13px; padding: 15px 10px 9px; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; color: #aabbd1; }
            QPushButton { background: #344153; border: 1px solid #4a5c72; border-radius: 5px; padding: 7px 12px; }
            QPushButton:hover { background: #435973; }
            QPushButton:disabled { color: #6b7583; background: #2a303a; border-color: #343d48; }
            QPushButton#stop { background: #883d42; border-color: #c96668; }
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: #171c23; border: 1px solid #445166; border-radius: 4px; padding: 5px; selection-background-color: #35689b; }
            QTabWidget::pane { border: 1px solid #3c4654; }
            QTabBar::tab { background: #2a323e; padding: 9px 20px; }
            QTabBar::tab:selected { background: #3a4d64; }
            QTableWidget, QPlainTextEdit { background: #191e26; gridline-color: #35404e; }
            QHeaderView::section { background: #303b4a; padding: 7px; border: none; }
            QScrollBar:vertical { width: 16px; background: #232b35; }
            QPlainTextEdit { font-family: monospace; }
        """

    def set_ui_font_size(self, size, persist=True):
        size = max(10, min(22, int(size)))
        blocker = QtCore.QSignalBlocker(self.font_size)
        self.font_size.setValue(size)
        del blocker
        self.setStyleSheet(
            self.base_stylesheet.replace("BASE", str(size))
            .replace("TITLE", str(size + 8))
            .replace("READOUT", str(size + 10))
        )
        font = QtGui.QFont(self.font())
        font.setPointSize(size)
        self.setFont(font)
        self.log.setFont(QtGui.QFont("monospace", size))
        metrics = QtGui.QFontMetrics(font)
        self.sidebar_scroll.setMinimumWidth(round(350 * size / 12))
        self.sidebar_scroll.setMaximumWidth(round(430 * size / 12))
        self.table.verticalHeader().setDefaultSectionSize(max(44, metrics.height() + 24))
        for row in range(self.table.rowCount()):
            self.table.setRowHeight(row, max(44, metrics.height() + 24))
        for col, width in enumerate((285, 155, 125, 55, 160, 175)):
            self.table.setColumnWidth(col, round(width * size / 10))
        self.plan_table.verticalHeader().setDefaultSectionSize(metrics.height() + 18)
        self.output_plot.setMinimumHeight(round(165 * size / 12))
        for plot in (self.plot, self.output_plot):
            for name in ("left", "bottom"):
                axis = plot.getAxis(name)
                axis.setTickFont(font)
                axis.setLabel(
                    axis.labelText,
                    units=axis.labelUnits,
                    **{"font-size": f"{size}pt", "color": "#aebdd0"},
                )
            if plot.plotItem.legend is not None:
                plot.plotItem.legend.setLabelTextSize(f"{size}pt")
                for _, label in plot.plotItem.legend.items:
                    label.setText(label.text, size=f"{size}pt")
        self.statusBar().setFont(font)
        if persist:
            self.settings.setValue("font_size", size)

    def build_monitor(self):
        page = W.QWidget()
        outer = W.QHBoxLayout(page)
        left = W.QWidget()
        self.sidebar_scroll = W.QScrollArea()
        self.sidebar_scroll.setWidgetResizable(True)
        self.sidebar_scroll.setFrameShape(W.QFrame.Shape.NoFrame)
        self.sidebar_scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.sidebar_scroll.setWidget(left)
        side = W.QVBoxLayout(left)
        self.channel = W.QComboBox()
        self.channel.addItems(["通道 1", "通道 2"])
        self.channel.currentIndexChanged.connect(self.channel_changed)
        side.addWidget(self.channel)
        self.readout = W.QLabel("— °C")
        self.readout.setObjectName("readout")
        side.addWidget(self.readout)
        self.details = W.QLabel("等待有效测量")
        self.details.setWordWrap(True)
        side.addWidget(self.details)
        box = W.QGroupBox("控制")
        form = W.QFormLayout(box)
        self.target = number(-400, 1000, 40, 5, " °C")
        self.target.setSingleStep(0.01)
        form.addRow("目标温度", self.target)
        self.target_btn = button(
            "写入目标温度", lambda: self.write_value("TG", str(self.target.value()))
        )
        form.addRow(self.target_btn)
        self.mode = W.QComboBox()
        self.mode.addItems(["制冷 + 加热", "仅制冷", "仅加热", "手动电压输出"])
        form.addRow("输出模式", self.mode)
        self.mode_btn = button(
            "写入模式", lambda: self.write_value("MODE", str(self.mode.currentIndex()))
        )
        form.addRow(self.mode_btn)
        self.limit = number(0, 90, 30, 0, " %")
        form.addRow("最大输出", self.limit)
        self.limit_btn = button(
            "写入输出上限", lambda: self.write_value("LIMITED", str(self.limit.value()))
        )
        form.addRow(self.limit_btn)
        self.enable_btn = button("开启当前通道输出", lambda: self.write_value("ENABLE", "1"))
        self.disable_btn = button("关闭当前通道输出", lambda: self.write_value("ENABLE", "0"))
        form.addRow(self.enable_btn)
        form.addRow(self.disable_btn)
        side.addWidget(box)
        self.stats = W.QLabel("最近 60 s\n等待数据")
        self.stats.setWordWrap(True)
        stats_box = W.QGroupBox("稳定性观察 · ±0.01 °C")
        sl = W.QVBoxLayout(stats_box)
        sl.addWidget(self.stats)
        side.addWidget(stats_box)
        side.addStretch()
        outer.addWidget(self.sidebar_scroll)
        right_widget = W.QWidget()
        right_scroll = W.QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(W.QFrame.Shape.NoFrame)
        right_scroll.setWidget(right_widget)
        right = W.QVBoxLayout(right_widget)
        right.setSizeConstraint(W.QLayout.SizeConstraint.SetMinimumSize)
        right.setContentsMargins(0, 0, 0, 0)
        cards = W.QHBoxLayout()
        self.cards = []
        for ch in (1, 2):
            box = W.QGroupBox(f"通道 {ch}")
            bl = W.QVBoxLayout(box)
            label = W.QLabel("— °C")
            label.setObjectName("readout")
            label.setStyleSheet(f"color: {COLORS[ch - 1]}")
            bl.addWidget(label)
            self.cards.append(label)
            cards.addWidget(box)
        right.addLayout(cards)
        pg.setConfigOptions(antialias=True, background="#171c23", foreground="#aebdd0")
        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.plot.setLabel("left", "温度", units="°C")
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.addLegend(offset=(10, 10), colCount=2)
        self.plot.setMinimumHeight(240)
        self.temp_curves = [
            self.plot.plot(pen=pg.mkPen(c, width=2), name=f"通道 {i + 1}")
            for i, c in enumerate(COLORS)
        ]
        self.target_curves = [
            self.plot.plot(
                pen=pg.mkPen(c, width=1, style=QtCore.Qt.PenStyle.DashLine), name=f"目标 {i + 1}"
            )
            for i, c in enumerate(COLORS)
        ]
        right.addWidget(self.plot, 3)
        self.output_plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.output_plot.setLabel("left", "输出", units="%")
        self.output_plot.getAxis("left").enableAutoSIPrefix(False)
        self.output_plot.showGrid(x=True, y=True, alpha=0.15)
        self.output_plot.setXLink(self.plot)
        self.output_curves = [self.output_plot.plot(pen=pg.mkPen(c, width=1.5)) for c in COLORS]
        right.addWidget(self.output_plot, 1)
        tools = W.QHBoxLayout()
        tools.addWidget(W.QLabel("读取周期"))
        self.interval = number(0.2, 10, 0.5, 2, " s")
        self.interval.valueChanged.connect(self.change_interval)
        tools.addWidget(self.interval)
        self.follow = W.QCheckBox("跟随最近 10 分钟")
        self.follow.setChecked(True)
        tools.addStretch()
        right.addLayout(tools)
        right.addWidget(self.follow)
        tools = W.QHBoxLayout()
        tools.addStretch()
        self.record_btn = button("开始 CSV 记录", self.toggle_recording)
        tools.addWidget(self.record_btn)
        tools.addWidget(button("导出当前曲线", self.export_csv))
        right.addLayout(tools)
        note = W.QLabel("实线：测量值 · 虚线：设备目标值\n采样实际时间以 CSV 为准")
        note.setObjectName("muted")
        note.setWordWrap(False)
        right.addWidget(note)
        outer.addWidget(right_scroll, 1)
        self.control_widgets = [
            self.target_btn,
            self.mode_btn,
            self.limit_btn,
            self.enable_btn,
            self.disable_btn,
        ]
        for w in self.control_widgets:
            w.setEnabled(False)
        self.tabs.addTab(page, "实时控制")

    def build_parameters(self):
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        top = W.QHBoxLayout()
        self.search = W.QLineEdit()
        self.search.setPlaceholderText("筛选名称、指令或分类…")
        self.search.textChanged.connect(self.filter_parameters)
        top.addWidget(self.search)
        self.read_all_btn = button("读取全部已定义参数", self.read_all)
        top.addWidget(self.read_all_btn)
        self.read_all_btn.setEnabled(False)
        layout.addLayout(top)
        self.table = W.QTableWidget(len(PARAMETERS), 8)
        self.table.setHorizontalHeaderLabels(
            [
                "分类 / 参数",
                "ASCII 指令",
                "寄存器 (TC1)",
                "权限",
                "设备值",
                "待写入值",
                "单位",
                "操作",
            ]
        )
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(W.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(W.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.editors, self.parameter_buttons = {}, []
        for row, p in enumerate(PARAMETERS):
            vals = [
                p.group + " / " + p.label,
                p.command,
                f"0x{p.address:04X}" if p.address is not None else "—",
                p.access,
                "—",
                "",
                p.unit,
            ]
            for col, text in enumerate(vals):
                item = W.QTableWidgetItem(text)
                item.setToolTip(
                    p.note
                    + f"\n范围：{p.display(p.minimum)} ~ {p.display(p.maximum)} {p.unit}\n类型：{'int' if p.signed else 'uint'}{p.bits}"
                )
                self.table.setItem(row, col, item)
            edit = W.QLineEdit()
            edit.setPlaceholderText("请输入值" if "W" in p.access else "只读")
            edit.setEnabled("W" in p.access)
            edit.setToolTip(self.table.item(row, 0).toolTip())
            self.table.setCellWidget(row, 5, edit)
            self.editors[p.command] = edit
            container = W.QWidget()
            buttons = W.QHBoxLayout(container)
            buttons.setContentsMargins(2, 0, 2, 0)
            if "R" in p.access:
                b = button("读", lambda _, p=p: self.read_parameter(p))
                buttons.addWidget(b)
                self.parameter_buttons.append((b, p))
            if "W" in p.access:
                b = button(
                    "写", lambda _, p=p: self.write_value(p.command, self.editors[p.command].text())
                )
                buttons.addWidget(b)
                self.parameter_buttons.append((b, p))
            self.table.setCellWidget(row, 7, container)
            self.table.setRowHeight(row, 42)
        self.table.setColumnWidth(0, 285)
        self.table.setColumnWidth(1, 155)
        self.table.setColumnWidth(2, 125)
        self.table.setColumnWidth(3, 55)
        self.table.setColumnWidth(4, 160)
        self.table.setColumnWidth(5, 175)
        self.table.horizontalHeader().setStretchLastSection(True)
        for b, p in self.parameter_buttons:
            b.setEnabled(False)
        layout.addWidget(self.table)
        self.param_note = W.QLabel(
            "当前通道由「实时控制」左侧选择；通用参数不带通道前缀。悬停可查看单位、范围和限制。\nMF501 系数与 CURRENT 的 Modbus 宽度在原协议中矛盾：仅提供 ASCII 访问。"
        )
        self.param_note.setWordWrap(True)
        layout.addWidget(self.param_note)
        self.tabs.addTab(page, "完整参数")

    def build_plan(self):
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        label = W.QLabel(
            "基础分段控温：写入设备目标值，进入容差带后才累计保温时间。执行通道在开始时锁定。\n本功能仍使用设备 PID；后续上位机二级控制通过独立策略接口接入。"
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        self.plan_table = W.QTableWidget(0, 3)
        self.plan_table.setHorizontalHeaderLabels(
            ["目标温度 / °C", "连续保温 / s", "到温容差 / °C"]
        )
        self.plan_table.horizontalHeader().setSectionResizeMode(W.QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.plan_table)
        bar = W.QHBoxLayout()
        bar.addWidget(button("添加步骤", self.add_step))
        bar.addWidget(button("删除所选步骤", self.remove_step))
        self.plan_start = button("开始分段控温", self.start_plan)
        self.plan_start.setEnabled(False)
        bar.addWidget(self.plan_start)
        bar.addWidget(
            button("取消计划", lambda: self.cancel_plan("已取消；设备保留当前目标和输出状态"))
        )
        bar.addStretch()
        layout.addLayout(bar)
        self.plan_status = W.QLabel("未运行 · 完成计划后保持最后目标，输出保持开启")
        layout.addWidget(self.plan_status)
        self.add_step()
        self.tabs.addTab(page, "分段控温")

    def build_diagnostics(self):
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
        bar = W.QHBoxLayout()
        self.bulk_buttons = []
        for label, cmd, val in (
            ("INQUIRE 全面查询", "INQUIRE", 1),
            ("DATADEMAND 1 · PWM 原始值", "DATADEMAND", 1),
            ("DATADEMAND 2 · 电压", "DATADEMAND", 2),
        ):
            b = button(label, lambda _, cmd=cmd, val=val: self.submit("bulk", cmd, val))
            bar.addWidget(b)
            self.bulk_buttons.append(b)
            b.setEnabled(False)
        bar.addStretch()
        bar.addWidget(button("清空日志", lambda: self.log.clear()))
        layout.addLayout(bar)
        self.show_wire = W.QCheckBox("显示串口收发（含分片）；关闭后仅显示操作和异常")
        layout.addWidget(self.show_wire)
        self.faults = W.QLabel("故障状态：尚未读取")
        self.faults.setWordWrap(True)
        layout.addWidget(self.faults)
        self.log = W.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2500)
        self.log.setFont(QtGui.QFont("monospace", 10))
        layout.addWidget(self.log)
        layout.addWidget(
            W.QLabel(
                "INQUIRE 中额外的 CHRATIO / STEADYIOB 保留为原始日志；协议没有可核对的写入定义。"
            )
        )
        self.tabs.addTab(page, "协议诊断")

    def refresh_ports(self):
        selected = self.port.currentData() or self.settings.value("port", "SIMULATOR")
        self.port.clear()
        self.port.addItem("模拟设备 · 不连接硬件", "SIMULATOR")
        for p in comports():
            self.port.addItem(f"{p.device} · {p.description}", p.device)
        index = self.port.findData(selected)
        if index >= 0:
            self.port.setCurrentIndex(index)
        elif selected != "SIMULATOR":
            self.port.setEditText(str(selected))

    def toggle_connection(self):
        if self.worker and self.worker.isRunning():
            self.submit("close", priority=1)
            self.connect_btn.setEnabled(False)
            return
        port = (
            self.port.currentData()
            if self.port.currentIndex() >= 0
            else self.port.currentText().strip()
        )
        if not port:
            return
        self.latest.clear()
        self.loaded_editors.clear()
        self.last_sample = 0
        for row in range(self.table.rowCount()):
            self.table.item(row, 4).setText("—")
        for card in self.cards:
            card.setText("— °C")
        self.faults.setText("故障状态：尚未读取")
        self.history.clear()
        self.rows.clear()
        self.update_plots()
        self.settings.setValue("port", port)
        self.settings.setValue("baud", self.baud.currentText())
        config = dict(
            port=port,
            baud=int(self.baud.currentText()),
            mode="ASCII" if self.protocol.currentIndex() == 0 else "RTU",
            station=self.station.value(),
            channels=self.channels.currentIndex() + 1,
        )
        self.worker = DeviceWorker(config, self.interval.value())
        self.worker.connected.connect(self.on_connection)
        self.worker.values.connect(self.on_values)
        self.worker.sample.connect(self.on_sample)
        self.worker.message.connect(self.append_log)
        self.worker.wire.connect(self.on_wire)
        self.worker.stopped.connect(self.on_stopped)
        self.worker.finished.connect(self.on_finished)
        self.connect_btn.setEnabled(False)
        self.set_connection_fields(False)
        self.state.setText("正在连接并读取设备身份…")
        self.worker.start()

    def set_connection_fields(self, enabled):
        for w in (self.port, self.baud, self.protocol, self.station, self.channels, self.scan):
            w.setEnabled(enabled)

    def on_connection(self, connected, description):
        self.connect_btn.setText("断开串口" if connected else "连接设备")
        self.connect_btn.setEnabled(connected)
        self.state.setText(description)
        for w in self.control_widgets + [self.read_all_btn, self.plan_start, self.stop_btn]:
            w.setEnabled(connected)
        for b, p in self.parameter_buttons:
            b.setEnabled(connected and (self.protocol.currentIndex() == 0 or p.rtu))
        for b in self.bulk_buttons:
            b.setEnabled(connected and self.protocol.currentIndex() == 0)
        self.channel.model().item(1).setEnabled(self.channels.currentIndex() == 1)
        if self.channels.currentIndex() == 0:
            self.channel.setCurrentIndex(0)
        if connected:
            for ch in range(1, self.channels.currentIndex() + 2):
                for cmd in ("TG", "MODE", "LIMITED", "ENABLE"):
                    self.submit("read", BY_COMMAND[cmd], ch)
        else:
            self.cancel_plan("连接已断开；设备输出状态未知")
            self.finish_recording()
            for card in self.cards:
                card.setText("— °C")
            self.readout.setText("— °C")
            self.details.setText("已断开 · 设备输出状态未知")
        self.append_log(description)

    def on_finished(self):
        self.connect_btn.setEnabled(True)
        self.set_connection_fields(True)
        if self.closing_after_stop:
            self.close()

    def submit(self, operation, *args, **kwargs):
        if self.worker and self.worker.isRunning():
            return self.worker.submit(operation, *args, **kwargs)
        self.append_log("未连接设备")
        return False

    def write_value(self, command, text):
        if self.stage_index is not None:
            W.QMessageBox.information(self, "分段计划运行中", "请先取消计划，再手动修改参数。")
            return
        p, ch = BY_COMMAND[command], self.channel.currentIndex() + 1
        try:
            value = p.encode(text)
        except ValueError as exc:
            W.QMessageBox.warning(self, "参数无效", str(exc))
            return
        if (
            command
            in (
                "ENABLE",
                "MODE",
                "AUTOPID",
                "TCADJTEMP",
                "PWMDUTY",
                "ONSENSOR",
                "RESET",
                "CONTMODE",
            )
            or command in LINK_COMMANDS
        ):
            if command != "ENABLE" or value != 0:
                msg = f"写入 {p.key(ch)} = {p.display(value)} {p.unit}\n{p.note}"
                if command == "RESET":
                    msg += "\n会清除设备配置并可能改变通信参数。"
                if (
                    W.QMessageBox.question(self, "确认设备操作", msg)
                    != W.QMessageBox.StandardButton.Yes
                ):
                    return
        self.submit("write", p, ch, value)

    def read_parameter(self, p):
        self.submit("read", p, self.channel.currentIndex() + 1)

    def read_all(self):
        for p in PARAMETERS:
            if "R" in p.access and (self.protocol.currentIndex() == 0 or p.rtu):
                self.read_parameter(p)
        self.append_log("已排队读取全部参数；全部输出关闭操作可清空此队列")

    def filter_parameters(self, text):
        for row, p in enumerate(PARAMETERS):
            self.table.setRowHidden(
                row, text.casefold() not in (p.label + p.command + p.group).casefold()
            )

    def on_values(self, values):
        self.latest.update(values)
        ch = self.channel.currentIndex() + 1
        for row, p in enumerate(PARAMETERS):
            if p.key(ch) in self.latest:
                value = self.latest[p.key(ch)]
                rendered = (
                    "无传感器"
                    if p.command == "TCADJTEMP" and not valid_temperature(value)
                    else p.display(value)
                )
                self.table.item(row, 4).setText(rendered)
        for command, widget in (("TG", self.target), ("MODE", self.mode), ("LIMITED", self.limit)):
            key = f"TC{ch}:{command}"
            if key in values and key not in self.loaded_editors and not widget.hasFocus():
                self.loaded_editors.add(key)
                if command == "MODE":
                    widget.setCurrentIndex(values[key])
                else:
                    value = float(BY_COMMAND[command].display(values[key]))
                    if widget.minimum() <= value <= widget.maximum():
                        widget.setValue(value)
                        widget.setToolTip("")
                    else:
                        widget.setToolTip(f"设备读回 {value}，超出文档可写范围；真实值见完整参数表")
                        self.append_log(
                            f"{key} 读回 {value} 超出文档可写范围；保留原值显示，不扩大写入范围"
                        )
        if "ERRORCODE" in values:
            self.faults.setText(
                f"故障状态：{decode_errors(values['ERRORCODE'])}  (0x{values['ERRORCODE']:04X})"
            )
        if self.stage_waiting and values.get(self.stage_waiting[0]) == self.stage_waiting[1]:
            self.stage_waiting = None
        # Bulk-only fields have no trusted standalone mapping; retain them in the log.
        unknown = {
            k: v
            for k, v in values.items()
            if k.split(":")[-1] not in BY_COMMAND and k.split(":")[-1] not in ("OUTV", "PWM")
        }
        if unknown:
            self.append_log("批量原始值：" + str(unknown))

    def channel_changed(self):
        for cmd in ("TG", "MODE", "LIMITED"):
            self.loaded_editors.discard(f"TC{self.channel.currentIndex() + 1}:{cmd}")
        self.on_values({})
        if self.worker and self.worker.isRunning():
            for cmd in ("TG", "MODE", "LIMITED"):
                key = f"TC{self.channel.currentIndex() + 1}:{cmd}"
                if key in self.latest:
                    self.on_values({key: self.latest[key]})

    def on_sample(self, epoch, monotonic, values):
        self.last_sample = time.monotonic()
        self.history.append((epoch, monotonic, values))
        for ch in range(1, self.channels.currentIndex() + 2):
            k = f"TC{ch}:"
            raw = values[k + "TCADJTEMP"]
            temp = raw / 100000 if valid_temperature(raw) else math.nan
            target = values[k + "TG"] / 100000
            duty = values[k + "PWMDUTY"] / 20000
            row = dict(
                zip(
                    CSV_FIELDS,
                    [
                        datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
                        monotonic,
                        ch,
                        temp if math.isfinite(temp) else "",
                        target,
                        temp - target if math.isfinite(temp) else "",
                        duty,
                        values.get(k + "OUTV", "") / 1e8 if k + "OUTV" in values else "",
                        values[k + "RESISTOR"] / 1e6,
                        values[k + "ENABLE"],
                        values["ERRORCODE"],
                    ],
                )
            )
            self.rows.append(row)
            if self.record_writer:
                try:
                    self.record_writer.writerow(row)
                    self.record_file.flush()
                except OSError as exc:
                    self.append_log(f"记录失败：{exc}")
                    self.finish_recording()
            self.cards[ch - 1].setText(f"{temp:.5f} °C" if math.isfinite(temp) else "传感器无效")
        ch = self.channel.currentIndex() + 1
        k = f"TC{ch}:"
        raw = values[k + "TCADJTEMP"]
        self.readout.setText(f"{raw / 100000:.5f} °C" if valid_temperature(raw) else "传感器无效")
        self.details.setText(
            f"目标 {values[k + 'TG'] / 100000:.5f} °C   ·   输出 {values[k + 'PWMDUTY'] / 20000:+.3f}%\n设备输出：{'开启' if values[k + 'ENABLE'] else '关闭'}   ·   内部 {values['SINTERIORTEMP']} °C"
        )
        demo = "模拟 · " if self.worker.config["port"] == "SIMULATOR" else ""
        self.state.setText(
            f"{demo}已连接 · {self.worker.config['port']} · 最近采样 {datetime.fromtimestamp(epoch).astimezone().strftime('%H:%M:%S')} · {decode_errors(values['ERRORCODE'])}"
        )
        self.update_plots()
        self.update_stats(ch, monotonic, values[k + "TG"])
        self.advance_plan(monotonic, values)

    def update_plots(self):
        points = list(self.history)
        for ch in (1, 2):
            data = [(t, v) for t, _, v in points if f"TC{ch}:TCADJTEMP" in v]
            x = [t for t, v in data]
            k = f"TC{ch}:"
            self.temp_curves[ch - 1].setData(
                x,
                [
                    v[k + "TCADJTEMP"] / 100000
                    if valid_temperature(v[k + "TCADJTEMP"])
                    else math.nan
                    for t, v in data
                ],
                connect="finite",
            )
            self.target_curves[ch - 1].setData(x, [v[k + "TG"] / 100000 for t, v in data])
            self.output_curves[ch - 1].setData(x, [v[k + "PWMDUTY"] / 20000 for t, v in data])
        if points and self.follow.isChecked():
            self.plot.setXRange(
                max(points[0][0], points[-1][0] - 600), points[-1][0] + 1, padding=0.01
            )

    def update_stats(self, ch, now, target):
        k = f"TC{ch}:"
        points = []
        # Only the contiguous segment at the current device target is meaningful.
        for _, t, v in reversed(self.history):
            if (
                now - t > 60
                or v.get(k + "TG") != target
                or not valid_temperature(v.get(k + "TCADJTEMP", 999999999))
            ):
                break
            points.append((t, v[k + "TCADJTEMP"] / 100000))
        if len(points) < 2:
            self.stats.setText("最近 60 s · 当前目标段\n数据不足")
            return
        temps = [v for t, v in points]
        errors = [v - target / 100000 for v in temps]
        self.stats.setText(
            f"最近 {points[0][0] - points[-1][0]:.1f} s · {len(points)} 点\n均温 {statistics.mean(temps):.5f} °C\n标准差 {statistics.pstdev(temps):.5f} °C\n峰峰值 {max(temps) - min(temps):.5f} °C\n最大绝对误差 {max(map(abs, errors)):.5f} °C\n±0.01 °C 内采样 {sum(abs(e) <= 0.01 for e in errors) / len(errors):.1%}"
        )

    def check_stale(self):
        if (
            self.worker
            and self.worker.isRunning()
            and self.last_sample
            and time.monotonic() - self.last_sample > max(3, self.interval.value() * 3)
        ):
            self.state.setText("数据已过期 · 设备输出状态未知")
            self.readout.setText("数据已过期")
            for card in self.cards:
                card.setText("数据已过期")
            if self.stage_index is not None:
                self.cancel_plan("数据过期，计划已取消；设备保持最后设置")

    def change_interval(self, value):
        if self.worker and self.worker.isRunning():
            self.submit("interval", value)

    def stop_outputs(self):
        self.cancel_plan("正在请求关闭全部输出")
        if self.worker and self.worker.isRunning():
            self.worker.stop_outputs()
            self.append_log("已清空待执行队列，优先关闭输出；等待设备确认")

    def on_stopped(self, ok):
        if self.closing_after_stop:
            if ok:
                self.submit("close", priority=0)
            else:
                self.closing_after_stop = False
                self.append_log("未确认输出关闭；窗口保持打开，请检查设备")

    def append_log(self, text):
        self.log.appendPlainText(datetime.now().astimezone().strftime("%H:%M:%S") + "  " + text)

    def on_wire(self, text):
        if self.show_wire.isChecked():
            self.append_log(text)

    def toggle_recording(self):
        if self.record_file:
            self.finish_recording()
            return
        path, _ = W.QFileDialog.getSaveFileName(
            self,
            "保存连续记录",
            str(Path.home() / f"sensefuture-{datetime.now():%Y%m%d-%H%M%S}.csv"),
            "CSV (*.csv)",
        )
        if not path:
            return
        try:
            self.record_file = open(path, "w", newline="", encoding="utf-8-sig")
            self.record_writer = csv.DictWriter(self.record_file, fieldnames=CSV_FIELDS)
            self.record_writer.writeheader()
            self.record_btn.setText("停止 CSV 记录")
            self.append_log(f"连续记录：{path}")
        except OSError as exc:
            self.finish_recording()
            W.QMessageBox.warning(self, "无法保存", str(exc))

    def finish_recording(self):
        if self.record_file:
            try:
                self.record_file.close()
            except OSError as exc:
                self.append_log(f"关闭记录文件失败：{exc}")
        self.record_file = self.record_writer = None
        self.record_btn.setText("开始 CSV 记录")

    def export_csv(self):
        path, _ = W.QFileDialog.getSaveFileName(
            self, "导出当前缓存", "sensefuture.csv", "CSV (*.csv)"
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                writer.writeheader()
                writer.writerows(self.rows)
            self.append_log(f"已导出 {len(self.rows)} 行：{path}")
        except OSError as exc:
            W.QMessageBox.warning(self, "导出失败", str(exc))

    def add_step(self):
        if self.stage_index is not None:
            return
        row = self.plan_table.rowCount()
        self.plan_table.insertRow(row)
        for col, text in enumerate(("40.00000", "60", "0.05")):
            self.plan_table.setItem(row, col, W.QTableWidgetItem(text))

    def remove_step(self):
        if self.stage_index is None and self.plan_table.currentRow() >= 0:
            self.plan_table.removeRow(self.plan_table.currentRow())

    def start_plan(self):
        if self.stage_index is not None:
            return
        ch = self.channel.currentIndex() + 1
        if self.latest.get(f"TC{ch}:ENABLE") != 1 or self.latest.get(f"TC{ch}:MODE") == 3:
            W.QMessageBox.warning(self, "无法开始", "请先选择设备 PID 模式并开启通道输出。")
            return
        try:
            plan = []
            for row in range(self.plan_table.rowCount()):
                texts = [self.plan_table.item(row, c).text() for c in range(3)]
                target = BY_COMMAND["TG"].encode(texts[0])
                dwell = float(texts[1])
                tolerance = float(texts[2])
                if not all(map(math.isfinite, (dwell, tolerance))) or dwell <= 0 or tolerance <= 0:
                    raise ValueError("保温时间和容差必须大于零")
                plan.append((target, dwell, tolerance))
            if not plan:
                raise ValueError("请添加至少一个步骤")
        except (ValueError, AttributeError) as exc:
            W.QMessageBox.warning(self, "计划无效", str(exc))
            return
        if (
            W.QMessageBox.question(
                self,
                "开始分段控温",
                f"将在通道 {ch} 执行 {len(plan)} 步。完成后保持最后目标和输出。继续？",
            )
            != W.QMessageBox.StandardButton.Yes
        ):
            return
        self.plan_channel = ch
        self.active_plan = plan
        self.stage_index = 0
        self.plan_table.setEnabled(False)
        self.plan_start.setEnabled(False)
        self.apply_step()

    def apply_step(self):
        target, _, _ = self.active_plan[self.stage_index]
        self.stage_since = None
        self.stage_waiting = (f"TC{self.plan_channel}:TG", target)
        self.stage_deadline = time.monotonic() + 10
        self.submit("write", BY_COMMAND["TG"], self.plan_channel, target)
        self.plan_status.setText(
            f"步骤 {self.stage_index + 1}/{len(self.active_plan)} · 等待目标写入确认"
        )

    def advance_plan(self, now, values):
        if self.stage_index is None:
            return
        k = f"TC{self.plan_channel}:"
        target, dwell, tolerance = self.active_plan[self.stage_index]
        if self.stage_waiting:
            if now > self.stage_deadline:
                self.cancel_plan("目标写入未确认，计划取消")
            return
        if (
            values.get("ERRORCODE")
            or values.get(k + "ENABLE") != 1
            or values.get(k + "MODE") == 3
            or not valid_temperature(values[k + "TCADJTEMP"])
        ):
            self.cancel_plan("故障、无传感器或输出关闭，计划取消；设备保持最后设置")
            return
        if values[k + "TG"] != target:
            self.cancel_plan("设备目标与计划不符，计划取消")
            return
        if abs(values[k + "TCADJTEMP"] / 100000 - target / 100000) > tolerance:
            self.stage_since = None
            state = "等待进入容差带"
        else:
            if self.stage_since is None:
                self.stage_since = now
            elapsed = now - self.stage_since
            state = f"保温 {elapsed:.1f} / {dwell:.1f} s"
            if elapsed >= dwell:
                self.stage_index += 1
                if self.stage_index >= len(self.active_plan):
                    self.cancel_plan("计划完成 · 保持最后目标和输出")
                    return
                self.apply_step()
                return
        self.plan_status.setText(
            f"通道 {self.plan_channel} · 步骤 {self.stage_index + 1} · {state}"
        )

    def cancel_plan(self, reason):
        was_active = self.stage_index is not None
        self.stage_index = None
        self.stage_waiting = None
        self.stage_since = None
        if hasattr(self, "plan_status"):
            self.plan_status.setText(reason)
            self.plan_table.setEnabled(True)
            self.plan_start.setEnabled(
                bool(
                    self.worker
                    and self.worker.isRunning()
                    and self.connect_btn.text() == "断开串口"
                )
            )
        if was_active:
            if self.worker and self.worker.isRunning():
                self.worker.clear_pending()
            self.append_log(reason)

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            event.ignore()
            reply = W.QMessageBox.question(
                self,
                "关闭工作台",
                "是否先请求关闭全部输出，再退出？\n选择 No 将仅断开串口，设备可能继续加热。",
                W.QMessageBox.StandardButton.Yes
                | W.QMessageBox.StandardButton.No
                | W.QMessageBox.StandardButton.Cancel,
            )
            if reply == W.QMessageBox.StandardButton.Yes:
                self.closing_after_stop = True
                self.stop_outputs()
            elif reply == W.QMessageBox.StandardButton.No:
                self.closing_after_stop = True
                self.submit("close", priority=0)
            return
        self.settings.setValue("geometry", self.saveGeometry())
        self.finish_recording()
        event.accept()


def main():
    parser = argparse.ArgumentParser(description="SenseFuture Linux Qt 温控工作台")
    parser.add_argument("--demo", action="store_true", help="自动连接模拟设备")
    parser.add_argument("--screenshot", type=Path, help="保存界面截图并退出（仅模拟模式）")
    args = parser.parse_args()
    if args.screenshot and not args.demo:
        parser.error("--screenshot 必须搭配 --demo，避免自动操作真实设备")
    app = W.QApplication(sys.argv[:1])
    app.setApplicationName("SenseFuture Workbench")
    # Qt 6 supplies per-monitor DPI scaling; preserve the desktop font family.
    app.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.GeneralFont))
    app.setStyle("Fusion")
    window = Window()
    window.show()
    if args.demo:
        window.port.setCurrentIndex(window.port.findData("SIMULATOR"))
        QtCore.QTimer.singleShot(100, window.toggle_connection)
    if args.screenshot:

        def capture():
            window.grab().save(str(args.screenshot))
            window.worker.finished.connect(app.quit)
            window.worker.submit("close", priority=0)

        QtCore.QTimer.singleShot(2500, capture)
    sys.exit(app.exec())
