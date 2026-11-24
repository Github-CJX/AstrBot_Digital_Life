"""M1 development fork: never automatically adopt the upstream data directory."""
from pathlib import Path

PLUGIN_ID = "astrbot_plugin_digital_life"
UPSTREAM_NAMES = {"astrbot_plugin_private_companion", "astrbot_plugin_private_companion_v2", "astrbot_plugin_remember_you"}

def validate_default_data_root(path: Path) -> Path:
    resolved = path.resolve()
    if path.name != PLUGIN_ID or resolved.name != PLUGIN_ID or any(p.name in UPSTREAM_NAMES for p in resolved.parents):
        raise ValueError("Digital Life requires a dedicated data directory; upstream data migration is not enabled")
    return resolved
