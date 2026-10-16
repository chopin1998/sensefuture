"""Ramp PID plugin entry: controller implementation and parameter definition."""

import math

from PyQt6 import QtCore

from ..config import YamlSettings
from .base import Algorithm, Field


class RampPID:
    """PID on ramped reference, derivative on measurement, conditional integration."""

    def __init__(self, settings):
        self.settings = settings
        self.reset()

    def reconfigure(self, settings):
        self.settings = dict(settings)
        if not settings["use_i"]:
            self.integral = 0.0

    def reset(self):
        self.last_time = self.last_temp = self.reference = None
        self.integral = self.derivative = self.output = 0.0

    def step(self, now, temperature, target):
        if not all(map(math.isfinite, (now, temperature, target))):
            raise ValueError("算法输入不是有限数值")
        s = self.settings
        if self.last_time is None:
            self.last_time, self.last_temp, self.reference = now, temperature, temperature
            return 0.0
        dt = now - self.last_time
        if dt <= 0 or dt > s["max_gap"]:
            raise ValueError("采样间隔异常，控制已停止")
        delta = target - self.reference
        self.reference += max(-s["ramp"] * dt, min(s["ramp"] * dt, delta))
        error = self.reference - temperature
        rate = (temperature - self.last_temp) / dt
        self.derivative += dt / (s["filter"] + dt) * (rate - self.derivative)
        p = s["kp"] * error if s["use_p"] else 0
        d = -s["kd"] * self.derivative if s["use_d"] else 0
        old_i = self.integral
        self.integral = old_i + s["ki"] * error * dt if s["use_i"] else 0
        limit = s["limit"]
        lo, hi = -limit, limit
        if s["direction"] == "仅制热":
            lo = 0
        elif s["direction"] == "仅制冷":
            hi = 0
        if not s["allow_overshoot"]:
            # Predictive braking: suppress drive toward a target likely to be crossed.
            predicted = temperature + self.derivative * s["brake"]
            if target >= self.reference and predicted >= target:
                hi = min(hi, 0)
            if target <= self.reference and predicted <= target:
                lo = max(lo, 0)
        lo = max(lo, self.output - s["slew"] * dt)
        hi = min(hi, self.output + s["slew"] * dt)
        # Braking takes precedence over slew when their constraints cannot overlap.
        if lo > hi:
            lo = hi = 0
        raw = p + self.integral + d
        output = max(lo, min(hi, raw))
        if s["use_i"] and (raw - output) * error > 0:
            self.integral = old_i
            output = max(lo, min(hi, p + self.integral + d))
        self.integral = max(-limit, min(limit, self.integral))
        self.last_time, self.last_temp, self.output = now, temperature, output
        return -output if s["reverse"] else output


def open_configuration(root, report):
    """This plugin owns its filename, format, and migration policy."""
    folder = root / "algorithms"
    ini = folder / "ramp_pid.ini"
    legacy = QtCore.QSettings(str(ini), QtCore.QSettings.Format.IniFormat) if ini.exists() else None
    return YamlSettings(folder / "ramp_pid.yaml", report, legacy)


def initialize_panel(panel):
    panel.section("PID")
    for key in ("use_p", "kp", "use_i", "ki", "use_d", "kd", "filter"):
        panel.parameter(key)
    panel.section("跟踪")
    for key in ("allow_overshoot", "ramp", "slew", "brake"):
        panel.parameter(key)
    panel.section("方向与边界")
    for key in ("direction", "reverse", "min_temp", "max_temp", "max_gap"):
        panel.parameter(key)


PLUGIN = Algorithm(
    "ramp_pid",
    "斜坡 PID · v1",
    "目标斜坡、测量微分滤波、抗积分饱和。",
    (
        Field(
            "direction",
            "热控制方向",
            "制热 + 制冷",
            choices=("制热 + 制冷", "仅制热", "仅制冷"),
        ),
        Field("reverse", "反转输出", True),
        Field("allow_overshoot", "允许过冲", False, live=True),
        Field("use_p", "启用比例 P", True, live=True),
        Field("kp", "Kp (% / °C)", 5.0, 0, 1000, live=True),
        Field("use_i", "启用积分 I", True, live=True),
        Field("ki", "Ki (% / °C / s)", 0.1, 0, 100, live=True),
        Field("use_d", "启用微分 D", False, live=True),
        Field("kd", "Kd (% · s / °C)", 1.0, 0, 1000, live=True),
        Field("filter", "微分滤波时间 (s)", 1.0, 0.01, 60, live=True),
        Field("ramp", "目标变化速度 (°C / s)", 0.05, 0.001, 10, 0.001, live=True),
        Field("slew", "输出最大变化 (% / s)", 2.0, 0.01, 100, live=True),
        Field("brake", "预测制动时间 (s)", 2.0, 0, 120, live=True),
        Field("min_temp", "允许温度下限 (°C)", 0.0, -400, 1000),
        Field("max_temp", "允许温度上限 (°C)", 80.0, -400, 1000),
        Field("max_gap", "最大采样间隔 (s)", 3.0, 0.1, 30),
    ),
    RampPID,
    configuration_factory=open_configuration,
    panel_factory=initialize_panel,
)
