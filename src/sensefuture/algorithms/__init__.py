"""Explicit trusted plugin registry; one entry module per algorithm."""

from .base import Algorithm, Field, TrackingController
from .ramp_pid import PLUGIN, RampPID

ALGORITHMS = {PLUGIN.key: PLUGIN}

__all__ = ["ALGORITHMS", "Algorithm", "Field", "RampPID", "TrackingController"]
