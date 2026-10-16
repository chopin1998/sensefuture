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

from .algorithm_panel import AlgorithmPanel
from .algorithms import ALGORITHMS
from .catalog import BY_COMMAND, PARAMETERS, decode_errors
from .config import Configuration
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


class DraggableObservation(W.QFrame):
    """Chart overlay; only its title drags, leaving buttons and plot gestures alone."""

    def __init__(self, parent, content, settings):
        super().__init__(parent)
        self.settings = settings
        self.anchor = settings.value("stability_overlay_position", QtCore.QPointF(1, 0))
        if not isinstance(self.anchor, QtCore.QPointF):
            self.anchor = QtCore.QPointF(1, 0)
        self.drag_offset = None
        self.setObjectName("stabilityOverlay")
        self.setStyleSheet(
            "QFrame#stabilityOverlay { background: #252e3a; border: 1px solid #64788e; border-radius: 6px; }"
        )
        layout = W.QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(5)
        self.handle = W.QLabel("⠿ 稳定性观察 · ±0.01 °C")
        self.handle.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        self.handle.setToolTip("拖动标题可移动；位置自动保存")
        self.handle.installEventFilter(self)
        layout.addWidget(self.handle)
        layout.addWidget(content)
        parent.installEventFilter(self)
        self.show()
        QtCore.QTimer.singleShot(0, self.fit_to_plot)

    def fit_to_plot(self):
        parent = self.parentWidget()
        self.layout().activate()
        preferred = self.sizeHint()
        self.setFixedSize(
            min(preferred.width(), parent.width()), min(preferred.height(), parent.height())
        )
        span_x, span_y = (
            max(0, parent.width() - self.width()),
            max(0, parent.height() - self.height()),
        )
        self.move(
            round(max(0, min(1, self.anchor.x())) * span_x),
            round(max(0, min(1, self.anchor.y())) * span_y),
        )
        self.raise_()

    def eventFilter(self, watched, event):
        kind = event.type()
        if watched is self.parentWidget() and kind == QtCore.QEvent.Type.Resize:
            QtCore.QTimer.singleShot(0, self.fit_to_plot)
        if watched is self.handle:
            if (
                kind == QtCore.QEvent.Type.MouseButtonPress
                and event.button() == QtCore.Qt.MouseButton.LeftButton
            ):
                self.drag_offset = event.globalPosition().toPoint() - self.mapToGlobal(
                    QtCore.QPoint()
                )
                self.handle.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
                return True
            if kind == QtCore.QEvent.Type.MouseMove and self.drag_offset is not None:
                parent = self.parentWidget()
                position = parent.mapFromGlobal(event.globalPosition().toPoint() - self.drag_offset)
                span_x, span_y = (
                    max(0, parent.width() - self.width()),
                    max(0, parent.height() - self.height()),
                )
                x, y = max(0, min(span_x, position.x())), max(0, min(span_y, position.y()))
                self.move(x, y)
                self.anchor = QtCore.QPointF(
                    x / span_x if span_x else 0, y / span_y if span_y else 0
                )
                return True
            if (
                kind == QtCore.QEvent.Type.MouseButtonRelease
                and event.button() == QtCore.Qt.MouseButton.LeftButton
            ):
                self.drag_offset = None
                self.handle.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
                self.settings.setValue("stability_overlay_position", self.anchor)
                return True
        return super().eventFilter(watched, event)


class TemperatureAxis(pg.AxisItem):
    """Temperature labels use three decimals without SI prefix rescaling."""

    def tickStrings(self, values, scale, spacing):
        return [f"{value:.3f}" for value in values]


