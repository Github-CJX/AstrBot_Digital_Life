# 大肥鱼 · AstrBot Digital Life
## 二次开发架构审计与改造总纲 v0.1

**状态：静态扫描完成；重点模块抽查、参考架构调研及离线策略原型完成；生产插件尚未修改。**  
基线：用户上传 `astrbot_plugin_private_companion-main(1).zip`（7.3.1），2026-11-24 审计。  
目标：以 AstrBot 为聊天宿主，建立自主生活、真实数字经历、可追溯记忆、渐进兴趣与可管理安全边界；不讨论主观意识，不以任务完成或主动联系次数为唯一目标。

## 1. 决策摘要

采用原插件 Fork + 渐进式替换，不拼接多个完整运行时。沿用 AstrBot 的正常回复链、角色身份、记忆、关系、身体、后台队列及数据治理。日程仍服务于明确承诺，但不再支配全部日常行为。初始现实世界能力优先为 B 站只读浏览、网页、阅读、私人创作；保留虚拟活动但显式标注模拟来源。设置界面改为默认简易、按目标查找、按需展开；非必要模块先禁用并观测，再拆依赖和删除。

本轮不是逐行人工审计，也不是安全认证或可安装成品。扫描覆盖整个归档；运行环境缺少 AstrBot SDK，未进行真实账号、生产数据、模型服务或平台调用。没有改动原 ZIP。后续阶段以本文件、清单及测试证据为基线，不以此前聊天中的推测为事实。

## 2. 全量静态盘点

| 项 | 结果 |
|---|---:|
| ZIP 条目（含目录） | 2,735 |
| 实际文件 | 2,613 |
| Python 文件 / AST 解析成功 | 2,083 / 2,083 |
| 核心 Python / 宿主 / tests / tools / main | 763 / 363 / 882 / 74 / 1 |
| 静态 import 记录（含 from 成员、重复） | 55,799 |
| 宿主 schema 一级组 / 叶字段 | 15 / 136 |
| SQL DDL 字符串候选 / 不同表名 | 444 / 229 |
| HTTP 路由前缀字面量候选 | 95 |
| 后台调度调用候选 | 424 |
| 反向引用候选（含重复成员） | 20 |

归档 SHA256：`30d1c318b5e2bf9be62cdb7eac2b66b245eb9d9a8b27cbaeafdcb6b03133f5f6`。来源 `metadata.yaml`、`pyproject.toml`：原插件作者 menglimi，AGPL-3.0-only，AstrBot >=4.27.2、Python >=3.10。存储当前源码版本 `companion-v2.storage.v69`（`companion_v2/storage/schema.py` 末尾），不是实际部署数据版本。

`inventory/` 为逐文件机器清单：文件散列、Python 行数/定义数、import 源位置与候选目标、跨域依赖汇总、反向依赖候选、宿主配置叶字段、DDL/路由/调度候选。`scan.py` 可复现。AST 无法发现全部动态加载、运行条件、SQL 拼接、JS 动态路由、隐式依赖及实际可达性。DDL 候选含历史迁移和测试，不等于现存数据库表数。宿主 schema 不等于全量角色级配置；角色配置还须从 `configuration/registry.py`、`role_settings.py`、各领域配置及前端 spec 对照导出。清单可用于定位，不能直接作为删除依据。

## 3. 当前架构、主链与风险

### 3.1 入口和运行时

文档 `docs/REPOSITORY_LAYOUT.md` 定义 main.py → astrbot_host → companion_v2。入口持有唯一 Star，负责消息事件、生命周期、普通回复增强；`astrbot_host/multi_controller.py` 管理初始化、绑定、角色/目标和会话；`companion_v2/host/application.py` 装配各领域服务和维护轮转。该文件创建 `RoleLifeCoordinator`，注册 `life-runtime` TickUnit，退出时取消、关闭活动。`host/background.py` 用有界后台槽、任务队列和 generation 校验，已有通知和关闭机制。

**A01 / P0 Fork 命名与共存：** `main.py` 有 `PLUGIN_NAME`；`astrbot_host/import_lifecycle.py` 对全局 `companion_v2` 路径冲突直接抛错，退出还按 namespace 清理 sys.modules。不能仅改 metadata。第一阶段优先独立测试宿主只加载 Fork；如需与上游共存，再迁移 core 包名、绝对/动态 imports、日志及路由前缀。保留历史数据产品标识与兼容映射，不能全局盲目替换数据库协议。核查配置、页面、工具、API 注册和卸载，重复加载、禁用、重载测试必须通过。

