"""The host builds the bili port factory only when the module is enabled."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from digital_life_host.bili_watch import configured_bili_factory, parse_cookie
from digital_life_host.bili_see import BiliSee, configured_bili_seer
from digital_life_host.module_gates import CONFIG_FILE, module_enabled


class BiliFactoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_cookie_accepts_header_and_json_forms(self):
        header = parse_cookie("SESSDATA=abc; bili_jct=xyz;")
        self.assertEqual(header, {"sessdata": "abc", "bili_jct": "xyz"})
        payload = parse_cookie('{"SESSDATA": "abc"}')
        self.assertEqual(payload, {"sessdata": "abc"})
        self.assertEqual(parse_cookie(""), {})

    def test_factory_is_none_when_the_module_is_off(self):
        self.assertFalse(module_enabled("bilibili"))
        self.assertIsNone(configured_bili_factory({}, self.root))

    def test_factory_builds_a_port_with_shared_quota_state(self):
        try:
            from digital_life_host.module_gates import configure_modules

            configure_modules(self.root)
            (self.root / CONFIG_FILE).write_text('{"modules": {"bilibili": true}}', encoding="utf-8")
            config = {"bilibili": {"cookie": "SESSDATA=abc"}}
            factory = configured_bili_factory(config, self.root)
            self.assertIsNotNone(factory)
            port = factory(SimpleNamespace())
            self.assertIsNotNone(port)
            self.assertEqual(port.cookie, {"sessdata": "abc"})
            (self.root / "bilibili").mkdir(exist_ok=True)
            second = factory(SimpleNamespace())
            self.assertIsNot(second, port)
        finally:
            from digital_life_host.bili_watch import _store_cache

            _store_cache.clear()

    def _module_on(self):
        from digital_life_host.module_gates import configure_modules

        configure_modules(self.root)
        (self.root / CONFIG_FILE).write_text('{"modules": {"bilibili": true}}', encoding="utf-8")

    def test_seer_is_none_when_the_module_is_off(self):
        self.assertFalse(module_enabled("bilibili"))
        self.assertIsNone(configured_bili_seer({"bilibili": {"see": {
            "api_base": "https://gw.example", "key": "k", "model": "m"}}}, self.root))

    def test_seer_builds_from_config_with_clamped_settings(self):
        self._module_on()
        config = {"bilibili": {"see": {"api_base": "https://gw.example/", "key": "k",
                                       "model": "m", "frames": 99, "scale": 10,
                                       "max_chars": 9_999_999}}}
        seer = configured_bili_seer(config, self.root)
        self.assertIsInstance(seer, BiliSee)
        self.assertEqual((seer.frames, seer.scale), (8, 240))
        self.assertEqual(seer.api_base, "https://gw.example")
        self.assertTrue(seer.settings()["available"])

    def test_seer_is_none_when_no_gateway_credentials_exist(self):
        self._module_on()
        self.assertIsNone(configured_bili_seer({"bilibili": {}}, self.root))

    def test_seer_can_be_disabled_explicitly(self):
        self._module_on()
        config = {"bilibili": {"see": {"enabled": False, "api_base": "https://gw.example",
                                       "key": "k", "model": "m"}}}
        self.assertIsNone(configured_bili_seer(config, self.root))


if __name__ == "__main__":
    unittest.main()
