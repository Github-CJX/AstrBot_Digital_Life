"""B 站「看画面」端口（宿主侧拥有 ffmpeg 与视觉网关凭据，核心侧只见鸭子类型端口）。

流程：playurl 匿名低清流 → ffmpeg 按时间点抽关键帧（HTTP range 定点取帧，
不整段下载）→ 现有聊天网关的视觉输入（deepseek-v4.1-flash 多模态）描述画面。

纯读：只下载帧图片用于本地理解，不做任何站内写操作。
配额纪律：playurl 走 ``BiliAPI.play_url``（自动记账+熔断）；帧与视觉调用
分别属于 CDN 与自家网关，不占 B 站 API 预算，但受帧数上限约束。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from .module_gates import module_enabled

API = "https://api.bilibili.com"
FFMPEG_TIMEOUT = 90.0

SEE_DEFAULTS = {"frames": 6, "scale": 480, "max_chars": 3000}


class BiliSee:
    """``describe(port, bvid) -> dict``：关键帧 + 视觉描述。"""

    def __init__(self, config: Mapping[str, Any] | None, data_root, *, runner: Callable | None = None):
        section = {}
        bili = config.get("bilibili") if isinstance(config, Mapping) else None
        if isinstance(bili, Mapping):
            candidate = bili.get("see")
            if isinstance(candidate, Mapping):
                section = candidate
        self.frames = max(3, min(8, int(section.get("frames") or SEE_DEFAULTS["frames"])))
        self.scale = max(240, min(960, int(section.get("scale") or SEE_DEFAULTS["scale"])))
        self.max_chars = max(600, min(6000, int(section.get("max_chars") or SEE_DEFAULTS["max_chars"])))
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
            sources = {str(s.get("id")): s for s in (conf.get("provider_sources") or [])}
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
        return {"frames": self.frames, "scale": self.scale,
                "max_chars": self.max_chars, "available": self.available}

    # ── 主流程 ───────────────────────────────────────────────────────────
    async def describe(self, port, bvid: str) -> dict[str, Any]:
        """返回 {"present", "frames", "offsets", "description"}；失败抛原异常。"""
        from digital_life_core.bilibili.errors import BiliError

        if not self.available:
            raise BiliError("视觉网关没配置（bilibili.see.model/api_base/key）")
        video = await port.video_info(bvid)
        duration = int(getattr(video, "duration", 0) or 0)
        play = await port.play_url(video)
        offsets = self._offsets(duration)
        frames = await self._extract(play["url"], offsets)
        if not frames:
            raise BiliError("一帧都没抽出来（视频流可能不可用）")
        description = await self._vision(port, video, frames, offsets)
        return {"present": True, "frames": len(frames),
                "offsets": [round(offset, 1) for offset, _ in frames],
                "description": description[:self.max_chars]}

    def _offsets(self, duration: int):
        count_frames = self.frames
        if duration <= 0:
            duration = 60
        # 首帧 + 均匀采样到结尾前 1 秒；短视频退化成 0.5s 间隔。
        if duration <= 6:
            return [max(0.0, min(duration - 0.5, 0.5 * index)) for index in range(count_frames)]
        step = max(1.0, (duration - 2.0) / (count_frames - 1))
        return [min(max(0.5, 0.5 + index * step), max(1.0, duration - 1.0))
                for index in range(count_frames)]

    async def _extract(self, url: str, offsets):
        """ffmpeg 定点抽帧；每帧独立失败只跳过，全部失败才抛错。"""
        loop = asyncio.get_event_loop()
        runner = self._runner or (lambda cmd, timeout: _run_ffmpeg(cmd, timeout))
        with tempfile.TemporaryDirectory(prefix="bilibili-see-") as workdir:
            jobs = []
            for index, offset in enumerate(offsets):
                output = os.path.join(workdir, f"frame-{index:02d}.jpg")
                cmd = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-headers", "Referer: https://www.bilibili.com/\r\nUser-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36\r\n",
                    "-ss", f"{offset:.2f}", "-i", url,
                    "-frames:v", "1", "-vf", f"scale={self.scale}:-2",
                    "-q:v", "7", "-f", "image2", "-y", output,
                ]
                jobs.append((index, offset, output, cmd))
            results = await asyncio.gather(*[
                loop.run_in_executor(None, lambda offset=offset, output=output, cmd=cmd:
                                     self._one_frame(runner, cmd, output, offset))
                for _, offset, output, cmd in jobs
            ])
            return [item for item in results if item is not None]

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

    async def _vision(self, port, video, frames, offsets) -> str:
        from digital_life_core.bilibili.errors import BiliError

        title = str(getattr(video, "title", ""))[:160]
        author = str(getattr(video, "author", ""))[:60]
        desc = str(getattr(video, "desc", ""))[:300]
        timeline = "、".join(f"{offset:.0f}s" for offset, _ in frames)
        prompt = (
            f"这是一个B站视频在 {timeline} 处抽出的 {len(frames)} 张关键帧。"
            f"标题：《{title}》，UP 主：{author}。简介：{desc}\n"
            "请以旁观者视角描述画面里发生了什么：场景、人物/动物、动作、镜头变化，"
            "画面里出现的字幕或花字也读出来。按时间顺序讲，简洁自然，"
            "不要编造帧之间没有发生的剧情。"
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for _, blob in frames:
            import base64
            content.append({"type": "image_url",
                            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(blob).decode()}})
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 1200,
        }
        body = await port.post_json(self.api_base + "/chat/completions", payload,
                                    headers={"User-Agent": "astrbot-digital-life/0.1",
                                             "x-opencode-session": self.session_id})
        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise BiliError(f"视觉网关响应异常：{json.dumps(body, ensure_ascii=False)[:200]}") from exc
        text = str(message.get("content") or "")
        if not text.strip():
            raise BiliError("视觉网关返回了空描述")
        return text.strip()


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
