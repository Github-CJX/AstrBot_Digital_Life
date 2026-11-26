"""On-demand Bilibili reading for one explicit user request.

The host owns the network client, cookie and traffic guard; this module only
validates the bounded query/result contract. ``bili.watch`` is read-only:
it never posts, likes or sends anything on Bilibili.

Two-step contract: without a BV号 the tool returns up to five candidates
(title/author/length/plays); with a BV号 it returns the bounded reading
material (metadata + subtitles + hot comments + danmaku hotspots) that the
reply model summarizes in its own voice.
"""

import asyncio
import re
from datetime import datetime

from digital_life_core.execution.models import TaskError, encode
from digital_life_core.foundation.refs import ScopeKind
from digital_life_core.model_gateway.models import ToolSpec


ACTION = "bili.watch"
INSTRUCTION = (
    "bili.watch 只在用户明确让你看某个B站视频、发来B站链接或BV号，或想聊B站上的具体内容时使用；"
    "只读不写：不要用其他工具往B站发评论、弹幕或私信。没有BV号时先用关键词搜索拿候选，"
    "把候选报给用户或自己选定后再用BV号取内容；总结时用自己的口吻说，不要整段照抄字幕。"
    "平台限流被熔断时如实说稍后再看，不要编造视频内容。"
)
MAX_TARGET_CHARS = 120
MAX_CANDIDATES = 5
MAX_COMMENTS = 6
MAX_DANMAKU = 8
MAX_SUBTITLE_CHARS = 9000
MAX_FETCH_SECONDS = 60
MAX_WATCHES_PER_TURN = 3
_BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")


def extract_bvid(target: str):
    """从 BV 号、视频链接或混在一段话里的目标中提取 BV 号（BV 号区分大小写）。"""
    match = _BV_RE.search(str(target or ""))
    return match.group(0) if match else None


