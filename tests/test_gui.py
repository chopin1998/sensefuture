import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt6 import QtCore
from PyQt6 import QtWidgets as W

from sensefuture.app import Window, valid_temperature
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
        self.w = Window()
        self.w.port.setCurrentIndex(self.w.port.findData("SIMULATOR"))

    def tearDown(self):
        if self.w.worker and self.w.worker.isRunning():
            self.w.worker.submit("close", priority=0)
            self.pump(lambda: not self.w.worker.isRunning())
        self.app.processEvents()
        self.w.close()
        self.app.processEvents()

    def test_connection_sample_parameter_read_and_output_stop(self):
        self.w.toggle_connection()
        self.pump(lambda: len(self.w.history) > 0)
        self.assertEqual(self.w.latest["TC1:ENABLE"], 0)
        self.assertIn("25.00000", self.w.readout.text())
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