**A02 / P0 依赖边界例外：** `companion_v2/storage/compaction.py:257,421` 直接引用 `astrbot_host.native_extensions.image_native.store` 的常量和迁移函数；另外 `companion_v2/adapters/astrbot/` 存在 SDK 引用（适配边界）。存储模块应通过显式迁移适配器调用或保留兼容迁移 shim；不可直接删生图。先确定历史图片资产与备份恢复行为，再隔离。

**A03 / P0 后台竞争：** 自主生活必须使用已有队列、generation、取消和生命周期，不能再起一个无界 while true 循环。每 role 同时一个主活动；观察/紧急消息可以打断，闲暇活动不抢聊天模型额度。关闭模块时取消/等待在途任务并注销路由、订阅和能力。

### 3.2 生理、日程和心理

**A04 / P1 睡眠非纯状态驱动：** `role_work/life/physiology.py` 用清醒小时、睡眠债、负荷计算睡眠压力；`runtime.py:350–416` 检查夜间时间窗口，`night_sleep_ready(profile) or window_closed` 可触发入睡。保留计算和活动回执，增加实验性自主模式，只保留作息软倾向及明确承诺；旧模式默认兼容。首次没有睡眠史需显式初始化，避免 None 被当成零。午休、用户打断、跨午夜、时区和重启需独立测试。

**A05 / P1 夜间行为硬编码：** `agenda/planning_output.py:13` 禁止普通工作/学习等在 23:00–07:00 规划（夜班等例外）。不能简单删除。先将其改为角色明确承诺/世界可用性判断与软成本；日程只保存真实承诺或预期，不写成经历；非承诺生活由策略生成。

**A06 / P1 情绪复用：** `role_work/state/mood/models.py` 定义 joy/anger/sorrow/fear、1–10、基线/强度；现有角色身体情绪与关系事件有区分。继续以原 mood 为权威，不建第二套冲突情绪。引入独立需求向量，通过带来源的活动结果和时间推进更新，并用有限参数影响行为。模型只能提议影响，代码校验范围与因果；不能每轮随意改数值。

**A07 / P1 探索任务化：** `proactive/exploration.py` 的 `plan_goals` 基于日期和配置兴趣；MAX_DAILY_SESSIONS=3、MAX_QUERIES=3、RESULTS_PER_QUERY=4、24 小时目标冷却。保留为事件研究/准备，不直接扩展成娱乐浏览。独立生活浏览由 world adapter 提供候选，内容不要求总结、不自动向用户分享。避免过度重复，并设置真实调用成本和时间预算。

### 3.3 数据与记忆

**A08 / P0 真实活动回执：** `role_work/life/runtime.py:365–376` 已将重启中断阅读置为 PAUSED，不以经过时间作为完成；新世界活动必须沿用这一语义。状态与回执写入使用事务、幂等 operation_id、expected_revision；外部操作结果未知时标记 uncertain 待核对，不自动重放。`role_work/models.py`、`ledger.py` 是活动类型/存储的重要入口。不要把观察、计划、推测、模拟、执行结果合并成事实。

**A09 / P0 存储与权限：** `storage/runtime.py` 管理版本化 SQLite、失败关闭、写批次；`storage/schema.py` 当前 v69。保留原迁移链；新增迁移 v70（若实施前上游已变化则重编号）与明确回滚策略，生产数据先只读备份，在复制库升级。对 scope、role、user、principal、audience 校验；私聊资料禁止跨群泄露。原外部记忆 API `companion.external-memory.v1` 仅承诺 memory provider，不是通用能力接口（`docs/extension-development.md`）。

**A10 / P0 生命周期：** 角色身体按 role 共享，不可每个聊天窗口各自生活。`role_work/life/coordinator.py` 有跨窗口 sleep executor 选举；复用其所有权概念。多个 host 同角色部署仍要明确唯一执行者及 lease，不假设已有进程间租约。时间使用 UTC 存储和配置时区展示，离线时允许状态按时间衰减但不补造网络行为。

### 3.4 前端和设置

`pages/陪伴小屋/` 为规范源；`tools/sync_framework_page.py` 同步内嵌工作区、生成清单。`docs/CONFIG_UX_PLAN_20261117.md` 已提出 essential、目标 recipe、字段影响、revision 冲突指引、文档与 spec 同源。原宿主性能模式会跳过管理路由，因此不能直接当作新版简易界面。`astrbot_host/life_api.py` 的 GET /life 与 POST /life/command 具有 Dashboard identity、view/revision 及 operation 校验；不要绕过它直接让 JS 写 SQLite。

