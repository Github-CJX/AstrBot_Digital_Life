"""Synthetic Bilibili curiosity contact: read-only candidates, no platform writes."""

import json
from dataclasses import replace
from datetime import datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

from digital_life_core.execution.models import digest, stamp
from digital_life_core.foundation.outcome import Outcome
from digital_life_core.foundation.refs import ScopeKind
from digital_life_core.identity_policy.personas import ConversationRoute
from digital_life_core.proactive.bili_events import (BiliWatchContactEvents, CuriosityState,
                                                     DEFAULT_CURIOSITY, SOURCE_OWNER, curiosity_settings)
from tests.contact_schedule_support import ContactScheduleFixture
from tests.test_management_access import SCOPE

WAKE_SETTINGS = {"wake_start_hour": 0, "wake_end_hour": 24, "skip_chance_percent": 0,
                 "max_per_day": 2, "min_gap_hours": 1}


def video(bvid, *, title=None, author="up 主", duration=120, play=1000, desc="简介"):
    return SimpleNamespace(bvid=bvid, title=title or ("视频" + bvid), author=author, duration=duration,
                           play=play, link="https://www.bilibili.com/video/" + bvid, desc=desc)


class FakePort:
    def __init__(self, videos=None, error=None):
        self.videos, self.error = list(videos or ()), error
        self.calls, self.trace = 0, []

    async def start(self):
        self.trace.append("start")

    async def close(self):
        self.trace.append("close")

    async def recommend(self, page_size=24):
        self.trace.append("recommend")
        self.calls += 1
        if self.error is not None and self.calls == 1:
            raise self.error
        return self.videos[:page_size]


class Prefs:
    def __init__(self, *, automatic=True, timezone="Asia/Shanghai", environment=None):
        self.values = {"automatic_enabled": automatic, "quiet_timezone": timezone}
        self._environment = environment

    def reason(self, _key):
        return self._environment


def stub_contacts(clock):
    sources = SimpleNamespace(
        publish=lambda record, *, observed_at: Outcome.success(record),
        validate_reference_for_audience=lambda reference, *, recipient, observed_at: Outcome.success(True))
    preferences = SimpleNamespace(read=lambda context, *, observed_at: Outcome.success(Prefs()))
    return SimpleNamespace(runtime=SimpleNamespace(), sources=sources, preferences=preferences,
                           tasks=SimpleNamespace(now=clock.now))


