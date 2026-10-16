import unittest
from unittest.mock import patch

from sensefuture.algorithms import ALGORITHMS, RampPID
from sensefuture.device import Client
from sensefuture.worker import DeviceWorker


class AlgorithmTests(unittest.TestCase):
    def setUp(self):
        self.spec = ALGORITHMS["ramp_pid"]
        self.settings = self.spec.defaults()

    def controller(self, **settings):
        return RampPID(self.settings | {"reverse": False, "limit": 30} | settings)

    def test_ramp_slew_limit_and_direction(self):
        pid = self.controller(ramp=10, kp=100, use_i=False, slew=2, limit=5)
        self.assertEqual(pid.step(0, 25, 40), 0)
        self.assertEqual(pid.step(1, 25, 40), 2)
        self.assertEqual(pid.reference, 35)
        self.assertEqual(pid.step(2, 25, 40), 4)
        self.assertEqual(pid.step(3, 25, 40), 5)
        for direction, target in (("仅制热", 10), ("仅制冷", 40)):
            pid = self.controller(direction=direction)
            pid.step(0, 25, target)
            self.assertEqual(pid.step(1, 25, target), 0)
        pid = self.controller(reverse=True)
        pid.step(0, 25, 40)
        self.assertLess(pid.step(1, 25, 40), 0)

    def test_default_stage_polarity_and_override(self):
        self.assertTrue(self.settings["reverse"])
        for target, expected_sign in ((42, -1), (20, 1)):
            pid = RampPID(self.settings | {"limit": 30})
            pid.step(0, 25, target)
            self.assertGreater(pid.step(1, 25, target) * expected_sign, 0)
            pid = self.controller(reverse=False)
            pid.step(0, 25, target)
            self.assertLess(pid.step(1, 25, target) * expected_sign, 0)

    def test_anti_windup_and_term_switches(self):
        pid = self.controller(ramp=10, kp=100, ki=10, limit=1)
        for i in range(100):
            pid.step(i, 25, 40)
        self.assertEqual(pid.integral, 0)
        self.assertLessEqual(pid.output, 1)
        pid = self.controller(use_p=False, use_i=False, use_d=False)
        pid.step(0, 25, 40)
        self.assertEqual(pid.step(1, 25, 40), 0)

    def test_braking_and_derivative_on_measurement(self):
        pid = self.controller(ramp=10, brake=2, filter=0.01, use_i=False)
        pid.step(0, 39, 40)
        self.assertEqual(pid.step(1, 39.9, 40), 0)
        pid = self.controller(use_p=False, use_i=False, use_d=True)
        pid.step(0, 25, 40)
        self.assertEqual(pid.step(1, 25, 50), 0)
        pid = self.controller(allow_overshoot=True, ramp=10, use_i=False)
        pid.step(0, 39, 40)
        self.assertGreater(pid.step(1, 39.9, 40), 0)

    def test_invalid_parameters_and_sample_gap(self):
        for change in (
            {"kp": float("nan")},
            {"slew": 101},
            {"direction": "unknown"},
            {"min_temp": 90},
            {"use_i": 1},
        ):
            with self.assertRaises(ValueError):
                self.spec.validate(self.settings | change)
        for now, temp in ((0, 25), (4, 25), (1, float("nan"))):
            pid = self.controller()
            pid.step(0, 25, 40)
            with self.assertRaises(ValueError):
                pid.step(now, temp, 40)


