"""M2.6d 看画面管线单测：官方 token 公式 / 帧差选帧 / describe 接线 / ffmpeg 拼图烟雾。"""

import asyncio
import shutil
import unittest
from types import SimpleNamespace

from digital_life_host import bili_see as see_mod
from digital_life_host.bili_see import (BiliSee, fit_sheet, frame_diff, hsv_grid,
                                        image_tokens, select_offsets)


def _async(value):
    async def _call():
        return value
    return _call()


def grid_of(hue, sat=255, val=180):
    g = bytearray(see_mod.GRID_W * see_mod.GRID_H * 3)
    for i in range(see_mod.GRID_W * see_mod.GRID_H):
        g[i * 3] = hue & 0xFF
        g[i * 3 + 1] = sat
        g[i * 3 + 2] = val
    return g


class TokenFormulaTests(unittest.TestCase):
    def test_matches_official_examples(self):
        # dsh-see-now README 实测值：1036×583=366、1066×600=407。
        self.assertEqual(image_tokens(1036, 583), 366)
        self.assertEqual(image_tokens(1066, 600), 407)

    def test_cap_and_floor(self):
        self.assertLessEqual(image_tokens(4096, 2160), see_mod.IMAGE_TOKEN_CAP)
        # 计费地板：小图按 544×544 面积收费。
        floor = image_tokens(100, 100)
        self.assertGreaterEqual(floor, 100)
        self.assertEqual(image_tokens(0, 0), 0)

    def test_fit_sheet_respects_budget(self):
        layout = fit_sheet(16 / 9, 420)
        self.assertIsNotNone(layout)
        w, h, tokens = layout
        self.assertLessEqual(tokens, 420)
        self.assertEqual(image_tokens(w, h), tokens)
        # 预算更大 → 画布不缩水。
        bigger = fit_sheet(16 / 9, 800)
        self.assertGreaterEqual(bigger[0] * bigger[1], w * h)


class FrameDiffTests(unittest.TestCase):
    def test_identical_frames_score_zero(self):
        grid = grid_of(120)
        self.assertEqual(frame_diff(grid, grid), 0.0)

    def test_missing_grid_scores_zero(self):
        self.assertEqual(frame_diff(None, grid_of(1)), 0.0)

    def test_hue_wraps_around(self):
        # 色相 250 vs 5：环形距离 10，不是 245 → diff≈3.3；不回绕会到 ~81。
        diff = frame_diff(grid_of(250, sat=255), grid_of(5, sat=255))
        self.assertLess(diff, 5.0)
        self.assertGreater(diff, 0.0)

    def test_different_frames_score_high(self):
        diff = frame_diff(grid_of(10), grid_of(200))
        self.assertGreater(diff, 15.0)

    def test_desaturated_frames_ignore_hue(self):
        diff = frame_diff(grid_of(10, sat=0), grid_of(200, sat=0))
        self.assertEqual(diff, 0.0)


class SelectOffsetsTests(unittest.TestCase):
    def test_first_frame_and_spike_picked(self):
        offsets = [0.5, 24.1, 47.7, 71.3, 94.9, 118.5]
        grids = [grid_of(100)] * len(offsets)
        grids[2] = grid_of(250)  # 唯一变化尖峰
        picked = select_offsets(offsets, grids, 3, min_gap=20.0)
        self.assertEqual(picked[0], 0.5)
        self.assertIn(47.7, picked)
        self.assertEqual(len(picked), 3)

    def test_min_gap_spreads_selection(self):
        offsets = [0.0, 1.0, 2.0, 100.0, 101.0, 102.0]
        grids = [grid_of(100 + i * 10) for i in range(len(offsets))]
        picked = select_offsets(offsets, grids, 3, min_gap=50.0)
        gaps = [b - a for a, b in zip(picked, picked[1:])]
        self.assertTrue(all(g >= 50.0 for g in gaps))

    def test_short_input_returns_all(self):
        offsets = [1.0, 2.0, 3.0]
        self.assertEqual(select_offsets(offsets, [None] * 3, 6, min_gap=1.0), offsets)


