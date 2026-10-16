import tempfile
import unittest
from pathlib import Path

import yaml
from PyQt6 import QtCore

from sensefuture.algorithms.ramp_pid import open_configuration
from sensefuture.config import Configuration, YamlSettings


class ConfigurationTests(unittest.TestCase):
    def test_legacy_migration_is_one_time_and_preserves_qt_values(self):
        with tempfile.TemporaryDirectory() as temp:
            legacy = QtCore.QSettings(
                str(Path(temp) / "old.ini"), QtCore.QSettings.Format.IniFormat
            )
            legacy.setValue("font_size", 18)
            legacy.setValue("geometry", QtCore.QByteArray(b"window geometry"))
            legacy.setValue("stability_overlay_position", QtCore.QPointF(0.25, 0.75))
            root = Path(temp) / "new"
            config = Configuration(root, legacy)
            settings = config.global_settings
            self.assertTrue((root / "global.yaml").is_file())
            self.assertEqual(settings.value("font_size", type=int), 18)
            self.assertEqual(settings.value("geometry"), QtCore.QByteArray(b"window geometry"))
            self.assertEqual(
                settings.value("stability_overlay_position"), QtCore.QPointF(0.25, 0.75)
            )
            settings.setValue("font_size", 16)
            legacy.setValue("font_size", 22)
            reloaded = Configuration(root, legacy)
            self.assertEqual(reloaded.global_settings.value("font_size", type=int), 16)
            self.assertEqual(
                reloaded.global_settings.value("geometry"), QtCore.QByteArray(b"window geometry")
            )

    def test_plugin_owns_yaml_storage_separate_from_global(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Configuration(temp)
            first = open_configuration(Path(temp), config.report)
            first.setValue("kp", 12.5)
            first.setValue("reverse", False)
            first.setValue("direction", "仅制热")
            data = yaml.safe_load((Path(temp) / "algorithms/ramp_pid.yaml").read_text())
            self.assertEqual(data["kp"], 12.5)
            self.assertIs(data["reverse"], False)
            self.assertEqual(data["direction"], "仅制热")
            self.assertFalse(config.global_settings.contains("kp"))
            self.assertFalse(hasattr(config, "algorithm"))
            restored = open_configuration(Path(temp), config.report)
            self.assertEqual(restored.value("kp", type=float), 12.5)

    def test_ini_migration_by_global_and_plugin_does_not_overwrite_yaml(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "algorithms").mkdir()
            (root / "global.ini").write_text("[General]\nfont_size=18\n")
            (root / "algorithms/ramp_pid.ini").write_text("[General]\nkp=7.89\nreverse=false\n")
            config = Configuration(root)
            algorithm = open_configuration(root, config.report)
            self.assertEqual(config.global_settings.value("font_size", type=int), 18)
            self.assertEqual(algorithm.value("kp", type=float), 7.89)
            self.assertFalse(algorithm.value("reverse", type=bool))
            algorithm.setValue("kp", 2.34)
            self.assertEqual(open_configuration(root, config.report).value("kp", type=float), 2.34)
            self.assertTrue((root / "algorithms/ramp_pid.ini").exists())

    def test_invalid_numeric_value_uses_default_and_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Configuration(temp)
            config.global_settings.setValue("station", "invalid")
            self.assertEqual(config.global_settings.value("station", 1, type=int), 1)
            self.assertTrue(config.errors)

    def test_invalid_or_unsafe_yaml_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.yaml"
            for source in ("bad: [", "- wrong shape", "unsafe: !!python/object:os.system {}"):
                path.write_text(source)
                errors = []
                settings = YamlSettings(path, errors.append)
                self.assertTrue(errors)
                self.assertEqual(settings.value("kp", 5.0), 5.0)
                settings.setValue("kp", 2.0)
                self.assertEqual(path.read_text(), source)
