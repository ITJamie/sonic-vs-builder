"""Exercise patched, pinned upstream Edgecore code without touching hardware.

Run directly. Set EDGECORE_SOURCE_CACHE to a checkout of COMMIT to avoid repeat
downloads during development. All device operations are substituted by fakes.
"""

import array
import ast
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.request
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from sonic_vs import apply_branch_patches
from upstream_patch_fixture import populate_patch_sources

ROOT = Path(__file__).parents[1]
COMMIT = "5ef9e45ccf7f004fb07bf3f9be2f471cf00573bf"
BASE = "platform/broadcom/sonic-platform-modules-accton/"
DEVICE = "device/accton/x86_64-accton_"
MODELS = ("as5835_54x", "as7326_56x", "as7726_32x", "as7816_64x")
SOURCE = None
ORIGINALS = {}
VENDOR_SDK = {}


def definitions(path, **environment):
    """Execute upstream definitions, excluding startup/import side effects."""
    tree = ast.parse((SOURCE / path).read_text(), filename=path)
    tree.body = [node for node in tree.body if isinstance(
        node, (ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign))]
    scope = {"logging": logging, "math": math, "time": time,
             "subprocess": Mock(), "sys": sys, **environment}
    exec(compile(tree, path, "exec"), scope)
    return scope


def platform_directory(model):
    return (DEVICE + model + "-r0/sonic_platform/" if model == "as5835_54x"
            else BASE + model.replace("_", "-") + "/sonic_platform/")