class HostWorkerTests(unittest.TestCase):
    def setUp(self):
        self.client = Client("SIMULATOR")
        self.worker = DeviceWorker({"port": "SIMULATOR", "mode": "ASCII"})
        self.settings = ALGORITHMS["ramp_pid"].defaults()
        self.states = []
        self.worker.algorithm_state.connect(self.states.append)

    def tearDown(self):
        self.worker.stop_algorithm(self.client, "test cleanup")
        self.client.close()

    def read(self, command):
        from sensefuture.catalog import BY_COMMAND

        return self.client.read(BY_COMMAND[command], 1)

    def test_start_control_target_and_confirmed_stop(self):
        self.worker.start_algorithm(self.client, "ramp_pid", 1, 40, self.settings)
        self.assertEqual(self.read("MODE"), 3)
        self.assertEqual(self.read("ENABLE"), 1)
        self.assertEqual(self.read("PWMDUTY"), 0)
        self.worker.control_sample(self.client, 10, self.client.sample())
        self.worker.control_sample(self.client, 11, self.client.sample())
        self.assertLess(self.read("PWMDUTY"), 0)
        self.assertIn("reference", self.states[-1])
        self.worker.stop_algorithm(self.client, "stopped")
        self.assertIsNone(self.worker.controller)
        self.assertEqual(self.read("ENABLE"), 0)
        self.assertEqual(self.states[-1]["state"], "idle")

    def test_output_limit_is_inherited_without_hardware_limit_writes(self):
        self.assertNotIn("limit", self.settings)
        original_read = self.client.read
        for limit in (0, 7, 90, 100):

            def read(parameter, channel=1):
                return (
                    limit if parameter.command == "LIMITED" else original_read(parameter, channel)
                )

            with (
                patch.object(self.client, "read", side_effect=read),
                patch.object(self.client, "write", wraps=self.client.write) as writes,
            ):
                self.worker.start_algorithm(self.client, "ramp_pid", 1, 40, self.settings)
                self.assertEqual(self.worker.host_settings["limit"], limit)
                self.assertEqual(self.worker.controller.settings["limit"], limit)
                self.assertFalse(
                    any(call.args[0].command == "LIMITED" for call in writes.call_args_list)
                )
                self.worker.stop_algorithm(self.client, "test stop")

    def test_fault_mode_target_sensor_and_boundary_rejected(self):
        self.worker.start_algorithm(self.client, "ramp_pid", 1, 40, self.settings)
        original = self.client.sample()
        for change in (
            {"ERRORCODE": 1},
            {"TC1:MODE": 0},
            {"TC1:ENABLE": 0},
            {"TC1:TCADJTEMP": 999999999},
            {"TC1:TG": 4100000},
            {"TC1:TCADJTEMP": 8100000},
        ):
            with self.assertRaises(ValueError):
                self.worker.control_sample(self.client, 10, original | change)
        self.assertEqual(self.read("PWMDUTY"), 0)

    def test_worker_rejects_unsafe_plugin_output(self):
        self.worker.start_algorithm(self.client, "ramp_pid", 1, 40, self.settings)
        for output in (float("nan"), 31.0):
            with patch.object(self.worker.controller, "step", return_value=output):
                with self.assertRaises(ValueError):
                    self.worker.control_sample(self.client, 10, self.client.sample())
        self.assertEqual(self.read("PWMDUTY"), 0)

    def test_live_reconfiguration_preserves_tracking_and_hardware_limit(self):
        self.worker.start_algorithm(self.client, "ramp_pid", 1, 40, self.settings)
        controller = self.worker.controller
        controller.step(10, 25, 40)
        controller.step(11, 25, 40)
        reference, last_time = controller.reference, controller.last_time
        controller.integral = 0.7
        values = self.worker.update_algorithm_settings({"kp": 8.5, "use_i": False, "ramp": 0.1})
        self.assertEqual(values["kp"], 8.5)
        self.assertEqual(controller.settings["kp"], 8.5)
        self.assertEqual(controller.integral, 0)
        self.assertEqual(controller.reference, reference)
        self.assertEqual(controller.last_time, last_time)
        self.assertEqual(controller.settings["limit"], 30)
        for changes in ({"reverse": False}, {"limit": 100}, {"kp": float("nan")}, {"unknown": 1}):
            previous = dict(controller.settings)
            with self.assertRaises(ValueError):
                self.worker.update_algorithm_settings(changes)
            self.assertEqual(controller.settings, previous)
            self.assertEqual(self.read("ENABLE"), 1)

    def test_failed_shutdown_is_not_reported_as_confirmed(self):
        self.worker.start_algorithm(self.client, "ramp_pid", 1, 40, self.settings)
        with patch.object(self.client, "write", side_effect=OSError("disconnected")):
            self.worker.stop_algorithm(self.client, "stopped")
        self.assertIsNone(self.worker.controller)
        self.assertEqual(self.states[-1]["state"], "failed")
        self.assertIn("未确认", self.states[-1]["message"])
