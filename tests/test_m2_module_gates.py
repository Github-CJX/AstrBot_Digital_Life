"""M2 模块开关：可选默认关闭、移除先停注册、保留永不受影响。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from digital_life_host import module_gates
from digital_life_host.module_gates import (
    ALL_MODULES,
    DEFAULTS,
    OPTIONAL_MODULES,
    REMOVED_MODULES,
    RETAINED_MODULES,
    configure_modules,
    describe,
    enabled_modules,
    module_enabled,
    resolved,
)


@pytest.fixture(autouse=True)
def _clear_module_switches():
    """本文件验证默认值，先摘掉 conftest 给老套件开的那组开关。"""
    prefix = module_gates.ENV_PREFIX
    saved = {key: value for key, value in os.environ.items() if key.startswith(prefix)}
    for key in saved:
        del os.environ[key]
    yield
    os.environ.update(saved)


class ModuleDefaultsTests(unittest.TestCase):
    def setUp(self):
        configure_modules(None)
        self.addCleanup(configure_modules, None)

    def test_retained_modules_are_on(self):
        for name in RETAINED_MODULES:
            self.assertTrue(module_enabled(name), name)

    def test_optional_modules_default_off(self):
        for name in OPTIONAL_MODULES:
            self.assertFalse(module_enabled(name), name)

    def test_removed_modules_default_off(self):
        for name in REMOVED_MODULES:
            self.assertFalse(module_enabled(name), name)

    def test_unknown_module_stays_available(self):
        self.assertTrue(module_enabled("something_new"))

    def test_defaults_cover_every_module(self):
        self.assertEqual(set(DEFAULTS), set(ALL_MODULES))

    def test_describe_lists_disabled_modules(self):
        text = describe()
        for name in sorted(REMOVED_MODULES):
            self.assertIn(name, text)


class ModuleOverrideTests(unittest.TestCase):
    def setUp(self):
        configure_modules(None)
        self.addCleanup(configure_modules, None)
        self.root = Path(tempfile.mkdtemp())

    def test_environment_enables_optional_module(self):
        with mock.patch.dict(os.environ, {"DIGITAL_LIFE_MODULE_IMAGE": "1"}):
            self.assertTrue(module_enabled("image"))
        self.assertFalse(module_enabled("image"))

    def test_environment_disables_retained_module(self):
        with mock.patch.dict(os.environ, {"DIGITAL_LIFE_MODULE_CONTENT": "off"}):
            self.assertFalse(module_enabled("content"))

    def test_environment_reads_false_words(self):
        for value in ("0", "false", "no", "off"):
            with mock.patch.dict(os.environ, {"DIGITAL_LIFE_MODULE_QZONE": value}):
                self.assertFalse(module_enabled("qzone"), value)

    def test_file_enables_module(self):
        (self.root / module_gates.CONFIG_FILE).write_text(
            json.dumps({"modules": {"image": True, "qzone": True}}), encoding="utf-8")
        self.assertTrue(module_enabled("image", settings_root=self.root))
        self.assertTrue(module_enabled("qzone", settings_root=self.root))

    def test_environment_beats_file(self):
        (self.root / module_gates.CONFIG_FILE).write_text(
            json.dumps({"modules": {"image": False}}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"DIGITAL_LIFE_MODULE_IMAGE": "1"}):
            self.assertTrue(module_enabled("image", settings_root=self.root))

    def test_broken_file_falls_back_to_defaults(self):
        (self.root / module_gates.CONFIG_FILE).write_text("{not json", encoding="utf-8")
        self.assertTrue(module_enabled("content", settings_root=self.root))
        self.assertFalse(module_enabled("image", settings_root=self.root))

    def test_unknown_keys_are_ignored(self):
        (self.root / module_gates.CONFIG_FILE).write_text(
            json.dumps({"modules": {"nope": True}}), encoding="utf-8")
        self.assertEqual(resolved(settings_root=self.root).get("nope"), None)

    def test_configured_root_applies_without_argument(self):
        (self.root / module_gates.CONFIG_FILE).write_text(
            json.dumps({"modules": {"screen": True}}), encoding="utf-8")
        configure_modules(self.root)
        self.assertTrue(module_enabled("screen"))


class ModuleFilterTests(unittest.TestCase):
    def setUp(self):
        configure_modules(None)
        self.addCleanup(configure_modules, None)

    def test_enabled_modules_keeps_order_and_drops_disabled(self):
        names = ("content", "image", "screen", "reality", "together", "game", "live")
        self.assertEqual(enabled_modules(names), ("content", "reality", "together"))

    def test_enabled_modules_respects_environment(self):
        with mock.patch.dict(os.environ, {"DIGITAL_LIFE_MODULE_GAME": "1"}):
            self.assertEqual(enabled_modules(("content", "game")), ("content", "game"))


class BundledExtensionGateTests(unittest.TestCase):
    def setUp(self):
        configure_modules(None)
        self.addCleanup(configure_modules, None)

    def test_bundled_extensions_skip_removed_and_optional_modules(self):
        from digital_life_host.native_extensions import builtins

        created = []

        def fake_create(root, name, **kwargs):
            created.append(name)
            return name

        with mock.patch.object(builtins, "create_bundled_extension", side_effect=fake_create):
            leases, issues = builtins.create_bundled_extensions(
                Path(tempfile.mkdtemp()), context=object(), controller=object())
        self.assertEqual(created, ["content", "reality", "together"])
        self.assertEqual(leases, ("content", "reality", "together"))
        self.assertEqual(issues, ())


class SectionGateTests(unittest.TestCase):
    def setUp(self):
        configure_modules(None)
        self.addCleanup(configure_modules, None)

    def test_image_sections_hidden_by_default(self):
        self.assertFalse(module_gates.section_visible("image.output"))
        self.assertFalse(module_gates.section_visible("photo.reference"))
        self.assertFalse(module_gates.section_visible("wardrobe"))

    def test_removed_module_sections_hidden_by_default(self):
        self.assertFalse(module_gates.section_visible("qzone"))
        self.assertFalse(module_gates.section_visible("screen"))
        self.assertFalse(module_gates.section_visible("device.phone"))

    def test_retained_module_sections_stay_visible(self):
        self.assertTrue(module_gates.section_visible("library"))
        self.assertTrue(module_gates.section_visible("content.authoring"))
        self.assertTrue(module_gates.section_visible("life"))
        self.assertTrue(module_gates.section_visible("system.prompts"))

    def test_sections_return_when_module_enabled(self):
        with mock.patch.dict(os.environ, {"DIGITAL_LIFE_MODULE_IMAGE": "1"}):
            self.assertTrue(module_gates.section_visible("image.output"))
            self.assertIn("image.output", module_gates.visible_sections(("image.output", "life")))

    def _configuration_api(self, **kwargs):
        try:
            from digital_life_host.configuration_api import ConfigurationWebAPI
        except ModuleNotFoundError as error:  # 本地无 astrbot 运行时
            raise unittest.SkipTest(f"astrbot runtime required: {error}")
        return ConfigurationWebAPI(object(), **kwargs)

    def test_configuration_api_filters_sections(self):
        api = self._configuration_api()
        self.assertIn("life", api.sections)
        # M2.5b 移除 qzone 实现后，配置 API 里不再存在 qzone 分区，
        # 开关也无法让它复活（实现已删，只有 section_visible 的默认值还在）。
        self.assertNotIn("qzone", api.sections)
        with mock.patch.dict(os.environ, {"DIGITAL_LIFE_MODULE_QZONE": "1"}):
            self.assertNotIn("qzone", self._configuration_api().sections)
        # 可选模块仍然由开关控制：打开 screen / image 后分区回到列表。
        self.assertNotIn("screen", api.sections)
        self.assertNotIn("image.output", api.sections)
        with mock.patch.dict(os.environ, {"DIGITAL_LIFE_MODULE_SCREEN": "1"}):
            self.assertIn("screen", self._configuration_api().sections)
        with mock.patch.dict(os.environ, {"DIGITAL_LIFE_MODULE_IMAGE": "1"}):
            self.assertIn("image.output", self._configuration_api().sections)

    def test_configuration_api_still_honours_allowlist(self):
        with mock.patch.dict(os.environ, {"DIGITAL_LIFE_MODULE_IMAGE": "1"}):
            api = self._configuration_api(allowed_sections=("life", "image.output"))
        self.assertEqual(tuple(api.sections), ("life", "image.output"))
        api = self._configuration_api(allowed_sections=("life", "image.output"))
        self.assertEqual(tuple(api.sections), ("life",))


class PageVisibilityTests(unittest.TestCase):
    def setUp(self):
        configure_modules(None)
        self.addCleanup(configure_modules, None)

    def test_optional_pages_hidden_by_default(self):
        visibility = module_gates.page_visibility()
        self.assertFalse(visibility["workspace"])
        self.assertFalse(visibility["multi_role"])
        self.assertFalse(visibility["game"])
        self.assertFalse(visibility["screen"])

    def test_page_returns_when_module_enabled(self):
        with mock.patch.dict(os.environ, {"DIGITAL_LIFE_MODULE_WORKSPACE": "1"}):
            self.assertTrue(module_gates.page_visible("workspace"))
            self.assertFalse(module_gates.page_visible("multi_role"))


if __name__ == "__main__":
    unittest.main()