class EdgecorePlatform(unittest.TestCase):
    def test_sdk_settings_match_each_vendor_profile_without_other_resource_changes(self):
        keys = {"l3_ecmp_levels", "use_all_splithorizon_groups", "riot_enable", "sai_tunnel_support",
                "riot_overlay_l3_intf_mem_size", "riot_overlay_l3_egress_mem_size",
                "riot_overlay_ecmp_resilient_hash_size", "flow_init_mode", "host_as_route_disable"}

        def properties(text):
            return dict(line.split(";", 1)[0].strip().split("=", 1)
                        for line in text.splitlines() if "=" in line and not line.lstrip().startswith("#"))

        for path, vendor_text in VENDOR_SDK.items():
            before = properties(ORIGINALS[path])
            after = properties((SOURCE / path).read_text())
            vendor = properties(vendor_text)
            self.assertEqual({key: after[key] for key in keys}, {key: vendor[key] for key in keys})
            self.assertEqual(after["stable_size"], "0x8000000")
            if "sai_load_hw_config" in before:
                self.assertEqual(after["sai_load_hw_config"], before["sai_load_hw_config"].replace("/b870.6.4.1/", "/default/"))
            excluded = keys | {"stable_size", "sai_load_hw_config"}
            self.assertEqual({k: v for k, v in before.items() if k not in excluded},
                             {k: v for k, v in after.items() if k not in excluded})

    def test_pddf_led_changes_preserve_every_register_and_sensor(self):
        for model in MODELS[1:]:
            path = DEVICE + model + "-r0/pddf/pddf-device.json"
            before, after = json.loads(ORIGINALS[path]), json.loads((SOURCE / path).read_text())
            for name, device in after.items():
                if device.get("dev_info", {}).get("device_type") == "LED":
                    # Label changes must not alter register addresses/values or topology.
                    for old, new in zip(before[name]["i2c"]["attr_list"], device["i2c"]["attr_list"]):
                        new["attr_name"] = old["attr_name"]
            self.assertEqual(before, after)

    def test_health_methods_and_every_configured_led_color(self):
        class PddfChassis:
            def get_system_led(self, name):
                return name, "green"

            def set_system_led(self, name, color):
                return name, color

        for model in MODELS[1:]:
            with self.subTest(model=model):
                scope = definitions(platform_directory(model) + "chassis.py", PddfChassis=PddfChassis)
                chassis = object.__new__(scope["Chassis"])
                self.assertTrue(chassis.initizalize_system_led())
                self.assertEqual(chassis.get_status_led(), ("DIAG_LED", "green"))
                self.assertEqual(chassis.set_status_led("red"), ("DIAG_LED", "red"))
                directory = SOURCE / (DEVICE + model + "-r0")
                data = json.loads((directory / "pddf/pddf-device.json").read_text())
                health = json.loads((directory / "system_health_monitoring_config.json").read_text())
                colors = {a["attr_name"] for a in data["DIAG_LED"]["i2c"]["attr_list"]}
                self.assertLessEqual(set(health["led_color"].values()), colors)
                allowed = {"off", "green", "red", "yellow", "blue", "amber",
                           "green_blink", "red_blink", "yellow_blink", "blue_blink", "amber_blink"}
                for device in data.values():
                    if device.get("dev_info", {}).get("device_type") == "LED":
                        self.assertLessEqual({a["attr_name"] for a in device["i2c"]["attr_list"]}, allowed)

    def test_native_as5835_health_is_preserved(self):
        path = platform_directory(MODELS[0]) + "chassis.py"
        scope = definitions(path, ChassisBase=object)
        chassis = object.__new__(scope["Chassis"])
        chassis._api_helper = Mock()
        chassis._api_helper.read_txt_file.return_value = "1"
        self.assertTrue(chassis.initizalize_system_led())
        self.assertEqual(chassis.get_status_led(), "STATUS_LED_COLOR_GREEN")
        self.assertFalse(chassis.set_status_led("unsupported"))

    def test_clock_ordering_and_monitor_restart(self):
        for model in MODELS:
            directory = SOURCE / BASE / model.replace("_", "-") / "service"
            files = list(directory.glob("*.service"))
            monitors = [f for f in files if ("pddf-platform-monitor" in f.name or
                        f.name == "as5835-54x-platform-monitor.service")]
            self.assertEqual(len(monitors), 1, model)
            self.assertIn("Restart=on-failure\nRestartSec=5", monitors[0].read_text())
        for model in MODELS[1:3]:
            name = model.replace("_", "-")
            service = "as7326-platform-handle_mac.service" if model == MODELS[1] else name + "-platform-handle_mac.service"
            text = (SOURCE / BASE / name / "service" / service).read_text()
            self.assertIn("Before=opennsl-modules.service pddf-platform-init.service", text)
            self.assertIn("DefaultDependencies=no", text)

    def monitor(self, model):
        names = {"as7326_56x": ("accton_as7326_pddf_monitor.py", "device_monitor"),
                 "as7726_32x": ("accton_as7726_32x_pddf_monitor.py", "device_monitor"),
                 "as7816_64x": ("accton_as7816_pddf_monitor.py", "accton_as7816_monitor")}
        file, name = names[model]
        scope = definitions(BASE + model.replace("_", "-") + "/utils/" + file)
        chassis = Mock()
        fan = Mock()
        fan.get_speed.return_value = 100
        fan.get_status.return_value = True
        fan.get_speed_rpm.return_value = 9000
        fan.get_direction.return_value = "exhaust"
        chassis.get_fan.return_value = fan
        scope["platform_chassis"] = chassis
        monitor = object.__new__(scope[name])
        return scope, chassis, fan, monitor

    def test_pddf_temperature_failure_full_speed_and_recovery(self):
        for model in MODELS[1:]:
            for failure in (None, float("nan"), float("inf"), True, "bad", OSError("not ready")):
                with self.subTest(model=model, failure=failure):
                    scope, chassis, fan, monitor = self.monitor(model)
                    thermal = chassis.get_thermal.return_value
                    if isinstance(failure, OSError):
                        thermal.get_temperature.side_effect = failure
                    else:
                        thermal.get_temperature.return_value = failure
                    self.assertFalse(monitor.manage_fans())
                    fan.set_speed.assert_called_with(100)
                    # A later successful poll must recover without restarting.
                    thermal.get_temperature.side_effect = None
                    thermal.get_temperature.return_value = 20.0
                    fan.set_speed.reset_mock()
                    self.assertTrue(monitor.manage_fans())
                    fan.set_speed.assert_called_with(52 if model == MODELS[3] else 38)
                    scope["subprocess"].getstatusoutput.assert_not_called()

    def test_as7726_airflow_selects_distinct_existing_policy(self):
        for direction, expected in [("exhaust", 38), ("intake", 63), ("EXHAUST", 38), ("INTAKE", 63)]:
            scope, chassis, fan, monitor = self.monitor("as7726_32x")
            fan.get_direction.return_value = direction
            chassis.get_thermal.return_value.get_temperature.return_value = 36.0
            self.assertTrue(monitor.manage_fans())
            fan.set_speed.assert_called_with(expected)
        _, chassis, fan, monitor = self.monitor("as7726_32x")
        fan.get_direction.return_value = None
        self.assertFalse(monitor.manage_fans())
        fan.set_speed.assert_called_with(100)

    def test_each_required_pddf_sensor_independently_triggers_full_speed(self):
        for model, indices in [(MODELS[1], [1, 3]), (MODELS[2], [3, 4]), (MODELS[3], list(range(6)))]:
            for unavailable in indices:
                with self.subTest(model=model, unavailable=unavailable):
                    _, chassis, fan, monitor = self.monitor(model)

                    def thermal(index):
                        return Mock(get_temperature=Mock(return_value=None if index == unavailable else 20.0))

                    chassis.get_thermal.side_effect = thermal
                    self.assertFalse(monitor.manage_fans())
                    fan.set_speed.assert_called_with(100)

    def test_native_as5835_missing_temperature_uses_full_speed(self):
        for value in (None, float("nan"), float("inf"), True, "bad"):
            fan = Mock()
            thermal = Mock()
            thermal.get_thermal_val.return_value = value
            scope = definitions(BASE + "as5835-54x/utils/accton_as5835_54x_monitor.py",
                                FanUtil=Mock(return_value=fan), ThermalUtil=Mock(return_value=thermal))
            cls = next(v for v in scope.values() if isinstance(v, type) and hasattr(v, "manage_fans"))
            self.assertFalse(object.__new__(cls).manage_fans())
            fan.set_fan_duty_cycle.assert_called_with(100)

    def test_native_as5835_sensor_recovery_resumes_original_policy(self):
        fan = Mock()
        fan.get_idx_fan_start.return_value = 1
        fan.get_num_fans.return_value = 5
        fan.get_fan_status.return_value = True
        fan.get_fan_duty_cycle.return_value = 100
        fan.set_fan_duty_cycle.return_value = True
        thermal = Mock()
        thermal.get_thermal_val.return_value = None
        scope = definitions(BASE + "as5835-54x/utils/accton_as5835_54x_monitor.py",
                            FanUtil=Mock(return_value=fan), ThermalUtil=Mock(return_value=thermal))
        cls = next(v for v in scope.values() if isinstance(v, type) and hasattr(v, "manage_fans"))
        monitor = object.__new__(cls)
        self.assertFalse(monitor.manage_fans())
        thermal.get_thermal_val.return_value = 20000
        self.assertTrue(monitor.manage_fans())
        fan.set_fan_duty_cycle.assert_called_with(40)
        # Loss and recovery at the SAME temperature must leave full-speed
        # fallback, rather than taking the old "temperature unchanged" shortcut.
        thermal.get_thermal_val.return_value = None
        self.assertFalse(monitor.manage_fans())
        fan.set_fan_duty_cycle.assert_called_with(100)
        thermal.get_thermal_val.return_value = 20000
        self.assertTrue(monitor.manage_fans())
        fan.set_fan_duty_cycle.assert_called_with(40)


