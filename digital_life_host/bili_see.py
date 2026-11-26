"""B 站「看画面」端口（宿主侧拥有 ffmpeg 与视觉网关凭据，核心侧只见鸭子类型端口）。

M2.6d 三段式管线（借鉴 dsh-see-now 的帧筛选 + 单张拼图纪律）：

    1. 探针抽帧   — 均匀抽 `probe` 张低分辨率候选帧（HTTP range 定点，不整段下载）；
    2. 变化量选帧 — 每帧降采样成 64×36 HSV 网格，与前一帧做色相加权差分（0–255，
                    照搬 dsh-see-now cuts.js scorePair 的口径），首帧必选 + 变化尖峰
                    按分数贪心入选（两帧间保最小间隔，避免全挤在一个切点）；
    3. 单张拼图   — 入选帧各缩放到格子尺寸后用 ffmpeg hstack/vstack 拼成一张网格图，
                    格子尺寸由官方 DeepSeek patch token 公式反推（14px patch →
                    42px token 格 → tokens = rows×(cols+1)+2），画布在预算内取最大。

一次视觉请求只传一张拼图：token 从「每帧独立计费」降为单图阶梯计费，
reasoning 烧尽导致空描述的问题从根上缓解；拼图失败/空描述时逐级退回
「少量独立帧 → 单帧」的旧路径（单帧请求被证明稳定出内容）。

纯读：只下载帧图片用于本地理解，不做任何站内写操作。
配额纪律：playurl 走 ``BiliAPI.play_url``（自动记账+熔断）；帧与视觉调用
分别属于 CDN 与自家网关，不占 B 站 API 预算，但受帧数/探针数上限约束。
临时目录 ``bilibili-see-*`` 用完即删；启动时顺手清扫孤儿目录（进程被杀残留）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from .module_gates import module_enabled

API = "https://api.bilibili.com"
FFMPEG_TIMEOUT = 90.0
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
REFERER_HEADERS = f"Referer: https://www.bilibili.com/\r\nUser-Agent: {UA}\r\n"

SEE_DEFAULTS = {"frames": 6, "max_chars": 3000, "probe": 12, "sheet_tokens": 420}

# ── 官方 DeepSeek 图像 token 公式（与 dsh-see-now tokens.js 同口径） ──────────
IMAGE_PATCH = 14          # 官方 patch 边长
IMAGE_DOWNSAMPLE = 3      # patch → token 格的 3:1 降采样（一个 token 格 = 42px）
IMAGE_TOKEN_CAP = 1024    # 单图 token 上限
IMAGE_UPSCALE_AREA = 544 * 544   # 计费地板：小于该面积的图按该面积收费


def image_tokens(w: int, h: int) -> int:
    """像素尺寸 → 官方口径 token 数（≤1024；无效尺寸返回 0）。"""
    W = int(w)
    H = int(h)
    if W <= 0 or H <= 0:
        return 0
    if W * H < IMAGE_UPSCALE_AREA:
        scale = math.sqrt(IMAGE_UPSCALE_AREA / (W * H))
        W = max(1, int(W * scale))
        H = max(1, int(H * scale))
    cur = _project(W, H)
    for _ in range(10):
        nxt = _project(cur[0], cur[1])
        if nxt == cur:
            break
        cur = nxt
    return _grid_tokens(_grid_cells(_pad_patch(cur[1])), _grid_cells(_pad_patch(cur[0])))


def _pad_patch(px: int) -> int:
    return ((px + IMAGE_PATCH - 1) // IMAGE_PATCH) * IMAGE_PATCH


def _grid_cells(padded: int) -> int:
    return (padded // IMAGE_PATCH + IMAGE_DOWNSAMPLE - 1) // IMAGE_DOWNSAMPLE


def _grid_tokens(rows: int, cols: int) -> int:
    return rows * (cols + 1) + 2


def _project(W: int, H: int, cap: int = IMAGE_TOKEN_CAP):
    """一次对齐 → 投影；塞得下就原样，塞不下就解出最大网格（tokens.js projectSize）。"""
    def tokens(w, h):
        return _grid_tokens(_grid_cells(_pad_patch(h)), _grid_cells(_pad_patch(w)))

    if tokens(W, H) <= cap:
        return (W, H)
    aspect = H / W
    ideal = math.sqrt((cap - 2) / aspect + 0.25) - 0.5
    sw = max(1, int(ideal))
    sh = max(1, int(ideal * aspect))
    if sh < 1:
        sh, sw = 1, (cap - 2) // 2
    cell = IMAGE_PATCH * IMAGE_DOWNSAMPLE
    scale = min(sw * cell / W, sh * cell / H)
    return (max(IMAGE_PATCH, int(W * scale) // IMAGE_PATCH * IMAGE_PATCH),
            max(IMAGE_PATCH, int(H * scale) // IMAGE_PATCH * IMAGE_PATCH))


def fit_sheet(aspect: float, max_tokens: int):
    """token 预算内找最大画布（14px 步进扫描，返回 (w, h, tokens) 或 None）。"""
    aspect = max(0.2, min(5.0, aspect or 16 / 9))
    best = None
    for w in range(112, 2048, IMAGE_PATCH):
        h = int(round(w / aspect / IMAGE_PATCH)) * IMAGE_PATCH
        if h < 112:
            continue
        tokens = image_tokens(w, h)
        if tokens <= 0 or tokens > max_tokens:
            if best is not None and tokens > max_tokens:
                break
            continue
        if best is None or w * h > best[0] * best[1]:
            best = (w, h, tokens)
    return best


# ── 帧差打分（cuts.js scorePair 的纯 Python 口径：64×36 HSV 网格，0–255） ─────
GRID_W, GRID_H = 64, 36


def hsv_grid(blob: bytes):
    """JPEG 字节 → bytearray(GRID_W*GRID_H*3) 的 HSV 网格（0–255）；无 Pillow 返回 None。"""
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return None
    import io

    with Image.open(io.BytesIO(blob)) as img:
        small = img.convert("RGB").resize((GRID_W, GRID_H))
        pixels = list(small.getdata())
    grid = bytearray(GRID_W * GRID_H * 3)
    for i, (r, g, b) in enumerate(pixels):
        h, s, v = _rgb_to_hsv_255(r, g, b)
        grid[i * 3] = h
        grid[i * 3 + 1] = s
        grid[i * 3 + 2] = v
    return grid


def _rgb_to_hsv_255(r, g, b):
    rn, gn, bn = r / 255.0, g / 255.0, b / 255.0
    mx = max(rn, gn, bn)
    mn = min(rn, gn, bn)
    d = mx - mn
    h = 0.0
    if d > 0:
        if mx == rn:
            h = ((gn - bn) / d) % 6
        elif mx == gn:
            h = (bn - rn) / d + 2
        else:
            h = (rn - gn) / d + 4
        h *= 60.0
        if h < 0:
            h += 360.0
    return int(h / 360.0 * 255), int((d / mx if mx else 0) * 255), int(mx * 255)


def frame_diff(prev, cur) -> float:
    """两帧 HSV 网格的色相加权绝对差均值（0–255）；任一帧缺失返回 0。"""
    if not prev or not cur:
        return 0.0
    total = 0.0
    n = len(cur) // 3
    for i in range(0, len(cur), 3):
        dh = abs(cur[i] - prev[i])
        hue = dh if dh < 255 - dh else 255 - dh
        weight = min(cur[i + 1], prev[i + 1]) / 255.0
        total += (hue * weight + abs(cur[i + 1] - prev[i + 1]) + abs(cur[i + 2] - prev[i + 2])) / 3.0
    return total / n


def select_offsets(offsets, grids, want: int, min_gap: float):
    """首帧必选 + 变化分贪心入选（两帧间保最小间隔）。返回入选 offset 列表。"""
    if not offsets:
        return []
    if len(offsets) <= want:
        return list(offsets)
    picked = [offsets[0]]
    scores = []
    for index in range(1, len(offsets)):
        scores.append((frame_diff(grids[index - 1], grids[index]), index))
    scores.sort(key=lambda item: (-item[0], item[1]))
    for _, index in scores:
        if len(picked) >= want:
            break
        offset = offsets[index]
        if all(abs(offset - p) >= min_gap for p in picked):
            picked.append(offset)
    picked.sort()
    return picked


class BiliSee:
    """``describe(port, bvid) -> dict``：变化选帧 + 单张拼图 + 视觉描述。"""

    def __init__(self, config: Mapping[str, Any] | None, data_root, *, runner: Callable | None = None):
        section = {}
        bili = config.get("bilibili") if isinstance(config, Mapping) else None
        if isinstance(bili, Mapping):
            candidate = bili.get("see")
            if isinstance(candidate, Mapping):
                section = candidate
        self.frames = max(3, min(8, int(section.get("frames") or SEE_DEFAULTS["frames"])))
        self.max_chars = max(600, min(6000, int(section.get("max_chars") or SEE_DEFAULTS["max_chars"])))
        self.probe = max(6, min(24, int(section.get("probe") or SEE_DEFAULTS["probe"])))
        self.sheet_tokens = max(160, min(1000, int(section.get("sheet_tokens") or SEE_DEFAULTS["sheet_tokens"])))
        self.model = str(section.get("model") or "")
        self.api_base = str(section.get("api_base") or "").rstrip("/")
        self.key = str(section.get("key") or "")
        if not (self.api_base and self.key and self.model):
            gateway = self._gateway_from_runtime(data_root)
            if gateway:
                self.api_base = self.api_base or gateway["api_base"]
                self.key = self.key or gateway["key"]
                self.model = self.model or gateway["model"]
        self._runner = runner
        self.session_id = uuid.uuid4().hex
        self._sweep_orphans()

    @staticmethod
    def _gateway_from_runtime(data_root):
        """默认沿用 AstrBot 主配置第一个已启用的 chat provider（视觉走同一网关）。

        model 名在顶层 ``provider[].model``（按 ``provider_source_id`` 关联 source），
        source 本身只存 api_base/key。
        """
        try:
            path = Path(data_root).parents[1].joinpath("cmd_config.json")
            with open(path, encoding="utf-8-sig") as handle:
                conf = json.load(handle)
            models = {}
            for entry in conf.get("provider") or []:
                if not isinstance(entry, Mapping):
                    continue
                model = str(entry.get("model") or "")
                if model:
                    models.setdefault(str(entry.get("provider_source_id")), model)
            for source in (conf.get("provider_sources") or []):
                keys = source.get("key") or []
                api_base = str(source.get("api_base") or "").rstrip("/")
                if keys and api_base:
                    return {"api_base": api_base, "key": str(keys[0]),
                            "model": models.get(str(source.get("id")), "")}
        except Exception:
            return {}
        return {}

    @property
    def available(self) -> bool:
        return bool(self.api_base and self.key and self.model)

    def settings(self) -> dict[str, Any]:
        return {"frames": self.frames, "probe": self.probe,
                "sheet_tokens": self.sheet_tokens,
                "max_chars": self.max_chars, "available": self.available}

    @staticmethod
    def _sweep_orphans(max_age_seconds: float = 3600.0):
        """清扫进程被杀残留的临时帧目录（正常流程 TemporaryDirectory 自删）。"""
        base = tempfile.gettempdir()
        now = time.time()
        try:
            for name in os.listdir(base):
                if not name.startswith("bilibili-see-"):
                    continue
                path = os.path.join(base, name)
                try:
                    if now - os.path.getmtime(path) > max_age_seconds:
                        shutil.rmtree(path, ignore_errors=True)
                except OSError:
                    continue
        except OSError:
            pass

    # ── 主流程 ───────────────────────────────────────────────────────────
    async def describe(self, port, bvid: str) -> dict[str, Any]:
        """返回 {"present", "frames", "offsets", "description"}；失败抛原异常。"""
        from digital_life_core.bilibili.errors import BiliError

        if not self.available:
            raise BiliError("视觉网关没配置（bilibili.see.model/api_base/key）")
        video = await port.video_info(bvid)
        duration = int(getattr(video, "duration", 0) or 0)
        play = await port.play_url(video)
        url = str(play.get("url") or "")
        if not url:
            raise BiliError("视频流地址不可用（视频可能不可匿名播放）")
        candidates = self._probe_offsets(duration)
        if not candidates:
            raise BiliError("视频时长不可用（视频流可能不可用）")
        probes = await self._probe_frames(url, candidates)
        usable = [(offset, grid, blob) for offset, grid, blob in
                  zip(candidates, probes[0], probes[1]) if blob]
        if len(usable) < 2:
            raise BiliError("一帧都没抽出来（视频流可能不可用）")
        offsets = [item[0] for item in usable]
        grids = [item[1] for item in usable]
        blobs = [item[2] for item in usable]
        if any(grid is not None for grid in grids):
            picked = select_offsets(offsets, grids, self.frames,
                                    min_gap=max(1.0, duration / max(2, self.frames * 2)))
        else:
            # 无 Pillow → 没有变化分，退化成均匀选帧。
            step = max(1, len(offsets) // self.frames)
            picked = offsets[::step][:self.frames]
        if not picked:
            picked = offsets[:self.frames]
        picked_blobs = [blobs[offsets.index(offset)] for offset in picked]
        description = await self._vision(port, url, video, picked, picked_blobs)
        return {"present": True, "frames": len(picked),
                "offsets": [round(offset, 1) for offset in picked],
                "description": description[:self.max_chars]}

    def _probe_offsets(self, duration: int):
        if duration <= 0:
            duration = 60
        if duration <= 6:
            return [max(0.0, min(duration - 0.5, 0.5 * index))
                    for index in range(min(self.probe, max(2, int(duration / 0.5))))]
        step = max(1.0, (duration - 2.0) / (self.probe - 1))
        return [min(max(0.5, 0.5 + index * step), max(1.0, duration - 1.0))
                for index in range(self.probe)]

    # ── 第 1 段：探针帧 ──────────────────────────────────────────────────
    async def _probe_frames(self, url: str, offsets):
        """低分辨率候选帧；每帧独立失败只跳过（对应位置返回 (None, None)）。"""
        loop = asyncio.get_event_loop()
        runner = self._runner or (lambda cmd, timeout: _run_ffmpeg(cmd, timeout))
        with tempfile.TemporaryDirectory(prefix="bilibili-see-") as workdir:
            jobs = []
            for index, offset in enumerate(offsets):
                output = os.path.join(workdir, f"probe-{index:02d}.jpg")
                cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
                       "-headers", REFERER_HEADERS,
                       "-ss", f"{offset:.2f}", "-i", url,
                       "-frames:v", "1", "-vf", "scale=96:-2",
                       "-q:v", "10", "-f", "image2", "-y", output]
                jobs.append((offset, output, cmd))
            results = await asyncio.gather(*[
                loop.run_in_executor(None, lambda offset=offset, output=output, cmd=cmd:
                                     self._one_frame(runner, cmd, output, offset))
                for _, offset, output, cmd in jobs
            ])
        pairs = [item for item in results if item is not None]
        blob_by_offset = {offset: blob for offset, blob in pairs}
        grids = [hsv_grid(blob_by_offset[offset]) if offset in blob_by_offset else None
                 for offset in offsets]
        blobs = [blob_by_offset.get(offset) for offset in offsets]
        return grids, blobs

    # ── 第 3 段：格子帧 + 拼图 ───────────────────────────────────────────
    async def _cell_frames(self, url: str, offsets, cell_w: int, cell_h: int):
        """按格子尺寸重抽入选帧；返回 [(offset, blob)]（失败跳过）。"""
        loop = asyncio.get_event_loop()
        runner = self._runner or (lambda cmd, timeout: _run_ffmpeg(cmd, timeout))
        with tempfile.TemporaryDirectory(prefix="bilibili-see-") as workdir:
            jobs = []
            for index, offset in enumerate(offsets):
                output = os.path.join(workdir, f"frame-{index:02d}.jpg")
                cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
                       "-headers", REFERER_HEADERS,
                       "-ss", f"{offset:.2f}", "-i", url,
                       "-frames:v", "1", "-vf",
                       f"scale={cell_w}:{cell_h}:force_original_aspect_ratio=decrease",
                       "-q:v", "6", "-f", "image2", "-y", output]
                jobs.append((offset, output, cmd))
            results = await asyncio.gather(*[
                loop.run_in_executor(None, lambda offset=offset, output=output, cmd=cmd:
                                     self._one_frame(runner, cmd, output, offset))
                for _, offset, output, cmd in jobs
            ])
        return [item for item in results if item is not None]

    async def _compose_sheet(self, frames, cell_w: int, cell_h: int, canvas_w: int, canvas_h: int):
        """ffmpeg hstack/vstack 把等尺寸帧拼成一张网格图；失败返回 None。

        每行把不足 cols 的尾巴用 pad 补齐，行高统一 cell_h，保证 vstack 可行；
        最后整体 scale 到画布（force_original_aspect_ratio=decrease + pad 到精确尺寸）。
        """
        loop = asyncio.get_event_loop()
        runner = self._runner or (lambda cmd, timeout: _run_ffmpeg(cmd, timeout))
        cols = max(1, int(math.ceil(math.sqrt(len(frames)))))
        rows = max(1, int(math.ceil(len(frames) / cols)))
        with tempfile.TemporaryDirectory(prefix="bilibili-see-") as workdir:
            inputs = []
            for index, (offset, blob) in enumerate(frames):
                path = os.path.join(workdir, f"cell-{index:02d}.jpg")
                with open(path, "wb") as handle:
                    handle.write(blob)
                inputs.append(path)
            output = os.path.join(workdir, "sheet.jpg")
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
            for path in inputs:
                cmd += ["-i", path]
            parts = []
            # 每格先缩进格子再 pad 到精确 cell_w×cell_h —— hstack 要求输入同尺寸。
            for m in range(len(inputs)):
                parts.append(f"[{m}:v]scale={cell_w}:{cell_h}:force_original_aspect_ratio=decrease,"
                             f"pad={cell_w}:{cell_h}:(ow-iw)/2:(oh-ih)/2[c{m}]")
            for row in range(rows):
                members = list(range(row * cols, min((row + 1) * cols, len(inputs))))
                if not members:
                    break
                srcs = "".join(f"[c{m}]" for m in members)
                if len(members) == 1:
                    parts.append(f"{srcs}null[r{row}]")
                else:
                    parts.append(f"{srcs}hstack=inputs={len(members)}[r{row}]")
                # 行尾 pad：把该行补到整幅画布宽（尾行不足 cols 时必需）。
                parts.append(f"[r{row}]pad={canvas_w}:{cell_h}:(ow-iw)/2:(oh-ih)/2[rr{row}]")
            row_srcs = "".join(f"[rr{row}]" for row in range(rows))
            if rows == 1:
                parts.append(f"{row_srcs}null[out]")
            else:
                parts.append(f"{row_srcs}vstack=inputs={rows}[out]")
            parts.append(f"[out]scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease"
                         f",pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2,setsar=1[final]")
            cmd += ["-filter_complex", ";".join(parts),
                    "-map", "[final]", "-frames:v", "1", "-q:v", "6", "-y", output]
            try:
                await loop.run_in_executor(None, lambda: runner(cmd, FFMPEG_TIMEOUT))
                if os.path.exists(output) and os.path.getsize(output) > 1024:
                    with open(output, "rb") as handle:
                        return handle.read()
            except Exception:
                return None
        return None

    @staticmethod
    def _one_frame(runner, cmd, output, offset):
        try:
            runner(cmd, FFMPEG_TIMEOUT)
            if os.path.exists(output) and os.path.getsize(output) > 1024:
                with open(output, "rb") as handle:
                    return (offset, handle.read())
        except Exception:
            return None
        return None

    # ── 视觉请求 ─────────────────────────────────────────────────────────
    async def _vision(self, port, url: str, video, picked, picked_blobs) -> str:
        from digital_life_core.bilibili.errors import BiliError

        title = str(getattr(video, "title", ""))[:160]
        author = str(getattr(video, "author", ""))[:60]
        desc = str(getattr(video, "desc", ""))[:300]

        def prompt_for(timeline: str, extra: str = "") -> str:
            return (
                f"这是一个B站视频的关键帧{extra}。标题：《{title}》，UP 主：{author}。简介：{desc}\n"
                f"抽帧时间点（按顺序）：{timeline}\n"
                "请以旁观者视角描述画面里发生了什么：场景、人物/动物、动作、镜头变化，"
                "画面里出现的字幕或花字也读出来。按时间顺序讲，简洁自然，"
                "不要编造帧之间没有发生的剧情。"
            )

        timeline = "、".join(f"{offset:.0f}s" for offset in picked)
        self._log().info("[CompanionV2][bili][see] picked=%d at %s", len(picked), timeline[:120])

        # ① 首选：单张拼图（token 成本最低，reasoning 空间最大）。
        description = await self._describe_sheet(port, url, video, picked, timeline, prompt_for)
        if description:
            return description

        # ② 兜底：独立帧逐级降帧（旧路径，单帧被证明稳定出内容）。
        #    探针图只有 96px，兜底帧要重抽 480px 才够单帧描述用。
        ladder = [(3, 2600), (1, 1800)]
        last_finish = "unknown"
        fallback = await self._cell_frames(url, picked[:3], 480, 270)
        for keep, budget in ladder:
            if len(fallback) < keep:
                break
            subset = fallback[:keep]
            sub_timeline = "、".join(f"{offset:.0f}s" for offset, _ in subset)
            content: list[dict[str, Any]] = [
                {"type": "text", "text": prompt_for(sub_timeline, "（独立帧）")}]
            for _, blob in subset:
                content.append({"type": "image_url",
                                "image_url": {"url": "data:image/jpeg;base64,"
                                                      + base64.b64encode(blob).decode()}})
            body = await self._vision_call(port, content, max_tokens=budget)
            message = self._choice_message(body)
            finish = self._finish_reason(body)
            text = str(message.get("content") or "").strip()
            if text:
                self._log().info("[CompanionV2][bili][see] 独立帧兜底成功 keep=%d finish=%s",
                                 keep, finish)
                return text
            last_finish = str(finish)
            self._log().warning("[CompanionV2][bili][see] 视觉空描述 keep=%d finish=%s tokens=%s",
                                keep, finish, json.dumps(body.get("usage") or {}, ensure_ascii=False)[:200])
        raise BiliError(f"视觉网关返回了空描述（finish={last_finish}）")

    async def _describe_sheet(self, port, url: str, video, picked, timeline, prompt_for):
        """拼图路径：按 token 预算反推画布 → 重抽格子帧 → ffmpeg 拼图 → 单图视觉请求。"""
        cols = max(1, int(math.ceil(math.sqrt(len(picked)))))
        rows = max(1, int(math.ceil(len(picked) / cols)))
        # 拼图整体仍是 16:9 内容的网格：画布 aspect = (cols×16)/(rows×9)。
        layout = fit_sheet((cols * 16.0) / (rows * 9.0), self.sheet_tokens)
        if not layout:
            return None
        sheet_w, sheet_h, sheet_tk = layout
        cell_w = max(2, (sheet_w // cols) // 2 * 2)
        cell_h = max(2, (sheet_h // rows) // 2 * 2)
        sheet_frames = await self._cell_frames(url, picked, cell_w, cell_h)
        if len(sheet_frames) < max(2, len(picked) // 2):
            self._log().warning("[CompanionV2][bili][see] 拼图帧不足 kept=%d want=%d，走独立帧兜底",
                                len(sheet_frames), len(picked))
            return None
        sheet_blob = await self._compose_sheet(sheet_frames, cell_w, cell_h, sheet_w, sheet_h)
        if not sheet_blob:
            self._log().warning("[CompanionV2][bili][see] ffmpeg 拼图失败，走独立帧兜底")
            return None
        actual = [offset for offset, _ in sheet_frames]
        actual_timeline = "、".join(f"{offset:.0f}s" for offset in actual)
        content = [{"type": "text", "text": prompt_for(actual_timeline)},
                   {"type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,"
                                          + base64.b64encode(sheet_blob).decode()}}]
        body = await self._vision_call(port, content, max_tokens=4000)
        message = self._choice_message(body)
        finish = self._finish_reason(body)
        text = str(message.get("content") or "").strip()
        if text:
            self._log().info("[CompanionV2][bili][see] 拼图成功 %dx%d (~%d tok) frames=%d finish=%s",
                             sheet_w, sheet_h, sheet_tk, len(actual), finish)
            return text
        self._log().warning("[CompanionV2][bili][see] 拼图空描述 finish=%s tokens=%s",
                            finish, json.dumps(body.get("usage") or {}, ensure_ascii=False)[:200])
        return None

    @staticmethod
    def _finish_reason(body) -> Any:
        try:
            return body["choices"][0].get("finish_reason")
        except (KeyError, IndexError, TypeError):
            return None

    @staticmethod
    def _log() -> logging.Logger:
        return logging.getLogger(__name__)

    async def _vision_call(self, port, content, *, max_tokens: int) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
        }
        return await port.post_json(self.api_base + "/chat/completions", payload,
                                    headers={"User-Agent": "astrbot-digital-life/0.1",
                                             "Authorization": f"Bearer {self.key}",
                                             "x-opencode-session": self.session_id})

    @staticmethod
    def _choice_message(body) -> dict[str, Any]:
        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise BiliError(f"视觉网关响应异常：{json.dumps(body, ensure_ascii=False)[:200]}") from exc
        return message


def _run_ffmpeg(cmd, timeout: float) -> None:
    import subprocess

    process = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg exit {process.returncode}: {process.stderr[:200]!r}")


def configured_bili_seer(config, data_root):
    """返回 BiliSee；bilibili 模块关闭时返回 None。see.enabled=false 也返回 None。"""
    if not module_enabled("bilibili"):
        return None
    bili = config.get("bilibili") if isinstance(config, Mapping) else None
    if isinstance(bili, Mapping):
        see = bili.get("see")
        if isinstance(see, Mapping) and see.get("enabled") is False:
            return None
    seer = BiliSee(config, data_root)
    return seer if seer.available else None