class BiliContactTests(ContactScheduleFixture, unittest.IsolatedAsyncioTestCase):
    async def setup_bili(self, factory, *, settings=None, state_path=None, **changes):
        host, batch = await self.scheduled(bili_factory=factory, bili_settings=settings or WAKE_SETTINGS,
                                           bili_state_path=state_path)
        app = host.application
        # Keep the ordinary conversation slot quiet so the hourly wake sweep can
        # reach the environment chain (weather/news are off by default here).
        self.settings(app, {"chat.enabled": False, "affection.enabled": False, "care.enabled": False,
                            "play.enabled": False, "repair.enabled": False, "invitation.enabled": False,
                            **changes}).unwrap()
        return app, self.context(app, batch)

    def bili_contacts(self, app):
        return [row for row in self.contacts(app)
                if "bili_event" in json.loads(row["record_json"]).get("automatic_origin", {})]

    def bili_origin(self, app, index=-1):
        return json.loads(self.bili_contacts(app)[index]["record_json"])["automatic_origin"]["bili_event"]

    async def wake(self, app, *, count=1, attempts=30, port=None):
        for _ in range(attempts):
            self.advance(app, 3600)
            app.contact_schedule.advance()
            try:
                await self.until(lambda: len(self.bili_contacts(app)) >= count or app.state == "faulted",
                                 seconds=5)
            except TimeoutError:
                continue
            break
        else:
            origins = [sorted(json.loads(row["record_json"]).get("automatic_origin", {}).keys())
                       for row in self.contacts(app)]
            owners = sorted({material.owner for item in self.contact_calls()
                             for material in item.original.context.materials})
            notes = [dict(row) for row in app.runtime.read_all(
                "SELECT stage, outcome, reason_code, purpose FROM proactive_schedule_diagnostics", ())]
            port_state = (port.calls, port.trace) if port is not None else None
            self.fail(f"curiosity wake never produced a contact; origins={origins}; "
                      f"material_owners={owners}; notes={notes}; port={port_state}")
        self.assertEqual(app.state, "running", app.failure_code)
        task_id = self.bili_contacts(app)[-1]["task_id"]
        return await self.delivery_state(app, task_id, "delivered")

    async def test_curiosity_wake_delivers_read_only_candidates_once(self):
        with TemporaryDirectory(prefix="bili-curious-") as temporary:
            state_path = Path(temporary) / "bilibili" / "curiosity.json"
            videos = [video("BV1%02d" % index) for index in range(10)] + [video("BV103")]
            port = FakePort(videos)
            app, context = await self.setup_bili(lambda _context: port, state_path=state_path)
            await self.wake(app)
            origin = self.bili_origin(app)
            self.assertTrue(origin["event_id"].startswith("bili-"))
            call = next(item for item in self.contact_calls()
                        if any(material.owner == SOURCE_OWNER for material in item.original.context.materials))
            material = next(material for material in call.original.context.materials
                            if material.owner == SOURCE_OWNER)
            body = json.loads(material.payload)
            self.assertEqual(body["event"], "bili_curiosity_batch")
            self.assertEqual(len(body["candidates"]), 8)
            self.assertEqual({row["bvid"] for row in body["candidates"]},
                             {row["bvid"] for row in body["candidates"][:8]})
            self.assertNotIn("_receipt", body)
            self.assertNotIn("_receipt", material.payload)
            self.assertEqual(port.trace[0], "start")
            self.assertLess(port.trace.index("recommend"), port.trace.index("close"))
            self.assertTrue(state_path.exists())
            state = json.loads(state_path.read_text(encoding="utf-8"))
            entry = state.get(context.key) or next(iter(state.values()))
            self.assertEqual(entry["last_event"]["id"], origin["event_id"])

    async def test_failed_fetch_retries_without_contacting_or_recording(self):
        with TemporaryDirectory(prefix="bili-curious-") as temporary:
            state_path = Path(temporary) / "bilibili" / "curiosity.json"
            port = FakePort([video("BV1fail")], error=RuntimeError("network down"))
            app, context = await self.setup_bili(lambda _context: port, state_path=state_path)
            await self.wake(app)
            self.assertEqual(port.calls, 2)
            self.assertEqual(len(self.bili_contacts(app)), 1)
            state = json.loads(state_path.read_text(encoding="utf-8"))[context.key]
            self.assertEqual(len(state["wakes"]), 1)

    async def test_persisted_wake_state_caps_the_same_day_after_restart(self):
        with TemporaryDirectory(prefix="bili-curious-") as temporary:
            root = Path(temporary)
            port = FakePort([video("BV1restart")])
            app, _ = await self.setup_bili(lambda _context: port,
                                           settings={"wake_start_hour": 0, "wake_end_hour": 24,
                                                     "skip_chance_percent": 0, "max_per_day": 1,
                                                     "min_gap_hours": 0},
                                           state_path=root / "bilibili" / "curiosity.json")
            await self.wake(app)
            self.assertEqual(len(self.bili_contacts(app)), 1)
            await app.close()
            restarted = (await self.start(self.host(runtime=self.open_runtime(),
                bili_factory=lambda _context: port, bili_settings={"wake_start_hour": 0,
                    "wake_end_hour": 24, "skip_chance_percent": 0, "max_per_day": 1, "min_gap_hours": 0},
                bili_state_path=root / "bilibili" / "curiosity.json"), 1, 1)).application
            for _ in range(3):
                self.advance(restarted, 60)
                restarted.contact_schedule.advance()
            await self.until(lambda: not restarted.contact_schedule.active_handles)
            self.assertEqual(len(self.bili_contacts(restarted)), 1)
            fresh = (await self.start(self.host(runtime=self.open_runtime(),
                bili_factory=lambda _context: port, bili_settings={"wake_start_hour": 0,
                    "wake_end_hour": 24, "skip_chance_percent": 0, "max_per_day": 1, "min_gap_hours": 0},
                bili_state_path=root / "bilibili" / "other.json"), 2, 2)).application
            await self.wake(fresh, count=2)
            self.assertEqual(port.calls, 2)


