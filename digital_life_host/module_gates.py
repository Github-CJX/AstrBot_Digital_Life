"""M2 模块开关。

用户确认的三类处置（见 docs/digital-life/FEATURE_REQUIREMENTS.md）：

* 保留 8 项永远开启，不受开关影响；
* 可选 6 项默认关闭，用户显式开启后才会注册能力、挂后台任务；
* 移除 4 项默认关闭，M2 第一步先停掉它们的注册与调度，实现保留以便回退。

开关只决定「注册与调度」，不删除任何实现：关闭之后历史数据仍可只读查询，
重新打开即可恢复。解析顺序为环境变量 > 开关文件 > 内置默认值。

环境变量：``DIGITAL_LIFE_MODULE_<NAME>=1``（大小写不敏感，``0/false/no/off`` 视为关闭）。
开关文件：插件数据目录下的 ``digital_life_modules.json``，形如::

    {"modules": {"image": true, "qzone": false}}
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

CONFIG_FILE = "digital_life_modules.json"
ENV_PREFIX = "DIGITAL_LIFE_MODULE_"

#: 保留项：用户确认必留，开关不会关掉它们。
RETAINED_MODULES = frozenset({
    "content",      # F08 阅读与创作（创作书架能力，与聊天共用）
    "reality",      # 现实触及的设备底座（手机/屏幕从它内部单独拆分）
    "together",     # F13 一起做事的共用底座
})
#: 可选项：默认关闭，用户显式开启。
OPTIONAL_MODULES = frozenset({
    "game",         # F11 内置小游戏
    "image",        # F12 生图与合照
    "workspace",    # F13 3D 陪伴小屋（前端入口另行隐藏）
    "multi_role",   # F14 多角色管理入口
    "phone",        # F15 手机控制
    "screen",       # F18 远程屏幕捕获
    "bilibili",     # F09 拆出的 B 站浏览（纯读：看懂与分享，不做直播/写操作）
})
#: 移除项：默认关闭，先停注册与调度。
REMOVED_MODULES = frozenset({
    "live",         # F09 B 站直播互动
    "qzone",        # F10 QQ 空间互动
    "mihome",       # F16 米家智能家居
    "huawei_health",  # F17 华为健康
})

ALL_MODULES = RETAINED_MODULES | OPTIONAL_MODULES | REMOVED_MODULES
DEFAULTS = {name: name in RETAINED_MODULES for name in sorted(ALL_MODULES)}

#: 配置分节所属模块。没登记的节按保留项处理，永远可见。
SECTION_MODULES = {
    "quota": "image",
    "image_review": "image",
    "image.output": "image",
    "wardrobe": "image",
    "photo.reference": "image",
    "screen": "screen",
    "device.phone": "phone",
    "qzone": "qzone",
    "content.authoring": "content",
    "library": "content",
}

#: 页面资产入口所属模块。没登记的入口按保留项处理。
PAGE_MODULES = {
    "workspace": "workspace",
    "room": "workspace",
    "multi_role": "multi_role",
    "game": "game",
    "phone": "phone",
    "screen": "screen",
}

_FALSE_WORDS = frozenset({"", "0", "false", "no", "off", "disable", "disabled"})
_state = {"settings_root": None}


def configure_modules(settings_root) -> None:
    """记住插件数据目录，供后续无参解析开关。"""
    _state["settings_root"] = Path(settings_root) if settings_root is not None else None


def settings_root():
    return _state["settings_root"]


def _env_override(name):
    raw = os.environ.get(ENV_PREFIX + str(name).upper())
    if raw is None:
        return None
    return raw.strip().lower() not in _FALSE_WORDS


def _file_overrides(settings_root):
    if settings_root is None:
        return {}
    path = Path(settings_root) / CONFIG_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except Exception:
        logging.getLogger(__name__).warning(
            "[DigitalLife][modules] 模块开关文件无法解析 path=%s", path)
        return {}
    modules = payload.get("modules") if isinstance(payload, dict) else None
    if not isinstance(modules, dict):
        return {}
    return {str(key): bool(value) for key, value in modules.items() if str(key) in ALL_MODULES}


def resolved(*, settings_root=None):
    """返回全部模块的生效开关（环境变量优先，其次开关文件，最后内置默认）。

    每次都重新解析：开关文件很小，而缓存会让运行期改开关、测试里改环境变量
    读到过期状态。
    """
    root = settings_root if settings_root is not None else _state["settings_root"]
    overrides = _file_overrides(root)
    values = {}
    for name in ALL_MODULES:
        environment = _env_override(name)
        values[name] = environment if environment is not None else overrides.get(name, DEFAULTS[name])
    return values


def module_enabled(name, *, settings_root=None) -> bool:
    if name not in ALL_MODULES:
        return True
    return bool(resolved(settings_root=settings_root).get(name, DEFAULTS.get(name, True)))


def enabled_modules(names, *, settings_root=None) -> tuple:
    """按顺序过滤出已开启的模块名。"""
    return tuple(name for name in names if module_enabled(name, settings_root=settings_root))


def describe(*, settings_root=None) -> str:
    values = resolved(settings_root=settings_root)
    disabled = [name for name in sorted(values) if not values[name]]
    return ",".join(sorted(values)) + " | off=" + (",".join(disabled) or "-")


def section_visible(section, *, settings_root=None) -> bool:
    """配置分节是否对当前安装可见（所属模块开启才可见）。"""
    return module_enabled(SECTION_MODULES.get(section, ""), settings_root=settings_root)


def visible_sections(names, *, settings_root=None) -> tuple:
    """按顺序过滤出可见的配置分节。"""
    return tuple(name for name in names if section_visible(name, settings_root=settings_root))


def page_visible(page, *, settings_root=None) -> bool:
    """页面入口是否可见（3D 小屋、多角色、游戏等挂在可选模块上）。"""
    return module_enabled(PAGE_MODULES.get(page, ""), settings_root=settings_root)


def page_visibility(*, settings_root=None) -> dict:
    """给前端的一份页面入口开关表。"""
    return {name: page_visible(name, settings_root=settings_root) for name in sorted(PAGE_MODULES)}

