"""补齐已安装原生 Content 创作能力在核心私聊上下文中的默认授权记录。"""

from __future__ import annotations

from datetime import datetime
import logging

from digital_life_core.conversation.content.provisioning import (
    CONTENT_CAPABILITIES,
    CONTENT_EXTENSION,
    content_authorization_status,
    ensure_content_access as _ensure_content_access,
)
from digital_life_core.execution.models import TaskContext
from digital_life_core.storage.runtime import StorageRuntime


logger = logging.getLogger(__name__)


def ensure_content_access(
    runtime: StorageRuntime,
    context: TaskContext,
    *,
    observed_at: datetime,
) -> bool:
    """在核心私聊上下文且开启创作开关时，幂等补齐 Content 角色与会话授权。"""
    return _ensure_content_access(runtime, context, observed_at=observed_at, log=logger)


__all__ = [
    "CONTENT_CAPABILITIES",
    "CONTENT_EXTENSION",
    "content_authorization_status",
    "ensure_content_access",
]