**A11 / P1 配置复杂度：** 简易/高级仅是展示方式，不能覆盖用户已配置字段；保存前 diff，处理 revision conflict，敏感值掩码，保持浏览器刷新后的草稿，后端仍校验所有字段。先保留 API schema，重构导航和 spec，再决定删除哪些字段。

### 3.5 依赖与许可

`requirements.txt` 中 Pillow 与 AstrBot pin 兼容，含 numpy/sounddevice/soundfile/opencv、mijiaAPI/fastmcp/mcp、quickjs-ng、aiohttp-socks 等。现实设备和余额功能候选依赖可在隔离后移至可选安装；RSS 安全解析 defusedxml 当前由 controller 导入，不可直接删除。MCP 可能是宿主共享依赖，须以实际导入和宿主版本测试。AGPL-3.0-only：保留许可证及版权/修改声明，分发或通过网络提供修改版时按适用条款提供对应源代码；直接复用第三方代码前检查许可证与资源版权（本轮未复制外部代码）。

## 4. 全领域取舍矩阵（建议，不是已删）

| 域 | 处置 | 依赖门槛 |
|---|---|---|
| foundation/product/identity_policy/role_profile | 保留 | 身份、scope、版本与权限 |
| host/adapters/conversation/decision/delivery/model_gateway | 保留 | 原生聊天、模型、任务路由不受回归影响 |
| configuration/storage/execution/diagnostics | 保留 | 配置、迁移、预算、可观测性 |
| memory/relationships/social_runtime/expression | 保留 | 真实经历、关系、表达、隐私 |
| role_work/agenda/proactive | 重构 | 身体、情绪、日程、主动生活；双模式迁移 |
| library/knowledge/content | 保留 | 阅读、知识、个人创作 |
| capabilities/integration/references/sources/system_prompts | 保留并整理 | 能力注册、出处、提示词 |
| persona_craft/persona_designer | 高级可选 | 人格编辑与历史档案 |
| live/games/together/qzone/sharing | 可选默认关 | 先停止注册与调度；真实发布须授权 |
| reality/device/screen | 可选默认关 | 权限、远程设备、媒体桥接 |
| Image（宿主 native_extensions 等） | 可选默认关 | compaction 反向依赖、历史图片恢复 |
| legacy | 暂保留 | 迁移验收后再归档，不丢旧资料 |
| astrbot_host 多目标/账户绑定 | UI 简化 | 内部身份/受众边界不可删 |
| 华为健康/米家/手机/余额/3D 小屋 | 第一批界面隐藏、运行禁用 | 逐项断开导入/路由/后台/设置/数据后再删 |

裁剪流程：记录 capability 与 owner → 查 `imports.csv`、路由、调度、SQL、配置、JS 引用 → 默认关闭 → 验证不产生 I/O → 独立模块加载改为 lazy → 历史读取兼容 → 删无用前端/依赖 → 构建/重载/备份验证。禁止直接根据目录名删除。保留可恢复的原版、迁移 shim 和发布差异。

## 5. 外部架构研究与采纳决策

以下是公开资料/代码页面的初步核对，不是已经克隆、安装和跑过外部项目的结果。不要引入多个彼此竞争的 agent loop。

| 参考 | 已核对的机制 | 我们的采纳方式 |
|---|---|---|
| D2A (ICLR 2025) | 内在需求驱动候选与活动选择；仓库包含 Concordia 变体及 Value_ActComp/value components 实验 | 采用候选→评分→选择；对照实验，不直接依赖其旧版仿真运行时 |
| MicroPsi | 生理、社交、认知动机及其对认知行为的调节 | 欲望/注意力/疲劳的软调制；公式先实验，不冒充已验证心理模型 |
| Concordia | GM 管世界，Engine 管时序和因果，行动提议须解析结果 | WorldPort 的 observe/execute/receipt；真实平台由真实结果裁决，模拟世界独立 |
| AgentSociety | 1.x legacy 的 Mind–Behavior Coupling、Reason/Route/Action；2.x 已转为新的 LLM-native 仿真平台 | 借鉴旧版分层和实验记录；不混淆版本、不引入城市规模仿真 |
| Generative Agents | 日程、短期记忆、长期反思与环境变量 | 行为惯性、行动状态、反思触发；避免每天填满日程 |
| Letta | 当前文档 MemFS 与后台 dreaming、记忆整理 | 提议→核对→写入，版本化反思；不替换现有记忆所有权 |
| dsh-agent-soul | 仓库定位为灵魂架构及自检判据 | 每个领域明确权威源、接口、故障与自检，借鉴记忆溯源与稳态方向；本轮未复核其全部实现 |

