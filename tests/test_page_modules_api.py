"""Page module visibility: the cottage hides optional entries without losing management."""

import os
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from astrbot.api.web import PluginRequest, bind_request_context
from starlette.requests import Request

from digital_life_host import module_gates
from digital_life_host.page_api import PageWebAPI, PREFIX

PLUGIN_NAME = "astrbot_plugin_digital_life"


class PageModulesApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        module_gates.configure_modules(None)
        self.addCleanup(module_gates.configure_modules, None)
        prefix = module_gates.ENV_PREFIX
        saved = {key: value for key, value in os.environ.items() if key.startswith(prefix)}
        for key in saved:
            del os.environ[key]
        self.addCleanup(os.environ.update, saved)
        self.controller = NS(_terminated=False, data_root=None)
        self.handler = next(
            handler for path, handler, _, _ in PageWebAPI(self.controller).routes()
            if path == PREFIX + "/page/modules")

    async def request(self, *, username="admin", query=b"", plugin_name=PLUGIN_NAME):
        request = Request({
            "type": "http",
            "method": "GET",
            "path": PREFIX + "/page/modules",
            "headers": [],
            "query_string": query,
            "app": NS(state=NS(services=NS())),
        })
        plugin_request = PluginRequest(request, plugin_name=plugin_name, username=username)
        with bind_request_context(plugin_request):
            return await self.handler()

    async def test_defaults_report_optional_modules_off(self):
        response = await self.request()

        self.assertEqual(response["status"], "ok")
        data = response["data"]
        self.assertTrue(data["modules"]["content"])
        self.assertFalse(data["modules"]["image"])
        self.assertFalse(data["modules"]["workspace"])
        self.assertFalse(data["pages"]["workspace"])
        self.assertFalse(data["pages"]["multi_role"])
        self.assertIn("qzone", data["hidden_sections"])
        self.assertIn("image.output", data["hidden_sections"])
        self.assertNotIn("life", data["hidden_sections"])

    async def test_environment_switch_shows_the_page_entry(self):
        with mock.patch.dict(os.environ, {"DIGITAL_LIFE_MODULE_WORKSPACE": "1"}):
            response = await self.request()

        data = response["data"]
        self.assertTrue(data["modules"]["workspace"])
        self.assertTrue(data["pages"]["workspace"])
        self.assertFalse(data["pages"]["multi_role"])

    async def test_rejects_api_keys_and_query_overrides(self):
        for username, query in (("api_key:synthetic", b""), ("admin", b"modules=all")):
            response = await self.request(username=username, query=query)
            self.assertEqual(response["status"], "error")
        self.assertEqual((await self.request(username=""))["data"]["code"],
                         "dashboard_identity_required")

    async def test_stopped_plugin_cannot_read_the_switchboard(self):
        self.controller._terminated = True

        response = await self.request()

        self.assertEqual(response["data"]["code"], "staging_host_terminated")


if __name__ == "__main__":
    unittest.main()
