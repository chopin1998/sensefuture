"""Atomic YAML storage and global preferences. Algorithms choose their own storage."""

import base64
import os
import tempfile
from pathlib import Path

import yaml
from PyQt6 import QtCore


def encode_value(value):
    if isinstance(value, QtCore.QByteArray):
        return {"qt_byte_array": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, QtCore.QPointF):
        return {"x": value.x(), "y": value.y()}
    if isinstance(value, dict):
        return {key: encode_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode_value(item) for item in value]
    return value


def decode_value(value):
    if isinstance(value, dict) and set(value) == {"qt_byte_array"}:
        return QtCore.QByteArray(base64.b64decode(value["qt_byte_array"], validate=True))
    if isinstance(value, dict) and set(value) == {"x", "y"}:
        return QtCore.QPointF(float(value["x"]), float(value["y"]))
    return value


class YamlSettings:
    """Small value-store adapter; safe YAML, atomic writes, optional INI migration."""

    def __init__(self, path, report, legacy=None):
        self.path = Path(path)
        self.report = report
        self.data = {}
        self.read_failed = False
        if self.path.exists():
            try:
                data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not all(isinstance(key, str) for key in data):
                    raise ValueError("配置顶层须为键值表")
                self.data = data
            except (OSError, ValueError, yaml.YAMLError) as exc:
                self.read_failed = True
                self.report(f"配置读取失败，保留原文件：{self.path}：{exc}")
        else:
            if legacy is not None:
                self.data = {key: encode_value(legacy.value(key)) for key in legacy.allKeys()}
            self.data.setdefault("schema_version", 1)
            self.sync()

    def contains(self, key):
        return key in self.data

    def fileName(self):
        return str(self.path)

    def value(self, key, defaultValue=None, type=None):
        try:
            value = decode_value(self.data.get(key, encode_value(defaultValue)))
            if type is bool:
                if isinstance(value, bool):
                    return value
                if str(value).lower() in ("true", "1", "false", "0"):
                    return str(value).lower() in ("true", "1")
                raise ValueError("无效开关值")
            return value if type is None else type(value)
        except (TypeError, ValueError):
            self.report(f"配置值无效，使用默认值：{self.path.name}/{key}")
            return defaultValue

    def setValue(self, key, value):
        self.data[key] = encode_value(value)
        self.sync()

    def sync(self):
        if self.read_failed:
            return
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                yaml.safe_dump(self.data, stream, allow_unicode=True, sort_keys=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except (OSError, TypeError, yaml.YAMLError) as exc:
            self.report(f"配置保存失败：{self.path}：{exc}")
        finally:
            if temporary and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass


class Configuration:
    def __init__(self, root=None, legacy=None):
        self.root = Path(
            root
            or os.environ.get("SENSEFUTURE_CONFIG_DIR")
            or Path(
                QtCore.QStandardPaths.writableLocation(
                    QtCore.QStandardPaths.StandardLocation.GenericConfigLocation
                )
            )
            / "sensefuture"
        )
        self.errors = []
        self.error_handler = None
        if legacy is None:
            ini = self.root / "global.ini"
            legacy = (
                QtCore.QSettings(str(ini), QtCore.QSettings.Format.IniFormat)
                if ini.exists()
                else QtCore.QSettings("SenseFuture", "Workbench")
            )
        self.global_settings = YamlSettings(self.root / "global.yaml", self.report, legacy)

    def report(self, message):
        if message not in self.errors:
            self.errors.append(message)
            if self.error_handler:
                self.error_handler(message)