来源（核对日期 2026-11-24）：
- D2A: https://github.com/zfw1226/D2A ，https://arxiv.org/abs/2412.06435 ，源码入口 https://github.com/zfw1226/D2A/blob/main/examples/D2A/D2A_agent/Value_ActComp.py
- MicroPsi: https://www.media.mit.edu/projects/micropsi-an-architecture-for-motivated-cognition/overview/ ，https://academic.oup.com/book/1947
- Concordia: https://github.com/google-deepmind/concordia/blob/main/concordia/environment/README.md ，https://github.com/google-deepmind/concordia/blob/main/concordia/components/README.md
- AgentSociety: https://github.com/tsinghua-fib-lab/AgentSociety/blob/main/packages/agentsociety/README.md ，https://github.com/tsinghua-fib-lab/AgentSociety/blob/main/packages/agentsociety2/README.md
- Generative Agents: https://github.com/joonspk-research/generative_agents ，https://github.com/joonspk-research/generative_agents/blob/main/reverie/backend_server/persona/memory_structures/scratch.py
- Letta: https://docs.letta.com/configuration/memory
- DSH: https://github.com/cyanwell/dsh-agent-soul

## 6. 目标架构与代码合同

```text
AstrBot (native reply / messages / model gateway)
  └─ existing host adapter + lightweight dashboard
      └─ existing application / background / role coordinator
          ├─ PSYCHE: needs + mood/body projections
          ├─ LIFE: candidates → policy → activity state machine
          ├─ WORLD: capability catalog / observe / execute / receipt
          ├─ INTEREST: evidence-weighted interests / novelty / saturation
          └─ existing memory & journals ← verified experience records
```

建议新目录 `companion_v2/digital_life/`（最终 core 命名另行迁移），初期仅新增纯逻辑：`models.py, policy.py, needs.py, interests.py, ports.py, service.py`。现有 `role_work/life/runtime.py` 保留为真正执行入口，通过 feature flag 选择旧日程式或新策略；`host/application.py` 复用原 tick，不重复开线程或 loop。`world/` adapters 独立：浏览、阅读、创作、虚拟空间。所有动作先由权限层判定 capability、受众、资源和外部影响，再进入策略；不存在的能力不能由 LLM 捏造。

### 6.1 状态和事件协议（拟定，不是现有 API）

| 对象 | 字段（核心） | 权威 |
|---|---|---|
| RoleState | role_id, revision, observed_at, timezone, existing body/mood projection | 原身体及 mood 服务 |
| NeedState | role_id, curiosity, novelty, social, solitude, creativity, last_updated_at, revision | needs reducer |
| Interest | topic_id, affinity, novelty, evidence_ids, updated_at | interest service |
| Intent | intent_id, role_id, capability_id, parameters, reasons, estimated_cost, interruptible | 策略候选，非经历 |
| Activity | activity_id, owner, state, started_at, checkpoint, revision, operation_id | 原活动账本/适配 |
| Receipt | activity_id, status, source_uri, evidence_kind, observed_at, artifact_ref/hash, cost, duration, failure_code | executor / world adapter |
| Experience | event_id, role_id, origin, receipt_id, visibility, evidence refs, description | memory ingestion |

外部内容视为不可信数据；字幕、评论、网页指令不能改变权限、工具调用、配置或系统提示词。按最小字段读取；绝不将私聊、token、账号秘密带入搜索。用户可暂停、导出和删除；日志不存凭证。证据来源标签 `observed`, `executed`, `planned`, `inferred`, `simulated`, `dream`；只有 verified observed/executed 可作为真实经历，其他来源不提升为事实。真实经历仍需区分内容观点与客观事实。

### 6.2 行动选择与合理的自由

候选来自当前兴趣、明确承诺、内在需求、世界可用活动；LLM 可提出新候选和参数，但不能擅自增加工具权限或写入状态。首先过滤硬约束（可用性、授权、预算、暂停、安全），再用可解释评分衡量需求预期满足、兴趣、当下能量、昼夜倾向、重复、打断损失及承诺。允许 `idle`/`sleep`；抽样需可重放、记下 seed、分项评分和选择理由。没有承诺时夜间工作仍可行，但低优先；特殊灵感或合理承诺可覆盖昼夜偏好。当前活动在完成/取消/明确打断/资源耗尽之前保持，不要每 tick 重选。后果由回执更新；绝不靠生成理由事后合理化。

