"""Registration surface for plugin-owned controls and descriptions."""

import math

from PyQt6 import QtCore
from PyQt6 import QtWidgets as W


class AlgorithmPanel(W.QWidget):
    changed = QtCore.pyqtSignal(str, object)

    def __init__(self, spec, store, report, parent=None):
        super().__init__(parent)
        self.spec, self.store, self.report = spec, store, report
        self.fields = {field.key: field for field in spec.fields}
        self.widgets, self.bindings, self.live = {}, {}, {}
        self.pending = set()
        self.running = self.active = False
        self.layout_root = W.QVBoxLayout(self)
        self.layout_root.setContentsMargins(0, 0, 0, 0)
        self.form = None

    def section(self, title):
        group = W.QGroupBox(title)
        self.form = W.QFormLayout(group)
        self.form.setRowWrapPolicy(W.QFormLayout.RowWrapPolicy.WrapLongRows)
        self.layout_root.addWidget(group)

    def note(self, text):
        label = W.QLabel(text)
        label.setWordWrap(True)
        self.layout_root.addWidget(label)
        return label

    def parameter(self, key):
        field = self.fields[key]
        if field.choices:
            widget = W.QComboBox()
            widget.addItems(field.choices)
            getter, setter, signal = (
                widget.currentText,
                widget.setCurrentText,
                widget.currentTextChanged,
            )
        elif isinstance(field.default, bool):
            widget = W.QCheckBox()
            getter, setter, signal = widget.isChecked, widget.setChecked, widget.toggled
        elif isinstance(field.default, str):
            widget = W.QLineEdit()
            getter, setter, signal = widget.text, widget.setText, widget.editingFinished
        elif isinstance(field.default, int):
            widget = W.QSpinBox()
            widget.setRange(int(field.minimum), int(field.maximum))
            widget.setSingleStep(max(1, int(field.step)))
            widget.setKeyboardTracking(False)
            getter, setter, signal = widget.value, widget.setValue, widget.valueChanged
        else:
            widget = W.QDoubleSpinBox()
            widget.setRange(field.minimum, field.maximum)
            widget.setDecimals(3)
            widget.setSingleStep(field.step)
            widget.setKeyboardTracking(False)
            getter, setter, signal = widget.value, widget.setValue, widget.valueChanged
        self.register(key, widget, getter, setter, signal)
        return widget

    def register(self, key, widget, getter, setter, signal, *, live=None):
        """Custom widgets use the same configuration, pending, and runtime contract."""
        if key in self.widgets:
            raise ValueError(f"重复控件：{key}")
        field = self.fields[key]
        value = self.store.value(key, field.default, type=type(field.default))
        if (field.choices and value not in field.choices) or (
            not isinstance(field.default, (bool, str))
            and (not math.isfinite(value) or not field.minimum <= value <= field.maximum)
        ):
            self.report(f"算法参数无效：{self.spec.key}/{key}")
            value = field.default
        blocker = QtCore.QSignalBlocker(widget)
        setter(value)
        del blocker
        if not self.store.contains(key):
            self.store.setValue(key, value)
        self.widgets[key] = widget
        self.bindings[key] = (getter, setter)
        self.live[key] = field.live if live is None else bool(live and field.live)
        if not self.live[key]:
            widget.setToolTip("运行时锁定")
        if self.form is None:
            self.section("参数")
        self.form.addRow(field.label, widget)
        signal.connect(lambda *_: self._changed(key))
        self.refresh()
        return widget

    def _changed(self, key):
        value = self.bindings[key][0]()
        self.store.setValue(key, value)
        self.changed.emit(key, value)

    def values(self):
        return {key: getter() for key, (getter, _) in self.bindings.items()}

    def set_values(self, values):
        for key, value in values.items():
            if key in self.widgets:
                blocker = QtCore.QSignalBlocker(self.widgets[key])
                self.bindings[key][1](value)
                del blocker
                self.store.setValue(key, value)

    def set_state(self, running, active):
        self.running, self.active = running, active
        self.refresh()

    def refresh(self):
        for key, widget in self.widgets.items():
            widget.setEnabled(
                key not in self.pending and (not self.running or (self.active and self.live[key]))
            )