class BiliClockTests(ContactScheduleFixture, unittest.IsolatedAsyncioTestCase):
    def events(self, factory, *, settings=None, state_path=None):
        contacts = stub_contacts(self.clock)
        return BiliWatchContactEvents(contacts, factory, lambda: settings, state_path)

    def context(self, *, scope=None):
        return self.contact_context(scope=scope if scope is not None else SCOPE)

    def planned(self, events, context, at, settings=None):
        settings = curiosity_settings(settings)
        zone = ZoneInfo("Asia/Shanghai")
        local = at.astimezone(zone)
        day = local.date().isoformat()
        window_start = datetime.combine(local.date(), time(settings["wake_start_hour"]), tzinfo=zone)
        span = timedelta(hours=settings["wake_end_hour"] - settings["wake_start_hour"])
        jitter = int(digest(["bili-wake-v1", context.key, day])[:8], 16) % max(1,
            int(span.total_seconds() // 60))
        return window_start + timedelta(minutes=jitter)

    def prefs(self):
        return Prefs()

    async def test_gates_refuse_group_scope_and_disabled_automatic(self):
        events = self.events(lambda _context: FakePort())
        group = replace(SCOPE, kind=ScopeKind.GROUP, native_scope_id="group-a",
                        conversation_id="conversation-g", session_id="session-g")
        self.personas.register_route(ConversationRoute(group, group_id="group-a"),
                                     observed_at=self.clock.now())
        self.grant(self.important, group, "proactive.contact")
        group_context = self.contact_context(group)
        self.assertIsNone(events.select(group_context, self.prefs(), self.clock.now()))
        self.assertIsNone(events.select(self.context(),
            Prefs(automatic=False), self.clock.now()))
        self.assertIsNone(events.select(self.context(),
            Prefs(environment="读旁白"), self.clock.now()))
        self.assertIsNone(BiliWatchContactEvents(stub_contacts(self.clock), None).select(
            self.context(), self.prefs(), self.clock.now()))

    async def test_wake_window_and_daily_skip_gate_the_clock(self):
        at = self.clock.now()  # 08:00 Shanghai local, before the default 9 o'clock window
        events = self.events(lambda _context: FakePort())
        self.assertIsNone(events.select(self.context(), self.prefs(), at))
        rolling = self.events(lambda _context: FakePort(), settings={"skip_chance_percent": 100})
        self.assertIsNone(rolling.select(self.context(), self.prefs(), at))
        capped = self.events(lambda _context: FakePort(), settings={"max_per_day": 0})
        self.assertIsNone(capped.select(self.context(), self.prefs(), at))

    async def test_jitter_requires_the_planned_minute(self):
        at = self.clock.now()
        settings = {"wake_start_hour": 0, "wake_end_hour": 24, "skip_chance_percent": 0,
                    "max_per_day": 2, "min_gap_hours": 0}
        events = self.events(lambda _context: FakePort(), settings=settings)
        context = self.context()
        planned = self.planned(events, context, at, settings)
        self.assertIsNone(events.select(context, self.prefs(), planned - timedelta(minutes=1)))
        self.assertIsNotNone(events.select(context, self.prefs(), planned + timedelta(seconds=30)))

    async def test_state_wakes_cap_and_gap_gate_repeats(self):
        settings = {"wake_start_hour": 0, "wake_end_hour": 24, "skip_chance_percent": 0,
                    "max_per_day": 8, "min_gap_hours": 3}
        events = self.events(lambda _context: FakePort(), settings=settings)
        context = self.context()
        at = self.clock.now()
        # Four hours ago (same local day): older than the three-hour gap, so a wake is allowed.
        events.state.save({context.key: {"wakes": [stamp(at - timedelta(hours=4))]}})
        self.assertIsNotNone(events.select(context, self.prefs(), at))
        # Two hours ago: inside the gap, blocked.
        events.state.save({context.key: {"wakes": [stamp(at - timedelta(hours=2))]}})
        self.assertIsNone(events.select(context, self.prefs(), at))
        # 25 hours ago: a different local day, so the entry is filtered out and wakes again.
        events.state.save({context.key: {"wakes": [stamp(at - timedelta(hours=25))]}})
        self.assertIsNotNone(events.select(context, self.prefs(), at))

    async def test_prepare_publishes_candidates_and_validates_marker(self):
        settings = {"wake_start_hour": 0, "wake_end_hour": 24, "skip_chance_percent": 0,
                    "max_per_day": 2, "min_gap_hours": 0}
        events = self.events(lambda _context: FakePort([video("BV1unit")]), settings=settings)
        context = self.context()
        at = self.clock.now()
        marker = events.select(context, self.prefs(), at)[1]
        prepared = await events.prepare(context, marker)
        opportunity, intents, published = prepared
        self.assertEqual(intents[0].purpose, "environment")
        self.assertEqual(set(published), {"event_id", "settings_hash", "source"})
        self.assertLessEqual(opportunity.expires_at, at + timedelta(minutes=60))
        events.validate(context, published, at)
        events.state.save({})
        with self.assertRaises(Exception) as caught:
            events.validate(context, published, at)
        self.assertEqual(getattr(caught.exception, "code", None), "contact_bili_changed")

    async def test_prepare_failure_sets_retry_window(self):
        settings = {"wake_start_hour": 0, "wake_end_hour": 24, "skip_chance_percent": 0,
                    "max_per_day": 2, "min_gap_hours": 0}
        port = FakePort(error=RuntimeError("down"))
        events = self.events(lambda _context: port, settings=settings)
        context = self.context()
        at = self.clock.now()
        marker = events.select(context, self.prefs(), at)[1]
        self.assertIsNone(await events.prepare(context, marker))
        self.assertIsNone(events.select(context, self.prefs(), at))
        self.clock.advance(timedelta(minutes=31))
        self.assertIsNotNone(events.select(context, self.prefs(), self.clock.now()))

    async def test_curiosity_settings_are_clamped_to_the_fixed_shape(self):
        self.assertEqual(curiosity_settings(None), DEFAULT_CURIOSITY)
        self.assertEqual(curiosity_settings("nope"), DEFAULT_CURIOSITY)
        clamped = curiosity_settings({"wake_start_hour": -5, "wake_end_hour": 99,
                                      "skip_chance_percent": -1, "max_per_day": 99, "min_gap_hours": 99,
                                      "stray": "ignored"})
        self.assertEqual((clamped["wake_start_hour"], clamped["wake_end_hour"],
                          clamped["skip_chance_percent"], clamped["max_per_day"], clamped["min_gap_hours"]),
                         (0, 24, 0, 8, 24))
        self.assertNotIn("stray", clamped)
        self.assertEqual(curiosity_settings({"wake_start_hour": 1.5})["wake_start_hour"], 9)


class CuriosityStateTests(unittest.TestCase):
    def test_memory_mode_round_trips_and_copies(self):
        state = CuriosityState()
        state.save({"a": {"wakes": ["x"]}})
        self.assertEqual(state.load(), {"a": {"wakes": ["x"]}})
        state.save({"a": {"wakes": ["x", "y"]}})
        self.assertEqual(state.load(), {"a": {"wakes": ["x", "y"]}})

    def test_file_mode_survives_reload_and_tolerates_corruption(self):
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "curiosity.json"
            state = CuriosityState(path)
            self.assertEqual(state.load(), {})
            state.save({"a": {"last_event": {"id": "bili-1"}}})
            self.assertEqual(CuriosityState(path).load(), {"a": {"last_event": {"id": "bili-1"}}})
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(CuriosityState(path).load(), {})
            path.write_text("[]", encoding="utf-8")  # valid JSON but not a dict body is caller's problem
            self.assertEqual(CuriosityState(path).load(), [])


if __name__ == "__main__":
    unittest.main()