class BiliWatchTools:
    actions = frozenset({ACTION})

    def __init__(self, factory=None):
        self.factory = factory if callable(factory) else None
        self._calls = {}
        self.catalog = (ToolSpec(ACTION, "读取一个B站视频的文字材料（标题、简介、字幕、热评、弹幕热点）或按关键词搜候选。", encode({
            "type": "object",
            "properties": {
                "target": {"type": "string",
                           "description": "BV号、B站视频链接，或没有BV号时的搜索关键词，最多 120 字"},
            },
            "required": ["target"],
            "additionalProperties": False,
        })),)

    def catalog_for(self, context):
        if self.factory is None or context.scope.kind not in (ScopeKind.PRIVATE, ScopeKind.GROUP):
            return ()
        return self.catalog

    async def execute(self, run, context, arguments, *, observed_at):
        target = self._arguments(arguments)
        if self.factory is None:
            return {"state": "failed", "code": "bili_watch_unavailable", "effect": "none"}
        task_id = getattr(getattr(run, "task", None), "task_id", "")
        if type(task_id) is not str or not task_id:
            return {"state": "failed", "code": "bili_watch_unavailable", "effect": "none"}
        used = self._calls.get(task_id, 0)
        if used >= MAX_WATCHES_PER_TURN:
            return {"state": "failed", "code": "bili_watch_turn_limit", "effect": "none"}
        if len(self._calls) > 512:
            self._calls.pop(next(iter(self._calls)))
        self._calls[task_id] = used + 1
        try:
            port = self.factory(context)
        except Exception:
            port = None
        if port is None:
            return {"state": "failed", "code": "bili_watch_unavailable", "effect": "none"}
        try:
            try:
                payload = await asyncio.wait_for(
                    self._material(port, target), timeout=MAX_FETCH_SECONDS)
            except asyncio.TimeoutError:
                return {"state": "failed", "code": "bili_watch_timeout", "effect": "none"}
            except TaskError:
                raise
            except Exception as error:
                return {"state": "failed", "code": self._failure_code(error),
                        "effect": "none", "detail": str(error)[:200]}
            run.validate()
            return {"state": "succeeded", "effect": "none",
                    "payload": {**payload, "observed_at":
                                observed_at.isoformat() if hasattr(observed_at, "isoformat") else None}}
        finally:
            close = getattr(port, "close", None)
            if close is not None:
                try:
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:
                    pass

    @staticmethod
    def _failure_code(error):
        # 保持三层错误与 vendored errors.py 的语义对应，不 import 也成立。
        name = type(error).__name__
        if name == "BiliRisk":
            return "bili_watch_rate_limited"
        if name == "BiliAuthError":
            return "bili_watch_auth"
        if name == "BudgetError":
            return "bili_watch_budget"
        return "bili_watch_failed"

    async def _material(self, port, target):
        start = getattr(port, "start", None)
        if start is not None:
            await start()
        bvid = extract_bvid(target)
        if bvid is None:
            keyword = str(target or "").strip()
            if not keyword:
                raise TaskError("invalid_tool_arguments", "bili.watch requires a BV id or a keyword")
            videos = await port.search(keyword, page_size=12)
            candidates = []
            for video in videos[:MAX_CANDIDATES]:
                candidates.append({
                    "bvid": str(getattr(video, "bvid", ""))[:20],
                    "title": str(getattr(video, "title", ""))[:160],
                    "author": str(getattr(video, "author", ""))[:60],
                    "duration": int(getattr(video, "duration", 0) or 0),
                    "play": int(getattr(video, "play", 0) or 0),
                    "link": str(getattr(video, "link", ""))[:120],
                })
            return {"mode": "candidates", "query": keyword[:MAX_TARGET_CHARS],
                    "candidates": candidates}
        video = await port.video_info(bvid)
        subtitle = await port.subtitles(video)
        comments = await port.hot_comments(video, limit=MAX_COMMENTS)
        danmaku = await port.danmaku_hotspots(int(getattr(video, "cid", 0) or 0),
                                              int(getattr(video, "duration", 0) or 0),
                                              buckets=MAX_DANMAKU)
        subtitle_text = (subtitle.text if subtitle is not None else "")[:MAX_SUBTITLE_CHARS]
        return {"mode": "video",
                "video": {"bvid": str(video.bvid)[:20], "title": str(video.title)[:200],
                          "author": str(video.author)[:60], "duration": int(video.duration or 0),
                          "play": int(video.play or 0), "desc": str(video.desc)[:500],
                          "link": str(video.link)[:120]},
                "subtitle": {"present": bool(subtitle_text), "lang": str(getattr(subtitle, "lang", "") or "") if subtitle_text else "",
                             "text": subtitle_text},
                "comments": [{"user": str(c.user)[:40], "text": str(c.text)[:160],
                              "like": int(c.like or 0)} for c in comments[:MAX_COMMENTS]],
                "danmaku": [{"label": str(getattr(s, "label", ""))[:12],
                             "count": int(getattr(s, "count", 0) or 0),
                             "samples": [str(x)[:80] for x in tuple(getattr(s, "samples", ()) or ())[:3]]}
                            for s in danmaku[:MAX_DANMAKU]]}

    def validate(self, row, context, result, observed_at):
        if result.get("state") != "succeeded" or result.get("effect") != "none":
            raise TaskError("bili_watch_result_invalid", "the original bili watch result changed")
        payload = result.get("payload")
        if type(payload) is not dict or payload.get("observed_at") is None \
                or type(payload["observed_at"]) is not str:
            raise TaskError("bili_watch_result_invalid", "the original bili watch payload is invalid")
        rest = {key: value for key, value in payload.items() if key != "observed_at"}
        if payload.get("mode") == "candidates":
            if set(rest) != {"mode", "query", "candidates"}:
                raise TaskError("bili_watch_result_invalid", "the original bili watch payload is invalid")
            if type(rest["query"]) is not str or not 1 <= len(rest["query"]) <= MAX_TARGET_CHARS:
                raise TaskError("bili_watch_result_invalid", "the original bili watch query is invalid")
            candidates = rest["candidates"]
            if type(candidates) is not list or len(candidates) > MAX_CANDIDATES:
                raise TaskError("bili_watch_result_invalid", "the original bili watch candidates are invalid")
            for item in candidates:
                if (type(item) is not dict or set(item) != {"bvid", "title", "author", "duration", "play", "link"}
                        or any(type(item[key]) is not str for key in ("bvid", "title", "author", "link"))
                        or type(item["duration"]) is not int or type(item["play"]) is not int
                        or not _BV_RE.fullmatch(item["bvid"] or "")):
                    raise TaskError("bili_watch_result_invalid", "the original bili watch candidate is invalid")
        elif payload.get("mode") == "video":
            if set(rest) != {"mode", "video", "subtitle", "comments", "danmaku"}:
                raise TaskError("bili_watch_result_invalid", "the original bili watch payload is invalid")
            video = rest["video"]
            subtitle = rest["subtitle"]
            if (type(video) is not dict
                    or set(video) != {"bvid", "title", "author", "duration", "play", "desc", "link"}
                    or any(type(video[key]) is not str for key in ("bvid", "title", "author", "desc", "link"))
                    or type(video["duration"]) is not int or type(video["play"]) is not int
                    or not _BV_RE.fullmatch(video["bvid"] or "")):
                raise TaskError("bili_watch_result_invalid", "the original bili watch video is invalid")
            if (type(subtitle) is not dict or set(subtitle) != {"present", "lang", "text"}
                    or type(subtitle["present"]) is not bool
                    or type(subtitle["lang"]) is not str or type(subtitle["text"]) is not str
                    or len(subtitle["text"]) > MAX_SUBTITLE_CHARS
                    or (subtitle["present"] and not subtitle["text"])):
                raise TaskError("bili_watch_result_invalid", "the original bili watch subtitle is invalid")
            comments = rest["comments"]
            if type(comments) is not list or len(comments) > MAX_COMMENTS:
                raise TaskError("bili_watch_result_invalid", "the original bili watch comments are invalid")
            for item in comments:
                if (type(item) is not dict or set(item) != {"user", "text", "like"}
                        or type(item["user"]) is not str or type(item["text"]) is not str
                        or type(item["like"]) is not int):
                    raise TaskError("bili_watch_result_invalid", "the original bili watch comment is invalid")
            danmaku = rest["danmaku"]
            if type(danmaku) is not list or len(danmaku) > MAX_DANMAKU:
                raise TaskError("bili_watch_result_invalid", "the original bili watch danmaku is invalid")
            for item in danmaku:
                if (type(item) is not dict or set(item) != {"label", "count", "samples"}
                        or type(item["label"]) is not str or type(item["count"]) is not int
                        or type(item["samples"]) is not list
                        or any(type(x) is not str for x in item["samples"])
                        or len(item["samples"]) > 3):
                    raise TaskError("bili_watch_result_invalid", "the original bili watch danmaku row is invalid")
        else:
            raise TaskError("bili_watch_result_invalid", "the original bili watch payload is invalid")
        return result, None

    @staticmethod
    def _arguments(arguments):
        if type(arguments) is not dict or set(arguments) - {"target"} or "target" not in arguments:
            raise TaskError("invalid_tool_arguments", "bili.watch requires one bounded target")
        target = arguments["target"]
        if (type(target) is not str or not 1 <= len(target.strip()) <= MAX_TARGET_CHARS
                or "\x00" in target):
            raise TaskError("invalid_tool_arguments", "bili.watch requires a bounded target")
        return target.strip()


def bili_watch_action(action):
    return action in BiliWatchTools.actions
