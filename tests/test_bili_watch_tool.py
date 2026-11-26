"""The chat bili.watch tool exposes only bounded, read-only Bilibili material."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from digital_life_core.conversation.bili_tools import (ACTION, BiliWatchTools,
                                                       extract_bvid)
from digital_life_core.execution.models import TaskError
from digital_life_core.foundation.refs import ScopeKind

NOW = datetime(2026, 11, 26, tzinfo=timezone.utc)


class BiliWatchToolTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def context():
        return SimpleNamespace(scope=SimpleNamespace(kind=ScopeKind.PRIVATE))

    @staticmethod
    def group():
        return SimpleNamespace(scope=SimpleNamespace(kind=ScopeKind.GROUP))

    @staticmethod
    def fake_run(task_id="turn-1"):
        return SimpleNamespace(task=SimpleNamespace(task_id=task_id), validate=lambda: None)

    @staticmethod
    def video(bvid="BV1bW4y1P7Kf"):
        return SimpleNamespace(bvid=bvid, title="标题", author="UP 主", duration=321,
                               play=12345, desc="简介", cid=99, link=f"https://b23.tv/{bvid}")

    class Port:
        def __init__(self, calls, subtitle=True):
            self.calls = calls
            self.subtitle = subtitle

        async def search(self, keyword, page_size=12):
            self.calls.append(("search", keyword))
            return [BiliWatchToolTests.video() for _ in range(2)]

        async def video_info(self, bvid, force=False):
            self.calls.append(("video_info", bvid))
            return BiliWatchToolTests.video(bvid)

        async def subtitles(self, video, preferred_lang="ai-zh"):
            self.calls.append(("subtitles", video.bvid))
            if not self.subtitle:
                return None
            return SimpleNamespace(lang="ai-zh", text="第一句。\n第二句。")

        async def hot_comments(self, video, limit=10):
            self.calls.append(("hot_comments", video.bvid))
            return [SimpleNamespace(user="路人", text="说得好", like=7, rpid=1, is_top=False)]

        async def danmaku_hotspots(self, cid, duration=0, buckets=8):
            self.calls.append(("danmaku", cid))
            return [SimpleNamespace(minute=1, count=12, samples=("哈哈哈",))]

        async def close(self):
            self.calls.append(("close", None))

    async def test_catalog_is_available_only_when_a_factory_exists(self):
        self.assertEqual(BiliWatchTools().catalog_for(self.context()), ())
        tool = BiliWatchTools(lambda context: None)
        self.assertEqual([item.action for item in tool.catalog_for(self.context())], [ACTION])

    async def test_candidates_without_a_bv_id(self):
        calls = []
        tool = BiliWatchTools(lambda context: self.Port(calls))
        result = await tool.execute(self.fake_run(), self.context(),
                                    {"target": "机器人"}, observed_at=NOW)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["effect"], "none")
        payload = result["payload"]
        self.assertEqual((payload["mode"], payload["query"]), ("candidates", "机器人"))
        self.assertEqual(len(payload["candidates"]), 2)
        self.assertEqual(set(payload["candidates"][0]), {"bvid", "title", "author", "duration", "play", "link"})
        self.assertEqual(calls[-1], ("close", None))
        validated, _ = tool.validate({}, self.context(), result, NOW)
        self.assertEqual(validated, result)

    async def test_video_material_with_a_bv_id(self):
        calls = []
        tool = BiliWatchTools(lambda context: self.Port(calls))
        result = await tool.execute(self.fake_run(), self.context(),
                                    {"target": "看看 https://b23.tv/BV1bW4y1P7Kf 这个"},
                                    observed_at=NOW)
        self.assertEqual(result["state"], "succeeded")
        payload = result["payload"]
        self.assertEqual(payload["mode"], "video")
        self.assertEqual(payload["video"]["bvid"], "BV1bW4y1P7Kf")
        self.assertTrue(payload["subtitle"]["present"])
        self.assertEqual(payload["comments"][0]["user"], "路人")
        self.assertEqual(calls[-1], ("close", None))
        validated, _ = tool.validate({}, self.context(), result, NOW)
        self.assertEqual(validated, result)

    async def test_video_material_without_subtitles_still_validates(self):
        calls = []
        tool = BiliWatchTools(lambda context: self.Port(calls, subtitle=False))
        result = await tool.execute(self.fake_run(), self.context(),
                                    {"target": "BV1bW4y1P7Kf"}, observed_at=NOW)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["payload"]["subtitle"]["present"], False)
        validated, _ = tool.validate({}, self.context(), result, NOW)
        self.assertEqual(validated, result)

    async def test_invalid_arguments_are_rejected_without_calling_the_port(self):
        calls = []
        tool = BiliWatchTools(lambda context: self.Port(calls))
        for arguments in ({}, {"target": ""}, {"target": "x" * 129}, {"target": "x", "extra": True}):
            with self.assertRaises(TaskError):
                await tool.execute(self.fake_run(), self.context(), arguments, observed_at=NOW)
        self.assertEqual(calls, [])

    async def test_missing_factory_fails_closed(self):
        tool = BiliWatchTools(lambda context: None)
        result = await tool.execute(self.fake_run(), self.context(),
                                    {"target": "机器人"}, observed_at=NOW)
        self.assertEqual((result["state"], result["code"]), ("failed", "bili_watch_unavailable"))

    async def test_failure_codes_map_the_vendored_error_names(self):
        calls = []

        class Port(BiliWatchToolTests.Port):
            async def search(self, keyword, page_size=12):
                from digital_life_core.bilibili.errors import BiliRisk

                raise BiliRisk("风控", -412, 5)

        tool = BiliWatchTools(lambda context: Port(calls))
        result = await tool.execute(self.fake_run(), self.context(),
                                    {"target": "机器人"}, observed_at=NOW)
        self.assertEqual((result["state"], result["code"]), ("failed", "bili_watch_rate_limited"))

    async def test_one_turn_is_limited_to_three_watches(self):
        calls = []
        tool = BiliWatchTools(lambda context: self.Port(calls))
        run = self.fake_run()
        for _ in range(3):
            result = await tool.execute(run, self.context(), {"target": "机器人"}, observed_at=NOW)
            self.assertEqual(result["state"], "succeeded")
        result = await tool.execute(run, self.context(), {"target": "机器人"}, observed_at=NOW)
        self.assertEqual((result["state"], result["code"]), ("failed", "bili_watch_turn_limit"))

    def test_extract_bvid_is_case_sensitive_and_finds_links(self):
        self.assertEqual(extract_bvid("https://www.bilibili.com/video/BV1bW4y1P7Kf?p=1"), "BV1bW4y1P7Kf")
        self.assertIsNone(extract_bvid("bv1bw4y1p7kf"))

    async def test_real_port_lifecycle_starts_before_reading(self):
        calls = []

        class Port(BiliWatchToolTests.Port):
            def __init__(self, calls):
                super().__init__(calls)
                self.started = False

            async def start(self):
                self.started = True
                self.calls.append(("start", None))

        port = Port(calls)
        tool = BiliWatchTools(lambda context: port)
        result = await tool.execute(self.fake_run(), self.context(),
                                    {"target": "机器人"}, observed_at=NOW)
        self.assertEqual(result["state"], "succeeded")
        self.assertTrue(port.started)
        self.assertEqual(calls[0], ("start", None))
        self.assertEqual(calls[-1], ("close", None))

    async def test_see_true_attaches_a_sight_description(self):
        calls = []

        class Seer:
            def __init__(self):
                self.ports = []

            async def describe(self, port, bvid):
                self.ports.append((port, bvid))
                return {"present": True, "frames": 6, "offsets": [0.5, 53.6],
                        "description": "画面里有一条鲸鱼在跳舞。"}

        seer = Seer()
        tool = BiliWatchTools(lambda context: self.Port(calls), seer)
        result = await tool.execute(self.fake_run(), self.context(),
                                    {"target": "BV1bW4y1P7Kf", "see": True},
                                    observed_at=NOW)
        self.assertEqual(result["state"], "succeeded")
        sight = result["payload"]["sight"]
        self.assertTrue(sight["present"])
        self.assertEqual((sight["frames"], sight["description"][:9]), (6, "画面里有一条鲸鱼在"))
        self.assertEqual(seer.ports[-1][1], "BV1bW4y1P7Kf")
        validated, _ = tool.validate({}, self.context(), result, NOW)
        self.assertEqual(validated, result)

    async def test_see_true_degrades_gracefully_without_a_seer_or_on_failure(self):
        calls = []
        tool = BiliWatchTools(lambda context: self.Port(calls))
        result = await tool.execute(self.fake_run(), self.context(),
                                    {"target": "BV1bW4y1P7Kf", "see": True},
                                    observed_at=NOW)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["payload"]["sight"],
                         {"present": False, "reason": "seer_unavailable"})
        validated, _ = tool.validate({}, self.context(), result, NOW)

        class BrokenSeer:
            async def describe(self, port, bvid):
                raise RuntimeError("ffmpeg 缺失")

        calls.clear()
        tool = BiliWatchTools(lambda context: self.Port(calls), BrokenSeer())
        result = await tool.execute(self.fake_run(), self.context(),
                                    {"target": "BV1bW4y1P7Kf", "see": True},
                                    observed_at=NOW)
        self.assertEqual(result["state"], "succeeded")
        sight = result["payload"]["sight"]
        self.assertEqual((sight["present"], sight["reason"]), (False, "ffmpeg 缺失"))
        validated, _ = tool.validate({}, self.context(), result, NOW)
        self.assertEqual(validated, result)

    async def test_see_true_skips_candidates_mode_and_bad_arguments_fail(self):
        calls = []
        tool = BiliWatchTools(lambda context: self.Port(calls))
        result = await tool.execute(self.fake_run(), self.context(),
                                    {"target": "鲸鱼", "see": True}, observed_at=NOW)
        self.assertEqual(result["state"], "succeeded")
        self.assertNotIn("sight", result["payload"])
        for bad in ({"target": "BV1bW4y1P7Kf", "see": "yes"},
                    {"target": "BV1bW4y1P7Kf", "extra": 1}):
            with self.assertRaises(TaskError):
                BiliWatchTools._arguments(bad)

    async def test_look_at_returns_a_focused_sight(self):
        calls = []

        class Seer:
            def __init__(self):
                self.moments = []

            async def describe(self, port, bvid, *, at=None):
                self.moments.append(at)
                return {"present": True, "frames": 5, "offsets": [22.0, 24.5, 27.0],
                        "changes": [{"at": 24.5, "score": 18.2}],
                        "description": "第24秒左右画面切到了特写。"}

        seer = Seer()
        tool = BiliWatchTools(lambda context: self.Port(calls), seer)
        result = await tool.execute(self.fake_run(), self.context(),
                                    {"target": "BV1bW4y1P7Kf", "at": 25},
                                    observed_at=NOW)
        self.assertEqual(result["state"], "succeeded")
        payload = result["payload"]
        self.assertEqual((payload["mode"], payload["at"]), ("look", 25.0))
        self.assertEqual(set(payload["video"]), {"bvid", "title", "author", "duration"})
        sight = payload["sight"]
        self.assertTrue(sight["present"])
        self.assertEqual(sight["changes"], [{"at": 24.5, "score": 18.2}])
        self.assertEqual(seer.moments, [25.0])
        # 回看不拉全量材料：没有字幕/热评/弹幕调用。
        self.assertNotIn(("subtitles", "BV1bW4y1P7Kf"), calls)
        validated, _ = tool.validate({}, self.context(), result, NOW)
        self.assertEqual(validated, result)

    async def test_look_at_sight_failure_still_validates(self):
        calls = []

        class Seer:
            async def describe(self, port, bvid, *, at=None):
                raise RuntimeError("视频流不可用")

        tool = BiliWatchTools(lambda context: self.Port(calls), Seer())
        result = await tool.execute(self.fake_run(), self.context(),
                                    {"target": "BV1bW4y1P7Kf", "at": 0},
                                    observed_at=NOW)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual((result["payload"]["mode"], result["payload"]["at"]), ("look", 0.0))
        self.assertEqual(result["payload"]["sight"],
                         {"present": False, "reason": "视频流不可用"})
        validated, _ = tool.validate({}, self.context(), result, NOW)
        self.assertEqual(validated, result)

    async def test_bad_at_arguments_are_rejected(self):
        for bad in ({"target": "BV1bW4y1P7Kf", "at": "25"},
                    {"target": "BV1bW4y1P7Kf", "at": -1},
                    {"target": "BV1bW4y1P7Kf", "at": 86401},
                    {"target": "BV1bW4y1P7Kf", "at": True}):
            with self.assertRaises(TaskError):
                BiliWatchTools._arguments(bad)

    def test_video_sight_validation_accepts_changes_and_rejects_malformed_rows(self):
        tool = BiliWatchTools()
        good = {"state": "succeeded", "effect": "none", "payload": {
            "mode": "look", "at": 25.0,
            "video": {"bvid": "BV1bW4y1P7Kf", "title": "标题", "author": "UP 主", "duration": 321},
            "sight": {"present": True, "frames": 5, "offsets": [22.0, 27.0],
                      "changes": [{"at": 24.5, "score": 18.2}],
                      "description": "特写。"},
            "observed_at": NOW.isoformat()}}
        validated, _ = tool.validate({}, self.context(), good, NOW)
        self.assertEqual(validated, good)
        bad = {"state": "succeeded", "effect": "none", "payload": {
            "mode": "look", "at": 25.0,
            "video": {"bvid": "BV1bW4y1P7Kf", "title": "标题", "author": "UP 主", "duration": 321},
            "sight": {"present": True, "frames": 5, "offsets": [22.0],
                      "changes": [{"at": 24.5, "score": "high"}],
                      "description": "特写。"},
            "observed_at": NOW.isoformat()}}
        from digital_life_core.execution.models import TaskError as _TE
        with self.assertRaises(_TE):
            tool.validate({}, self.context(), bad, NOW)


if __name__ == "__main__":
    unittest.main()
