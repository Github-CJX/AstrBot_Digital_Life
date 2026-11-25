"""Regression tests for management-plane RBAC and plugin reload/shutdown cleanup."""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tests import astrbot_data_root  # noqa: F401 - isolate ASTRBOT_ROOT before SDK imports

from astrbot.api.web import PluginMultiDict
from digital_life_core.execution.models import TaskError
from digital_life_core.foundation.outcome import Outcome
from digital_life_core.identity_policy.models import AuthorizationTier
from digital_life_core.identity_policy.object_access import ObjectAccessPolicy
from digital_life_core.storage.runtime import StorageRuntime

from digital_life_host.account_session_api import AccountSessionWebAPI
from digital_life_host.backup_api import BackupWebAPI
from digital_life_host.host_options_api import HostOptionsWebAPI
from digital_life_host import laya_client
from digital_life_host.log_api import LogWebAPI
from digital_life_host.management_identity import (
    dashboard_operator,
    require_controller_management_operator,
    require_management_operator,
)
from digital_life_host.multi_api import MultiHostWebAPI
from digital_life_host.multi_controller import AstrBotMultiController
from digital_life_host.private_debounce import PrivateDebounce
from digital_life_host import service_balance_script
from digital_life_host.setup_api import SetupWebAPI
from digital_life_host.storage_api import StorageWebAPI
from digital_life_host.web_api import StagingWebAPI, http_handler
from digital_life_host.works_api import WorksWebAPI


NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
INSTALLATION_ID = "rbac-test-installation"


def _downgrade_dashboard_user(runtime: StorageRuntime, username: str) -> None:
    """Bind a Dashboard user and downgrade their tier to STANDARD."""
    operator = dashboard_operator(runtime, username, at=NOW)
    policy = ObjectAccessPolicy(runtime)
    policy.set_tier(
        operator.subject,
        AuthorizationTier.STANDARD,
        expected_revision=policy.revision,
        observed_at=NOW,
    )


def _request_proxy(username="admin", *, query=None, payload: bytes = b""):
    query_obj = query if query is not None else PluginMultiDict([])

    async def body():
        return payload

    return NS(username=username, query=query_obj, body=body)


class ManagementIdentityRBACTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="rbac-id-")
        self.addCleanup(self._tmp.cleanup)
        self.runtime = StorageRuntime.open(
            data_root=Path(self._tmp.name),
            installation_id=INSTALLATION_ID,
        )
        self.addCleanup(self.runtime.close)

    def test_core_operator_allowed_and_downgraded_standard_rejected(self):
        core = require_management_operator(self.runtime, "core-admin", at=NOW)
        self.assertEqual(core.principal.native_account_id, "core-admin")

        _downgrade_dashboard_user(self.runtime, "low-priv")
        # dashboard_operator remains backward compatible and resolves the identity.
        resolved = dashboard_operator(self.runtime, "low-priv", at=NOW)
        self.assertEqual(resolved.principal.native_account_id, "low-priv")
        # require_management_operator enforces ObjectAccessPolicy(memory.manage -> CORE).
        with self.assertRaises(TaskError) as raised:
            require_management_operator(self.runtime, "low-priv", at=NOW)
        self.assertEqual(raised.exception.code, "core_access_required")

    def test_rejects_empty_whitespace_and_forged_api_key(self):
        for bad in ("", "   ", " admin ", "api_key:forged", None):
            with self.subTest(username=bad):
                with self.assertRaises(TaskError) as raised:
                    require_management_operator(self.runtime, bad, at=NOW)
                self.assertEqual(raised.exception.code, "dashboard_identity_required")

    def test_controller_helper_enforces_termination_and_bootstrap_bounds(self):
        terminated = NS(_terminated=True, runtime=self.runtime, now=lambda: NOW)
        with self.assertRaises(TaskError) as raised:
            require_controller_management_operator(terminated, "admin")
        self.assertEqual(raised.exception.code, "staging_host_terminated")

        uninitialized = NS(_terminated=False, runtime=None, now=lambda: NOW)
        self.assertIsNone(require_controller_management_operator(uninitialized, "admin"))
        with self.assertRaises(TaskError) as raised:
            require_controller_management_operator(uninitialized, "api_key:forged")
        self.assertEqual(raised.exception.code, "dashboard_identity_required")
        with self.assertRaises(TaskError) as raised:
            require_controller_management_operator(uninitialized, "admin", require_runtime=True)
        self.assertEqual(raised.exception.code, "staging_not_initialized")


class ManagementAPIsRBACTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="rbac-apis-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.runtime = StorageRuntime.open(
            data_root=self.root / "runtime",
            installation_id=INSTALLATION_ID,
        )
        self.addCleanup(self.runtime.close)
        # Pre-bind one CORE admin and one downgraded STANDARD user.
        dashboard_operator(self.runtime, "core-admin", at=NOW)
        _downgrade_dashboard_user(self.runtime, "standard-user")

    def _controller(self, **overrides):
        base = {
            "_control_lock": asyncio.Lock(),
            "_terminated": False,
            "runtime": self.runtime,
            "session": NS(application=NS(clear_caches=lambda: {"ok": 1})),
            "last_session": None,
            "data_root": self.root,
            "default_data_root": self.root,
            "profile_root": self.root,
            "config": {
                "storage": {"path": "", "backup_path": "", "retention_count": 5, "retention_days": 30},
                "voice_reply": {"enabled": False},
                "poke": {},
                "runtime": {},
                "multi_host": {"enabled": True, "accounts": [], "targets": [], "personas": [], "group_targets": []},
            },
            "settings": NS(
                installation_id=INSTALLATION_ID,
                personas=(NS(bot_id="bot-a", role_id="role-a", persona_id="p1"),),
                target=lambda target_id: NS(target_id=target_id, bot_id="bot-a"),
            ),
            "now": lambda: NOW,
            "status": lambda: {"mode": "multi", "targets": []},
            "probe": AsyncMock(return_value=Outcome.success({"ok": True})),
            "initialize_profile": AsyncMock(return_value=Outcome.success({"ok": True})),
            "authorize_delivery": AsyncMock(return_value=Outcome.success({"ok": True})),
            "prepare_screen": AsyncMock(return_value=Outcome.success({"ok": True})),
            "revoke_delivery": AsyncMock(return_value=Outcome.success({"ok": True})),
            "start": AsyncMock(return_value=Outcome.success({"ok": True})),
            "stop": AsyncMock(return_value=Outcome.success({"ok": True})),
            "inspect": AsyncMock(return_value=Outcome.success({"ok": True})),
            "preview": AsyncMock(return_value=Outcome.success({"ok": True})),
            "batches": MagicMock(return_value=Outcome.success({"items": []})),
        }
        base.update(overrides)
        return NS(**base)

    async def test_works_list_requires_core_and_catches_sqlite_error(self):
        controller = self._controller()
        api = WorksWebAPI(controller)
        query = PluginMultiDict([("target_id", "t1"), ("role_id", "role-a")])

        with patch("digital_life_host.works_api.request", _request_proxy("standard-user", query=query)):
            denied = await api.list_works()
        self.assertEqual(denied["status"], "error")
        self.assertEqual(denied["data"]["code"], "core_access_required")

        with patch("digital_life_host.works_api.request", _request_proxy("core-admin", query=query)), \
             patch("digital_life_host.works_api.read_works", return_value={"works": []}):
            allowed = await api.list_works()
        self.assertEqual(allowed["status"], "ok")

        with patch("digital_life_host.works_api.request", _request_proxy("core-admin", query=query)), \
             patch("digital_life_host.works_api.read_works", side_effect=sqlite3.OperationalError("locked")):
            db_err = await api.list_works()
        self.assertEqual(db_err["status"], "error")
        self.assertEqual(db_err["data"]["code"], "works_unavailable")

    async def test_log_api_requires_core_and_returns_error_envelope(self):
        controller = self._controller()
        api = LogWebAPI(controller)

        with patch("digital_life_host.log_api.request", _request_proxy("")):
            anon = await api.read_logs()
        self.assertEqual(anon["data"]["code"], "dashboard_identity_required")

        with patch("digital_life_host.log_api.request", _request_proxy("standard-user")):
            denied = await api.read_logs()
        self.assertEqual(denied["data"]["code"], "core_access_required")

        with patch("digital_life_host.log_api.request", _request_proxy("core-admin", query=PluginMultiDict([("limit", "bad")]))):
            bad_query = await api.read_logs()
        self.assertEqual(bad_query["status"], "error")
        self.assertEqual(bad_query["data"]["code"], "logs_query_invalid")

        with patch("digital_life_host.log_api.request", _request_proxy("core-admin")):
            ok = await api.read_logs()
        self.assertEqual(ok["status"], "ok")
        self.assertIn("records", ok["data"])
        self.assertIn("generated_at", ok["data"])

    async def test_account_session_api_rbac_and_missing_settings_attribute(self):
        controller = self._controller()
        delattr(controller, "settings")
        api = AccountSessionWebAPI(controller)
        api._discovered = AsyncMock(return_value={"accounts": [], "personas": [], "targets": []})

        # _view() must not raise AttributeError when controller has no .settings attribute.
        view = api._view()
        self.assertEqual(view["mode"], "multi")

        with patch("digital_life_host.account_session_api.request", _request_proxy("standard-user")):
            denied = await api.get()
        self.assertEqual(denied["data"]["code"], "core_access_required")

        with patch("digital_life_host.account_session_api.request", _request_proxy("api_key:forged")):
            forged = await api.get()
        self.assertEqual(forged["data"]["code"], "dashboard_identity_required")

        with patch("digital_life_host.account_session_api.request", _request_proxy("core-admin")):
            ok = await api.get()
        self.assertEqual(ok["status"], "ok")

    async def test_multi_api_rbac_route_wrapping_and_termination_guard(self):
        controller = self._controller(
            preview=MagicMock(return_value=Outcome.success({"parts": []})),
            context=NS(registered_web_apis=[]),
            unregister_routes=MagicMock(),
            register_routes=MagicMock(),
        )
        api = MultiHostWebAPI(controller)

        with patch("digital_life_host.multi_api.request", _request_proxy("standard-user")):
            for response in (
                await api.api_status(),
                await api.api_performance(),
                await api.control("start")(),
            ):
                self.assertEqual(response["status"], "error")
                self.assertEqual(response["data"]["code"], "core_access_required")

        # _ensure_role_routes must not resurrect zombie routes once terminated.
        controller._terminated = True
        api._ensure_role_routes([{"roles": [{"config_api": "/astrbot_plugin_digital_life/host/t/r/config"}]}])
        controller.unregister_routes.assert_not_called()
        controller.register_routes.assert_not_called()

        # _register_routes must wrap handlers in http_handler while preserving __self__.
        registered = []

        class DemoOwner:
            async def boom(self):
                raise RuntimeError("unhandled")

        owner = DemoOwner()
        api._register_routes(
            lambda r, h, m, d: registered.append((r, h, m, d)),
            [("/demo/boom", owner.boom, ["GET"], "demo")],
        )
        wrapped = registered[0][1]
        self.assertIs(wrapped.__self__, owner)
        envelope = await wrapped()
        self.assertEqual(envelope["status"], "error")
        self.assertEqual(envelope["data"]["code"], "plugin_web_internal_error")

    async def test_staging_web_api_requires_core_on_all_management_endpoints(self):
        controller = self._controller()
        api = StagingWebAPI(controller)

        endpoints = (
            api.api_status,
            api.api_probe,
            api.api_initialize,
            api.api_authorize_delivery,
            api.api_prepare_screen,
            api.api_upgrade_screen,
            api.api_revoke_delivery,
            api.api_start,
            api.api_stop,
            api.api_inspect,
            api.api_preview,
        )

        class MissingRequest:
            @property
            def username(self):
                raise RuntimeError("outside request context")

            query = {}

        with patch("digital_life_host.web_api.request", MissingRequest()):
            for endpoint in endpoints:
                response = await endpoint()
                self.assertEqual(response["data"]["code"], "dashboard_identity_required", endpoint.__name__)

        with patch("digital_life_host.web_api.request", _request_proxy("standard-user")):
            for endpoint in endpoints:
                response = await endpoint()
                self.assertEqual(response["data"]["code"], "core_access_required", endpoint.__name__)

        with patch("digital_life_host.web_api.request", _request_proxy("core-admin")):
            self.assertEqual((await api.api_probe())["status"], "ok")
            self.assertEqual((await api.api_start())["status"], "ok")
            self.assertEqual((await api.api_stop())["status"], "ok")

    async def test_backup_storage_and_host_options_reject_downgraded_standard_user(self):
        controller = self._controller()

        backup_api = BackupWebAPI(controller)
        with patch("digital_life_host.backup_api.request", _request_proxy("standard-user")):
            denied = await backup_api.listing()
        self.assertEqual(denied["data"]["code"], "core_access_required")
        with patch("digital_life_host.backup_api.request", _request_proxy("core-admin")):
            allowed = await backup_api.listing()
        self.assertEqual(allowed["status"], "ok", allowed)

        storage_api = StorageWebAPI(controller)
        with patch("digital_life_host.storage_api.request", _request_proxy("standard-user")):
            denied = await storage_api.status()
        self.assertEqual(denied["data"]["code"], "core_access_required")
        with patch("digital_life_host.storage_api.request", _request_proxy("core-admin")):
            allowed = await storage_api.status()
        self.assertEqual(allowed["status"], "ok", allowed)

        host_options_api = HostOptionsWebAPI(controller)
        with patch("digital_life_host.host_options_api.request", _request_proxy("standard-user")):
            denied = await host_options_api.status()
        self.assertEqual(denied["data"]["code"], "core_access_required")
        with patch("digital_life_host.host_options_api.request", _request_proxy("core-admin")):
            allowed = await host_options_api.status()
        self.assertEqual(allowed["status"], "ok", allowed)