class DescribeWiringTests(unittest.TestCase):
    def _seer(self):
        seer = BiliSee({"bilibili": {"see": {"api_base": "https://gw.example", "key": "k",
                                             "model": "m", "frames": 3}}},
                       "/tmp/nonexistent-root")
        return seer

    def test_describe_selects_spike_and_returns_result(self):
        seer = self._seer()
        offsets = seer._probe_offsets(120)
        grids = [grid_of(100)] * len(offsets)
        grids[2] = grid_of(250)
        blobs = [b"x" * 10] * len(offsets)

        async def fake_probe(url, offs):
            return grids, blobs

        async def fake_vision(port, url, video, picked, picked_blobs):
            fake_vision.picked = picked
            return "描述文本"

        seer._probe_frames = fake_probe
        seer._vision = fake_vision
        port = SimpleNamespace(
            video_info=lambda bvid: _async(SimpleNamespace(duration=120, title="t", author="a", desc="d")),
            play_url=lambda video: _async({"url": "https://cdn.example/video.m4s"}),
        )
        result = asyncio.run(seer.describe(port, "BVxxx"))
        self.assertTrue(result["present"])
        self.assertEqual(result["frames"], 3)
        self.assertEqual(result["description"], "描述文本")
        self.assertEqual(fake_vision.picked[0], offsets[0])
        self.assertIn(offsets[2], fake_vision.picked)

    def test_describe_requires_available(self):
        seer = BiliSee({"bilibili": {"see": {}}}, "/tmp/nonexistent-root")
        port = SimpleNamespace(video_info=lambda bvid: None, play_url=lambda v: {})
        from digital_life_core.bilibili.errors import BiliError
        with self.assertRaises(BiliError):
            asyncio.run(seer.describe(port, "BVxxx"))


class ComposeSheetSmokeTests(unittest.TestCase):
    """真 ffmpeg 拼图烟雾测试：本机/容器有 ffmpeg 才跑。"""

    @unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
    def test_compose_sheet_produces_canvas(self):
        seer = BiliSee({"bilibili": {"see": {"api_base": "https://gw.example", "key": "k",
                                             "model": "m"}}}, "/tmp/nonexistent-root")
        cells = []
        for i in range(6):
            cells.append((i * 10.0, _solid_jpg(i, f"{i}")))
        blob = asyncio.run(seer._compose_sheet(cells, 160, 90, 480, 270))
        self.assertIsNotNone(blob)
        self.assertGreater(len(blob), 1024)
        # 画布尺寸正确（6 格 2×3 → 480×270）；无 Pillow 就只验字节数。
        try:
            from PIL import Image
        except ImportError:
            return
        import io
        with Image.open(io.BytesIO(blob)) as img:
            self.assertEqual(img.size, (480, 270))


_PALETTE = ["red", "green", "blue", "yellow", "magenta", "cyan"]


def _solid_jpg(index: int, label: str):
    """ffmpeg 生成一张纯色测试图（不依赖 Pillow 的绘制）。"""
    import subprocess

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", f"color=c={_PALETTE[index % len(_PALETTE)]}:s=160x90",
           "-frames:v", "1", "-q:v", "6", "-f", "image2", "-y",
           _tmp_jpg_path(index, label)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    with open(_tmp_jpg_path(index, label), "rb") as handle:
        return handle.read()


_JPG_CACHE = {}


def _tmp_jpg_path(index: int, label: str):
    import tempfile

    key = (index, label)
    if key not in _JPG_CACHE:
        fd, path = tempfile.mkstemp(prefix="bilibili-see-test-", suffix=".jpg")
        import os

        os.close(fd)
        _JPG_CACHE[key] = path
    return _JPG_CACHE[key]


if __name__ == "__main__":
    unittest.main()
