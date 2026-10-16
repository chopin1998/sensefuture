"""Reserved proposal contract for future cascaded/external-sensor control.

Direct-output tracking plugins are implemented in the algorithms package and run by DeviceWorker."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Measurement:
    monotonic: float
    channel: int
    temperature_c: float
    target_c: float
    output_percent: float
    valid: bool


@dataclass(frozen=True)
class ControlProposal:
    channel: int
    target_c: float | None = None
    output_percent: float | None = None
    reason: str = ""


class SupervisoryController(Protocol):
    def reset(self) -> None: ...
    def propose(self, measurement: Measurement) -> ControlProposal | None: ...
