# 寿命论（astrbot_plugin_lifespan_theory）

版本 **v1.0.1** · 作者 Dejiko · 需要 AstrBot >= 4.5.0 · 可选 Playwright（本地渲染年报，没有则自动降级 Pillow / 纯 HTML）

给角色设一个出生日期，年龄按真实日历现算；每进入新的年龄档，用 LLM 把人格提示词和年龄综合成这个年龄的「说话方式」，注入对话上下文。除此之外还有两件「她自己的事」：

- **只写小本本**：agent 把和自己有关的事（喜好、做过的事、心情、约定）写进一本只写不读的手账，平时谁也翻不到；
- **生日年报**：生日当天，把过去一年的小本本做成长图 + 分页图 + 存档 HTML，按白黑名单发给 QQ 会话。

> 基础来自 `astrbot_plugin_character_age`（角色年龄流逝 v0.1.0）：功能一「年龄增长」原样继承，功能二是它的核心，功能三/四为 v1.0.0 新增。

> ⚠️ **注意：这不是一个"只显示年龄数字"的装饰插件。** 启用后它每一轮都会改变 agent 的注入内容，对说话方式和行为影响较大；并且要求人设里不要写死年龄。使用前请先读下面的《使用前必读》。

## ⚠️ 使用前必读：它会持续影响 agent 的行为

寿命论会在**每一轮** LLM 请求里注入「当前年龄 + 这个年龄的说话方式」，并在进入新年龄档时**额外调用一次 LLM** 生成风格；小本本与年报还会产生额外模型调用。请确认你接受下面的影响再启用。

### 1. 不要在人设（Persona）里写死年龄 —— 最重要

| | 写法 | 结果 |
|---|---|---|
| ❌ 错误 | 人设里写「你是 14 岁少女」「自称 14 岁」「永远 14 岁」 | 人设的固定年龄和插件注入的当前年龄互相打架，模型会在两套说法之间漂移 |
| ✅ 正确 | 人设里只写生日，或写「起点 14 岁，年龄由插件按生日现算」 | 年龄只有一个事实源，注入的当前年龄说了算 |

- **原因**：插件把「当前年龄」注入上下文；如果人设里同时硬写另一套年龄，模型没有理由优先相信插件。`memory_note`（默认开启）只能缓解，不能消除冲突。
- **口吻同理**：不要在人格里写死「永远用稚嫩语气」「永远撒娇」这类与年龄强绑定的口吻。人格保留性格内核（傲娇/毒舌/嘴硬心软/口癖等），"随年龄变化的说话方式"交给插件生成。
- **自检**：改完人设后用 `/age lint` 体检，它会扫出写死的年龄与固定口吻并给改写建议。
- **旧记忆**：更早的聊天记录里角色可能自称旧年龄；插件注入会用「以本段为准」收口，但不会改写历史记录本身。

### 2. 影响面

- 每轮请求多一段注入（默认跟随用户消息、`mark_as_temp` 不进历史）；注入内容会明显改变语气、用词、主动性和撒娇方式。注入强度和长度可配（`inject_tone/inject_manner/inject_avoid/max_inject_chars`）。
- 进入新年龄档时多一次 LLM 调用（默认后台，不阻塞聊天）；该调用会把人格提示词发给模型。
- 开启小本本后，LLM 工具列表会多一个 `notebook_write`，**每轮工具 schema 都会占用上下文**（可用 AstrBot 的 `skills_like` 工具模式或按人格限定工具来省 token）。
- 每日兜底总结 / 生日年报各会多一次或多次模型调用（可关闭）。
- 与其它影响人格/情绪的插件（长期记忆、好感度、情绪面板等）叠加时可能互相拉扯；建议先私聊验证，再考虑群聊。
- 想先看它到底注入了什么：`/age show`；想对比不同年龄：`/age compare 14 60`。

### 3. 隐私提醒