class LifecycleAndLeakFixTests(unittest.IsolatedAsyncioTestCase):
    async def test_laya_and_balance_executors_shutdown_and_lazy_recreate(self):
        # Laya resources shutdown: cancels warmup, clears routers, shuts down pool, empties CUDA cache.
        warmup_cancelled = asyncio.Event()

        async def slow_warmup():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                warmup_cancelled.set()
                raise

        router_closed = []
        fake_router = NS(close=lambda: router_closed.append(True))
        laya_client._LOCAL_ROUTERS["demo"] = (fake_router, MagicMock())
        laya_client._LOCAL_READY.add("demo")
        laya_client._LOCAL_WARMUP_TASKS["demo"] = asyncio.create_task(slow_warmup())
        await asyncio.sleep(0)

        first_laya_pool = laya_client._get_local_inference_executor()
        empty_cache = MagicMock()
        fake_torch = NS(cuda=NS(is_available=lambda: True, empty_cache=empty_cache))
        with patch.dict(sys.modules, {"torch": fake_torch}):
            await laya_client.shutdown_laya_resources()

        self.assertTrue(warmup_cancelled.is_set())
        self.assertEqual(router_closed, [True])
        self.assertEqual(laya_client._LOCAL_ROUTERS, {})
        self.assertEqual(laya_client._LOCAL_READY, set())
        self.assertIsNone(laya_client._LOCAL_INFERENCE_EXECUTOR)
        empty_cache.assert_called_once()
        # Recreates cleanly after shutdown.
        second_laya_pool = laya_client._get_local_inference_executor()
        self.assertIsNot(second_laya_pool, first_laya_pool)
        await laya_client.shutdown_laya_resources()

        # Balance script executor shutdown and lazy recreate.
        first_balance_pool = service_balance_script._get_script_workers()
        service_balance_script.shutdown_balance_script_workers()
        self.assertIsNone(service_balance_script.SCRIPT_WORKERS)
        second_balance_pool = service_balance_script._get_script_workers()
        self.assertIsNot(second_balance_pool, first_balance_pool)
        service_balance_script.shutdown_balance_script_workers()

    async def test_multi_controller_terminate_closes_extensions_early_and_bounds_drain(self):
        controller = AstrBotMultiController.__new__(AstrBotMultiController)
        controller._terminated = False
        controller._accepting = True
        controller._termination_task = None
        controller._control_lock = asyncio.Lock()
        controller.session_topology = NS(close=AsyncMock())
        controller.stop = AsyncMock(return_value=Outcome.success(NS(state="draining")))
        extensions_closed = []
        controller.native_extensions = NS(aclose=AsyncMock(side_effect=lambda: extensions_closed.append(True)))
        runtime_closed = []
        controller.runtime = NS(close=lambda: runtime_closed.append(True))
        controller.authority = object()
        controller.session = NS(close=AsyncMock(return_value=NS(state="draining")))
        controller.last_session = None
        setup_closed = []
        controller._setup_api = NS(aclose=AsyncMock(side_effect=lambda: setup_closed.append(True)))
        controller.web_api = NS(setup=controller._setup_api, unregister=MagicMock())
        controller.unregister_routes = MagicMock()
        controller._failure = MagicMock()
        controller.TERMINATION_DRAIN_TIMEOUT_SECONDS = 0.05

        with patch("digital_life_host.exchange_rate.close_exchange_rate_services", AsyncMock()), \
             patch("digital_life_host.pricing_sources.close_pricing_catalog_services", AsyncMock()):
            await controller.terminate()

            # Native extensions and SetupWebAPI must already be closed before terminate() returns!
            self.assertEqual(extensions_closed, [True])
            self.assertEqual(setup_closed, [True])
            self.assertIsNone(controller.native_extensions)
            self.assertIsNotNone(controller._termination_task)

            # Bounded drain finishes within timeout and releases runtime.
            await asyncio.wait_for(controller._termination_task, timeout=2.0)

        self.assertEqual(runtime_closed, [True])
        self.assertIsNone(controller.runtime)
        self.assertIsNone(controller._termination_task)

    async def test_multi_controller_finish_termination_releases_runtime_on_exception(self):
        controller = AstrBotMultiController.__new__(AstrBotMultiController)
        controller._control_lock = asyncio.Lock()
        controller._termination_task = object()
        runtime_closed = []
        controller.runtime = NS(close=lambda: runtime_closed.append(True))
        controller.authority = object()
        controller.native_extensions = None
        controller.session = NS(close=AsyncMock(side_effect=RuntimeError("close boom")))
        controller.last_session = None
        controller._failure = MagicMock()

        await controller._finish_termination()

        self.assertEqual(runtime_closed, [True])
        self.assertIsNone(controller.runtime)
        self.assertIsNone(controller._termination_task)
        controller._failure.assert_called()

    async def test_setup_api_retains_instance_cancels_test_task_and_has_no_duplicates(self):
        with TemporaryDirectory(prefix="setup-close-") as tmpdir:
            controller = NS(
                data_root=Path(tmpdir),
                context=NS(registered_web_apis=[]),
                config={},
                settings=None,
                _terminated=False,
            )
            setup = SetupWebAPI(controller)
            cancelled = asyncio.Event()

            async def hanging_test():
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            setup._test_task = asyncio.create_task(hanging_test())
            await asyncio.sleep(0)
            await setup.aclose()
            self.assertTrue(cancelled.is_set())
            self.assertIsNone(setup._test_task)

        # Static hygiene: no duplicate keys in PROACTIVE_TEST_SUGGESTIONS or duplicate _configured_provider_id.
        source = (Path(__file__).resolve().parents[1] / "digital_life_host" / "setup_api.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "PROACTIVE_TEST_SUGGESTIONS":
                        keys = [k.value for k in node.value.keys if isinstance(k, ast.Constant)]
                        self.assertEqual(len(keys), len(set(keys)))
            elif isinstance(node, ast.ClassDef) and node.name == "SetupWebAPI":
                methods = [item.name for item in node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
                self.assertEqual(methods.count("_configured_provider_id"), 1)

    async def test_private_debounce_wait_for_resolution_timeout_and_stale_collecting_recovery(self):
        now = [100.0]
        manager = PrivateDebounce(policy=None, clock=lambda: now[0], resolution_timeout=0.05)
        e1, e2 = object(), object()
        r1 = manager.reserve("target-1", e1, "hi")
        r2 = manager.reserve("target-1", e2, "there")
        self.assertIs(r1.pending, r2.pending)
        self.assertEqual(await manager.configure(r1, e1, None), "configuring")

        # Simulate stalled owner e2; e1 must not hang forever even with a stationary clock.
        status = await asyncio.wait_for(
            manager.wait_for_resolution(r1, e1, timeout=0.03),
            timeout=1.0,
        )
        self.assertEqual(status, "aborted")
        self.assertNotIn("target-1", manager._pending)

        real_mgr = PrivateDebounce(policy=None, resolution_timeout=0.05)
        re1, re2 = object(), object()
        rr1 = real_mgr.reserve("target-2", re1, "hi")
        real_mgr.reserve("target-2", re2, "there")
        self.assertEqual(await real_mgr.configure(rr1, re1, None), "configuring")
        real_status = await asyncio.wait_for(real_mgr.wait_for_resolution(rr1, re1), timeout=1.0)
        self.assertEqual(real_status, "aborted")
        self.assertNotIn("target-2", real_mgr._pending)

        # Stale collecting entry is discarded when a later message arrives after resolution_timeout.
        stale_mgr = PrivateDebounce(policy=None, clock=lambda: now[0], resolution_timeout=30.0)
        stale_e1 = object()
        stale_r1 = stale_mgr.reserve("target-3", stale_e1, "old")
        now[0] += 31.0
        fresh_e2 = object()
        fresh_r2 = stale_mgr.reserve("target-3", fresh_e2, "new")
        self.assertIsNot(fresh_r2.pending, stale_r1.pending)
        self.assertEqual(stale_r1.pending.state, "aborted")
        self.assertEqual(len(fresh_r2.pending.entries), 1)

    async def test_main_private_debounce_aborts_on_cancellation_and_terminate_cleans_all(self):
        from tests.test_main_no_reply import ENTRY

        # Verify private debounce reservation is aborted if prepare_core_reply_context is cancelled.
        plugin = ENTRY.PrivateCompanionV2StagingPlugin.__new__(ENTRY.PrivateCompanionV2StagingPlugin)
        debounce = PrivateDebounce(policy=None)
        plugin._private_debounce = debounce
        plugin._private_debounce_manager = lambda: debounce
        plugin._private_delivery_duplicate = lambda *_args, **_kwargs: False
        plugin._origin_key_for_event = lambda _ev: "origin-1"
        plugin._harassment_features = lambda _ev: (0, 0)
        plugin._harassment_guard = lambda: NS(check=lambda *_a, **_kw: {"blocked": False})
        plugin._harassment_scope = lambda _ev: "scope-1"
        plugin._harassment_user = lambda _ev: "user-1"
        plugin._fold_private_follow_up = lambda *_args, **_kwargs: False
        plugin._record_no_reply = MagicMock()
        plugin._guard_passthrough_core_reply = AsyncMock()
        plugin._suppress_debounced_private_event = MagicMock()

        prepare_entered = asyncio.Event()

        async def slow_prepare(*_args, **_kwargs):
            prepare_entered.set()
            await asyncio.sleep(60)

        plugin.staging = NS(
            _terminated=False,
            _accepting=True,
            runtime=None,
            session=object(),
            now=lambda: NOW,
            claims=lambda _ev: True,
            target_for_event=lambda _ev: NS(target_id="priv-1", participant_id="u1"),
            session_topology=NS(reply_enabled=AsyncMock(return_value=True)),
            prepare_core_reply_context=slow_prepare,
        )
        event = NS(
            message_obj=NS(
                type=NS(value="FriendMessage"),
                message=[NS(type="Plain", text="hello")],
                message_str="hello",
                message_id="msg-1",
            ),
            message_str="hello",
            _has_send_oper=False,
            extra={},
            get_platform_name=lambda: "aiocqhttp",
            get_extra=lambda k, d=None: event.extra.get(k, d),
            set_extra=lambda k, v: event.extra.__setitem__(k, v),
        )

        with patch.object(ENTRY, "is_notice_event", return_value=False), \
             patch.object(ENTRY, "competing_command_handler", return_value=None), \
             patch.object(ENTRY, "receive_phone_confirmation", AsyncMock(return_value=None)), \
             patch.object(ENTRY, "receive_reset_command", AsyncMock(return_value=None)), \
             patch.object(ENTRY, "parse_calendar_command", return_value=None):
            task = asyncio.create_task(plugin.receive_companion_message(event))
            await asyncio.wait_for(prepare_entered.wait(), timeout=2.0)
            self.assertIn("priv-1", debounce._pending)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertNotIn("priv-1", debounce._pending)

        # Also verify cancellation during reply_enabled aborts the reservation.
        reply_entered = asyncio.Event()

        async def slow_reply_enabled(_ev):
            reply_entered.set()
            await asyncio.sleep(60)

        plugin.staging.session_topology = NS(reply_enabled=slow_reply_enabled)
        with patch.object(ENTRY, "is_notice_event", return_value=False), \
             patch.object(ENTRY, "competing_command_handler", return_value=None), \
             patch.object(ENTRY, "receive_phone_confirmation", AsyncMock(return_value=None)), \
             patch.object(ENTRY, "receive_reset_command", AsyncMock(return_value=None)), \
             patch.object(ENTRY, "parse_calendar_command", return_value=None):
            task = asyncio.create_task(plugin.receive_companion_message(event))
            await asyncio.wait_for(reply_entered.wait(), timeout=2.0)
            self.assertIn("priv-1", debounce._pending)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertNotIn("priv-1", debounce._pending)

        # Verify plugin.terminate() invokes all resource shutdown hooks independently.
        calls = []
        setup_mock = NS(aclose=AsyncMock(side_effect=lambda: calls.append("setup")))
        plugin.staging = NS(
            _setup_api=setup_mock,
            runtime=None,
            terminate=AsyncMock(side_effect=lambda: calls.append("staging")),
        )
        plugin._imports_retired = True
        plugin.extension_api = None
        plugin._private_debounce = None

        pkg = ENTRY.__package__
        prefix = f"{pkg}.digital_life_host" if pkg else "digital_life_host"
        with patch(f"{prefix}.laya_client.shutdown_laya_resources",
                   AsyncMock(side_effect=lambda: calls.append("laya"))), \
             patch(f"{prefix}.service_balance_script.shutdown_balance_script_workers",
                   side_effect=lambda: calls.append("balance")):
            await plugin.terminate()

        self.assertEqual(calls, ["setup", "staging", "laya", "balance"])


if __name__ == "__main__":
    unittest.main()
