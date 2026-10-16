"""Shared algorithm metadata and controller contract."""

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    default: float | bool | str
    minimum: float = 0
    maximum: float = 1000
    step: float = 0.01
    choices: tuple[str, ...] = ()
    live: bool = False


class TrackingController(Protocol):
    reference: float | None

    def reset(self) -> None: ...
    def reconfigure(self, settings: dict) -> None: ...
    def step(self, now: float, temperature: float, target: float) -> float: ...


class ParameterStore(Protocol):
    def value(self, key, defaultValue=None, type=None): ...
    def setValue(self, key, value): ...
    def contains(self, key): ...


class MemorySettings:
    """Optional nonpersistent backend for plugins that do not choose disk storage."""

    def __init__(self):
        self.data = {}

    def value(self, key, defaultValue=None, type=None):
        value = self.data.get(key, defaultValue)
        return value if type is None else type(value)

    def setValue(self, key, value):
        self.data[key] = value

    def contains(self, key):
        return key in self.data


@dataclass(frozen=True)
class Algorithm:
    key: str
    name: str
    description: str
    fields: tuple[Field, ...]
    factory: Callable[[dict], TrackingController]
    configuration_factory: Callable[[Path, Callable[[str], None]], ParameterStore] | None = None
    panel_factory: Callable[[object], None] | None = None

    def initialize_panel(self, panel):
        if self.panel_factory:
            self.panel_factory(panel)
        else:
            for field in self.fields:
                panel.parameter(field.key)

    def open_configuration(self, root, report):
        return (
            self.configuration_factory(root, report)
            if self.configuration_factory
            else MemorySettings()
        )

    def defaults(self):
        return {field.key: field.default for field in self.fields}

    def validate(self, values):
        if set(values) != {field.key for field in self.fields}:
            raise ValueError("算法参数不完整")
        for field in self.fields:
            value = values[field.key]
            if field.choices:
                valid = value in field.choices
            elif isinstance(field.default, bool):
                valid = isinstance(value, bool)
            elif isinstance(field.default, str):
                valid = isinstance(value, str)
            else:
                valid = (
                    isinstance(value, (float, int))
                    and math.isfinite(value)
                    and field.minimum <= value <= field.maximum
                )
            if not valid:
                raise ValueError(f"参数无效：{field.label}")
        if values["min_temp"] >= values["max_temp"]:
            raise ValueError("温度下限必须小于上限")
