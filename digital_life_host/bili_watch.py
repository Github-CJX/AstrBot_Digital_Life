"""B 站浏览端口的装配（宿主侧拥有网络与凭据，核心侧只见鸭子类型端口）。

模块开关 ``bilibili``（可选，默认关）决定是否提供 ``bili.watch`` 工具的端口
工厂；配置 ``bilibili.cookie`` 是可选的登录 cookie（``SESSDATA=…; bili_jct=…``
整串粘贴即可，JSON 键值对也认）。没有 cookie 也能看公开视频，只是字幕与
推荐流会更弱。

风控预算沿用 vendored bili_whale 的三道闸：请求间隔、按小时/天记账、
412/-352 等风控码熔断；预算状态持久化在 ``bilibili/quota.json``。
"""

from __future__ import annotations

import json
from pathlib import Path

from .module_gates import module_enabled

_QUOTA_RELATIVE = ("bilibili", "quota.json")

_store_cache: dict[Path, "object"] = {}


def parse_cookie(raw) -> dict[str, str]:
    """接受 ``SESSDATA=…; bili_jct=…`` 头串或 JSON 键值对，返回小写键映射。"""
    if isinstance(raw, dict):
        return {str(k).strip().lower(): str(v).strip() for k, v in raw.items() if str(v).strip()}
    if not isinstance(raw, str) or not raw.strip():
        return {}
    text = raw.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return {str(k).strip().lower(): str(v).strip() for k, v in data.items() if str(v).strip()}
        except ValueError:
            pass
    cookie: dict[str, str] = {}
    for part in text.replace("\n", ";").split(";"):
        name, _, value = part.strip().partition("=")
        name, value = name.strip().lower(), value.strip()
        if name and value:
            cookie[name] = value
    return cookie


def _shared_store(path: Path):
    store = _store_cache.get(path)
    if store is None:
        from digital_life_core.bilibili.state import QuotaStore

        store = QuotaStore(path)
        _store_cache[path] = store
    return store


def configured_bili_factory(config, data_root):
    """返回 ``factory(context) -> 端口``，模块关闭或装配失败时返回 ``None``。"""
    if not module_enabled("bilibili"):
        return None
    section = config.get("bilibili") if isinstance(config, dict) else None
    settings = section if isinstance(section, dict) else {}
    cookie = parse_cookie(settings.get("cookie"))
    path = Path(data_root).joinpath(*_QUOTA_RELATIVE)

    def factory(context):
        from digital_life_core.bilibili.bili_api import BiliAPI
        from digital_life_core.bilibili.traffic import TrafficGuard

        guard = TrafficGuard(_shared_store(path), lambda: settings)
        return BiliAPI(cookie=cookie, guard=guard)

    return factory


def configured_bili_settings(config):
    """好奇心生物钟配置：``bilibili.curiosity`` 缺省时用核心默认值。"""
    section = config.get("bilibili") if isinstance(config, dict) else None
    curiosity = section.get("curiosity") if isinstance(section, dict) else None
    if isinstance(curiosity, dict) and curiosity:
        return curiosity
    return None