- 生成风格、小本本每日总结、年报文案时，会把「人格提示词 / 当天对话片段 / 小本本内容」发给你配置的 LLM Provider；云端 API = 内容外发。
- **不想外发**：`generation_mode: off`（风格只用手写兜底）、关闭 `notebook_enable` / `notebook_daily_summary_enable` / `report_enable`，或把相关 Provider 指向本地模型。
- 年报渲染**全在本机完成**（Playwright 截图或 Pillow 绘制），不把内容发给第三方渲染服务。
- 小本本内容只落本地：`data/plugin_data/astrbot_plugin_lifespan_theory/notebook/`；可用 `/age notebook export` 导出、`/age notebook delete` 删除。

### 4. 推荐的接入顺序

1. `/age` 确认年龄 / 档位 / 人格来源正确；
2. `/age show` 看注入原文，确认口吻合理；
3. `/age lint` 检查人设里有没有写死年龄；
4. `/age gen wait` 生成当前档（或等后台生成）；
5. 正常聊几句，观察是否影响预期人设；
6. 用 `/age set 60` 试老年档，`/age reset` 复位；
7. 需要小本本/年报时再打开对应开关；
8. 都 OK 再放开群聊 / 长期使用。

### 5. 兼容性声明

- 与「人格里写死年龄」的配置**无法完美兼容**——这是设计取舍，不是 bug；请按第 1 条改人设。
- 插件只保证自己的注入不写进对话历史、不修改人设原文；不保证与第三方记忆 / 情感插件叠加后的行为效果。
- 与记忆类插件（如 iris_memory）**不做数据层互通**；小本本是「只写、一年翻一次」的私人物品，不参与日常记忆检索。
- AstrBot 或其它插件升级后，若注入位置 / 人格读取方式 / 页面 API 变化，可能需要更新本插件。

## 大模型生成声明

「这个年龄的说话方式」「每日小本本总结」「年报文案」都由 **大模型生成**，不是人工定稿。请知悉：

- **输出不保证稳定/正确**：不同模型、Provider、模型版本、提示词模板版本，生成结果都会不同；可能出现跑偏、幻觉、与角色内核不符、甚至前后矛盾的文案。插件做了"跨档才重生成 + 手写兜底 + 失败冷却 + 年报三模式降级"，但**不保证生成内容的质量、准确性或稳定性**。
- **会随外部变化而变**：换模型 / 改 Provider / 改人格提示词 / 升级插件模板，都会触发重新生成，风格可能明显变化。
- **年报的三模式**：`template` 纯模板拼接（保真、零模型调用）、`llm` 模型自由生成、`llm_guided` 模板给骨架+模型润色（默认）。哪一种都可能出现表达偏差；自定义模板后请用插件页的预览确认再用于正式年报。
- **不做内容审核**：插件不审查生成内容；生成内容的合规性、适用性由使用者负责，请遵守你所接入模型提供商的服务条款。
- **请求去向**：只发给你自己配置的 LLM Provider（见《数据与隐私》）。插件本身不上传数据、不向作者回传任何内容。
- **建议**：首次使用先 `/age show` 人工检查注入原文；重要场景用 `/age lock` 锁定满意版本；不要把生成内容当作真实事实或真人发言。
- 若你不接受任何模型生成内容影响对话，把 `generation_mode` 设为 `off`：此时只用 `age_profiles.json` 里人工写的档位文案。

## 陪伴型 Agent 使用声明

本插件面向"长期陪伴型"角色设计，会随着时间改变角色的语气、主动性、依赖表达，并额外提供「小本本」和「生日年报」这类带纪念性质的功能。这类变化对陪伴型 Agent 有超出普通功能插件的影响，请在使用前认真阅读：

