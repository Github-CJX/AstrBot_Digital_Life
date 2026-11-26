# -*- coding: utf-8 -*-
"""游客身份（buvid3）自动领取：机房 IP 无 cookie 访问 view 接口会被 412 挡掉。"""

import asyncio
import unittest
from types import SimpleNamespace

from digital_life_core.bilibili.bili_api import BiliAPI
from digital_life_core.bilibili.traffic import TrafficGuard
from digital_life_core.bilibili.state import QuotaStore
import tempfile
import os


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status = 200

    async def text(self, errors="strict"):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.headers_seen = None

    def get(self, url, headers=None, timeout=None, params=None):
        self.headers_seen = headers
        if self.error is not None:
            raise self.error
        return _FakeResponse(self.payload)


def _api(session):
    with tempfile.TemporaryDirectory() as tmp:
        guard = TrafficGuard(QuotaStore(os.path.join(tmp, "quota.json")), lambda: {})
    api = BiliAPI(cookie={}, guard=guard)
    api._session = session
    return api


class BuvidProvisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_provision_sets_buvid3_and_buvid4(self):
        session = _FakeSession(payload='{"code":0,"data":{"b_3":"buvid-abc","b_4":"buvid4-xyz"}}')
        api = _api(session)
        await api._provision_buvid()
        self.assertEqual(api.cookie.get("buvid3"), "buvid-abc")
        self.assertEqual(api.cookie.get("buvid4"), "buvid4-xyz")
        self.assertIn("buvid3=buvid-abc", api._cookie_header())
        self.assertIn("buvid4=buvid4-xyz", api._cookie_header())

    async def test_provision_failure_is_silent_and_keeps_cookie_empty(self):
        session = _FakeSession(error=OSError("network down"))
        api = _api(session)
        await api._provision_buvid()
        self.assertNotIn("buvid3", api.cookie)

    async def test_provision_skipped_when_cookie_already_has_buvid3(self):
        session = _FakeSession(payload='{"code":0,"data":{"b_3":"fresh","b_4":"fresh4"}}')
        with tempfile.TemporaryDirectory() as tmp:
            guard = TrafficGuard(QuotaStore(os.path.join(tmp, "quota.json")), lambda: {})
        api = BiliAPI(cookie={"buvid3": "existing"}, guard=guard)
        api._session = session
        await api._provision_buvid()
        self.assertEqual(api.cookie.get("buvid3"), "existing")


if __name__ == "__main__":
    unittest.main()