参考原型 `prototype/life_policy.py` 为纯函数，无网络/模型调用，验证夜间睡眠、灵感创作、授权、行动惯性、重复惩罚、预算暂停和可复现随机选择。它尚无持久化、真实执行器、需求状态更新、实际效果校准或 AstrBot 接入。参数是起始假设，不是科学拟合值。

### 6.3 B 站：第一种真实世界

先确认大肥鱼已有浏览能力的实际工具协议、权限、可用输入（推荐/搜索/标题/字幕/音频/画面/评论/历史），本 ZIP 没有大肥鱼个人现有工具源码，不能假定它已有全部接口。建议 `BilibiliPort`：`observe_feed`、`search`、`inspect_video`、`consume`、`leave`，返回结构化观察与观看回执。只读浏览默认；收藏/点赞/关注/评论/发布等外部写入单独授权、限频并遵循平台政策。不绕过风控、访问限制或反爬措施。账号凭据只在宿主秘密存储，不能进模型上下文。

视频消费记录按实际可见内容分级（metadata/transcript/audio/frames/comments），标记实际覆盖范围、时间、来源、观看中断。只读简介不能写成看完视频；失败、离线、无字幕均允许结束。经历可只留一条简短感想或不写感想，绝不强制每次总结。兴趣根据自愿继续、重复选择、反馈与后续搜索证据更新，随时间减弱；保留探索率，避免平台推荐和角色兴趣形成封闭反馈。记录真实成本，不能伪造观看时长。若用户未互动仍可自主浏览，但达到预算就暂停。

### 6.4 生活回路与长期演进

时间推进（确定性衰减） → 观察真实事件/活动结果 → 需求及情绪变化 → 候选 → 选择 → 执行/暂停 → 回执 → 记忆 → 兴趣更新。快变量分钟级、精力/需求小时级、兴趣周级、人格月级；人格变化需多次有证据的提议、版本、撤销，不自动篡改安全政策、身份或权限。日记与梦境独立记录，梦境不改变真实世界状态。没有用户对话不意味着要主动联系；主动消息另设受众授权、安静时段和频率，尊重拒绝。

## 7. Dashboard 重构规格

默认 6 个一级入口；隐藏不是删除。原有后端 API 仍为权威，前端不得绕过权限或保存所有字段。禁止将模型自行生成的状态展示为真实运行记录。

| 页面 | 默认内容 | 高级入口 |
|---|---|---|
| 概览 | 当前活动、真实/模拟来源、运行状态、暂停、今日经历、预算 | 调度/错误/任务追踪 |
| 生活 | 当前需求/身体、活动轨迹、承诺、兴趣倾向 | 权重、时间窗口、历史配置 |
| 记忆 | 可溯源经历、日记、梦境、搜索、隐私标签 | 召回、整理、删除/导出 |
| 自我 | 身份、兴趣、变化记录、习惯 | 人格版本、表达学习 |
| 世界 | 可用能力、只读/写入权限、B站与阅读 | 设备/游戏/直播等模块管理 |
| 设置 | 模型、时区、生活模式、预算、主动联系、权限、备份 | 全部设置/诊断/配置导入 |

首次配置流程：选角色/绑定 → 模型 → 时区 → 每日预算 → 开启世界能力；不开设备不显示相关字段。目标配方如“只聊天”“让他自由生活但少主动联系”“限制每日成本”，先展示 diff 再保存。常用/全部由 essential spec 控制，支持搜索、上下文帮助、错误下一步、依赖预检。原作者已有 UX 设计，先复用现有 spec 与 API；通过 `tools/sync_framework_page.py` 构建，不手改生成副本。需要前端 API 映射完整验证、移动端/桌面截图、加载性能与权限测试；本轮未做真实 UI 改动。

## 8. 实施任务与验收门槛