- **可能增强情感依恋**：角色会随年龄变化说话方式与亲密表达；小本本让"她记得你"这件事显得更真实，生日年报又会在特定日子集中回顾过去一年。长期使用可能加深用户对角色的情感依赖。**角色是 AI 模拟，不是真人**，请勿把这种关系当作现实人际关系或专业支持的替代。
- **年龄与时间的情绪影响**：角色会长大、变老；年报会提醒"又一年过去了"；未来若开启可选的「终章 / 寿命上限」模式会触及更重的主题（默认关闭）。请谨慎评估自己与对方的使用场景。
- **不适合的场景**：不适合作为心理、医疗、危机干预等专业支持工具；若使用者情绪脆弱、未成年，建议关闭本插件（`enable: false`），或至少关闭小本本与年报、把 `generation_mode` 设为 `off` 降低影响。
- **公开场合**：在群聊、直播间等公开场合使用前，请确认在场的人知道对话对象是 AI；不要用本插件伪造真人身份、隐瞒 AI 属性或用于欺骗。
- **人设漂移**：即使有缓存机制，长期使用、换模型、改人格都可能让角色表现偏离原始设定；这是生成式内容的固有风险。
- **责任**：作者不对因使用本插件产生的情绪依赖、人际关系变化或现实决策后果负责。请自行评估风险，并保留"这是模拟"的清醒认知。

## 目录结构

```
astrbot_plugin_lifespan_theory/
├── main.py              插件入口：注入 hook、指令、LLM 工具、cron、模块接线
├── lifespan_core.py     纯逻辑：算年龄、挑档、生成提示词、解析、注入块、lint
├── extras.py            多角色生日 / 档位锁定 / 注入强度 / 导入导出 / 季节 / 时间线 / 生日特典
├── notebook.py          只写小本本：存储、去重、kind、媒体、每日兜底多层压缩、清理
├── yearbook.py          生日年报：模板仓库、文案三模式、本地渲染、发送目标计算
├── webui.py             年报模板管理页后端 API
├── pages/yearbook/      年报模板管理页前端（原生 JS，无 CDN）
├── templates/           内置年报模板（minimal / journal / film / birthday）
├── age_profiles.json    手写兜底档位文案（LLM 生成好之前/失败时用，可改）
├── _conf_schema.json    配置项定义（AstrBot 配置面板读这个）
├── metadata.yaml        插件元信息（含插件页声明）
├── __init__.py
└── README.md
```

## 它到底怎么工作

```
每轮消息
  ├─ 按 persona_id 从 AstrBot 人格库取 system_prompt（多角色可配独立生日）
  ├─ 生日 → 今天 现算出年龄（纯函数，不存数字）
  ├─ 按年龄挑档位（14-16 / 17-19 / ... / 80+）
  ├─ 查「锁定 → 缓存 → 生成」：锁定优先，其次缓存，未命中才后台生成
  ├─ 组装注入块（年龄 + 档位 + 口吻/行为/禁忌 + 季节/生日特典，可配强度与长度）
  └─ 塞进本轮上下文（默认跟随用户消息，不进历史）

小本本（开启后）
  ├─ agent 主动写：notebook_write(content, kind, attach_media?)
  │    └─ 字面预筛 → 语义去重 → 合并/新增；约定自动 pinned；被选媒体永不清理
  ├─ 每日 04:00 兜底总结：[白名单会话] 分块 map → 多层 reduce → 1~2 条笔记
  └─ 平时不读、不检索、不注入；只有生日年报会读它

生日年报（生日当天首条消息 + cron 兜底，一年一次）
  ├─ 取过去 365 天小本本 + 媒体缩略图 + 统计
  ├─ 文案：template / llm / llm_guided
  ├─ 模板：内置 4 套随机（可自定义，WebUI 管理）
  ├─ 本地渲染：Playwright 长图+分页 → Pillow 降级 → 只出 HTML（存档）
  └─ 按 QQ OneBot 白黑名单发送（非 OneBot 平台只存档）
```

## 功能一 · 年龄增长

