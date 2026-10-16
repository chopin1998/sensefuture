import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6 import QtCore
from PyQt6 import QtWidgets as W
from PyQt6.QtTest import QTest

from sensefuture.app import Window, encode_temperature_control, valid_temperature
from sensefuture.catalog import BY_COMMAND
from sensefuture.worker import DeviceWorker


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = W.QApplication.instance() or W.QApplication([])
        QtCore.QSettings.setDefaultFormat(QtCore.QSettings.Format.IniFormat)
        cls.temp = tempfile.TemporaryDirectory()
        QtCore.QSettings.setPath(
            QtCore.QSettings.Format.IniFormat, QtCore.QSettings.Scope.UserScope, cls.temp.name
        )

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def pump(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Qt condition timed out")

    def setUp(self):
        self.config_temp = tempfile.TemporaryDirectory()
        self.config_env = patch.dict(os.environ, {"SENSEFUTURE_CONFIG_DIR": self.config_temp.name})
        self.config_env.start()
        settings = QtCore.QSettings("SenseFuture", "Workbench")
        settings.clear()
        settings.remove("output_window_seconds")
        settings.remove("temperature_window_seconds")
        settings.remove("stability_window_seconds")
        self.w = Window()
        self.w.port.setCurrentIndex(self.w.port.findData("SIMULATOR"))

    def tearDown(self):
        if self.w.worker and self.w.worker.isRunning():
            self.w.worker.submit("close", priority=0)
            self.pump(lambda: not self.w.worker.isRunning())
        self.app.processEvents()
        self.w.close()
        self.app.processEvents()
        self.config_env.stop()
        self.config_temp.cleanup()

    def test_global_and_algorithm_configuration_restore_without_device_actions(self):
        self.w.baud.setCurrentText("19200")
        self.w.protocol.setCurrentIndex(1)
        self.w.station.setValue(7)
        self.w.interval.setValue(0.8)
        self.w.curve_toggles[1].setChecked(False)
        self.w.algorithm_fields["kp"].setValue(12.34)
        self.w.algorithm_fields["reverse"].setChecked(False)
        self.w.algorithm_fields["direction"].setCurrentText("仅制热")
        self.w.algorithm_target.setValue(42.01)
        self.w.algorithm_channel.setCurrentIndex(1)
        root = self.w.configuration.root
        self.assertTrue((root / "global.yaml").is_file())
        self.assertTrue((root / "algorithms/ramp_pid.yaml").is_file())
        self.assertIn("kp: 12.34", (root / "algorithms/ramp_pid.yaml").read_text())
        other = Window()
        try:
            self.assertEqual(other.baud.currentText(), "19200")
            self.assertEqual(other.protocol.currentIndex(), 1)
            self.assertEqual(other.station.value(), 7)
            self.assertEqual(other.interval.value(), 0.8)
            self.assertFalse(other.curve_toggles[1].isChecked())
            self.assertEqual(other.algorithm_fields["kp"].value(), 12.34)
            self.assertFalse(other.algorithm_fields["reverse"].isChecked())
            self.assertEqual(other.algorithm_fields["direction"].currentText(), "仅制热")
            self.assertEqual(other.algorithm_target.value(), 42.01)
            self.assertEqual(other.algorithm_channel.currentIndex(), 1)
            self.assertIsNone(other.worker)
            self.assertFalse(other.algorithm_busy)
            self.assertNotIn("limit", other.algorithm_settings())
        finally:
            other.close()

    def test_each_algorithm_has_independent_parameters_and_target(self):
        from dataclasses import replace

        from sensefuture.algorithms import ALGORITHMS, Field

        spec = replace(
            ALGORITHMS["ramp_pid"],
            key="other",
            name="Other",
            fields=(Field("special", "Special", False),),
            configuration_factory=None,
            panel_factory=None,
        )
        with patch.dict(ALGORITHMS, {"other": spec}):
            self.w.algorithm_select.addItem(spec.name, spec.key)
            self.w.algorithm_fields["kp"].setValue(7.89)
            self.w.algorithm_target.setValue(42.01)
            self.w.algorithm_select.setCurrentIndex(1)
            self.assertNotIn("kp", self.w.algorithm_fields)
            self.w.algorithm_fields["special"].setChecked(True)
            self.w.algorithm_target.setValue(30.01)
            self.w.algorithm_select.setCurrentIndex(0)
            self.assertEqual(self.w.algorithm_fields["kp"].value(), 7.89)
            self.assertEqual(self.w.algorithm_target.value(), 42.01)
            self.w.algorithm_select.setCurrentIndex(1)
            self.assertTrue(self.w.algorithm_fields["special"].isChecked())
            self.assertEqual(self.w.algorithm_target.value(), 30.01)

    def test_connection_sample_parameter_read_and_output_stop(self):
        self.w.toggle_connection()
        self.pump(lambda: len(self.w.history) > 0)
        self.assertEqual(self.w.latest["TC1:ENABLE"], 0)
        self.assertIn("25.000", self.w.readout.text())
        self.w.submit("write", BY_COMMAND["ENABLE"], 1, 1)
        self.pump(lambda: self.w.latest.get("TC1:ENABLE") == 1)
        self.w.stop_outputs()
        self.pump(lambda: self.w.latest.get("TC1:ENABLE") == 0)
        self.assertTrue(valid_temperature(-1))
        self.assertFalse(valid_temperature(999999999))

    def test_csv_and_missing_sensor(self):
        self.w.toggle_connection()
        self.pump(lambda: len(self.w.rows) > 0)
        path = Path(self.temp.name) / "measurement.csv"
        with patch.object(W.QFileDialog, "getSaveFileName", return_value=(str(path), "CSV")):
            self.w.toggle_recording()
        start = len(self.w.rows)
        self.pump(lambda: len(self.w.rows) > start)
        self.w.finish_recording()
        self.assertIn("temperature_C", path.read_text(encoding="utf-8-sig"))
        self.assertIn("25.0", path.read_text(encoding="utf-8-sig"))
        data = dict(self.w.latest)
        data["TC1:TCADJTEMP"] = 999999999
        self.w.on_sample(time.time(), time.monotonic(), data)
        self.assertEqual(self.w.readout.text(), "传感器无效")
        self.assertEqual(self.w.rows[-2]["temperature_C"], "")

    def test_target_enter_writes_and_stability_window_filters(self):
        self.assertEqual(self.w.target.suffix(), "")
        self.w.target_btn.setEnabled(True)
        self.w.target.setValue(42.37)
        with patch.object(self.w, "write_value") as write:
            QTest.keyClick(self.w.target.lineEdit(), QtCore.Qt.Key.Key_Return)
            write.assert_called_once_with("TG", "42.37")
        self.w.history.extend(
            [
                (1000 + t, t, {"TC1:TG": 4000000, "TC1:TCADJTEMP": temp})
                for t, temp in ((0, 5000000), (10, 4000000), (20, 4000000))
            ]
        )
        self.w.latest["TC1:TG"] = 4000000
        self.w.update_stats(1, 20, 4000000)
        self.assertIn("2 点", self.w.stats.text())
        self.w.adjust_window("stability", 10)
        self.assertIn("3 点", self.w.stats.text())
        self.assertEqual(self.w.settings.value("stability_window_seconds", type=int), 25)

    def test_algorithm_page_simulator_ownership_target_and_stop(self):
        self.assertEqual(self.w.tabs.tabText(1), "算法控制")
        self.assertIn("kp", self.w.algorithm_fields)
        self.assertNotIn("limit", self.w.algorithm_fields)
        self.assertFalse(self.w.algorithm_toggle.isEnabled())
        self.w.toggle_connection()
        self.pump(lambda: bool(self.w.history))
        self.w.algorithm_toggle.click()
        self.assertFalse(self.w.algorithm_toggle.isChecked())
        self.assertFalse(self.w.algorithm_toggle.isEnabled())
        self.pump(lambda: len(self.w.algorithm_trace) >= 2)
        self.assertTrue(self.w.algorithm_toggle.isChecked())
        self.assertTrue(self.w.algorithm_busy)
        self.assertEqual(self.w.latest["TC1:MODE"], 3)
        self.assertEqual(self.w.latest["TC1:ENABLE"], 1)
        self.assertFalse(self.w.algorithm_fields["reverse"].isEnabled())
        self.assertTrue(self.w.algorithm_fields["kp"].isEnabled())
        self.assertFalse(self.w.target_btn.isEnabled())
        self.assertFalse(self.w.plan_start.isEnabled())
        self.assertFalse(self.w.submit("write", BY_COMMAND["TG"], 1, 5000000))
        self.w.algorithm_target.lineEdit().setText("41.23")
        QTest.keyClick(self.w.algorithm_target.lineEdit(), QtCore.Qt.Key.Key_Return)
        self.pump(lambda: self.w.latest.get("TC1:TG") == 4123000)
        self.w.algorithm_toggle.click()
        self.assertTrue(self.w.algorithm_toggle.isChecked())
        self.assertFalse(self.w.algorithm_toggle.isEnabled())
        self.pump(lambda: not self.w.algorithm_busy)
        self.assertFalse(self.w.algorithm_toggle.isChecked())
        self.assertEqual(self.w.latest["TC1:ENABLE"], 0)
        self.assertEqual(self.w.latest["TC1:PWMDUTY"], 0)
        self.assertTrue(self.w.algorithm_parameters.isEnabled())
        self.assertTrue(self.w.algorithm_toggle.isEnabled())

    def test_algorithm_toggle_pending_failure_and_retry(self):
        from unittest.mock import Mock

        worker = Mock()
        worker.isRunning.return_value = True
        worker.submit.return_value = True
        self.w.worker = worker
        self.w.device_connected = True
        self.w.stop_state = "ready"
        self.w.refresh_algorithm_controls()
        try:
            self.w.algorithm_toggle.click()
            self.assertEqual(self.w.algorithm_transition, "starting")
            self.assertFalse(self.w.algorithm_toggle.isChecked())
            self.w.on_algorithm_state({"state": "running", "message": "running"})
            self.assertTrue(self.w.algorithm_toggle.isChecked())
            self.w.algorithm_toggle.click()
            self.assertEqual(self.w.algorithm_transition, "stopping")
            self.w.on_algorithm_state({"state": "running", "message": "old sample"})
            self.assertEqual(self.w.algorithm_transition, "stopping")
            self.assertFalse(self.w.algorithm_toggle.isEnabled())
            self.w.on_algorithm_state({"state": "failed", "message": "shutdown failed"})
            self.assertTrue(self.w.algorithm_toggle.isChecked())
            self.assertIn("重试", self.w.algorithm_toggle.text())
            self.assertFalse(self.w.algorithm_apply.isEnabled())
            self.w.algorithm_toggle.click()
            self.assertEqual(worker.submit.call_args.args[0], "host_stop")
            self.w.on_algorithm_state({"state": "idle", "message": "off confirmed"})
            self.assertFalse(self.w.algorithm_toggle.isChecked())
            self.assertTrue(self.w.algorithm_toggle.isEnabled())
        finally:
            self.w.worker = None

    def test_chart_footer_explanation_removed(self):
        labels = [label.text() for label in self.w.findChildren(W.QLabel)]
        self.assertFalse(any("主图展示清空以来" in text for text in labels))

    def test_live_parameters_apply_in_worker_and_locked_options_stay_disabled(self):
        self.w.toggle_connection()
        self.pump(lambda: bool(self.w.history))
        acknowledgements = []
        self.w.worker.algorithm_parameters.connect(acknowledgements.append)
        self.w.start_algorithm()
        self.pump(lambda: bool(self.w.algorithm_trace))
        for key in ("reverse", "direction", "min_temp", "max_temp", "max_gap"):
            self.assertFalse(self.w.algorithm_fields[key].isEnabled())
        for key in ("kp", "ki", "kd", "use_p", "use_i", "use_d", "ramp", "slew", "allow_overshoot"):
            self.assertTrue(self.w.algorithm_fields[key].isEnabled())
        self.w.algorithm_fields["kp"].setValue(8.5)
        self.assertFalse(self.w.algorithm_fields["kp"].isEnabled())
        self.pump(
            lambda: any(
                result["accepted"] and result["values"]["kp"] == 8.5 for result in acknowledgements
            )
        )
        self.assertTrue(self.w.algorithm_fields["kp"].isEnabled())
        self.assertEqual(self.w.algorithm_config.value("kp", type=float), 8.5)
        self.w.worker.submit("host_config", {"reverse": False})
        self.pump(lambda: any(not result["accepted"] for result in acknowledgements))
        self.assertTrue(self.w.algorithm_busy)
        self.assertTrue(self.w.algorithm_fields["reverse"].isChecked())
        self.w.stop_algorithm()
        self.pump(lambda: not self.w.algorithm_busy)
        self.assertTrue(self.w.algorithm_fields["reverse"].isEnabled())

    def test_plugin_registers_its_own_controls_and_notes(self):
        from dataclasses import replace

        from sensefuture.algorithms import ALGORITHMS, Field

        initialized = []

        def initialize(panel):
            initialized.append(panel)
            panel.section("Custom")
            panel.note("Local description")
            widget = W.QCheckBox()
            panel.register("special", widget, widget.isChecked, widget.setChecked, widget.toggled)

        spec = replace(
            ALGORITHMS["ramp_pid"],
            key="custom_panel",
            name="Custom",
            fields=(Field("special", "Special", False),),
            configuration_factory=None,
            panel_factory=initialize,
        )
        with patch.dict(ALGORITHMS, {spec.key: spec}):
            self.w.algorithm_select.addItem(spec.name, spec.key)
            self.w.algorithm_select.setCurrentIndex(self.w.algorithm_select.findData(spec.key))
            self.assertEqual(initialized, [self.w.algorithm_panel])
            self.assertEqual(set(self.w.algorithm_fields), {"special"})
            self.w.algorithm_fields["special"].setChecked(True)
            self.assertTrue(self.w.algorithm_config.value("special"))
            self.assertIn(
                "Local description",
                [label.text() for label in self.w.algorithm_panel.findChildren(W.QLabel)],
            )

    def test_algorithm_stop_all_and_startup_failure(self):
        self.w.toggle_connection()
        self.pump(lambda: bool(self.w.history))
        self.w.submit("write", BY_COMMAND["ONSENSOR"], 1, 0)
        self.pump(lambda: self.w.latest.get("TC1:ONSENSOR") == 0)
        self.w.start_algorithm()
        self.pump(lambda: not self.w.algorithm_busy)
        self.assertIn("ONSENSOR", self.w.algorithm_status.text())
        self.assertEqual(self.w.latest["TC1:ENABLE"], 0)
        self.w.submit("write", BY_COMMAND["ONSENSOR"], 1, 1)
        self.pump(lambda: self.w.latest.get("TC1:ONSENSOR") == 1)
        self.w.start_algorithm()
        self.pump(lambda: bool(self.w.algorithm_trace))
        self.w.stop_outputs()
        self.pump(lambda: self.w.stop_state == "stopped" and not self.w.algorithm_busy)
        self.assertEqual(self.w.latest["TC1:ENABLE"], 0)
        self.assertEqual(self.w.latest["TC2:ENABLE"], 0)
        self.assertTrue(self.w.algorithm_toggle.isEnabled())

    def test_algorithm_immediate_stop_closes_preexisting_output(self):
        self.w.toggle_connection()
        self.pump(lambda: bool(self.w.history))
        self.w.submit("write", BY_COMMAND["ENABLE"], 1, 1)
        self.pump(lambda: self.w.latest.get("TC1:ENABLE") == 1)
        self.w.start_algorithm()
        self.w.stop_algorithm()
        self.pump(lambda: not self.w.algorithm_busy)
        self.assertEqual(self.w.latest["TC1:ENABLE"], 0)
        self.assertTrue(self.w.algorithm_toggle.isEnabled())

    def test_algorithm_exception_shuts_down_in_worker(self):
        self.w.toggle_connection()
        self.pump(lambda: bool(self.w.history))
        with patch(
            "sensefuture.algorithms.RampPID.step", side_effect=ValueError("test sample fault")
        ):
            self.w.start_algorithm()
            self.pump(lambda: not self.w.algorithm_busy)
        self.assertEqual(self.w.latest["TC1:ENABLE"], 0)
        self.assertIn("test sample fault", self.w.algorithm_status.text())
        self.assertTrue(self.w.algorithm_toggle.isEnabled())

    def test_cancel_plan_clears_pending_target(self):
        self.w.worker = DeviceWorker(dict(port="SIMULATOR", mode="ASCII"))
        self.w.worker.submit("write", BY_COMMAND["TG"], 1, 5000000)
        self.w.worker.start()
        self.pump(lambda: self.w.worker.isRunning())
        self.w.stage_index = 0
        self.w.cancel_plan("cancel")
        self.assertIsNone(self.w.stage_index)
        self.assertTrue(self.w.worker.commands.empty())

    def test_plan_continuous_dwell_and_completion(self):
        self.w.stage_index = 0
        self.w.plan_channel = 1
        self.w.active_plan = [(4000000, 2, 0.01)]
        self.w.stage_waiting = None
        data = {"TC1:TG": 4000000, "TC1:TCADJTEMP": 4000000, "TC1:ENABLE": 1, "ERRORCODE": 0}
        self.w.advance_plan(10, data)
        self.w.advance_plan(11, data)
        self.assertEqual(self.w.stage_index, 0)
        outside = dict(data)
        outside["TC1:TCADJTEMP"] = 4002000
        self.w.advance_plan(11.5, outside)
        self.assertIsNone(self.w.stage_since)
        self.w.advance_plan(12, data)
        self.w.advance_plan(14.1, data)
        self.assertIsNone(self.w.stage_index)
        self.assertIn("计划完成", self.w.plan_status.text())

    def test_poll_does_not_replace_pending_target_edit(self):
        self.w.on_values({"TC1:TG": 4000000})
        self.w.target.setValue(45.01)
        self.w.on_values({"TC1:TG": 4000000})
        self.assertAlmostEqual(self.w.target.value(), 45.01)

    def test_manual_mode_cancels_active_segment(self):
        self.w.stage_index = 0
        self.w.plan_channel = 1
        self.w.active_plan = [(4000000, 2, 0.01)]
        self.w.stage_waiting = None
        data = {
            "TC1:TG": 4000000,
            "TC1:TCADJTEMP": 4000000,
            "TC1:ENABLE": 1,
            "TC1:MODE": 3,
            "ERRORCODE": 0,
        }
        self.w.advance_plan(10, data)
        self.assertIsNone(self.w.stage_index)

    def plotting_sample(self, epoch):
        values = {"ERRORCODE": 0, "SINTERIORTEMP": 28}
        for ch in (1, 2):
            values.update(
                {
                    f"TC{ch}:TCADJTEMP": 2754567,
                    f"TC{ch}:TG": 2800000,
                    f"TC{ch}:PWMDUTY": 0,
                    f"TC{ch}:ENABLE": 0,
                    f"TC{ch}:RESISTOR": 9000000000,
                }
            )
        return (epoch, epoch, values)

    def test_control_tabs_share_observation_widgets_and_settings(self):
        for offset in (0, 95, 100):
            point = self.plotting_sample(1700000000 + offset)
            self.w.plot_history.append(point)
            self.w.history.append(point)
        self.w.update_plots()
        observation, plot, stats = self.w.observation, self.w.plot, self.w.stats_box
        self.w.tabs.setCurrentIndex(1)
        self.assertIs(observation.parentWidget(), self.w.algorithm_page)
        self.assertIs(stats.parentWidget(), self.w.stability_overlay)
        self.w.window_buttons["output"]["plus"].click()
        self.w.window_buttons["temperature"]["plus"].click()
        self.w.window_buttons["stability"]["plus"].click()
        self.w.curve_toggles[1].setChecked(False)
        self.w.recent_enabled.setChecked(False)
        self.w.tabs.setCurrentIndex(2)
        self.w.tabs.setCurrentIndex(0)
        self.assertIs(self.w.observation, observation)
        self.assertIs(self.w.plot, plot)
        self.assertIs(observation.parentWidget(), self.w.monitor_page)
        self.assertIs(stats.parentWidget(), self.w.stability_overlay)
        self.assertEqual(self.w.window_seconds, {"output": 6, "temperature": 6, "stability": 16})
        self.assertFalse(self.w.temp_curves[1].isVisible())
        self.assertTrue(self.w.recent_plot.isHidden())
        self.assertEqual(len(self.w.temp_curves[0].getData()[0]), 3)
        self.w.tabs.setCurrentIndex(1)
        self.w.clear_plot_action.trigger()
        self.w.tabs.setCurrentIndex(0)
        self.assertFalse(self.w.plot_history)
        self.assertEqual(len(self.w.history), 3)

    def test_stability_overlay_drag_bounds_and_shared_position(self):
        self.w.resize(1600, 1150)
        self.w.show()
        self.app.processEvents()
        overlay = self.w.stability_overlay
        self.assertIs(overlay.parentWidget(), self.w.plot.viewport())
        start = overlay.pos()
        handle = overlay.handle
        point = handle.rect().center()
        QTest.mousePress(handle, QtCore.Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseMove(handle, point + QtCore.QPoint(-150, 70))
        QTest.mouseRelease(handle, QtCore.Qt.MouseButton.LeftButton, pos=point)
        self.app.processEvents()
        self.assertNotEqual(overlay.pos(), start)
        saved = self.w.settings.value("stability_overlay_position")
        self.assertEqual(saved, overlay.anchor)
        self.w.tabs.setCurrentIndex(1)
        self.app.processEvents()
        self.assertIs(self.w.stats_box.parentWidget(), overlay)
        self.assertEqual(overlay.anchor, saved)
        self.w.window_buttons["stability"]["plus"].click()
        self.assertEqual(self.w.window_seconds["stability"], 16)
        self.w.resize(1150, 850)
        self.app.processEvents()
        overlay.fit_to_plot()
        self.assertTrue(overlay.parentWidget().rect().contains(overlay.geometry()))
        self.assertIs(self.w.recent_plot.parentWidget(), self.w.output_plot.parentWidget())

    def test_shared_stability_follows_visible_control_channel(self):
        for offset in (0, 1):
            point = self.plotting_sample(1700000000 + offset)
            point[2]["TC2:TCADJTEMP"] = 3000000
            self.w.history.append(point)
        self.w.refresh_observation_stats()
        self.assertIn("均温 27.546", self.w.stats.text())
        self.w.algorithm_channel.setCurrentIndex(1)
        self.w.tabs.setCurrentIndex(1)
        self.assertIn("均温 30.000", self.w.stats.text())
        self.w.tabs.setCurrentIndex(0)
        self.assertIn("均温 27.546", self.w.stats.text())

    def test_algorithm_reference_uses_shared_history_and_channel_visibility(self):
        point = self.plotting_sample(1700000000)
        self.w.plot_history.append(point)
        self.w.on_algorithm_state(
            {
                "state": "running",
                "message": "test",
                "channel": 2,
                "monotonic": point[1],
                "temperature": 27.5,
                "target": 28.0,
                "reference": 27.6,
                "output": 1.0,
            }
        )
        x, y = self.w.reference_curves[1].getData()
        self.assertEqual(list(x), [point[0]])
        self.assertEqual(list(y), [27.6])
        self.w.tabs.setCurrentIndex(1)
        self.w.curve_toggles[1].setChecked(False)
        self.assertFalse(self.w.reference_curves[1].isVisible())
        self.w.tabs.setCurrentIndex(0)
        self.w.curve_toggles[1].setChecked(True)
        self.assertTrue(self.w.reference_curves[1].isVisible())
        self.w.clear_plots()
        x, _ = self.w.reference_curves[1].getData()
        self.assertTrue(x is None or len(x) == 0)
        self.assertFalse(self.w.reference_curves[1].isVisible())

    def test_entire_history_and_recent_five_second_window(self):
        for offset in (0, 600, 995, 996, 1000):
            self.w.plot_history.append(self.plotting_sample(1700000000 + offset))
        self.w.recent_enabled.setChecked(True)
        self.w.update_plots()
        x, y = self.w.temp_curves[0].getData()
        self.assertEqual(len(x), 5)
        self.assertEqual(x[0], 1700000000)
        short_x, _ = self.w.recent_curves[0].getData()
        self.assertEqual(list(short_x), [1700000995, 1700000996, 1700001000])
        self.assertAlmostEqual(
            self.w.recent_plot.viewRange()[0][1] - self.w.recent_plot.viewRange()[0][0], 5
        )
        self.assertLessEqual(self.w.plot.viewRange()[0][0], x[0])
        self.assertGreaterEqual(self.w.plot.viewRange()[0][1], x[-1])
        output_x, _ = self.w.output_curves[0].getData()
        self.assertEqual(list(output_x), [1700000995, 1700000996, 1700001000])
        self.assertAlmostEqual(
            self.w.output_plot.viewRange()[0][1] - self.w.output_plot.viewRange()[0][0], 5
        )
        self.w.curve_toggles[0].setChecked(False)
        self.assertFalse(self.w.temp_curves[0].isVisible())
        self.assertFalse(self.w.target_curves[0].isVisible())
        self.assertFalse(self.w.recent_curves[0].isVisible())
        self.assertTrue(self.w.temp_curves[1].isVisible())
        self.w.curve_toggles[0].setChecked(True)
        self.assertTrue(self.w.temp_curves[0].isVisible())
        self.w.recent_enabled.setChecked(False)
        self.assertTrue(self.w.recent_plot.isHidden())

    def test_click_curve_switches_survive_new_data_and_header_layout(self):
        self.w.resize(1600, 1150)
        self.w.show()
        self.app.processEvents()
        self.w.plot_history.append(self.plotting_sample(1700000000))
        self.w.update_plots()
        self.assertIsNone(self.w.findChild(W.QStatusBar))
        self.assertFalse(hasattr(self.w, "channels"))
        self.assertEqual(self.w.port.parentWidget(), self.w.connect_btn.parentWidget())
        self.assertEqual(self.w.port.parentWidget(), self.w.station.parentWidget())
        QTest.mouseClick(self.w.curve_toggles[1], QtCore.Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertIn("隐藏", self.w.curve_toggles[1].text())
        self.assertFalse(self.w.temp_curves[1].isVisible())
        self.assertFalse(self.w.target_curves[1].isVisible())
        self.assertFalse(self.w.recent_curves[1].isVisible())
        self.w.plot_history.append(self.plotting_sample(1700000001))
        self.w.update_plots()
        self.assertFalse(self.w.temp_curves[1].isVisible())
        self.assertTrue(self.w.temp_curves[0].isVisible())
        for sample, label in self.w.plot.plotItem.legend.items:
            if sample.item is self.w.temp_curves[1]:
                self.assertFalse(label.isVisible())
        QTest.mouseClick(self.w.curve_toggles[1], QtCore.Qt.MouseButton.LeftButton)
        self.assertTrue(self.w.temp_curves[1].isVisible())
        self.assertIn("显示", self.w.curve_toggles[1].text())

    def test_plot_time_windows_are_independent_adjustable_and_persistent(self):
        for offset in (0, 90, 95, 96, 100):
            self.w.plot_history.append(self.plotting_sample(1700000000 + offset))
        self.w.recent_enabled.setChecked(True)
        self.w.update_plots()
        self.assertEqual(self.w.window_seconds, {"output": 5, "temperature": 5, "stability": 15})
        self.w.window_buttons["output"]["plus"].click()
        self.w.window_buttons["temperature"]["minus"].click()
        self.assertEqual(self.w.window_seconds, {"output": 6, "temperature": 4, "stability": 15})
        self.assertIn("6 s", self.w.window_labels["output"].text())
        self.assertIn("4 s", self.w.window_labels["temperature"].text())
        self.assertEqual(
            list(self.w.output_curves[0].getData()[0]), [1700000095, 1700000096, 1700000100]
        )
        self.assertEqual(list(self.w.recent_curves[0].getData()[0]), [1700000096, 1700000100])
        self.assertEqual(len(self.w.temp_curves[0].getData()[0]), 5)
        self.assertAlmostEqual(
            self.w.output_plot.viewRange()[0][1] - self.w.output_plot.viewRange()[0][0], 6
        )
        self.assertAlmostEqual(
            self.w.recent_plot.viewRange()[0][1] - self.w.recent_plot.viewRange()[0][0], 4
        )
        other = Window()
        try:
            self.assertEqual(other.window_seconds, {"output": 6, "temperature": 4, "stability": 15})
        finally:
            other.close()
        self.w.adjust_window("output", -10000)
        self.assertEqual(self.w.window_seconds["output"], 1)
        self.assertFalse(self.w.window_buttons["output"]["minus"].isEnabled())
        self.w.adjust_window("output", 10000)
        self.assertEqual(self.w.window_seconds["output"], 3600)
        self.assertFalse(self.w.window_buttons["output"]["plus"].isEnabled())

    def test_clear_chart_preserves_statistics_csv_and_control(self):
        point = self.plotting_sample(1700000000)
        self.w.history.append(point)
        self.w.plot_history.append(point)
        self.w.rows.append({"temperature_C": 27.54567})
        self.w.latest = {"TC1:ENABLE": 1}
        marker = object()
        self.w.record_writer = marker
        self.w.clear_plot_action.trigger()
        self.assertFalse(self.w.plot_history)
        self.assertEqual(len(self.w.history), 1)
        self.assertEqual(len(self.w.rows), 1)
        self.assertEqual(self.w.latest["TC1:ENABLE"], 1)
        self.assertIs(self.w.record_writer, marker)
        x, _ = self.w.temp_curves[0].getData()
        self.assertTrue(x is None or len(x) == 0)
        self.w.record_writer = None

    def test_temperature_display_and_control_precision(self):
        self.assertEqual(self.w.target.decimals(), 2)
        self.assertEqual(BY_COMMAND["TCADJTEMP"].display(2754567), "27.546")
        self.assertEqual(self.w.plot.getAxis("left").tickStrings([27.54567], 1, 0.01), ["27.546"])
        self.assertEqual(encode_temperature_control(BY_COMMAND["TG"], "40.01"), 4001000)
        with self.assertRaises(ValueError):
            encode_temperature_control(BY_COMMAND["TG"], "40.001")
        # The protocol API retains its full wire resolution; only manual control is constrained.
        self.assertEqual(BY_COMMAND["TG"].encode("40.00001"), 4000001)

    def test_output_toggle_waits_for_readback_and_tracks_channel(self):
        self.w.show()
        self.w.toggle_connection()
        self.pump(lambda: self.w.latest.get("TC1:ENABLE") == 0 and self.w.output_toggle.isEnabled())
        QTest.mouseClick(self.w.output_toggle, QtCore.Qt.MouseButton.LeftButton)
        self.assertFalse(self.w.output_toggle.isChecked())
        self.assertFalse(self.w.output_toggle.isEnabled())
        self.assertIn("正在开启", self.w.output_toggle.text())
        self.pump(lambda: self.w.output_toggle.isChecked() and self.w.output_toggle.isEnabled())
        self.assertEqual(self.w.latest["TC1:ENABLE"], 1)
        self.w.channel.setCurrentIndex(1)
        self.assertFalse(self.w.output_toggle.isChecked())
        self.w.channel.setCurrentIndex(0)
        self.assertTrue(self.w.output_toggle.isChecked())
        QTest.mouseClick(self.w.output_toggle, QtCore.Qt.MouseButton.LeftButton)
        self.assertTrue(self.w.output_toggle.isChecked())
        self.pump(lambda: not self.w.output_toggle.isChecked() and self.w.output_toggle.isEnabled())
        self.assertEqual(self.w.latest["TC1:ENABLE"], 0)

    def test_output_toggle_failure_and_disconnect_do_not_claim_success(self):
        self.w.device_connected = True
        self.w.on_values({"TC1:ENABLE": 0})
        with patch.object(self.w, "submit", return_value=True):
            self.w.toggle_channel_output()
        self.assertIsNotNone(self.w.output_pending)
        self.w.on_write_finished("TC1:ENABLE", False)
        self.assertFalse(self.w.output_toggle.isChecked())
        self.assertTrue(self.w.output_toggle.isEnabled())
        self.w.on_connection(False, "test disconnect")
        self.assertFalse(self.w.output_toggle.isEnabled())
        self.assertEqual(self.w.output_toggle.text(), "输出状态未知")

    def test_exit_silently_stops_both_outputs_before_closing(self):
        self.w.show()
        self.w.toggle_connection()
        self.pump(lambda: self.w.latest.get("TC2:ENABLE") == 0)
        self.w.submit("write", BY_COMMAND["ENABLE"], 1, 1)
        self.w.submit("write", BY_COMMAND["ENABLE"], 2, 1)
        self.pump(
            lambda: self.w.latest.get("TC1:ENABLE") == 1 and self.w.latest.get("TC2:ENABLE") == 1
        )
        with patch.object(W.QMessageBox, "question") as question:
            self.w.close()
            self.w.close()  # Repeated close must not restart shutdown.
            self.pump(lambda: not self.w.worker.isRunning() and not self.w.isVisible())
            question.assert_not_called()
        self.assertEqual(self.w.latest["TC1:ENABLE"], 0)
        self.assertEqual(self.w.latest["TC2:ENABLE"], 0)

    def test_exit_failure_has_no_dialog_and_keeps_window_open(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        self.w.show()
        stop = Mock()
        self.w.worker = SimpleNamespace(isRunning=lambda: True, stop_outputs=stop)
        with patch.object(W.QMessageBox, "question") as question:
            self.w.close()
            self.w.close()
            self.assertEqual(stop.call_count, 1)
            self.w.on_stopped(False)
            question.assert_not_called()
        self.assertFalse(self.w.closing_after_stop)
        self.assertTrue(self.w.isVisible())
        self.assertTrue(self.w.centralWidget().isEnabled())
        self.assertIn("未确认输出关闭", self.w.state.text())
        self.w.worker = None

    def test_pid_panel_reads_writes_and_follows_selected_channel(self):
        self.w.toggle_connection()
        self.pump(lambda: self.w.pid_write_btn.isEnabled() and "TC2:KD" in self.w.latest)
        self.assertEqual([self.w.pid_fields[c].value() for c in ("KP", "KI", "KD")], [3000, 150, 0])
        for cmd, value in (("KP", 2345), ("KI", 101), ("KD", 7)):
            self.w.pid_fields[cmd].setValue(value)
        self.w.pid_write_btn.click()
        self.pump(
            lambda: all(
                self.w.latest.get(f"TC1:{cmd}") == value
                for cmd, value in (("KP", 2345), ("KI", 101), ("KD", 7))
            )
        )
        self.w.channel.setCurrentIndex(1)
        self.assertEqual(self.w.pid_fields["KP"].value(), 3000)
        self.w.channel.setCurrentIndex(0)
        self.assertEqual(self.w.pid_fields["KP"].value(), 2345)
        self.w.submit("write", BY_COMMAND["KP"], 1, 9876)
        self.pump(lambda: self.w.latest.get("TC1:KP") == 9876)
        self.w.pid_read_btn.click()
        self.pump(lambda: self.w.pid_fields["KP"].value() == 9876)

    def test_stop_all_button_confirms_both_channels_and_changes_state(self):
        self.w.show()
        self.w.toggle_connection()
        self.pump(lambda: self.w.latest.get("TC2:ENABLE") == 0)
        self.w.submit("write", BY_COMMAND["ENABLE"], 1, 1)
        self.w.submit("write", BY_COMMAND["ENABLE"], 2, 1)
        self.pump(lambda: all(self.w.latest.get(f"TC{ch}:ENABLE") == 1 for ch in (1, 2)))
        self.assertEqual(self.w.stop_btn.property("stopState"), "ready")
        QTest.mouseClick(self.w.stop_btn, QtCore.Qt.MouseButton.LeftButton)
        self.assertEqual(self.w.stop_btn.property("stopState"), "pending")
        self.assertFalse(self.w.stop_btn.isEnabled())
        self.assertFalse(self.w.output_toggle.isEnabled())
        self.assertFalse(self.w.submit("write", BY_COMMAND["ENABLE"], 1, 1))
        self.pump(lambda: self.w.stop_btn.property("stopState") == "stopped")
        self.assertEqual([self.w.latest[f"TC{ch}:ENABLE"] for ch in (1, 2)], [0, 0])
        self.assertIn("全部输出已关闭", self.w.stop_btn.text())
        self.assertFalse(self.w.output_toggle.isChecked())

    def test_stop_failure_is_not_displayed_as_success(self):
        self.w.device_connected = True
        self.w.on_values({"TC1:ENABLE": 0, "TC2:ENABLE": 0})
        self.w.stop_state = "pending"
        self.w.on_stopped(False)
        self.assertEqual(self.w.stop_btn.property("stopState"), "failed")
        self.assertIn("重试", self.w.stop_btn.text())
        self.assertTrue(self.w.stop_btn.isEnabled())
        self.w.on_values({"TC1:ENABLE": 0, "TC2:ENABLE": 0})
        self.assertEqual(self.w.stop_btn.property("stopState"), "stopped")

    def test_worker_failure_is_disconnected_without_retry(self):
        class Broken:
            def __init__(self, *a, **k):
                raise OSError("test unplugged")

        events = []
        worker = DeviceWorker(dict(port="test"))
        worker.connected.connect(lambda state, text: events.append(state))
        with patch("sensefuture.worker.Client", Broken):
            worker.start()
            self.pump(lambda: not worker.isRunning())
        self.app.processEvents()
        self.assertEqual(events, [False])


if __name__ == "__main__":
    unittest.main()
