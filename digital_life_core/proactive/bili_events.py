"""Opt-in, read-only curiosity contact driven by the Bilibili browsing module.

Behaviour model borrowed from bili_whale (MIT): a daily bio-clock decides when
the role feels like browsing (wake window, deterministic daily skip, a cap on
wakes per day and a minimum gap), then hands the model a candidate batch. The
contact turn itself screens and deep-reads with the ``bili.watch`` tool and
decides what, if anything, to share. Nothing here writes to Bilibili.
"""

import json
import os
import threading
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from digital_life_core.conversation.messages import source_from_document
from digital_life_core.execution.models import TaskContext, TaskError, digest, encode, require, stamp
from digital_life_core.foundation.refs import ScopeKind, SourceRef, VersionRef
from digital_life_core.sources.governance import bind
from digital_life_core.sources.models import SourceRecord

from .contact_models import ContactIntent
from .models import EvidenceSnapshot, Opportunity, Thought, ThoughtKind

SOURCE_OWNER = "proactive.bili_curiosity"
EVENT_WINDOW = timedelta(minutes=60)
RETRY = timedelta(minutes=30)
MAX_CANDIDATES = 8
MEANING = ("这是角色自己起的 B 站好奇心：候选列表只是发现渠道的公开信息，"
           "不代表角色已经看过或认同；角色可以挑感兴趣的深入看，也可以全部略过，"
           "不承诺必须分享，不代用户表态。")
THOUGHT_TEXT = ("角色今天起了一点好奇心，想去 B 站逛逛：先从候选里挑自己真感兴趣的"
                "（可用 bili.watch 工具看懂一条视频），再决定要不要把看法分享给对方；"
                "不强求，也不必逐条介绍，更不编造看过的内容。")
DEFAULT_CURIOSITY = {"wake_start_hour": 9, "wake_end_hour": 23, "skip_chance_percent": 30,
                     "max_per_day": 1, "min_gap_hours": 6}


def curiosity_settings(values):
    """Coerce untrusted settings into the fixed-shape curiosity clock."""
    settings = dict(DEFAULT_CURIOSITY)
    if type(values) is not dict:
        return settings
    for name in DEFAULT_CURIOSITY:
        value = values.get(name)
        if type(value) is int:
            settings[name] = value
    settings["wake_start_hour"] = min(max(settings["wake_start_hour"], 0), 23)
    settings["wake_end_hour"] = min(max(settings["wake_end_hour"], 1), 24)
    settings["skip_chance_percent"] = min(max(settings["skip_chance_percent"], 0), 100)
    settings["max_per_day"] = min(max(settings["max_per_day"], 0), 8)
    settings["min_gap_hours"] = min(max(settings["min_gap_hours"], 0), 24)
    return settings