def encode_temperature_control(parameter, text):
    value = parameter.encode(text)
    if parameter.unit == "°C" and parameter.scale >= 100 and value % (parameter.scale // 100):
        raise ValueError("控制温度请按 0.01 °C 的步进输入")
    return value


class Window(W.QMainWindow):
    def __init__(self):
        super().__init__()
        self.algorithm_busy = False
        self.algorithm_transition = None
        self.algorithm_failed_shutdown = False
        self.algorithm_configs = {}
        self.algorithm_live_values = {}
        self.algorithm_pending = {}
        self.algorithm_parameter_timer = QtCore.QTimer(self)
        self.algorithm_parameter_timer.setSingleShot(True)
        self.algorithm_parameter_timer.setInterval(100)
        self.algorithm_parameter_timer.timeout.connect(self.flush_algorithm_parameters)
        self.worker = None
        self.device_connected = False
        self.output_pending = None
        self.stop_state = "unknown"
        self.channel_count = 2
        self.latest = {}
        self.loaded_editors = set()
        self.history = deque(maxlen=30000)  # Recent samples for stability statistics.
        self.plot_history = []  # Entire plotted session, until explicitly cleared.
        self.rows = deque(maxlen=60000)
        self.last_sample = 0
        self.record_file = self.record_writer = None
        self.closing_after_stop = False
        self.stage_index = None
        self.stage_since = None
        self.stage_waiting = None
        self.stage_deadline = None
        self.active_plan = []
        self.configuration = Configuration()
        self.settings = self.configuration.global_settings
        self.window_seconds = {
            kind: max(
                1, min(3600, self.settings.value(f"{kind}_window_seconds", default, type=int))
            )
            for kind, default in (("output", 5), ("temperature", 5), ("stability", 15))
        }
        self.window_labels = {}
        self.window_buttons = {}
        self.setWindowTitle("SenseFuture · 温控工作台")
        self.resize(1440, 950)
        self.setMinimumSize(1100, 760)
        self.default_font_size = max(
            14,
            QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.GeneralFont).pointSize(),
        )
        self.build()
        self.set_ui_font_size(
            self.settings.value("font_size", self.default_font_size, type=int), persist=False
        )
        self.refresh_stop_button()
        self.refresh_pid_controls()
        self.refresh_algorithm_controls()
        self.refresh_ports()
        self.restore_preferences()
        self.configuration.error_handler = self.append_log
        for message in self.configuration.errors:
            self.append_log(message)
        geometry = self.settings.value("geometry", QtCore.QByteArray())
        if isinstance(geometry, QtCore.QByteArray):
            self.restoreGeometry(geometry)
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
        row.addWidget(W.QLabel("站号"))
        self.station = W.QSpinBox()
        self.station.setRange(1, 247)
        row.addWidget(self.station)
        self.connect_btn = button("连接设备", self.toggle_connection)
        row.addWidget(self.connect_btn)
        row.addStretch()
        self.stop_btn = button("关闭全部输出", self.stop_outputs)
        self.stop_btn.setObjectName("stop")
        self.stop_btn.setEnabled(False)
        row.addWidget(self.stop_btn)
        layout.addLayout(row)
        self.state = W.QLabel("未连接")
        self.state.setObjectName("muted")
        layout.addWidget(self.state)
        self.tabs = W.QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.build_monitor()
        self.build_algorithm()
        self.build_parameters()
        self.build_plan()
        self.build_diagnostics()
        self.tabs.currentChanged.connect(self.move_observation)
        self.setCentralWidget(root)

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
            QPushButton#stop { background: #883d42; border: 2px solid #e78888; font-weight: 700; }
            QPushButton#stop[stopState="pending"] { background: #805f1e; border-color: #ffd46d; color: #fff0bd; }
            QPushButton#stop[stopState="stopped"] { background: #286145; border-color: #7cdbac; color: #e5fff1; }
            QPushButton#stop[stopState="failed"] { background: #9b2e35; border-color: #ffb1b1; color: #ffffff; }
            QPushButton#stop[stopState="unknown"] { background: #303842; border-color: #657080; color: #c3cbd6; }
            QPushButton#outputToggle, QPushButton#algorithmToggle { background: #304c62; border: 2px solid #69b8ed; font-weight: 700; padding: 12px; }
            QPushButton#outputToggle:checked, QPushButton#algorithmToggle:checked { background: #a34a23; border: 2px solid #ffb16e; color: #ffffff; }
            QPushButton#outputToggle:disabled, QPushButton#algorithmToggle:disabled { background: #303842; border-color: #657080; color: #c3cbd6; }
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: #171c23; border: 1px solid #445166; border-radius: 4px; padding: 5px; selection-background-color: #35689b; }
            QTabWidget::pane { border: 1px solid #3c4654; }
            QTabBar::tab { background: #2a323e; padding: 9px 20px; }
            QTabBar::tab:selected { background: #3a4d64; }
            QTableWidget, QPlainTextEdit { background: #191e26; gridline-color: #35404e; }
            QHeaderView::section { background: #303b4a; padding: 7px; border: none; }
            QScrollBar:vertical { width: 16px; background: #232b35; }
            QPlainTextEdit { font-family: monospace; }
            QToolButton#windowAdjust { background: #344153; color: #e4e9f1; border: 1px solid #64788e; border-radius: 4px; padding: 1px 5px; }
            QToolButton#windowAdjust:hover { background: #435973; }
            QToolButton#windowAdjust:disabled { color: #697586; border-color: #394655; }
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
        self.algorithm_sidebar_scroll.setMinimumWidth(round(350 * size / 12))
        self.algorithm_sidebar_scroll.setMaximumWidth(round(430 * size / 12))
        self.table.verticalHeader().setDefaultSectionSize(max(44, metrics.height() + 24))
        for row in range(self.table.rowCount()):
            self.table.setRowHeight(row, max(44, metrics.height() + 24))
        for col, width in enumerate((285, 155, 125, 55, 160, 175)):
            self.table.setColumnWidth(col, round(width * size / 10))
        self.plan_table.verticalHeader().setDefaultSectionSize(metrics.height() + 18)
        self.output_plot.setMinimumHeight(round(165 * size / 12))
        self.recent_plot.setMinimumHeight(max(150, size * 10))
        for plot in (
            self.plot,
            self.output_plot,
            self.recent_plot,
        ):
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
        self.plot.setMinimumHeight(max(300, metrics.height() * 11))
        QtCore.QTimer.singleShot(0, self.stability_overlay.fit_to_plot)
        if persist:
            self.settings.setValue("font_size", size)

    def build_algorithm(self):
        page = W.QWidget()
        self.algorithm_page = page
        self.algorithm_observation_layout = W.QHBoxLayout(page)
        self.algorithm_sidebar_scroll = W.QScrollArea()
        self.algorithm_sidebar_scroll.setWidgetResizable(True)
        self.algorithm_sidebar_scroll.setFrameShape(W.QFrame.Shape.NoFrame)
        self.algorithm_sidebar_scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        sidebar = W.QWidget()
        self.algorithm_sidebar_scroll.setWidget(sidebar)
        layout = W.QVBoxLayout(sidebar)
        self.algorithm_observation_layout.addWidget(self.algorithm_sidebar_scroll)
        heading = W.QVBoxLayout()
        self.algorithm_select = W.QComboBox()
        for key, spec in ALGORITHMS.items():
            self.algorithm_select.addItem(spec.name, key)
        self.algorithm_channel = W.QComboBox()
        self.algorithm_channel.addItems(["通道 1", "通道 2"])
        heading.addWidget(W.QLabel("算法"))
        heading.addWidget(self.algorithm_select)
        heading.addWidget(self.algorithm_channel)
        layout.addLayout(heading)
        target_row = W.QVBoxLayout()
        target_row.addWidget(W.QLabel("跟踪目标 (°C)"))
        self.algorithm_target = number(-400, 1000, 40, 2)
        self.algorithm_target.setSingleStep(0.01)
        target_row.addWidget(self.algorithm_target)
        self.algorithm_apply = button("更新跟踪目标 ↵", self.apply_algorithm_target)
        self.algorithm_target.lineEdit().installEventFilter(self)
        target_row.addWidget(self.algorithm_apply)
        target_row.addStretch()
        layout.addLayout(target_row)
        self.algorithm_parameters = W.QWidget()
        self.algorithm_panel_layout = W.QVBoxLayout(self.algorithm_parameters)
        self.algorithm_panel_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.algorithm_parameters)
        self.algorithm_trace = deque(maxlen=30000)
        self.algorithm_status = W.QLabel("未运行")
        self.algorithm_status.setWordWrap(True)
        layout.addWidget(self.algorithm_status)
        self.algorithm_readout = W.QLabel("参考 — °C · 命令 — %")
        self.algorithm_readout.setWordWrap(True)
        layout.addWidget(self.algorithm_readout)
        actions = W.QVBoxLayout()
        self.algorithm_toggle = button("○ 接管输出", self.toggle_algorithm)
        self.algorithm_toggle.setObjectName("algorithmToggle")
        self.algorithm_toggle.setCheckable(True)
        self.algorithm_toggle.setMinimumHeight(56)
        actions.addWidget(self.algorithm_toggle)
        actions.addStretch()
        layout.insertLayout(2, actions)
        layout.removeWidget(self.algorithm_status)
        layout.insertWidget(3, self.algorithm_status)
        layout.removeWidget(self.algorithm_readout)
        layout.insertWidget(4, self.algorithm_readout)
        selected = self.algorithm_select.findData(self.settings.value("algorithm", "ramp_pid"))
        self.algorithm_select.setCurrentIndex(max(0, selected))
        self.algorithm_target.valueChanged.connect(
            lambda value: self.algorithm_config.setValue("target_c", value)
        )
        self.algorithm_channel.currentIndexChanged.connect(
            lambda value: self.algorithm_config.setValue("channel", value + 1)
        )
        self.algorithm_select.currentIndexChanged.connect(self.populate_algorithm)
        self.populate_algorithm()
        layout.addStretch()
        self.algorithm_channel.currentIndexChanged.connect(self.refresh_observation_stats)
        self.tabs.addTab(page, "算法控制")

    def populate_algorithm(self):
        if hasattr(self, "algorithm_panel"):
            self.algorithm_panel_layout.removeWidget(self.algorithm_panel)
            self.algorithm_panel.deleteLater()
        spec = ALGORITHMS[self.algorithm_select.currentData()]
        self.settings.setValue("algorithm", spec.key)
        if spec.key not in self.algorithm_configs:
            self.algorithm_configs[spec.key] = spec.open_configuration(
                self.configuration.root, self.configuration.report
            )
        self.algorithm_config = self.algorithm_configs[spec.key]
        self.algorithm_select.setToolTip(spec.description)
        for widget, key, default, setter in (
            (self.algorithm_target, "target_c", 40.0, self.algorithm_target.setValue),
            (
                self.algorithm_channel,
                "channel",
                1,
                lambda value: self.algorithm_channel.setCurrentIndex(max(0, min(1, value - 1))),
            ),
        ):
            blocker = QtCore.QSignalBlocker(widget)
            value = self.algorithm_config.value(key, default, type=type(default))
            if isinstance(value, float) and not math.isfinite(value):
                value = default
            setter(value)
            del blocker
            if not self.algorithm_config.contains(key):
                self.algorithm_config.setValue(key, default)
        self.algorithm_panel = AlgorithmPanel(
            spec, self.algorithm_config, self.configuration.report
        )
        spec.initialize_panel(self.algorithm_panel)
        if set(self.algorithm_panel.widgets) != {field.key for field in spec.fields}:
            raise ValueError(f"算法面板参数注册不完整：{spec.key}")
        self.algorithm_panel.changed.connect(self.on_algorithm_parameter_changed)
        self.algorithm_panel_layout.addWidget(self.algorithm_panel)
        self.algorithm_fields = self.algorithm_panel.widgets

    def algorithm_settings(self):
        return self.algorithm_panel.values()

    def on_algorithm_parameter_changed(self, key, value):
        if not self.algorithm_busy:
            return
        if (
            self.algorithm_transition is not None
            or self.algorithm_failed_shutdown
            or not self.algorithm_panel.live[key]
        ):
            self.algorithm_panel.set_values({key: self.algorithm_live_values[key]})
            return
        self.algorithm_pending[key] = value
        self.algorithm_panel.pending.add(key)
        self.algorithm_panel.refresh()
        self.algorithm_parameter_timer.start()

    def flush_algorithm_parameters(self):
        if not self.algorithm_pending:
            return
        changes, self.algorithm_pending = self.algorithm_pending, {}
        if not self.submit("host_config", changes, priority=3):
            self.on_algorithm_parameters(
                {
                    "accepted": False,
                    "algorithm": self.algorithm_select.currentData(),
                    "changes": changes,
                    "values": self.algorithm_live_values,
                    "message": "参数未提交",
                }
            )

    def on_algorithm_parameters(self, result):
        if result["algorithm"] != self.algorithm_select.currentData():
            return
        self.algorithm_panel.pending.difference_update(result["changes"])
        if result["accepted"]:
            self.algorithm_live_values.update(result["values"])
        else:
            self.algorithm_panel.set_values(
                {key: result["values"][key] for key in result["changes"] if key in result["values"]}
            )
            self.algorithm_status.setText(result.get("message", "参数未更新"))
        self.algorithm_panel.refresh()

    def clear_algorithm_parameter_queue(self):
        self.algorithm_parameter_timer.stop()
        self.algorithm_pending.clear()
        self.algorithm_panel.pending.clear()

    def refresh_algorithm_controls(self):
        available = self.device_connected and self.stop_state != "pending"
        for widget in self.control_widgets:
            widget.setEnabled(available and not self.algorithm_busy)
        self.plan_start.setEnabled(
            available and not self.algorithm_busy and self.stage_index is None
        )
        self.refresh_output_toggle()
        self.refresh_pid_controls()
        blocker = QtCore.QSignalBlocker(self.algorithm_toggle)
        self.algorithm_toggle.setChecked(
            self.algorithm_busy and self.algorithm_transition != "starting"
        )
        del blocker
        if self.algorithm_transition == "starting":
            label = "正在接管输出…"
        elif self.algorithm_transition == "stopping":
            label = "正在停止并关闭输出…"
        elif self.algorithm_failed_shutdown:
            label = "关断未确认 · 点击重试关闭"
        elif self.algorithm_busy:
            label = "● 停止并关闭输出"
        else:
            label = "○ 接管输出"
        self.algorithm_toggle.setText(label)
        self.algorithm_toggle.setEnabled(available and self.algorithm_transition is None)
        self.algorithm_apply.setEnabled(
            available
            and self.algorithm_busy
            and not self.algorithm_failed_shutdown
            and self.algorithm_transition is None
        )
        for widget in (self.algorithm_select, self.algorithm_channel):
            widget.setEnabled(not self.algorithm_busy)
        self.algorithm_panel.set_state(
            self.algorithm_busy,
            available and self.algorithm_transition is None and not self.algorithm_failed_shutdown,
        )

    def toggle_algorithm(self):
        self.refresh_algorithm_controls()
        if self.algorithm_transition is not None or not self.algorithm_toggle.isEnabled():
            return
        if self.algorithm_busy:
            self.stop_algorithm()
        else:
            self.start_algorithm()

    def start_algorithm(self):
        if self.algorithm_busy or not self.device_connected or self.stop_state == "pending":
            return
        if self.stage_index is not None:
            self.algorithm_status.setText("请先取消分段计划，再启动算法")
            return
        settings = self.algorithm_settings()
        try:
            ALGORITHMS[self.algorithm_select.currentData()].validate(settings)
            if self.interval.value() >= settings["max_gap"]:
                raise ValueError("读取周期必须小于算法最大采样间隔")
            if not settings["min_temp"] <= self.algorithm_target.value() <= settings["max_temp"]:
                raise ValueError("跟踪目标超出算法温度边界")
        except ValueError as exc:
            self.algorithm_status.setText(str(exc))
            return
        self.worker.clear_pending()
        self.algorithm_live_values = dict(settings)
        self.algorithm_busy = True
        self.algorithm_transition = "starting"
        self.algorithm_trace.clear()
        self.algorithm_readout.setText("等待算法采样…")
        self.algorithm_status.setText("正在接管输出…")
        if not self.submit(
            "host_start",
            self.algorithm_select.currentData(),
            self.algorithm_channel.currentIndex() + 1,
            self.algorithm_target.value(),
            settings,
            priority=2,
        ):
            self.algorithm_busy = False
            self.algorithm_transition = None
        self.refresh_algorithm_controls()

    def apply_algorithm_target(self):
        if not self.algorithm_busy or not self.algorithm_apply.isEnabled():
            return
        settings = self.algorithm_settings()
        if not settings["min_temp"] <= self.algorithm_target.value() <= settings["max_temp"]:
            self.algorithm_status.setText("目标超出算法温度边界，未写入")
            return
        self.submit("host_target", self.algorithm_target.value(), priority=3)

    def stop_algorithm(self):
        if not self.algorithm_busy or self.algorithm_transition == "stopping":
            return
        self.clear_algorithm_parameter_queue()
        self.worker.clear_pending()
        self.algorithm_status.setText("正在停止…")
        self.algorithm_transition = "stopping"
        if not self.submit("host_stop", self.algorithm_channel.currentIndex() + 1, priority=0):
            self.algorithm_transition = None
        self.refresh_algorithm_controls()

    def on_algorithm_state(self, state):
        if state["state"] != "running":
            self.clear_algorithm_parameter_queue()
        if not (self.algorithm_transition == "stopping" and state["state"] == "running"):
            self.algorithm_failed_shutdown = state["state"] == "failed"
            self.algorithm_busy = state["state"] in ("running", "failed")
            self.algorithm_transition = None
            self.algorithm_status.setText(state["message"])
        if "temperature" in state:
            self.algorithm_trace.append(
                (
                    state["monotonic"],
                    state["temperature"],
                    state["target"],
                    state["reference"],
                    state["output"],
                )
            )
            # The worker emits this after the matching device sample. Attach the
            # reference to that same history point, so both tabs use one time base.
            if self.plot_history and self.plot_history[-1][1] == state["monotonic"]:
                self.plot_history[-1][2][f"TC{state['channel']}:HOSTREFERENCE"] = state["reference"]
                self.update_plots()
            self.algorithm_readout.setText(
                f"参考 {state['reference']:.3f} °C · 命令 {state['output']:+.3f} %"
            )
        else:
            self.append_log("算法控制：" + state["message"])
        self.refresh_algorithm_controls()

    def build_monitor(self):
        page = W.QWidget()
        self.monitor_page = page
        outer = self.monitor_observation_layout = W.QHBoxLayout(page)
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
        self.target = number(-400, 1000, 40, 2)
        self.target.setSingleStep(0.01)
        form.addRow("目标温度 (°C)", self.target)
        self.target_btn = button(
            "写入目标温度", lambda: self.write_value("TG", str(self.target.value()))
        )
        form.addRow(self.target_btn)
        self.target.lineEdit().installEventFilter(self)
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
        self.output_toggle = button("输出状态未知", self.toggle_channel_output)
        self.output_toggle.setObjectName("outputToggle")
        self.output_toggle.setCheckable(True)
        self.output_toggle.setMinimumHeight(56)
        self.output_toggle.setToolTip("切换当前所选通道输出；状态以设备读回为准")
        form.addRow(self.output_toggle)
        side.addWidget(box)
        self.pid_group = W.QGroupBox("PID 控制参数 · 当前通道")
        pid_form = W.QFormLayout(self.pid_group)
        self.pid_fields = {}
        for command, label in (("KP", "比例 P"), ("KI", "积分 I"), ("KD", "微分 D")):
            field = W.QSpinBox()
            field.setRange(0, 9000000)
            field.setKeyboardTracking(False)
            field.setEnabled(False)
            self.pid_fields[command] = field
            pid_form.addRow(label, field)
        self.pid_read_btn = button("读取 PID", self.read_pid)
        self.pid_write_btn = button("写入 PID", self.write_pid)
        pid_buttons = W.QHBoxLayout()
        pid_buttons.addWidget(self.pid_read_btn)
        pid_buttons.addWidget(self.pid_write_btn)
        pid_form.addRow(pid_buttons)
        side.addWidget(self.pid_group)
        self.stats = W.QLabel("等待数据")
        self.stats.setWordWrap(False)
        stats_box = W.QWidget()
        sl = W.QVBoxLayout(stats_box)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(self.create_window_controls("stability"))
        sl.addWidget(self.stats)
        self.stats_box = stats_box
        side.addStretch()
        outer.addWidget(self.sidebar_scroll)
        self.observation = self.build_observation()
        outer.addWidget(self.observation, 1)
        self.control_widgets = [
            self.target_btn,
            self.mode_btn,
            self.limit_btn,
            self.output_toggle,
            self.pid_read_btn,
            self.pid_write_btn,
        ]
        for w in self.control_widgets:
            w.setEnabled(False)
        self.tabs.addTab(page, "实时控制")

    def build_observation(self):
        """One physical observation widget, moved between the two control tabs."""
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
        curve_controls = W.QHBoxLayout()
        curve_controls.addWidget(W.QLabel("温度曲线"))
        self.curve_toggles = []
        for ch in (1, 2):
            toggle = W.QPushButton(f"通道 {ch}：显示")
            toggle.setCheckable(True)
            toggle.setObjectName("curveToggle")
            toggle.setChecked(True)
            toggle.setStyleSheet(
                f"QPushButton:checked {{ background: #304c62; border: 2px solid {COLORS[ch - 1]}; }}"
            )
            toggle.setToolTip("仅切换温度及目标曲线的显示，不改变设备输出")
            toggle.toggled.connect(self.update_curve_visibility)
            self.curve_toggles.append(toggle)
            curve_controls.addWidget(toggle)
        curve_controls.addStretch()
        right.addLayout(curve_controls)
        self.plot = pg.PlotWidget(
            axisItems={"bottom": pg.DateAxisItem(), "left": TemperatureAxis("left")}
        )
        self.plot.setLabel("left", "温度 · 全部历史", units="°C")
        self.plot.getAxis("left").enableAutoSIPrefix(False)
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.addLegend(offset=(10, 10), colCount=2)
        self.plot.setMinimumHeight(300)
        self.stability_overlay = DraggableObservation(
            self.plot.viewport(), self.stats_box, self.settings
        )
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
        self.reference_curves = [
            self.plot.plot(
                pen=pg.mkPen(c, width=1, style=QtCore.Qt.PenStyle.DotLine), name=f"斜坡 {i + 1}"
            )
            for i, c in enumerate(COLORS)
        ]
        for curve in self.temp_curves + self.target_curves + self.reference_curves:
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
        clear_action = self.plot.getViewBox().getMenu(None).addAction("清空图形")
        clear_action.triggered.connect(self.clear_plots)
        self.clear_plot_action = clear_action
        right.addWidget(self.plot, 3)
        self.recent_enabled = W.QCheckBox("温度小图")
        self.recent_enabled.setChecked(self.settings.value("recent_plot_enabled", True, type=bool))
        self.recent_enabled.toggled.connect(self.toggle_recent_plot)
        self.recent_plot = pg.PlotWidget(
            axisItems={"bottom": pg.DateAxisItem(), "left": TemperatureAxis("left")}
        )
        self.recent_plot.setLabel(
            "left", f"最近 {self.window_seconds['temperature']} 秒", units="°C"
        )
        self.recent_plot.getAxis("left").enableAutoSIPrefix(False)
        self.recent_plot.showGrid(x=True, y=True, alpha=0.15)
        self.recent_curves = [self.recent_plot.plot(pen=pg.mkPen(c, width=2)) for c in COLORS]
        self.recent_plot.setVisible(self.recent_enabled.isChecked())
        self.output_plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.output_plot.setLabel("left", "输出", units="%")
        self.output_plot.getAxis("left").enableAutoSIPrefix(False)
        self.output_plot.showGrid(x=True, y=True, alpha=0.15)
        self.output_curves = [self.output_plot.plot(pen=pg.mkPen(c, width=1.5)) for c in COLORS]
        window_controls = W.QHBoxLayout()
        window_controls.addWidget(self.create_window_controls("output", "输出 %"), 1)
        window_controls.addWidget(
            self.create_window_controls("temperature", toggle=self.recent_enabled), 1
        )
        right.addLayout(window_controls)
        detail_plots = W.QHBoxLayout()
        detail_plots.addWidget(self.output_plot, 1)
        detail_plots.addWidget(self.recent_plot, 1)
        right.addLayout(detail_plots, 1)
        tools = W.QHBoxLayout()
        tools.addWidget(W.QLabel("读取周期"))
        self.interval = number(0.2, 10, 0.5, 2, " s")
        self.interval.valueChanged.connect(self.change_interval)
        tools.addWidget(self.interval)
        tools.addStretch()
        right.addLayout(tools)
        tools = W.QHBoxLayout()
        tools.addStretch()
        self.record_btn = button("开始 CSV 记录", self.toggle_recording)
        tools.addWidget(self.record_btn)
        tools.addWidget(button("导出当前曲线", self.export_csv))
        right.addLayout(tools)
        return right_scroll

    def move_observation(self, _index):
        page = self.tabs.currentWidget()
        if page not in (self.monitor_page, self.algorithm_page):
            return
        algorithm = page is self.algorithm_page
        for owner in (self.monitor_observation_layout, self.algorithm_observation_layout):
            owner.removeWidget(self.observation)
        destination = (
            self.algorithm_observation_layout if algorithm else self.monitor_observation_layout
        )
        destination.addWidget(self.observation, 1)
        self.observation.show()
        self.stats_box.show()
        self.refresh_observation_stats()

    def observation_channel(self):
        selector = (
            self.algorithm_channel
            if self.tabs.currentWidget() is self.algorithm_page
            else self.channel
        )
        return selector.currentIndex() + 1

    def refresh_observation_stats(self, *_):
        if self.history:
            ch = self.observation_channel()
            point = self.history[-1]
            self.update_stats(ch, point[1], point[2].get(f"TC{ch}:TG"))

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
        self.tabs.addTab(page, "完整参数")

    def build_plan(self):
        page = W.QWidget()
        layout = W.QVBoxLayout(page)
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
        self.plan_status = W.QLabel("未运行")
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
        self.show_wire = W.QCheckBox("显示串口收发")
        layout.addWidget(self.show_wire)
        self.faults = W.QLabel("故障状态：尚未读取")
        self.faults.setWordWrap(True)
        layout.addWidget(self.faults)
        self.log = W.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2500)
        self.log.setFont(QtGui.QFont("monospace", 10))
        layout.addWidget(self.log)
        self.tabs.addTab(page, "协议诊断")

    def restore_preferences(self):
        for key, widget, default in (
            ("protocol", self.protocol, "ASCII"),
            ("station", self.station, 1),
            ("interval", self.interval, 0.5),
            ("control_channel", self.channel, 0),
            ("show_wire", self.show_wire, False),
            *(
                (f"curve_{ch}_visible", widget, True)
                for ch, widget in enumerate(self.curve_toggles, 1)
            ),
        ):
            value = self.settings.value(key, default, type=type(default))
            if isinstance(default, float) and not math.isfinite(value):
                value = default
            if isinstance(widget, W.QCheckBox) or isinstance(widget, W.QPushButton):
                widget.setChecked(value)
                widget.toggled.connect(lambda value, key=key: self.settings.setValue(key, value))
                current = widget.isChecked()
            elif isinstance(widget, W.QComboBox):
                if isinstance(default, str):
                    widget.setCurrentText(value)
                    widget.currentTextChanged.connect(
                        lambda value, key=key: self.settings.setValue(key, value)
                    )
                    current = widget.currentText()
                else:
                    widget.setCurrentIndex(max(0, min(widget.count() - 1, value)))
                    widget.currentIndexChanged.connect(
                        lambda value, key=key: self.settings.setValue(key, value)
                    )
                    current = widget.currentIndex()
            else:
                widget.setValue(value)
                widget.valueChanged.connect(
                    lambda value, key=key: self.settings.setValue(key, value)
                )
                current = widget.value()
            if not self.settings.contains(key):
                self.settings.setValue(key, current)
        for key, value in {
            "font_size": self.font_size.value(),
            "baud": self.baud.currentText(),
            "port": self.selected_port(),
            "recent_plot_enabled": self.recent_enabled.isChecked(),
            "stability_overlay_position": self.stability_overlay.anchor,
            **{f"{kind}_window_seconds": seconds for kind, seconds in self.window_seconds.items()},
        }.items():
            if not self.settings.contains(key):
                self.settings.setValue(key, value)
        self.baud.currentTextChanged.connect(lambda value: self.settings.setValue("baud", value))
        self.port.currentTextChanged.connect(
            lambda _: self.settings.setValue("port", self.selected_port())
        )

    def selected_port(self):
        index = self.port.currentIndex()
        text = self.port.currentText().strip()
        return (
            self.port.itemData(index) if index >= 0 and text == self.port.itemText(index) else text
        )

    def refresh_ports(self):
        selected = self.selected_port() or self.settings.value("port", "SIMULATOR")
        blocker = QtCore.QSignalBlocker(self.port)
        self.port.clear()
        self.port.addItem("模拟设备 · 不连接硬件", "SIMULATOR")
        for p in comports():
            self.port.addItem(f"{p.device} · {p.description}", p.device)
        index = self.port.findData(selected)
        if index >= 0:
            self.port.setCurrentIndex(index)
        elif selected != "SIMULATOR":
            self.port.setEditText(str(selected))
        del blocker

    def toggle_connection(self):
        if self.worker and self.worker.isRunning():
            self.submit("close", priority=1)
            self.connect_btn.setEnabled(False)
            return
        port = self.selected_port()
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
        self.plot_history.clear()
        self.rows.clear()
        self.update_plots()
        self.settings.setValue("port", port)
        self.settings.setValue("baud", self.baud.currentText())
        config = dict(
            port=port,
            baud=int(self.baud.currentText()),
            mode="ASCII" if self.protocol.currentIndex() == 0 else "RTU",
            station=self.station.value(),
            channels=self.channel_count,
        )
        self.worker = DeviceWorker(config, self.interval.value())
        self.worker.connected.connect(self.on_connection)
        self.worker.values.connect(self.on_values)
        self.worker.write_finished.connect(self.on_write_finished)
        self.worker.sample.connect(self.on_sample)
        self.worker.message.connect(self.append_log)
        self.worker.wire.connect(self.on_wire)
        self.worker.stopped.connect(self.on_stopped)
        self.worker.algorithm_state.connect(self.on_algorithm_state)
        self.worker.algorithm_parameters.connect(self.on_algorithm_parameters)
        self.worker.finished.connect(self.on_finished)
        self.connect_btn.setEnabled(False)
        self.set_connection_fields(False)
        self.state.setText("正在连接并读取设备身份…")
        self.worker.start()

    def set_connection_fields(self, enabled):
        for w in (self.port, self.baud, self.protocol, self.station, self.scan):
            w.setEnabled(enabled)

    def on_connection(self, connected, description):
        self.device_connected = connected
        if not connected:
            self.clear_algorithm_parameter_queue()
            self.algorithm_busy = False
            self.algorithm_transition = None
            self.algorithm_failed_shutdown = False
        self.refresh_algorithm_controls()
        self.stop_state = "ready" if connected else "unknown"
        if not connected:
            self.output_pending = None
        self.connect_btn.setText("断开串口" if connected else "连接设备")
        self.connect_btn.setEnabled(connected)
        self.state.setText(description)
        for w in self.control_widgets + [self.read_all_btn, self.plan_start, self.stop_btn]:
            w.setEnabled(connected)
        for b, p in self.parameter_buttons:
            b.setEnabled(connected and (self.protocol.currentIndex() == 0 or p.rtu))
        for b in self.bulk_buttons:
            b.setEnabled(connected and self.protocol.currentIndex() == 0)
        if connected:
            for ch in range(1, self.channel_count + 1):
                for cmd in ("TG", "MODE", "LIMITED", "ENABLE", "KP", "KI", "KD"):
                    self.submit("read", BY_COMMAND[cmd], ch)
        else:
            self.cancel_plan("连接已断开；设备输出状态未知")
            self.finish_recording()
            for card in self.cards:
                card.setText("— °C")
            self.readout.setText("— °C")
            self.details.setText("已断开 · 设备输出状态未知")
        self.refresh_stop_button()
        self.refresh_output_toggle()
        self.refresh_pid_controls()
        self.refresh_algorithm_controls()
        self.append_log(description)

    def on_finished(self):
        self.connect_btn.setEnabled(True)
        self.set_connection_fields(True)
        if self.closing_after_stop:
            self.close()

    def submit(self, operation, *args, **kwargs):
        if operation == "write" and self.algorithm_busy:
            self.append_log("算法正在接管输出，请先停止算法再手动写入")
            return False
        if (
            operation in ("host_start", "host_target", "host_config")
            and self.stop_state == "pending"
        ):
            return False
        if operation == "write" and self.stop_state == "pending":
            self.append_log("正在关闭全部输出，暂不接受参数写入")
            return False
        if self.worker and self.worker.isRunning():
            return self.worker.submit(operation, *args, **kwargs)
        self.append_log("未连接设备")
        return False

    def refresh_output_toggle(self):
        ch = self.channel.currentIndex() + 1
        state = self.latest.get(f"TC{ch}:ENABLE") if self.device_connected else None
        blocker = QtCore.QSignalBlocker(self.output_toggle)
        self.output_toggle.setChecked(state == 1)
        del blocker
        if not self.device_connected or state not in (0, 1):
            text = "输出状态未知"
        elif self.output_pending is not None:
            key, desired = self.output_pending
            channel = key.split(":")[0].replace("TC", "")
            text = f"通道 {channel}：正在{'开启' if desired else '关闭'}…"
        elif state:
            text = "● 输出已开启 · 点击关闭"
        else:
            text = "○ 输出已关闭 · 点击开启"
        self.output_toggle.setText(text)
        self.output_toggle.setEnabled(
            self.device_connected
            and state in (0, 1)
            and self.output_pending is None
            and self.stop_state != "pending"
            and not self.algorithm_busy
        )

    def toggle_channel_output(self):
        ch = self.channel.currentIndex() + 1
        key = f"TC{ch}:ENABLE"
        state = self.latest.get(key)
        # A checkable button changes immediately on click; restore the confirmed
        # state until the write transaction and readback have completed.
        self.refresh_output_toggle()
        if (
            not self.device_connected
            or state not in (0, 1)
            or self.output_pending is not None
            or self.stop_state == "pending"
        ):
            return
        desired = 1 - state
        if desired == 0 and self.stage_index is not None and self.plan_channel == ch:
            self.cancel_plan("当前通道请求关闭输出，分段计划已取消")
        self.output_pending = (key, desired)
        self.refresh_output_toggle()
        if not self.submit("write", BY_COMMAND["ENABLE"], ch, desired):
            self.output_pending = None
            self.refresh_output_toggle()

    def on_write_finished(self, key, success):
        if self.output_pending is not None and self.output_pending[0] == key:
            self.output_pending = None
            self.refresh_output_toggle()
            if not success:
                self.append_log("输出切换未确认，显示保留设备最近读回状态")

    def refresh_pid_controls(self):
        ch = self.channel.currentIndex() + 1
        available = (
            self.device_connected and self.stop_state != "pending" and not self.algorithm_busy
        )
        for command, field in self.pid_fields.items():
            field.setEnabled(available and f"TC{ch}:{command}" in self.latest)
        self.pid_read_btn.setEnabled(self.device_connected)
        self.pid_write_btn.setEnabled(
            available and all(f"TC{ch}:{cmd}" in self.latest for cmd in self.pid_fields)
        )

    def read_pid(self):
        ch = self.channel.currentIndex() + 1
        for command in self.pid_fields:
            self.loaded_editors.discard(f"TC{ch}:{command}")
            self.submit("read", BY_COMMAND[command], ch)

    def write_pid(self):
        if self.stage_index is not None:
            W.QMessageBox.information(self, "分段计划运行中", "请先取消计划，再修改 PID 参数。")
            return
        if not self.pid_write_btn.isEnabled():
            return
        for command, field in self.pid_fields.items():
            self.write_value(command, str(field.value()))

    def refresh_stop_button(self):
        if not self.device_connected:
            self.stop_state = "unknown"
        elif self.stop_state not in ("pending", "failed"):
            states = [self.latest.get(f"TC{ch}:ENABLE") for ch in range(1, self.channel_count + 1)]
            if all(state == 0 for state in states):
                self.stop_state = "stopped"
            elif self.stop_state != "failed":
                self.stop_state = "ready"
        labels = {
            "unknown": "关闭全部输出 · 状态未知",
            "ready": "关闭全部输出",
            "pending": "正在关闭全部输出…",
            "stopped": "✓ 全部输出已关闭",
            "failed": "关闭未确认 · 点击重试",
        }
        self.stop_btn.setText(labels[self.stop_state])
        self.stop_btn.setProperty("stopState", self.stop_state)
        self.stop_btn.setEnabled(self.device_connected and self.stop_state != "pending")
        self.stop_btn.style().unpolish(self.stop_btn)
        self.stop_btn.style().polish(self.stop_btn)
        self.stop_btn.update()

    def write_value(self, command, text):
        if self.stage_index is not None:
            W.QMessageBox.information(self, "分段计划运行中", "请先取消计划，再手动修改参数。")
            return
        p, ch = BY_COMMAND[command], self.channel.currentIndex() + 1
        try:
            value = encode_temperature_control(p, text)
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
        if self.stop_state == "failed" and all(
            f"TC{ch}:ENABLE" in values for ch in range(1, self.channel_count + 1)
        ):
            self.stop_state = "ready"  # A new complete sample supersedes failed/stale status.
        self.refresh_output_toggle()
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
        for command, widget in [
            ("TG", self.target),
            ("MODE", self.mode),
            ("LIMITED", self.limit),
            *self.pid_fields.items(),
        ]:
            key = f"TC{ch}:{command}"
            if key in values and key not in self.loaded_editors and not widget.hasFocus():
                self.loaded_editors.add(key)
                if command == "MODE":
                    widget.setCurrentIndex(values[key])
                else:
                    value = float(BY_COMMAND[command].display(values[key]))
                    if widget.minimum() <= value <= widget.maximum():
                        widget.setValue(int(value) if isinstance(widget, W.QSpinBox) else value)
                        widget.setToolTip("")
                    else:
                        widget.setToolTip(f"设备读回 {value}，超出文档可写范围；真实值见完整参数表")
                        self.append_log(
                            f"{key} 读回 {value} 超出文档可写范围；保留原值显示，不扩大写入范围"
                        )
        self.refresh_stop_button()
        self.refresh_pid_controls()
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
        for cmd in ("TG", "MODE", "LIMITED", "KP", "KI", "KD"):
            self.loaded_editors.discard(f"TC{self.channel.currentIndex() + 1}:{cmd}")
        self.on_values({})
        self.refresh_observation_stats()
        if self.worker and self.worker.isRunning():
            for cmd in ("TG", "MODE", "LIMITED", "KP", "KI", "KD"):
                key = f"TC{self.channel.currentIndex() + 1}:{cmd}"
                if key in self.latest:
                    self.on_values({key: self.latest[key]})

    def on_sample(self, epoch, monotonic, values):
        self.last_sample = time.monotonic()
        point = (epoch, monotonic, values)
        self.history.append(point)
        self.plot_history.append(point)
        for ch in range(1, self.channel_count + 1):
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
            self.cards[ch - 1].setText(f"{temp:.3f} °C" if math.isfinite(temp) else "传感器无效")
        ch = self.channel.currentIndex() + 1
        k = f"TC{ch}:"
        raw = values[k + "TCADJTEMP"]
        self.readout.setText(f"{raw / 100000:.3f} °C" if valid_temperature(raw) else "传感器无效")
        self.details.setText(
            f"目标 {values[k + 'TG'] / 100000:.3f} °C   ·   输出 {values[k + 'PWMDUTY'] / 20000:+.3f}%\n设备输出：{'开启' if values[k + 'ENABLE'] else '关闭'}   ·   内部 {values['SINTERIORTEMP']:.3f} °C"
        )
        demo = "模拟 · " if self.worker.config["port"] == "SIMULATOR" else ""
        self.state.setText(
            f"{demo}已连接 · {self.worker.config['port']} · 最近采样 {datetime.fromtimestamp(epoch).astimezone().strftime('%H:%M:%S')} · {decode_errors(values['ERRORCODE'])}"
        )
        self.update_plots()
        self.refresh_observation_stats()
        self.advance_plan(monotonic, values)

    def eventFilter(self, watched, event):
        if (
            hasattr(self, "algorithm_target")
            and watched is self.algorithm_target.lineEdit()
            and event.type() == QtCore.QEvent.Type.KeyPress
            and event.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter)
        ):
            self.algorithm_target.interpretText()
            self.apply_algorithm_target()
            return True
        if (
            watched is self.target.lineEdit()
            and event.type() == QtCore.QEvent.Type.KeyPress
            and event.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter)
        ):
            self.target.interpretText()
            self.target_btn.click()
            return True
        return super().eventFilter(watched, event)

    def create_window_controls(self, kind, title="", toggle=None):
        widget = W.QWidget()
        row = W.QHBoxLayout(widget)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(5)
        row.addWidget(toggle if toggle is not None else W.QLabel(title))
        label = W.QLabel(f"最近 {self.window_seconds[kind]} s")
        self.window_labels[kind] = label
        row.addWidget(label)
        self.window_buttons[kind] = {}
        for name, text, delta in (("minus", "−", -1), ("plus", "+", 1)):
            control = W.QToolButton()
            control.setObjectName("windowAdjust")
            control.setText(text)
            control.setAutoRepeat(True)
            control.setMaximumWidth(36)
            control.setToolTip("减少 1 秒" if delta < 0 else "增加 1 秒")
            control.clicked.connect(
                lambda _, kind=kind, delta=delta: self.adjust_window(kind, delta)
            )
            self.window_buttons[kind][name] = control
            row.addWidget(control)
        row.addStretch()
        self.window_buttons[kind]["minus"].setEnabled(self.window_seconds[kind] > 1)
        self.window_buttons[kind]["plus"].setEnabled(self.window_seconds[kind] < 3600)
        return widget

    def adjust_window(self, kind, delta):
        seconds = max(1, min(3600, self.window_seconds[kind] + delta))
        self.window_seconds[kind] = seconds
        self.settings.setValue(f"{kind}_window_seconds", seconds)
        self.window_labels[kind].setText(f"最近 {seconds} s")
        self.window_buttons[kind]["minus"].setEnabled(seconds > 1)
        self.window_buttons[kind]["plus"].setEnabled(seconds < 3600)
        if kind == "temperature":
            self.recent_plot.setLabel(
                "left",
                f"最近 {seconds} 秒",
                units="°C",
                **{"font-size": f"{self.font_size.value()}pt", "color": "#aebdd0"},
            )
        if kind == "stability":
            self.refresh_observation_stats()
        else:
            self.update_plots()

    def recent_points(self, seconds):
        if not self.plot_history:
            return []
        cutoff = self.plot_history[-1][1] - seconds
        result = []
        for point in reversed(self.plot_history):
            if point[1] < cutoff:
                break
            result.append(point)
        result.reverse()
        return result

    def toggle_recent_plot(self, enabled):
        self.recent_plot.setVisible(enabled)
        self.settings.setValue("recent_plot_enabled", enabled)
        self.update_plots()

    def update_curve_visibility(self):
        for index, toggle in enumerate(self.curve_toggles):
            visible = toggle.isChecked()
            toggle.setText(f"通道 {index + 1}：{'显示' if visible else '隐藏'}")
            for curves in (self.temp_curves, self.target_curves, self.recent_curves):
                curves[index].setVisible(visible)
            reference = self.reference_curves[index]
            reference.setVisible(
                visible and bool(reference.getData()[0] is not None and len(reference.getData()[0]))
            )
            for sample, label in self.plot.plotItem.legend.items:
                if sample.item is reference:
                    sample.setVisible(reference.isVisible())
                    label.setVisible(reference.isVisible())
                elif sample.item in (self.temp_curves[index], self.target_curves[index]):
                    sample.setVisible(visible)
                    label.setVisible(visible)
        for plot in (self.plot, self.recent_plot):
            plot.enableAutoRange(axis="y", enable=True)
            plot.getViewBox().updateAutoRange()

    def clear_plots(self):
        self.plot_history.clear()
        self.update_plots()
        self.append_log("图形已清空；CSV 记录、导出缓存、稳定性统计和设备控制均不受影响")

    def update_plots(self):
        points = self.plot_history
        recent = (
            self.recent_points(self.window_seconds["temperature"])
            if self.recent_enabled.isChecked()
            else []
        )
        output_points = self.recent_points(self.window_seconds["output"])
        for ch in (1, 2):
            k = f"TC{ch}:"
            data = [(t, v) for t, _, v in points if k + "TCADJTEMP" in v]
            x = [t for t, v in data]
            temperatures = [
                v[k + "TCADJTEMP"] / 100000 if valid_temperature(v[k + "TCADJTEMP"]) else math.nan
                for t, v in data
            ]
            self.temp_curves[ch - 1].setData(x, temperatures, connect="finite")
            self.target_curves[ch - 1].setData(x, [v[k + "TG"] / 100000 for t, v in data])
            references = [
                (t, v[k + "HOSTREFERENCE"]) for t, _, v in points if k + "HOSTREFERENCE" in v
            ]
            self.reference_curves[ch - 1].setData(
                [t for t, _ in references], [r for _, r in references]
            )
            output = [(t, v) for t, _, v in output_points if k + "PWMDUTY" in v]
            self.output_curves[ch - 1].setData(
                [t for t, v in output], [v[k + "PWMDUTY"] / 20000 for t, v in output]
            )
            short = [(t, v) for t, _, v in recent if k + "TCADJTEMP" in v]
            self.recent_curves[ch - 1].setData(
                [t for t, v in short],
                [
                    v[k + "TCADJTEMP"] / 100000
                    if valid_temperature(v[k + "TCADJTEMP"])
                    else math.nan
                    for t, v in short
                ],
                connect="finite",
            )
        self.update_curve_visibility()
        if points:
            first, last = points[0][0], points[-1][0]
            if last <= first:
                first, last = last - 0.5, last + 0.5
            self.plot.setXRange(first, last, padding=0.01)
            self.output_plot.setXRange(
                points[-1][0] - self.window_seconds["output"], points[-1][0], padding=0
            )
            self.recent_plot.setXRange(
                points[-1][0] - self.window_seconds["temperature"], points[-1][0], padding=0
            )
        else:
            for plot in (self.plot, self.output_plot, self.recent_plot):
                plot.enableAutoRange(axis="xy", enable=True)

    def update_stats(self, ch, now, target):
        QtCore.QTimer.singleShot(0, self.stability_overlay.fit_to_plot)
        k = f"TC{ch}:"
        points = []
        # Only the contiguous segment at the current device target is meaningful.
        for _, t, v in reversed(self.history):
            if (
                now - t > self.window_seconds["stability"]
                or v.get(k + "TG") != target
                or not valid_temperature(v.get(k + "TCADJTEMP", 999999999))
            ):
                break
            points.append((t, v[k + "TCADJTEMP"] / 100000))
        if len(points) < 2:
            self.stats.setText(f"最近 {self.window_seconds['stability']} s · 当前目标段\n数据不足")
            return
        temps = [v for t, v in points]
        errors = [v - target / 100000 for v in temps]
        self.stats.setText(
            f"最近 {points[0][0] - points[-1][0]:.1f} s · {len(points)} 点\n均温 {statistics.mean(temps):.3f} °C\n标准差 {statistics.pstdev(temps):.3f} °C\n峰峰值 {max(temps) - min(temps):.3f} °C\n最大绝对误差 {max(map(abs, errors)):.3f} °C\n±0.01 °C 内采样 {sum(abs(e) <= 0.01 for e in errors) / len(errors):.1%}"
        )

    def check_stale(self):
        if (
            self.worker
            and self.worker.isRunning()
            and self.last_sample
            and time.monotonic() - self.last_sample > max(3, self.interval.value() * 3)
        ):
            if self.algorithm_busy:
                self.stop_algorithm()
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
        if self.stop_state == "pending":
            return
        self.clear_algorithm_parameter_queue()
        self.stop_state = "pending"
        self.refresh_algorithm_controls()
        self.refresh_stop_button()
        self.refresh_output_toggle()
        self.refresh_pid_controls()
        self.cancel_plan("正在请求关闭全部输出")
        if self.worker and self.worker.isRunning():
            self.worker.stop_outputs()
            self.append_log("已清空待执行队列，优先关闭两路输出；等待设备读回确认")
        else:
            self.on_stopped(False)

    def on_stopped(self, ok):
        if ok:
            self.algorithm_busy = False
            self.algorithm_transition = None
            self.algorithm_failed_shutdown = False
        self.output_pending = None
        self.stop_state = "stopped" if ok else "failed"
        self.refresh_algorithm_controls()
        self.refresh_stop_button()
        self.refresh_output_toggle()
        self.refresh_pid_controls()
        if not self.closing_after_stop:
            self.state.setText(
                "全部输出已关闭，设备读回确认" if ok else "关闭输出未确认，请查看协议诊断或重试"
            )
        if self.closing_after_stop:
            if ok:
                self.submit("close", priority=0)
            else:
                self.closing_after_stop = False
                self.centralWidget().setEnabled(True)
                self.state.setText("未确认输出关闭；窗口保持打开，请检查设备")
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
        for col, text in enumerate(("40.00", "60", "0.050")):
            self.plan_table.setItem(row, col, W.QTableWidgetItem(text))

    def remove_step(self):
        if self.stage_index is None and self.plan_table.currentRow() >= 0:
            self.plan_table.removeRow(self.plan_table.currentRow())

    def start_plan(self):
        if self.algorithm_busy:
            self.plan_status.setText("请先停止上位机算法，再启动分段控温")
            return
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
                target = encode_temperature_control(BY_COMMAND["TG"], texts[0])
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
        # Commit a focused numeric editor before persisting/closing; this never
        # applies a target to hardware by itself.
        for widget in [self.algorithm_target, *self.algorithm_fields.values()]:
            if isinstance(widget, W.QAbstractSpinBox):
                widget.interpretText()
        if self.worker and self.worker.isRunning():
            event.ignore()
            if not self.closing_after_stop:
                self.closing_after_stop = True
                self.centralWidget().setEnabled(False)
                self.stop_outputs()
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