class EdgecoreWatchdog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules = patch.dict(sys.modules, {
            "sonic_platform_base": types.ModuleType("sonic_platform_base"),
            "sonic_platform_base.watchdog_base": types.SimpleNamespace(WatchdogBase=object)})
        cls.modules.start()
        cls.addClassCleanup(cls.modules.stop)
        path = SOURCE / "src/sonic-platform-common/sonic_platform_base/linux_watchdog.py"
        spec = importlib.util.spec_from_file_location("sonic_platform_base.linux_watchdog", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        sys.modules[spec.name] = cls.module
        cls.addClassCleanup(sys.modules.pop, spec.name)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.device = self.root / "watchdog0"
        self.device.mkdir()
        self.state("inactive")
        (self.device / "identity").write_text("iTCO_wdt\n")
        self.path = patch.object(self.module.LinuxWatchdog, "SYSFS", self.root)
        self.path.start()
        self.addCleanup(self.path.stop)
        self.open = self.mock(self.module.os, "open", return_value=123)
        self.close = self.mock(self.module.os, "close")
        self.ioctl = self.mock(self.module.fcntl, "ioctl", side_effect=self.kernel)

    def mock(self, target, name, **kwargs):
        replacement = patch.object(target, name, **kwargs)
        self.addCleanup(replacement.stop)
        return replacement.start()

    def state(self, value):
        (self.device / "state").write_text(value)

    def kernel(self, fd, operation, value, mutate=None):
        self.assertEqual(fd, 123)
        if operation == self.module.LinuxWatchdog.WDIOC_SETTIMEOUT:
            self.assertIsInstance(value, array.array)
            self.assertEqual(value.itemsize, 4)
            value[0] = ((value[0] + 9) // 10) * 10
            self.state("active")
        elif operation == self.module.LinuxWatchdog.WDIOC_SETOPTIONS:
            self.assertEqual(value.itemsize, 4)
            self.assertEqual(value[0], 1)
            self.state("inactive")

    def test_all_model_adapters_load_and_queries_never_open(self):
        for model in MODELS:
            path = SOURCE / platform_directory(model) / "watchdog.py"
            spec = importlib.util.spec_from_file_location("edgecore_test_watchdog", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            device = module.Watchdog()
            self.assertFalse(device.is_armed())
            self.assertEqual(device.get_remaining_time(), -1)
            self.assertTrue(device.disarm())
        self.open.assert_not_called()
        self.ioctl.assert_not_called()

    def test_chassis_getters_resolve_and_cache_each_actual_adapter(self):
        base = definitions("platform/pddf/platform-api-pddf-base/sonic_platform_pddf_base/pddf_chassis.py",
                           ChassisBase=object, syslog=Mock())["PddfChassis"]
        for model in MODELS:
            directory = platform_directory(model)
            spec = importlib.util.spec_from_file_location("sonic_platform.watchdog", SOURCE / directory / "watchdog.py")
            adapter = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(adapter)
            parent = types.ModuleType("sonic_platform")
            parent.__path__ = []
            with patch.dict(sys.modules, {"sonic_platform": parent, "sonic_platform.watchdog": adapter}):
                chassis_type = definitions(directory + "chassis.py", PddfChassis=base, ChassisBase=object)["Chassis"]
                chassis = object.__new__(chassis_type)
                chassis._watchdog = None
                watchdog = chassis.get_watchdog()
                self.assertIsInstance(watchdog, adapter.Watchdog)
                self.assertIs(chassis.get_watchdog(), watchdog)
        self.open.assert_not_called()

    def test_missing_adapter_logs_original_error_without_nameerror(self):
        base = definitions("platform/pddf/platform-api-pddf-base/sonic_platform_pddf_base/pddf_chassis.py",
                           ChassisBase=object, syslog=Mock())["PddfChassis"]
        chassis = object.__new__(base)
        chassis._watchdog = None
        parent = types.ModuleType("sonic_platform")
        parent.__path__ = []
        with patch.dict(sys.modules, {"sonic_platform": parent, "sonic_platform.watchdog": None}):
            self.assertIsNone(chassis.get_watchdog())

    def test_real_kernel_state_and_optional_timeleft(self):
        self.state("active")
        (self.device / "timeleft").write_text("27\n")
        device = self.module.LinuxWatchdog()
        self.assertTrue(device.is_armed())
        self.assertEqual(device.get_remaining_time(), 27)
        (self.device / "timeleft").unlink()
        self.assertEqual(device.get_remaining_time(), -1)
        self.open.assert_not_called()

    def test_arm_uses_actual_timeout_and_disarm_is_idempotent(self):
        device = self.module.LinuxWatchdog()
        self.assertEqual(device.arm(61), 70)
        self.assertTrue(device.is_armed())
        self.assertTrue(device.disarm())
        self.close.assert_called_once_with(123)
        count = self.ioctl.call_count
        self.assertTrue(device.disarm())
        self.assertEqual(self.ioctl.call_count, count)

    def test_invalid_or_unavailable_device_never_reports_success(self):
        device = self.module.LinuxWatchdog()
        for value in (0, -1, True, 1.5, "30", 2**31):
            self.assertEqual(device.arm(value), -1)
        self.open.assert_not_called()
        self.open.side_effect = PermissionError("denied")
        self.assertEqual(device.arm(30), -1)
        self.state("active")
        self.assertFalse(device.disarm())
        (self.device / "identity").write_text("unrelated_driver")
        with self.assertRaises(RuntimeError):
            self.module.LinuxWatchdog()

    def test_ioctl_failure_or_short_timeout_disables_opened_device(self):
        for result in (OSError("unsupported"), 10):
            self.state("inactive")
            device = self.module.LinuxWatchdog()

            def ioctl(fd, op, values, mutate=None):
                if op == device.WDIOC_SETTIMEOUT:
                    self.state("active")
                    if isinstance(result, OSError):
                        raise result
                    values[0] = result
                else:
                    return self.kernel(fd, op, values, mutate)

            self.ioctl.side_effect = ioctl
            self.assertEqual(device.arm(60), -1)
            self.assertFalse(device.is_armed())
            self.assertIsNone(device._fd)

    def test_nowayout_and_unknown_state_do_not_fake_success(self):
        self.state("active")
        device = self.module.LinuxWatchdog()
        self.ioctl.side_effect = OSError("nowayout")
        self.assertFalse(device.disarm())
        self.state("unknown")
        with self.assertRaises(RuntimeError):
            device.is_armed()


def main():
    global SOURCE, ORIGINALS, VENDOR_SDK
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        SOURCE = work / "source"
        SOURCE.mkdir()
        patches = sorted((ROOT / "patches/202605").glob("*.patch"))
        populate_patch_sources(SOURCE, COMMIT, patches, os.environ.get("EDGECORE_SOURCE_CACHE"))
        ORIGINALS = {str(path.relative_to(SOURCE)): path.read_text() for path in SOURCE.rglob("*") if path.is_file()}
        for path in ORIGINALS:
            if path.endswith(".bcm"):
                url = "https://raw.githubusercontent.com/edge-core/sonic-buildimage/d39662e996c0e8748c9b96ad88dc5f5f524e8048/" + path
                with urllib.request.urlopen(url, timeout=60) as response:
                    VENDOR_SDK[path] = response.read().decode()
        subprocess.run(["git", "init", "-q", str(SOURCE)], check=True)
        # Exercise adding a new Python module inside an initialized submodule.
        common = SOURCE / "src/sonic-platform-common"
        common.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(common)], check=True)
        subprocess.run(["git", "-C", str(SOURCE), "update-index", "--add", "--cacheinfo",
                        "160000", "a" * 40, "src/sonic-platform-common"], check=True)
        results = apply_branch_patches(SOURCE, work, "202605")
        assert len(results) == 10 and all(r["status"] == "applied" for r in results)
        assert all(r["status"] == "already-applied" for r in apply_branch_patches(SOURCE, work, "202605"))
        for path in SOURCE.rglob("*.py"):
            compile(path.read_text(), str(path), "exec")
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
        if not result.wasSuccessful():
            raise SystemExit(1)


if __name__ == "__main__":
    main()