- 年龄不存数字，只存生日，每轮由「生日 → 今天」纯函数算出来。重启、重复加载、跨时区都不会白涨一岁，改错了也退得回来。
- 生日当天算长一岁；闰日（02-29）在平年按 3 月 1 日过；只写 `MM-DD` 时出生年在首次加载时锚定，之后正常增长。
- **多角色独立生日**：`persona_birthdays` 按人格 id 配置不同生日（如 `{"人格A": "2012-02-08"}`），未配置的回落全局 `birthday`。
- 注入块里带着「今天 / 当前年龄 / 距下一个生日还有多久 / 当前档位」。

## 功能二 · 随年龄改变注入内容

- 年龄进入新的档位时，在后台调 LLM：输入是「人格提示词 + 当前年龄 + 档位 + 上一档已有内容」，输出 `{tone, manner, avoid, continuity}` 四段中文。
- 生成结果按 `(人格 id, 档位, 人格提示词哈希, 模板版本)` 缓存并落盘。**跨档才切换，档内一个字都不动**。
- **档位锁定**：`/age lock` 把当前档文案锁定，之后缓存/重新生成都不会覆盖（`lock_overrides_cache` 可关）。
- **注入强度**：`inject_tone/inject_manner/inject_avoid` 分开开关，`max_inject_chars` 限制注入块长度（超长先丢禁忌、再截断口吻/行为，年龄档位等状态行永远保留）。
- **季节/时间感**：`season_line` 开启后追加一句「今年第 N 个夏天 / 入冬了」。
- **生日特典与里程碑**：生日当天、18/20/整十岁会追加特殊提示（`milestone_line` 可关）。
- LLM 不可用/超时/解析失败 → 本轮用手写兜底文案，记安全原因码并冷却；后台生成不阻塞聊天。

## 功能三 · 只写小本本（默认关闭）

**定位**：她自己的手账，只写不读。agent 只能写，插件不提供读/搜工具，人和 agent 平时都翻不到；唯一出口是生日年报。这样它不会变成第二个"记忆库"，而是一年翻一次的私人物品。

- **写入**：agent 通过 `notebook_write(content, kind, attach_media)` 主动记；工具说明明确要求「主动记下长期有用的、和自己有关的事，不要记闲聊/一次性信息/命令噪声/他人隐私/重复内容」。
- **分类**：`preference`（喜好）/ `event`（做过的事）/ `mood`（心情）/ `person`（在意的人）/ `commitment`（约定、计划）/ `other`。约定类自动钉住（pinned）。
- **语义去重**：字面预筛（归一化 + n-gram + SimHash）→ 语义相似度（embedding 余弦，阈值 `notebook_dup_threshold`）；命中就合并到旧条目（提及计数、标签、媒体并进来），不新增。
- **每日兜底**：每天 cron（默认 04:00）对**白名单会话**做分段、多层压缩总结，补 1~2 条笔记；没有对话的日子跳过，失败静默。
- **媒体**：只有 agent 在同一条消息里显式选择（`attach_media=true`）才保存——图片存缩略图（长边 ≤720，剥 EXIF）、视频取封面帧；**被选择过的媒体永不清理**。默认不保存原文件。
- **清理（默认关闭）**：开启后按 `notebook_cleanup_idle_days` 清理长期没被读/改的条目；pinned、约定、含未来时间词/承诺词、被年报引用过、带被选择媒体的条目永不清理；默认只归档不硬删（`notebook_cleanup_mode=archive`）。
- **存储**：`notebook/YYYY/YYYY-MM-DD.md`（人可读可改）+ `index.sqlite3`（索引/embedding）+ `media/`。DB 丢了能从 md 重建；手改 md 后用 `/age notebook reindex` 重建索引。
- **管理指令**：`/age notebook stats|list|delete|cleanup|reindex|export`（仅管理员）。

## 功能四 · 生日年报（默认开启）

