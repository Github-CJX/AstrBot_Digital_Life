# NOTICE

本包内 `bili_api.py`、`errors.py`、`models.py`、`quota.py`、`traffic.py`、`util.py`
源自开源项目 [astrbot_plugin_bili_whale](https://github.com/momo-OwO-qwq/astrbot_plugin_bili_whale)
（"养鱼刷B站"），以 MIT License 发布，Copyright (c) 2026 astrbot_plugin_bili_whale contributors。

引入时的改动（遵守原许可，保留本署名）：
- 移除全部写侧能力：`post_comment` / `send_dm` / `relation` / `mentions` /
  `_find_comment` / `verify_comment` / `has_write_cookie` 及相关常量——
  Digital Life 的 B 站浏览是纯读，不做任何站内写操作。
- `traffic.py` 的持久化后端由 whale 的 `store.Store` 替换为本包 `state.QuotaStore`
  （插件数据目录 JSON，原子写）。
- 文档与注释措辞按 Digital Life 语境微调，行为语义不变。

原项目 MIT License 文本（LICENSES/bili-whale-MIT.txt 存有完整副本）。