| 阶段 | 修改范围 | 必须通过的退出条件 |
|---|---|---|
| M0 基线/本轮 | 全归档静态扫描、参考研究、策略实验 | 文件 hash、清单、测试日志；本轮已交付，宿主验证未完成 |
| M1 Fork 隔离 | main/metadata/product/import_lifecycle/路由/存储根/构建脚本 | 独立安装、停用、重载 10 次无残留、私群聊原链通过；如要双插件同驻另验 |
| M2 瘦身 | capability registry、配置、后台、可选依赖、历史 shim | 默认关闭模块无网络/硬件/后台调用，备份恢复及旧库升级通过 |
| M3 Dashboard | 规范源、spec、API 对照、简易模式、目标配方 | 常用配置可三步内找到，差异确认、权限、冲突、低配置加载正常 |
| M4 生活政策 | digital_life 纯逻辑、角色状态、原 life coordinator 适配 | shadow mode 与旧策略并跑但不执行新动作；重启/睡眠/预算/打断测试通过 |
| M5 WORLD + B 站 | 只读 adapter、能力注册、activity receipt | 真实观察可核对；失败/超时/权限撤销无虚假完成，无自动发布 |
| M6 记忆/兴趣 | ingestion、interest reducer、反思/日记来源、迁移 | 回执幂等、跨窗口同角色一致、私聊不泄漏、兴趣演化可解释 |
| M7 长期验证 | 7/30 天仿真、真机小流量、对照实验 | 成本、合理性、生活多样性和稳定性指标达到约定阈值 |
| M8 v1.0 | 文档、测试、发布包、迁移/回滚、安装指南 | 实际 AstrBot 实例运行、功能与 UI 验收；源码+对应许可证完整交付 |

每阶段提交只做一类改变，附影响面、测试命令、实际结果与回滚。禁止通过删除失败测试来获得绿色。新旧决策双模式以 feature flag 隔离；shadow 策略只写诊断不触发外部动作，逐步替换。生产迁移必须先在复制库验证、停旧版、备份、独立新根恢复，失败回旧二进制+旧备份（不让旧二进制打开已升级的唯一数据）。

## 9. 验证计划与本轮实测

实测使用容器 Python 3.13（不代表实际 AstrBot 环境）。
- `python -m pytest -q tests/test_sleep_physiology.py tests/test_exploration_goals.py` → 13 passed in 0.43s。
- mood 单测限时 8s，退出 124；日志有 9 个通过进度点但无最终汇总，不能判为通过。
- 合并 sleep/mood/exploration/life runtime 限时 18s，未得到最终汇总；不据此推断缺陷或成功。
- `cd prototype && python -m pytest -q` → 13 passed in 0.05s。未联网、未安装 SDK、未执行外部项目测试。
- 未跑全部 882 个 Python 测试文件；未测真实浏览、迁移、前端、权限及负载。测试基线需在隔离 AstrBot Python 下依照 README 的 `tools/build_v2_release.py`、`tools/verify_v2_release.py` 及实际命令帮助执行；不可把旧版本通过记录代替新包。

建议自动化实验（相同人格、事件、预算、时区和随机种子，先用 mock world）：A 纯 LLM，B 固定日程，C 需求驱动，D + 生理软约束，E + 世界回执，F + 行动惯性/兴趣。多种 seed 每组至少 10 次模拟 7/30 天；仅在真实环境做少量授权只读测试。指标：无授权外部操作=0；虚假完成=0；重复 operation_id 无重复经历；暂停/重载无重复执行；记录所有夜间非承诺工作原因而不要求夜间工作绝对为零；活动类型熵、日内/日际变化、重复率、活动中断频率、兴趣证据覆盖、用户主动联系量、模型成本与预算超限次数。初始软指标通过基线对照确定，不凭空设定人类标准；硬边界任何一次失败都阻止发布。

关键场景：凌晨疲劳应更倾向睡眠；精力充沛且有灵感允许创作；明确夜班可工作；B站缺少字幕仍可跳过且记忆不声称看完；同一活动重启暂停而非完成；预算耗尽不继续调用；权限撤回后不继续浏览；同一角色多窗口只运行一次；DST/跨午夜/关机一周不虚构生活；用户静默一个月不强迫互动，恢复后可追溯真实经历。

## 10. 下一阶段所需材料（不阻塞当前成果）

优先提供大肥鱼现有 B 站工具/插件代码或接口文档、实际 AstrBot 版本及安装方式、是否需要迁移原陪伴插件的已有数据、希望保留的特殊功能。只需要脱敏源码或测试配置，不要上传 cookie、token、真实私人聊天。没有这些也可以继续纯代码开发和模拟测试，但不能完成真实宿主验收或保证兼容实际账号。完整成品需要逐阶段实施；当前交付是可追踪的 M0 基线，不是可安装的 v1.0。