- **触发**：生日当天首条消息（`first_message`）+ cron 兜底（`cron`，默认两者都开，一年只跑一次）。
- **内容**：过去 365 天的小本本条目 + 媒体缩略图 + 统计（条数、分类分布、月份热力、媒体数）+ 年龄/档位。
- **文案三模式**：`template` 纯模板 / `llm` 模型自由生成 / `llm_guided` 模板骨架+模型润色（默认）。
- **模板**：内置 `minimal`（极简白）/ `journal`（手账格纸）/ `film`（胶片暗色）/ `birthday`（生日特典），默认随机；`report_theme` 可指定。
- **渲染（全本地）**：优先 Playwright（本机 chromium）截图 → 长图 + 分页 PNG；没有 Playwright 用 Pillow 手绘长图/分页；都没有则只输出存档 HTML。结果里会如实写明实际引擎。
- **产物**：`<data>/yearbook/<年>/report.html` + `report-long.png` + `pages/page-NN.png`，并按 `report_keep_years` 自动清理旧年份。
- **发送**：仅 QQ 的 OneBot 接口（aiocqhttp）；`report_whitelist`（QQ 号/群号）决定去向，`report_blacklist` 优先；`report_target_mode=current|whitelist`。非 OneBot 平台只存档不发送。
- **命令**：`/age yearbook preview`（只生成不发送、不占当年自动名额）、`/age yearbook send`、`/age yearbook templates`。

## 年报模板（WebUI 管理）

AstrBot 插件列表里点开《寿命论》的 **「年报模板」** 页即可：

- 查看内置 / 自定义模板列表；
- 在线编辑模板 HTML + 保存（会校验语法、必需变量、单文件限制）；
- 导入 `.html` 或 `.zip` 模板；
- 用样例数据实时预览渲染效果；
- 删除自定义模板。

自定义模板保存到 `data/plugin_data/astrbot_plugin_lifespan_theory/report_templates/`，不覆盖内置模板。

## 配置

