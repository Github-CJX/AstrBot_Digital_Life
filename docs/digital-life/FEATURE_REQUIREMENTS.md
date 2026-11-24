# 用户确认需求 · M1/M2 基线

2026-11-24；保留 8、可选 6、移除 4。仅记录需求，M1 不删除、未强制关停旧功能。可选模块 M2 默认关闭（无后台、路由、外部 I/O），用户显式开启。以下为候选定位，不是完整依赖清单。

| 编号 | 功能 | 处置 | 候选定位（core/host 或 pages） |
|---|---|---|---|
| F01 | 聊天与表达学习 | 保留 | `conversation/, expression/` |
| F02 | 长期记忆 | 保留 | `memory/` |
| F03 | 生理与睡眠 | 保留 | `role_work/life/` |
| F04 | 情绪系统 | 保留 | `role_work/state/mood/` |
| F05 | 自我认知与成长 | 保留 | `role_work/growth/, role_profile/` |
| F06 | 日记与梦境 | 保留 | `role_work/journal/` |
| F07 | 社交关系 | 保留 | `relationships/, social_runtime/` |
| F08 | 阅读与创作 | 保留 | `library/, content/` |
| F09 | B 站直播互动 | 移除 | `live/, native_extensions/live/` |
| F10 | QQ 空间互动 | 移除 | `qzone/, qzone_api.py, qzone_client.py` |
| F11 | 内置小游戏 | 可选 | `games/, native_extensions/game/` |
| F12 | 生图与合照 | 可选 | `native_extensions/image_native/, storage/compaction.py` |
| F13 | 3D 陪伴小屋 | 可选 | `pages/陪伴小屋/（保留管理后台和工作区）` |
| F14 | 多角色管理 | 可选 | `multi_controller.py, role/identity/scope` |
| F15 | 手机控制 | 可选 | `mobile_together.py, native_extensions/reality/mobile_gateway.py` |
| F16 | 米家智能家居 | 移除 | `native_extensions/reality/mihome_web_api.py, _mihome_*` |
| F17 | 华为健康 | 移除 | `huawei_health/` |
| F18 | 远程屏幕捕获 | 可选 | `screen/, native_extensions/screen_provider.py` |

## 重要边界
- 移除 B 站直播，不等于移除未来 B 站视频只读浏览；保留通用视频理解与真实观看回执。
- 移除 QQ 空间，不等于关闭 QQ 私聊、群聊。
- 3D 小屋可选，管理 API 和非 3D 管理界面必须保留；不能直接删除整个 pages。
- 多角色可选仅简化使用入口，角色、安装、受众及权限边界不能删除。
- 米家、华为健康与手机/远程屏幕必须拆分，不能一刀切删除 reality。
- 生图可选，必须先解除 storage/compaction.py 反向依赖，保留历史附件。
- 移除顺序：注册/调度停止 → 配置和页面隐藏 → 依赖解耦 → 验证历史读取 → 清除实现与依赖。每项验证关闭后无外部 I/O、保留正常聊天和重载。
