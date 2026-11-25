"""老套件按「全功能安装」跑：M2 的模块开关在这里统一打开。

M2 把可选 6 项与待移除 4 项改成默认关闭，能力注册也随之停止。历史上这些
套件直接断言七类内置 provider 都在场，因此这里统一用环境变量把开关打开，
代表「用户显式开启了全部可选模块」的安装形态。

`tests/test_m2_module_gates.py` 会清掉这些变量，单独验证默认值。
"""

from __future__ import annotations

import os

import pytest

_FULL_FEATURED = (
    "game",
    "image",
    "workspace",
    "multi_role",
    "phone",
    "screen",
    "live",
    "qzone",
    "mihome",
    "huawei_health",
)


@pytest.fixture(autouse=True, scope="session")
def full_featured_install():
    for name in _FULL_FEATURED:
        os.environ.setdefault("DIGITAL_LIFE_MODULE_" + name.upper(), "1")
    yield