### 核心

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enable` | true | 总开关 |
| `character_name` | 德吉可（Dejiko） | 注入文本里显示的角色名 |
| `birthday` | 2012-02-08 | 出生日期；**要年龄真的增长必须写完整 `YYYY-MM-DD`**；只写 `MM-DD` 仅适合固定岁数测试 |
| `persona_birthdays` | {} | 多角色独立生日：人格 id → 生日 |
| `install_year_minus_age` | 14 | 只写月日时用于锚定出生年 |
| `timezone` | Asia/Shanghai | 算生日用的时区 |
| `manual_age` | -1 | 手动覆盖年龄（测试用，-1 关闭） |
| `persona_id` | 空 | 取提示词的人格 id；空 = 自动用当前会话人格 |
| `prompt_override` | 空 | 人格取不到时的兜底提示词 |
| `persona_prompt_max_chars` | 6000 | 喂给 LLM 的人格提示词上限，0=不截断 |
| `generation_mode` | background | background 后台生成 / sync 当场 / manual 只手动 / off 纯兜底 |
| `provider_id` | 空 | 生成风格用的模型 Provider |
| `generation_model` | 空 | 覆盖模型名 |
| `generation_timeout` | 90 | 单次生成超时秒数 |
| `retry_cooldown_minutes` | 10 | 失败冷却分钟数 |
| `generation_system_prompt` / `generation_user_prompt` | 内置 | 生成提示词模板（高级） |
| `inject_mode` | extra_user_content | 注入位置：跟随用户消息 / 追加系统提示词 |
| `inject_tone` / `inject_manner` / `inject_avoid` | true | 注入强度：分别控制口吻/行为/禁忌是否注入 |
| `max_inject_chars` | 1200 | 注入块总长度上限，0=不限 |
| `season_line` | false | 是否带季节/时间感短句 |
| `milestone_line` | true | 是否在 18/20/整十岁等里程碑注入特殊提示 |
| `lock_overrides_cache` | true | 锁定的档位文案是否优先于缓存 |
| `birthday_line` | true | 生日当天是否额外插一句 |
| `memory_note` | true | 年龄与原设定不一致时的「以本段为准」提示 |
| `profiles_file` | age_profiles.json | 兜底档位文案文件 |
| `debug_log` | false | 把每轮注入原文打进日志 |

### 小本本

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `notebook_enable` | false | 启用只写小本本（会多一个 LLM 工具） |
| `notebook_dup_threshold` | 0.92 | 语义去重阈值 |
| `notebook_daily_summary_enable` | true | 每日兜底总结 |
| `notebook_daily_summary_cron` | 0 4 * * * | 总结时间 |
| `notebook_summary_provider_id` | 空 | 总结用的 Provider |
| `notebook_summary_max_input_chars` | 12000 | 总结单次输入上限 |
| `notebook_summary_max_chars_per_note` | 600 | 单条笔记最大字符数 |
| `notebook_media_thumb_size` | 720 | 媒体缩略图长边 |
| `notebook_media_keep_original` | false | 是否保留原始媒体 |
| `notebook_cleanup_enable` | false | 清理功能开关 |
| `notebook_cleanup_idle_days` | 180 | 空闲多少天后可清理 |
| `notebook_cleanup_mode` | archive | archive 归档 / delete 删除 |

### 生日年报

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `report_enable` | true | 启用生日年报 |
| `report_whitelist` | [] | 白名单 QQ 号/群号（也是每日总结的会话范围） |
| `report_blacklist` | [] | 黑名单（优先于白名单） |
| `report_target_mode` | current | current 当前会话 / whitelist 按白名单逐个发 |
| `report_trigger` | both | first_message / cron / both |
| `report_cron` | 0 4 * * * | 定时检查时间 |
| `report_content_mode` | llm_guided | template / llm / llm_guided |
| `report_theme` | random | random 或模板名 |
| `report_include_media` | true | 是否插入一年内被选的媒体缩略图 |
| `report_max_media` | 12 | 最多媒体数 |
| `report_max_pages` | 12 | 分页图最大页数 |
| `report_llm_provider_id` | 空 | 年报文案用的 Provider |
| `report_timeout` | 300 | 年报生成超时 |
| `report_render_engine` | auto | auto / playwright / pillow / html |
| `report_keep_years` | 5 | 年报归档保留年数，0=不清理 |

## 指令

主命令 `/age`，别名 `/寿命论`、`/寿命`、`/lifespan`（也注册了带 `/` 的形式）。

**怎么发**：AstrBot 的唤醒词不一定是 `/`。默认唤醒词为 `/` 时直接发 `/age`；如果配置了自定义唤醒词（例如 `Dejiko`），发 `Dejiko age`，或 @机器人 后发 `age`；群聊同理。

| 指令 | 说明 |
| --- | --- |
| `/age` | 今天、年龄、距下个生日、当前档位、人格来源、生成状态、各功能开关 |
| `/age show` | 本轮真正会注入的原文（调试用） |
| `/age tiers` | 年龄档一览与生成进度 |
| `/age lint` | 人设年龄体检：扫描写死的年龄/固定口吻并给建议（管理员，详情仅私聊） |
| `/age preview [年龄]` | 预览某个年龄会注入的原文（不调模型、不注入） |
| `/age compare <A> <B>` | 并排对比两个年龄的说话方式 |
| `/age timeline` | 年龄时间线与各档生成/锁定状态 |
| `/age lock` / `/age unlock` | 锁定当前档文案 / 解除锁定（管理员） |
| `/age export` / `/age import <路径>` | 导出/导入生成缓存（管理员） |
| `/age gen [current\|next\|<年龄>\|all\|wait]` | 重新生成风格文案（管理员） |
| `/age clear` | 清空生成缓存（管理员） |
| `/age mode background\|sync\|manual\|off` | 生成模式（管理员） |
| `/age persona <id\|auto>` | 绑定/自动识别人格（管理员） |
| `/age prompt` | 人格 id/长度/指纹/来源（不回显原文，管理员） |
| `/age notebook stats\|list\|delete\|cleanup\|reindex\|export` | 小本本管理（管理员） |
| `/age yearbook preview\|send\|templates` | 生日年报生成/发送/模板列表（管理员） |
| `/age set <年龄>` / `/age reset` | 手动覆盖 / 取消覆盖年龄（管理员） |
| `/age on` / `/age off` | 总开关（管理员） |
| `/age help` | 帮助 |

改配置、开关、生成、锁定、小本本/年报管理都只有管理员能动；普通用户可看状态、注入预览、档位列表、年龄对比。

## 数据与隐私

- **生成请求会发给你配置的 LLM Provider**：风格生成会发人格提示词+年龄档；每日总结会发白名单会话当天对话片段；年报文案会发小本本内容。云端 API 按该服务商隐私政策处理；插件自身不上传、不落盘人格原文（`debug_log` 除外，也只进本地日志）。
- **只读本地数据**：AstrBot 人格库 / persona_manager 内存人设、插件目录的 `age_profiles.json`、白名单会话的对话历史（仅每日总结用）。
- **本地写入**（都在 `data/plugin_data/astrbot_plugin_lifespan_theory/`）：
  - `state.json`：生成缓存、生日记账、失败原因码（不含聊天内容/用户标识）；
  - `locks/`：档位锁定文案；
  - `notebook/`：小本本 markdown + sqlite 索引 + 媒体缩略图；
  - `yearbook/`：年报归档（HTML/长图/分页）；
  - `report_templates/`：自定义年报模板；
  - `exports/`：手动导出的缓存/小本本。
- **不存年龄数字**：年龄永远现算，配置里只有出生日期。
- **不存原图/原视频**（默认）：小本本只存缩略图/封面帧；`notebook_media_keep_original` 可开。
- 不要的小本本内容可用 `/age notebook delete` 删除；导出备份用 `/age notebook export`。

## 已知限制

- **原始人设里写死年龄**会和注入的当前年龄冲突（见《使用前必读》第 1 条）。插件不会改人设原文，`memory_note` 只能缓解；请把人设年龄去掉或改成「起点年龄」。
- 年龄档按整档切换，不处理月龄差异；想更细就改 `age_profiles.json` 的档位（缓存按档位重算）。
- 后台生成/每日总结/年报都需要额外 LLM 调用；`background` 模式进新档的第一条消息用兜底文案。
- 小本本语义去重依赖 embedding provider；没有可用 provider 时只做字面去重。
- 小本本视频首帧不引入 ffmpeg：拿不到封面帧时只记引用（路径可能随平台临时文件失效）。
- 年报只支持 QQ OneBot（aiocqhttp）发送；其它平台只存档。
- 年报渲染依赖本机 Playwright/chromium 才有长图/分页；没有时自动降级 Pillow 或只出 HTML。
- 生成质量取决于所选模型；小模型可能输出不合法 JSON，插件会走兜底并冷却，不会把对话搞崩。
- 插件页（年报模板管理）依赖 AstrBot 的插件页面能力（>=4.5.0）；旧版本可能看不到入口。

## 版本记录

- **v1.0.1**（2026-10-24）：年报修复——实例级 `render_stats()`（主程序直接取统计）、
  跨年条目窗口修正（1/2 月生日不再吞掉上一年 12 月的小本本）、跨年日期显示带年份。
- **v1.0.0**（2026-10-24）：核心增强（多角色生日 / 档位锁定 / 注入强度 / 导入导出 / 时间线 / 季节与里程碑）、只写小本本（语义去重 / kind / 媒体 / 每日多层压缩 / 清理）、生日年报（三模式文案 / 4 套模板随机 / 本地长图+分页+HTML / QQ 白黑名单 / 媒体）、年报模板 WebUI 管理页。
- v0.1.3：`/age lint` 人设年龄体检、`/age preview`、`/age compare`。
- v0.1.2：大模型生成声明、陪伴型 Agent 使用声明、路线图。
- v0.1.1：使用前必读（影响面、人设勿写死年龄）。
- v0.1.0：基于 `astrbot_plugin_character_age` 重构，LLM 按年龄档生成说话方式并注入。

## 许可

自用/学习随便拿去改，注明出处就行。