class CuriosityState:
    """Atomic JSON state for the daily curiosity clock, one entry per origin."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._memory = {}

    def load(self):
        with self._lock:
            if self.path is None:
                return dict(self._memory)
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            except (OSError, ValueError):
                return {}

    def save(self, state):
        with self._lock:
            if self.path is None:
                self._memory = dict(state)
                return
            body = json.dumps(state, ensure_ascii=False, sort_keys=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(body, encoding="utf-8")
                os.replace(tmp, self.path)
            except OSError:
                pass


def _candidate_row(video):
    return {"bvid": str(getattr(video, "bvid", ""))[:20],
            "title": str(getattr(video, "title", ""))[:160],
            "author": str(getattr(video, "author", ""))[:60],
            "duration": int(getattr(video, "duration", 0) or 0),
            "play": int(getattr(video, "play", 0) or 0),
            "link": str(getattr(video, "link", ""))[:160],
            "desc": str(getattr(video, "desc", "") or "")[:120]}


class BiliWatchContactEvents:
    """Daily curiosity wake that publishes a read-only candidate batch."""

    def __init__(self, contacts, factory, settings_getter=None, state_path=None):
        self.contacts, self.factory = contacts, factory
        # The host passes either a static mapping or a live getter; normalise here.
        if settings_getter is None:
            self.settings_getter = None
        elif callable(settings_getter):
            self.settings_getter = settings_getter
        else:
            self.settings_getter = lambda: settings_getter
        self.runtime = contacts.runtime
        self.state = CuriosityState(state_path)
        self.retry_at = {}
        self.guard = bind(self.runtime, SOURCE_OWNER, self._validate_source)

    def close(self):
        self.guard.close()
        self.retry_at.clear()

    def _settings(self, context, preferences):
        if (self.factory is None or context.scope.kind is not ScopeKind.PRIVATE
                or not preferences.values["automatic_enabled"]
                or preferences.reason("environment")):
            return None
        try:
            values = self.settings_getter() if self.settings_getter is not None else None
        except Exception:
            values = None
        return digest({"curiosity": curiosity_settings(values),
                       "automatic_enabled": preferences.values["automatic_enabled"]})

    def _entry(self, key):
        return self.state.load().get(key) or {}

    def select(self, context, preferences, at):
        if self.factory is None or self.retry_at.get(context.key, at) > at:
            return None
        settings_hash = self._settings(context, preferences)
        if settings_hash is None:
            return None
        settings = curiosity_settings(
            self.settings_getter() if self.settings_getter is not None else None)
        zone = ZoneInfo(preferences.values["quiet_timezone"])
        local = at.astimezone(zone)
        if not settings["wake_start_hour"] <= local.hour < settings["wake_end_hour"]:
            return None
        local_day = local.date()
        roll = int(digest(["bili-skip-v1", context.key, local_day.isoformat()])[:8], 16) % 100
        if roll < settings["skip_chance_percent"]:
            return None
        entry = self._entry(context.key)
        wakes = [datetime.fromisoformat(wake) for wake in entry.get("wakes", ())]
        today = [wake for wake in wakes if wake.astimezone(zone).date() == local_day]
        if len(today) >= settings["max_per_day"]:
            return None
        if today and at < today[-1] + timedelta(hours=settings["min_gap_hours"]):
            return None
        window_start = datetime.combine(local.date(), time(settings["wake_start_hour"]), tzinfo=zone)
        span = timedelta(hours=settings["wake_end_hour"] - settings["wake_start_hour"])
        jitter = int(digest(["bili-wake-v1", context.key, local_day.isoformat()])[:8], 16) % max(
            1, int(span.total_seconds() // 60))
        planned = window_start + timedelta(minutes=jitter)
        if at < planned:
            return None
        opportunity = Opportunity("bili-wake-" + digest([context.key, stamp(at)]),
                                  context.scope, at, at + EVENT_WINDOW)
        return opportunity, {"settings_hash": settings_hash}

    def _record(self, context, event):
        opportunity = Opportunity(event["id"], context.scope,
            datetime.fromisoformat(event["opened_at"]), datetime.fromisoformat(event["expires_at"]))
        payload = encode({"event": "bili_curiosity_batch", "candidates": event["candidates"],
                          "opened_at": event["opened_at"], "meaning": MEANING,
                          "_receipt": {"context": json.loads(encode(context)),
                                       "event_id": event["id"], "settings_hash": event["settings_hash"]}})
        identifier = "contact-bili-source-" + digest([context.key, event["id"]])
        source = SourceRef(identifier, SOURCE_OWNER, context.scope, VersionRef(identifier, 1),
                           opportunity.opened_at, opportunity.expires_at)
        return SourceRecord(source, context.audience, digest(payload), payload)

    async def prepare(self, context, marker):
        if self.factory is None or type(marker) is not dict or set(marker) != {"settings_hash"}:
            return None
        at = self.contacts.tasks.now()
        preferences = require(self.contacts.preferences.read(context, observed_at=at))
        settings_hash = self._settings(context, preferences)
        if settings_hash is None or settings_hash != marker["settings_hash"]:
            return None
        candidate = self.select(context, preferences, at)
        if candidate is None:
            return None
        port = None
        try:
            port = self.factory(context)
            if port is None:
                return None
            start = getattr(port, "start", None)
            if start is not None:
                await start()
            videos = await port.recommend(page_size=24)
        except Exception:
            if port is not None:
                close = getattr(port, "close", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:
                        pass
            self.retry_at[context.key] = self.contacts.tasks.now() + RETRY
            return None
        finally:
            close = getattr(port, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    pass
        at = self.contacts.tasks.now()
        preferences = require(self.contacts.preferences.read(context, observed_at=at))
        if self._settings(context, preferences) != settings_hash:
            return None
        candidates, seen = [], set()
        for video in videos or ():
            row = _candidate_row(video)
            if not row["bvid"] or row["bvid"] in seen:
                continue
            seen.add(row["bvid"])
            candidates.append(row)
            if len(candidates) >= MAX_CANDIDATES:
                break
        if not candidates:
            self.retry_at[context.key] = at + RETRY
            return None
        event = {"id": "bili-" + digest([context.key, settings_hash, stamp(at)]),
                 "opened_at": stamp(at), "expires_at": stamp(at + EVENT_WINDOW),
                 "settings_hash": settings_hash, "candidates": candidates}
        state = self.state.load()
        entry = state.get(context.key) or {}
        local_day = at.astimezone(ZoneInfo(preferences.values["quiet_timezone"])).date()
        entry["wakes"] = [wake for wake in entry.get("wakes", ())
                          if datetime.fromisoformat(wake).astimezone(
                              ZoneInfo(preferences.values["quiet_timezone"])).date() == local_day]
        entry["wakes"] = [*entry["wakes"], stamp(at)][-8:]
        entry["last_event"] = event
        state[context.key] = entry
        self.state.save(state)
        self.retry_at.pop(context.key, None)
        record = self._record(context, event)
        require(self.contacts.sources.publish(record, observed_at=at))
        require(self.contacts.sources.validate_reference_for_audience(
            record.reference, recipient=context.actor.subject, observed_at=at))
        marker = {"event_id": event["id"], "settings_hash": settings_hash,
                  "source": json.loads(encode(record.reference))}
        opportunity = Opportunity(event["id"], context.scope,
                                  datetime.fromisoformat(event["opened_at"]),
                                  datetime.fromisoformat(event["expires_at"]))
        thought = Thought("bili-thought-" + digest(marker), opportunity, ThoughtKind.CONVERSATION,
                          THOUGHT_TEXT, EvidenceSnapshot(context.scope, at, (record.reference,)), 1.0)
        return opportunity, (ContactIntent("environment", thought),), marker

    def _validate_source(self, record, at):
        try:
            receipt = json.loads(record.payload)["_receipt"]
            context = TaskContext.restore(receipt["context"])
            preferences = require(self.contacts.preferences.read(context, observed_at=at))
            if self._settings(context, preferences) != receipt["settings_hash"]:
                raise ValueError("curiosity settings changed")
            event = self._entry(context.key).get("last_event")
            if event is None or event["id"] != receipt["event_id"] or event["settings_hash"] != receipt["settings_hash"]:
                raise ValueError("curiosity batch is no longer current")
            opportunity = Opportunity(event["id"], context.scope,
                datetime.fromisoformat(event["opened_at"]), datetime.fromisoformat(event["expires_at"]))
            if (not opportunity.opened_at <= at < opportunity.expires_at
                    or record != self._record(context, event) or record.content_hash != digest(record.payload)):
                raise ValueError("curiosity batch changed")
        except TaskError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise TaskError("contact_bili_changed", "bilibili curiosity source is no longer current") from error

    def validate(self, context, marker, at):
        if type(marker) is not dict or set(marker) != {"event_id", "settings_hash", "source"}:
            raise TaskError("contact_bili_invalid", "bilibili curiosity marker is invalid")
        try:
            source = source_from_document(marker["source"])
            event = self._entry(context.key).get("last_event")
            if (source.owner != SOURCE_OWNER or source.scope != context.scope or event is None
                    or event["id"] != marker["event_id"] or event["settings_hash"] != marker["settings_hash"]
                    or source != self._record(context, event).reference):
                raise ValueError("curiosity marker changed")
            require(self.contacts.sources.validate_reference_for_audience(
                source, recipient=context.actor.subject, observed_at=at))
        except TaskError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            raise TaskError("contact_bili_changed", "bilibili curiosity marker is no longer current") from error
