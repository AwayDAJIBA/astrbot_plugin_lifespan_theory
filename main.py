# -*- coding: utf-8 -*-
"""寿命论 · AstrBot 插件入口。

功能一：年龄增长（继承 astrbot_plugin_character_age 的口径）
    年龄不存数字，只存「出生日期」，每轮由 生日 → 今天 现算。纯函数，
    重启、重复加载、跨时区都不会白涨一岁，也退得回来。

功能二：随年龄改变注入内容
    年龄进入新的年龄档时，后台调一次 LLM，把「AstrBot 人格库里的系统提示词 + 当前年龄 + 档位」
    综合成一段「这个年龄的她的说话方式」，缓存并落盘，之后每轮注入同一份。
    跨档才换、档内一个字都不动——这样人设不会随对话一天一个说法地漂移。
    生成好之前用手写档位文案兜底；LLM 失败有冷却，绝不会把对话拖死。

设计要点（都是踩过坑才这么写的）：
1. 年龄是现算的纯函数，不在状态文件里累加。
2. 生成内容是「按 (人格, 档位, 人格提示词哈希, 模板版本) 缓存」的，人格改了/模板升级了才会重生成。
3. 注入走 extra_user_content_parts（跟随本轮消息，不进缓存），或者按配置追加到 system_prompt 末尾。
   原始人设一个字都不碰。
4. 后台生成不阻塞聊天：本轮先用兜底，生成好了下一轮起生效。
5. 旧记忆里的年龄跟当前年龄打架时，注入里加一句「以本段为准」。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from . import lifespan_core as core
from .lifespan_core import (
    BLOCK_MARK,
    DEFAULT_GENERATION_SYSTEM,
    DEFAULT_GENERATION_USER,
    TEMPLATE_VERSION,
    build_block,
    build_generation_messages,
    cache_key,
    compute_age,
    days_to_next_birthday,
    is_birthday,
    is_month_day_only,
    load_profiles,
    next_birthday,
    parse_birthday,
    parse_generation_reply,
    pick_tier,
    prompt_hash,
    tier_key,
    tier_span_text,
)

# ---- v1.0.0 可选模块：缺任一模块时插件仍能加载（便于分阶段开发/裁剪发布）----
try:
    from . import extras as extras_mod
except Exception:  # noqa: BLE001
    extras_mod = None
try:
    from .notebook import Notebook as _Notebook
except Exception:  # noqa: BLE001
    _Notebook = None
try:
    from .yearbook import TemplateStore as _TemplateStore
    from .yearbook import YearbookBuilder as _YearbookBuilder
except Exception:  # noqa: BLE001
    _TemplateStore = None
    _YearbookBuilder = None
try:
    from . import webui as webui_mod
except Exception:  # noqa: BLE001
    webui_mod = None

PLUGIN_NAME = "astrbot_plugin_lifespan_theory"
STATE_FILE = "state.json"
INJECT_FLAG = "lifespan_theory_injected"
COMMAND = "age"
# 同时注册带 "/" 的别名：AstrBot 的唤醒前缀不一定是 "/"（本机是 DEJIKO/Dejiko 等），
# 这时 "/age" 不会被当作唤醒词剥掉，裸名 "age" 匹配不到；带上 "/" 别名后
# 默认前缀实例和自定义唤醒词实例两种发送方式都能用。
COMMAND_ALIAS = {
    "寿命论",
    "寿命",
    "lifespan",
    "lifespan_theory",
    "/age",
    "/寿命论",
    "/寿命",
    "/lifespan",
    "/lifespan_theory",
}
PROMPT_CACHE_TTL = 120.0
"""人格提示词内存缓存秒数：人格被编辑后最多 2 分钟生效，避免每轮都查库。"""
MAX_LAST_BLOCKS = 32
MAX_RAW_CACHE_CHARS = 8000
MAX_CACHE_ENTRIES = 200
GENERATION_MODES = ("background", "sync", "manual", "off")
_SAFE_FAILURE_REASON_RE = re.compile(
    r"(?:timeout|parse_failed|too_short|not_chinese|llm_error:[A-Za-z_][A-Za-z0-9_.]{0,40})"
)
"""[S9] state.json 里 failures[].error 只允许这些安全原因码：
provider 的报错正文可能回显请求片段，而请求里含人格提示词，不能原样落盘。"""


@register(
    PLUGIN_NAME,
    "Dejiko",
    "按生日现算角色年龄；年龄进入新档时用 LLM 把人格提示词与年龄综合成说话方式注入上下文，让人设随年龄自然生长。",
    "1.0.0",
)
class LifespanTheoryPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._profiles_cache: dict | None = None
        self._profiles_mtime: float = 0.0
        self._state: dict | None = None
        self._tasks: set[asyncio.Task] = set()
        self._inflight: set[str] = set()
        self._prompt_cache: dict[str, tuple[str, float]] = {}
        self._last_blocks: dict[str, str] = {}
        # 测试时可以把插件数据目录指到临时目录，生产环境不要动它。
        self._data_dir_override: str | Path | None = None
        self._loaded_at: float = time.time()
        self._warned_bad_timezone: bool = False
        # ---- v1.0.0 新增状态 ----
        self._lock_store = None
        self._notebook = None
        self._templates = None
        self._yearbook = None
        self._cron_job_ids: dict[str, str] = {}
        self._report_tasks: set[asyncio.Task] = set()
        self._embed_provider = None

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
        await self._init_modules()
        try:
            info = self._resolve_info()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 初始化时解析年龄失败：{exc!r}")
            info = None
        if info is not None:
            logger.info(
                f"[{PLUGIN_NAME}] 已加载：{info['today'].isoformat()} / {info['age']} 岁 / "
                f"档位 {tier_key(info['tier'])} / 生成模式 {self._cfg('generation_mode', 'background')}"
            )
        logger.info(
            f"[{PLUGIN_NAME}] v1.0.0 模块：notebook={'有' if self._notebook else '关/缺'} / "
            f"yearbook={'有' if self._yearbook else '缺'} / webui={'有' if webui_mod else '缺'} / "
            f"extras={'有' if extras_mod else '缺'}"
        )
        await self._init_tools()
        await self._init_cron()
        await self._init_webui()

    async def terminate(self) -> None:
        for task in list(self._tasks) + list(self._report_tasks):
            task.cancel()
        self._tasks.clear()
        self._report_tasks.clear()
        self._inflight.clear()
        await self._remove_cron()

    # ------------------------------------------------------------------ 小工具
    def _cfg(self, key: str, default=None):
        try:
            value = self.config.get(key, default)
        except AttributeError:
            value = default
        return default if value is None else value

    def _save_config(self) -> bool:
        save = getattr(self.config, "save_config", None)
        if not callable(save):
            return False
        try:
            save()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 写配置失败：{exc!r}")
            return False

    def _data_dir(self) -> Path:
        override = getattr(self, "_data_dir_override", None)
        if override:
            return Path(override)
        return Path(StarTools.get_data_dir(PLUGIN_NAME))

    @staticmethod
    def _umo(event) -> str:
        try:
            return str(event.unified_msg_origin or "")
        except AttributeError:
            return str(getattr(event, "session_id", "") or "")

    def _today(self) -> date:
        tz_name = str(self._cfg("timezone", "Asia/Shanghai"))
        try:
            return datetime.now(ZoneInfo(tz_name)).date()
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            if not self._warned_bad_timezone:
                self._warned_bad_timezone = True
                logger.warning(f"[{PLUGIN_NAME}] 时区 {tz_name} 不可用，退回本地时间")
            return datetime.now().date()

    def _anchor_age(self) -> int:
        try:
            return max(0, int(self._cfg("install_year_minus_age", 14) or 14))
        except (TypeError, ValueError):
            logger.warning(f"[{PLUGIN_NAME}] install_year_minus_age 不是整数，按 14 处理")
            return 14

    def _persona_birthday_raw(self, persona_id: str | None) -> str:
        """多角色独立生日：persona_birthdays[人格id] 优先，其次全局 birthday。"""
        global_raw = str(self._cfg("birthday", "2012-02-08")).strip()
        if not persona_id:
            return global_raw
        mapping = self._cfg("persona_birthdays", {}) or {}
        if isinstance(mapping, dict):
            for key, value in mapping.items():
                text = str(value or "").strip()
                if text and str(key) == str(persona_id):
                    return text
        return global_raw

    def _birth_anchor_year(
        self,
        today: date,
        anchor_age: int,
        raw: str | None = None,
        persona_id: str | None = None,
    ) -> int:
        """只写 MM-DD 时，出生年在第一次确定后落盘复用。

        多角色各写 MM-DD 时按 (persona_id|raw|anchor_age) 分别记账，否则切换人格
        会反复重算 anchor、年龄来回抖。配置变了（signature 变化）才重新锚定。
        """
        text = (
            raw if raw is not None else str(self._cfg("birthday", "2012-02-08"))
        ).strip()
        signature = f"{persona_id or ''}|{text}|{anchor_age}"
        notices = self._load_state().setdefault("notices", {})
        anchors = notices.get("birth_anchors")
        if not isinstance(anchors, dict):
            anchors = {}
            # 兼容旧版单条 birth_anchor
            legacy = notices.get("birth_anchor")
            if isinstance(legacy, dict) and legacy.get("signature"):
                anchors[str(legacy["signature"])] = legacy.get("year")
        try:
            year = int(anchors.get(signature))
            if year > 0:
                return year
        except (TypeError, ValueError):
            pass
        year = today.year - anchor_age
        anchors[signature] = year
        notices["birth_anchors"] = anchors
        self._save_state()
        return year

    def _birthday(
        self, today: date | None = None, persona_id: str | None = None
    ) -> date | None:
        today = today or self._today()
        raw = self._persona_birthday_raw(persona_id)
        anchor_age = self._anchor_age()
        anchor_year = (
            self._birth_anchor_year(today, anchor_age, raw, persona_id)
            if is_month_day_only(raw)
            else None
        )
        return parse_birthday(
            raw,
            today=today,
            anchor_age=anchor_age,
            anchor_year=anchor_year,
        )

    def _profiles(self) -> dict:
        path = Path(__file__).parent / str(self._cfg("profiles_file", "age_profiles.json"))
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if self._profiles_cache is None or mtime != self._profiles_mtime:
            self._profiles_cache = load_profiles(path)
            self._profiles_mtime = mtime
            logger.info(f"[{PLUGIN_NAME}] 已加载年龄档：{path}")
        return self._profiles_cache

    def _resolve_info(
        self, persona_id: str | None = None, today: date | None = None
    ) -> dict | None:
        """算出这一轮该用的全部信息，算不出来返回 None（配置再离谱也不许让指令/注入连坐）。"""
        try:
            today = today or self._today()
            birth = self._birthday(today, persona_id)
            if birth is None:
                logger.warning(f"[{PLUGIN_NAME}] 生日配置解析失败，本轮跳过注入")
                return None
            try:
                manual = int(self._cfg("manual_age", -1) or -1)
            except (TypeError, ValueError):
                manual = -1
            age = manual if manual >= 0 else compute_age(birth, today)
            age = max(0, age)
            profiles = self._profiles()
            try:
                base_age = int(profiles.get("base_age", 14) or 14)
            except (TypeError, ValueError):
                base_age = 14
            return {
                "birth": birth,
                "today": today,
                "age": age,
                "manual": manual >= 0,
                "tier": pick_tier(profiles, age),
                "base_age": base_age,
                "days_left": days_to_next_birthday(birth, today),
                "next_birthday": next_birthday(birth, today),
                "is_birthday": is_birthday(birth, today),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 解析年龄状态失败，本轮跳过：{exc!r}")
            return None

    # ------------------------------------------------------------------ 状态存档
    def _state_path(self) -> Path:
        return self._data_dir() / STATE_FILE

    def _load_state(self) -> dict:
        if self._state is None:
            self._state = {"version": 1, "cache": {}, "notices": {}, "failures": {}}
            try:
                raw = json.loads(self._state_path().read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = None
            if isinstance(raw, dict):
                for key in ("cache", "notices", "failures"):
                    if not isinstance(raw.get(key), dict):
                        raw[key] = {}
                raw["version"] = 1
                self._state = raw
        return self._state

    def _save_state(self) -> None:
        """先写临时文件再原子替换：进程被杀也不会留下半个 JSON。"""
        try:
            data_dir = self._data_dir()
            data_dir.mkdir(parents=True, exist_ok=True)
            path = self._state_path()
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(self._load_state(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError as exc:
            logger.warning(f"[{PLUGIN_NAME}] 状态写盘失败：{exc!r}")

    def _cache_bucket(self) -> dict:
        cache = self._load_state().setdefault("cache", {})
        return cache

    def _cached_style(self, key: str) -> dict | None:
        entry = self._cache_bucket().get(key)
        if not isinstance(entry, dict):
            return None
        style = entry.get("style")
        if isinstance(style, dict) and (
            str(style.get("tone") or "").strip() or str(style.get("manner") or "").strip()
        ):
            return style
        return None

    def _put_cache(self, key: str, entry: dict) -> None:
        cache = self._cache_bucket()
        cache[key] = entry
        if len(cache) > MAX_CACHE_ENTRIES:
            ordered = sorted(
                cache.items(),
                key=lambda kv: str((kv[1] or {}).get("generated_at") or ""),
            )
            for old_key, _ in ordered[: len(cache) - MAX_CACHE_ENTRIES]:
                cache.pop(old_key, None)
        self._save_state()

    def _entries_for(self, persona_id: str, prompt_h: str) -> dict:
        suffix = f"|{prompt_h}|{TEMPLATE_VERSION}"
        prefix = f"{(persona_id or 'unknown').strip()}|"
        out: dict = {}
        for key, entry in self._cache_bucket().items():
            if not isinstance(entry, dict):
                continue
            raw = str(entry.get("generation_id") or "")
            if raw.startswith(prefix) and raw.endswith(suffix):
                out[str(entry.get("tier") or "")] = entry
        return out

    # ------------------------------------------------------------------ 人格提示词
    def _truncate_prompt(self, text: str) -> str:
        try:
            limit = int(self._cfg("persona_prompt_max_chars", 6000) or 6000)
        except (TypeError, ValueError):
            limit = 6000
        if limit <= 0 or len(text) <= limit:
            return text
        head_len = int(limit * 0.75)
        tail_len = max(0, limit - head_len)
        head = text[:head_len].rstrip()
        tail = text[-tail_len:].lstrip() if tail_len else ""
        return f"{head}\n…（中段省略）…\n{tail}"

    async def _fetch_persona_prompt(self, persona_id: str, force: bool = False) -> str:
        if not persona_id:
            return ""
        now = time.monotonic()
        cached = self._prompt_cache.get(persona_id)
        if cached is not None and not force and now - cached[1] < PROMPT_CACHE_TTL:
            return cached[0]

        text = ""
        manager = getattr(self.context, "persona_manager", None)
        if manager is not None:
            try:
                persona = manager.get_persona_v3_by_id(persona_id)
                if isinstance(persona, dict):
                    text = str(persona.get("prompt") or "").strip()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[{PLUGIN_NAME}] 从 persona_manager 取人格失败：{exc!r}")

        if not text:
            db = None
            try:
                db = self.context.get_db()
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[{PLUGIN_NAME}] 取数据库失败：{exc!r}")
            if db is not None:
                try:
                    row = await db.get_persona_by_id(persona_id)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[{PLUGIN_NAME}] 查人格表失败：{exc!r}")
                    row = None
                if row is not None:
                    text = str(getattr(row, "system_prompt", "") or "").strip()

        if not text and persona_id == "_chatui_default_":
            # WebChat 的默认人格没有 persona_id（resolve 返回 _chatui_default_），
            # 用 AstrBot 内置的默认人格提示词当素材，功能2 才不会退化成纯兜底。
            text = self._chatui_default_prompt()

        if text:
            self._prompt_cache[persona_id] = (text, now)
        elif cached is not None:
            # 人格暂时取不到（比如编辑中），先沿用上一次的，别让注入断档。
            return cached[0]
        return text

    @staticmethod
    def _chatui_default_prompt() -> str:
        try:
            from astrbot.core.astr_main_agent_resources import (
                CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT,
            )

            return str(CHATUI_SPECIAL_DEFAULT_PERSONA_PROMPT or "").strip()
        except Exception:  # noqa: BLE001 AstrBot 内部模块变动时静默降级
            return ""

    async def _resolve_persona(
        self, event, req=None, force: bool = False
    ) -> tuple[str | None, str]:
        """返回 (persona_id, 人格提示词全文)。优先 config 指定，其次当前会话生效人格。"""
        if event is None:  # cron 场景：没有当前会话，只能按配置
            cfg_id = str(self._cfg("persona_id", "") or "").strip()
            if cfg_id:
                prompt = await self._fetch_persona_prompt(cfg_id, force=force)
                if prompt:
                    return cfg_id, prompt
            return (cfg_id or None), ""
        cfg_id = str(self._cfg("persona_id", "") or "").strip()
        candidates: list[str] = []
        if cfg_id:
            candidates.append(cfg_id)

        conv_pid = None
        conversation = getattr(req, "conversation", None) if req is not None else None
        if conversation is not None:
            conv_pid = getattr(conversation, "persona_id", None)
            if conv_pid and str(conv_pid).strip() != "[%None]":
                candidates.append(str(conv_pid).strip())

        if not cfg_id:
            manager = getattr(self.context, "persona_manager", None)
            if manager is not None:
                try:
                    platform_name = ""
                    try:
                        platform_name = str(event.get_platform_name() or "")
                    except Exception:  # noqa: BLE001
                        platform_name = ""
                    resolved = await manager.resolve_selected_persona(
                        umo=self._umo(event),
                        conversation_persona_id=(
                            str(conv_pid) if conv_pid and str(conv_pid) != "[%None]" else None
                        ),
                        platform_name=platform_name,
                        provider_settings={},
                    )
                    if isinstance(resolved, (tuple, list)) and resolved and resolved[0]:
                        candidates.append(str(resolved[0]))
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[{PLUGIN_NAME}] resolve_selected_persona 失败：{exc!r}")

        for persona_id in dict.fromkeys(candidates):
            prompt = await self._fetch_persona_prompt(persona_id, force=force)
            if prompt:
                return persona_id, prompt

        override = str(self._cfg("prompt_override", "") or "").strip()
        if override:
            return (cfg_id or (candidates[0] if candidates else None)), override
        return (candidates[0] if candidates else None), ""

    def _persona_key(self, persona_id: str | None, prompt_text: str) -> str:
        """人格提示词指纹。带上截断上限，改配置也能让缓存失效。"""
        try:
            limit = int(self._cfg("persona_prompt_max_chars", 6000) or 6000)
        except (TypeError, ValueError):
            limit = 6000
        return prompt_hash(f"{prompt_text}\u0000{limit}")

    # ------------------------------------------------------------------ 生成
    async def _resolve_provider_id(self, umo: str) -> str:
        configured = str(self._cfg("provider_id", "") or "").strip()
        if configured:
            return configured
        try:
            provider_id = await self.context.get_current_chat_provider_id(umo)
            if provider_id:
                return str(provider_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] get_current_chat_provider_id 失败：{exc!r}")
        for getter_name in ("get_using_provider_async", "get_using_provider"):
            getter = getattr(self.context, getter_name, None)
            if not callable(getter):
                continue
            try:
                provider = getter(umo=umo)
                if asyncio.iscoroutine(provider):
                    provider = await provider
            except Exception:  # noqa: BLE001
                continue
            if provider is not None:
                config = getattr(provider, "provider_config", None) or {}
                for attr in ("id", "provider_id", "name"):
                    value = config.get(attr) if isinstance(config, dict) else None
                    if value:
                        return str(value)
        return ""

    def _previous_style(
        self,
        persona_id: str | None,
        prompt_h: str,
        tier: dict,
        profiles: dict,
    ) -> tuple[dict | None, dict | None]:
        tiers = sorted(profiles.get("tiers") or [], key=lambda t: int(t.get("min", 0)))
        cur_min = int(tier.get("min", 0))
        previous = None
        for candidate in tiers:
            if int(candidate.get("min", 0)) < cur_min:
                previous = candidate
        if previous is None:
            return None, None
        cached = self._cached_style(cache_key(persona_id or "", previous, prompt_h))
        if cached:
            return cached, previous
        return {
            "tone": str(previous.get("tone") or ""),
            "manner": str(previous.get("manner") or ""),
        }, previous

    def _cooldown_active(self, key: str) -> bool:
        failures = self._load_state().get("failures", {})
        record = failures.get(key)
        if not isinstance(record, dict):
            return False
        try:
            at = datetime.fromisoformat(str(record.get("at")))
        except (TypeError, ValueError):
            return False
        try:
            minutes = max(1, int(self._cfg("retry_cooldown_minutes", 10) or 10))
        except (TypeError, ValueError):
            minutes = 10
        try:
            count = max(1, int(record.get("count", 1) or 1))
        except (TypeError, ValueError):
            count = 1
        cooldown = minutes * min(count, 6)
        return (datetime.now() - at).total_seconds() < cooldown * 60

    def _record_failure(self, key: str, error: str) -> None:
        """只记异常类型/短原因：provider 报错可能回显请求片段，而请求里含人格提示词。"""
        state = self._load_state()
        failures = state.setdefault("failures", {})
        record = failures.get(key) if isinstance(failures.get(key), dict) else {}
        try:
            count = int(record.get("count", 0) or 0) + 1
        except (TypeError, ValueError):
            count = 1
        # 白名单收口：只允许已知的安全原因码，任何意外文本一律压成 llm_error。
        reason = str(error).strip()
        if not _SAFE_FAILURE_REASON_RE.fullmatch(reason):
            reason = "llm_error"
        failures[key] = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "count": count,
            "error": reason,
        }
        self._save_state()

    def _clear_failure(self, key: str) -> None:
        failures = self._load_state().setdefault("failures", {})
        if failures.pop(key, None) is not None:
            self._save_state()

    async def _generate(
        self,
        *,
        event,
        info: dict,
        persona_id: str | None,
        persona_prompt: str,
        prompt_h: str,
        key: str,
        force: bool = False,
    ) -> dict | None:
        """调 LLM 生成当前档的说话方式，成功写缓存并返回 cache entry。"""
        failure_reason = ""
        try:
            profiles = self._profiles()
            tier = info["tier"]
            previous_style, previous_tier = self._previous_style(
                persona_id, prompt_h, tier, profiles
            )
            continuity_budget = ""
            if previous_tier is not None:
                continuity_budget = (
                    f"（她从 {tier_span_text(previous_tier)} 岁档进入 {tier_span_text(tier)} 岁档，"
                    "重点写这段年龄带来的变化。）"
                )
            system_prompt, user_prompt = build_generation_messages(
                persona_prompt=self._truncate_prompt(persona_prompt),
                persona_id=persona_id or "unknown",
                character_name=str(self._cfg("character_name", "德吉可（Dejiko）")),
                birth=info["birth"],
                today=info["today"],
                age=info["age"],
                tier=tier,
                base_age=info["base_age"],
                fallback_tier=tier,
                previous_style=previous_style,
                continuity_budget=continuity_budget,
                system_template=str(self._cfg("generation_system_prompt", "") or "")
                or DEFAULT_GENERATION_SYSTEM,
                user_template=str(self._cfg("generation_user_prompt", "") or "")
                or DEFAULT_GENERATION_USER,
            )
            provider_id = await self._resolve_provider_id(self._umo(event))
            if not provider_id:
                raise RuntimeError("找不到可用的模型 Provider（可在插件配置里指定 provider_id）")

            kwargs = {
                "chat_provider_id": provider_id,
                "prompt": user_prompt,
                "system_prompt": system_prompt,
                "contexts": [],
            }
            model = str(self._cfg("generation_model", "") or "").strip()
            if model:
                kwargs["model"] = model

            try:
                timeout = max(10, int(self._cfg("generation_timeout", 90) or 90))
            except (TypeError, ValueError):
                timeout = 90
            response = await asyncio.wait_for(
                self.context.llm_generate(**kwargs), timeout=timeout
            )
            raw_text = str(getattr(response, "completion_text", "") or "")
            style = parse_generation_reply(raw_text)
            if style is None:
                failure_reason = "parse_failed"
                raise RuntimeError(f"LLM 返回无法解析：{raw_text[:160]!r}")
            if len(style["tone"]) + len(style["manner"]) < 40:
                failure_reason = "too_short"
                raise RuntimeError(
                    f"LLM 返回过短（{len(style['tone']) + len(style['manner'])} 字）："
                    f"{raw_text[:160]!r}"
                )
            cjk = sum(
                1
                for ch in style["tone"] + style["manner"]
                if "\u4e00" <= ch <= "\u9fff"
            )
            if cjk < 30:
                failure_reason = "not_chinese"
                raise RuntimeError(f"LLM 返回不像中文风格文案（汉字 {cjk} 个）")
            abstract_hits = [
                word
                for word in ("更成熟", "更稳重", "岁月沉淀", "作为一个AI", "作为 AI", "设定上是")
                if word in raw_text
            ]
            if abstract_hits:
                logger.warning(
                    f"[{PLUGIN_NAME}] 生成内容疑似有抽象评价/破壁词 {abstract_hits}，"
                    "可对照 design/generation_prompt.md §7 的验收清单复查"
                )

            entry = {
                "persona_id": persona_id or "",
                "tier": tier_key(tier),
                "tier_label": str(tier.get("label") or ""),
                "age": info["age"],
                "prompt_hash": prompt_h,
                "template_version": TEMPLATE_VERSION,
                "provider_id": provider_id,
                "model": model,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "style": style,
                "raw": raw_text[:MAX_RAW_CACHE_CHARS],
                "generation_id": f"{(persona_id or 'unknown').strip()}|{prompt_h}|{TEMPLATE_VERSION}",
            }
            self._put_cache(key, entry)
            self._clear_failure(key)
            logger.info(
                f"[{PLUGIN_NAME}] 已生成「{persona_id or 'unknown'} · {tier_key(tier)}」"
                f"档的说话方式（{info['age']} 岁，provider={provider_id}）"
            )
            return entry
        except asyncio.TimeoutError:
            self._record_failure(key, "timeout")
            logger.warning(f"[{PLUGIN_NAME}] 生成超时（key={key[:8]}），本轮继续用兜底文案")
            return None
        except Exception as exc:  # noqa: BLE001 生成失败绝不能拖死对话
            # 只落「安全原因码」：provider 的报错正文可能回显请求片段，而请求里含人格提示词
            self._record_failure(
                key, failure_reason or f"llm_error:{type(exc).__name__}"
            )
            logger.warning(f"[{PLUGIN_NAME}] 生成失败（key={key[:8]}）：{exc!r}")
            return None
        finally:
            self._inflight.discard(key)

    def _schedule_generation(self, **kwargs) -> bool:
        key = kwargs["key"]
        if key in self._inflight:
            return False
        if not kwargs.get("force") and self._cooldown_active(key):
            logger.debug(f"[{PLUGIN_NAME}] key={key[:8]} 失败冷却中，跳过生成")
            return False
        self._inflight.add(key)
        task = asyncio.create_task(self._generate(**kwargs))
        self._tasks.add(task)

        def _done(finished: asyncio.Task) -> None:
            self._tasks.discard(finished)
            self._inflight.discard(key)

        task.add_done_callback(_done)
        return True

    async def _generate_all(
        self,
        *,
        event,
        info: dict,
        persona_id: str | None,
        persona_prompt: str,
        prompt_h: str,
    ) -> None:
        """后台串行生成当前及以后的所有档位，避免一次并发打爆模型。"""
        profiles = self._profiles()
        tiers = sorted(profiles.get("tiers") or [], key=lambda t: int(t.get("min", 0)))
        current_min = int(info["tier"].get("min", 0) or 0)
        done = failed = 0
        for tier in tiers:
            low = int(tier.get("min", 0) or 0)
            if low < current_min:
                continue
            key = cache_key(persona_id or "", tier, prompt_h)
            self._clear_failure(key)
            self._inflight.add(key)
            entry = await self._generate(
                event=event,
                info={**info, "age": max(low, int(info["age"])), "tier": tier},
                persona_id=persona_id,
                persona_prompt=persona_prompt,
                prompt_h=prompt_h,
                key=key,
                force=True,
            )
            if entry is not None:
                done += 1
            else:
                failed += 1
            await asyncio.sleep(0.5)
        logger.info(
            f"[{PLUGIN_NAME}] 批量生成结束：成功 {done} 档，失败 {failed} 档"
        )

    # ------------------------------------------------------------------ 注入
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        try:
            await self._inject(event, req)
        except Exception as exc:  # noqa: BLE001 注入失败绝不能拖死对话
            logger.warning(f"[{PLUGIN_NAME}] 注入失败，本轮放行：{exc!r}")

    async def _prepare_style(
        self,
        event,
        req,
        info: dict,
        persona_id: str | None = None,
        persona_prompt: str | None = None,
    ) -> tuple[dict | None, str, str]:
        """返回 (style, key, persona_id)。没就绪时 style=None（用兜底）。

        v1.0.0：锁定的档位文案优先于缓存（lock_overrides_cache 可关）。
        """
        if persona_prompt is None:
            persona_id, persona_prompt = await self._resolve_persona(event, req)
        if not persona_prompt:
            return None, "", persona_id or ""
        prompt_h = self._persona_key(persona_id, persona_prompt)
        key = cache_key(persona_id or "", info["tier"], prompt_h)
        if (
            extras_mod is not None
            and self._lock_store is not None
            and bool(self._cfg("lock_overrides_cache", True))
        ):
            try:
                locked = self._lock_store.get(persona_id or "", tier_key(info["tier"]))
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[{PLUGIN_NAME}] 读锁定档位失败：{exc!r}")
                locked = None
            if isinstance(locked, dict) and isinstance(locked.get("style"), dict):
                return locked["style"], key, persona_id or ""
        mode = str(self._cfg("generation_mode", "background")).strip().lower()
        if mode == "off":
            # off = 纯手写兜底：连旧缓存也不用，与 README/SPEC 保持一致
            return None, key, persona_id or ""
        style = self._cached_style(key)
        if style is not None:
            return style, key, persona_id or ""

        payload = {
            "event": event,
            "info": info,
            "persona_id": persona_id,
            "persona_prompt": persona_prompt,
            "prompt_h": prompt_h,
            "key": key,
            "force": False,
        }
        if mode == "sync":
            entry = await self._generate(**payload)
            return (entry or {}).get("style"), key, persona_id or ""
        if mode == "background":
            if self._schedule_generation(**payload):
                logger.info(
                    f"[{PLUGIN_NAME}] 进入新档 {tier_key(info['tier'])}（{info['age']} 岁），"
                    "已排队后台生成，本轮先用兜底文案"
                )
        return None, key, persona_id or ""

    def _inject_options(self) -> dict:
        if extras_mod is not None:
            try:
                options = extras_mod.resolve_injection_options(self.config)
                if isinstance(options, dict):
                    return options
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[{PLUGIN_NAME}] resolve_injection_options 失败：{exc!r}")
        try:
            max_chars = int(self._cfg("max_inject_chars", 1200) or 0)
        except (TypeError, ValueError):
            max_chars = 1200
        return {
            "tone": bool(self._cfg("inject_tone", True)),
            "manner": bool(self._cfg("inject_manner", True)),
            "avoid": bool(self._cfg("inject_avoid", True)),
            "max_chars": max(0, max_chars),
        }

    def _extra_inject_lines(self, info: dict) -> list[str]:
        """季节感 + 生日/里程碑特典短句（v1.0.0）。"""
        if extras_mod is None:
            return []
        lines: list[str] = []
        try:
            if bool(self._cfg("season_line", False)):
                lines.extend(extras_mod.season_lines(info["today"], info["birth"]))
            if bool(self._cfg("milestone_line", True)):
                special = extras_mod.birthday_special(
                    info["today"], info["birth"], info["age"], info["base_age"]
                )
                if isinstance(special, dict) and special.get("note"):
                    lines.append(str(special["note"]))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 计算额外注入行失败：{exc!r}")
        return lines

    def _render_block(self, info: dict, style: dict | None, pending: bool = False) -> str:
        return build_block(
            character_name=str(self._cfg("character_name", "德吉可（Dejiko）")),
            birth=info["birth"],
            today=info["today"],
            age=info["age"],
            tier=info["tier"],
            style=style,
            base_age=info["base_age"],
            birthday_line=bool(self._cfg("birthday_line", True)),
            memory_note=bool(self._cfg("memory_note", True)),
            manual=info["manual"],
            pending=pending,
            inject_options=self._inject_options(),
            extra_lines=self._extra_inject_lines(info),
        )

    def _remember_block(self, event, text: str) -> None:
        umo = self._umo(event)
        self._last_blocks[umo] = text
        if len(self._last_blocks) > MAX_LAST_BLOCKS:
            for stale in list(self._last_blocks)[: len(self._last_blocks) - MAX_LAST_BLOCKS]:
                self._last_blocks.pop(stale, None)

    def _append_injection(self, req, text: str) -> bool:
        mode = str(self._cfg("inject_mode", "extra_user_content")).strip().lower()
        parts = getattr(req, "extra_user_content_parts", None)
        if mode == "extra_user_content" and parts is not None:
            try:
                from astrbot.core.agent.message import TextPart

                part = TextPart(text=text)
                mark = getattr(part, "mark_as_temp", None)
                if callable(mark):
                    part = mark()
                parts.append(part)
                return True
            except Exception as exc:  # noqa: BLE001 拿不到 TextPart 就退回系统提示词
                logger.debug(f"[{PLUGIN_NAME}] TextPart 不可用，退回 system_prompt：{exc!r}")
        base = getattr(req, "system_prompt", "") or ""
        if BLOCK_MARK in base:
            return False
        req.system_prompt = (base + "\n\n" + text).strip()
        return True

    async def _inject(self, event: AstrMessageEvent, req) -> None:
        if not bool(self._cfg("enable", True)):
            return
        try:
            if event.get_extra(INJECT_FLAG):
                return
        except AttributeError:
            pass
        persona_id, persona_prompt = await self._resolve_persona(event, req)
        info = self._resolve_info(persona_id)
        if info is None:
            return

        style, key, persona_id = await self._prepare_style(
            event, req, info, persona_id, persona_prompt
        )
        pending = style is None and str(self._cfg("generation_mode", "background")).lower() == "background"
        text = self._render_block(info, style, pending=pending)
        if not text:
            return
        if self._append_injection(req, text):
            self._remember_block(event, text)
            try:
                event.set_extra(INJECT_FLAG, True)
            except AttributeError:
                pass
            if bool(self._cfg("debug_log", False)):
                logger.info(f"[{PLUGIN_NAME}] 本轮注入原文：\n{text}")
            self._notice(info, style is not None, key, persona_id)
            self._maybe_report_first_message(event, info, persona_id)


    # ================================================================== v1.0.0 装配
    def _module_data_dir(self, name: str) -> Path:
        return self._data_dir() / name

    async def _init_modules(self) -> None:
        """装配 extras / notebook / yearbook / webui；任何一块失败只降级不拖垮插件。"""
        if extras_mod is not None:
            try:
                self._lock_store = extras_mod.LockStore(self._module_data_dir("locks"))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] extras 初始化失败：{exc!r}")
        if _Notebook is not None and bool(self._cfg("notebook_enable", False)):
            try:
                self._notebook = _Notebook(
                    self._module_data_dir("notebook"),
                    self.config,
                    llm_call=self._notebook_llm_call,
                    embed_fn=self._embed_texts,
                    logger=logger,
                )
                logger.info(f"[{PLUGIN_NAME}] 小本本已启用：{self._module_data_dir('notebook')}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 小本本初始化失败：{exc!r}")
                self._notebook = None
        if _TemplateStore is not None:
            try:
                self._templates = _TemplateStore(
                    Path(__file__).parent / "templates",
                    self._module_data_dir("report_templates"),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 年报模板仓库初始化失败：{exc!r}")
                self._templates = None
        if _YearbookBuilder is not None and self._templates is not None:
            try:
                self._yearbook = _YearbookBuilder(
                    self._module_data_dir("yearbook"),
                    self._templates,
                    self.config,
                    llm_call=self._report_llm_call,
                    logger=logger,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 年报模块初始化失败：{exc!r}")
                self._yearbook = None

    async def _init_tools(self) -> None:
        """小本本只写工具：启用时激活，关闭时停用（工具本身注册在类上）。"""
        enabled = self._notebook is not None and bool(self._cfg("notebook_enable", False))
        for tool_name in ("notebook_write",):
            try:
                if enabled:
                    await self.context.activate_llm_tool_async(tool_name)
                else:
                    await self.context.deactivate_llm_tool_async(tool_name)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[{PLUGIN_NAME}] 切换工具 {tool_name} 失败：{exc!r}")

    async def _init_cron(self) -> None:
        manager = getattr(self.context, "cron_manager", None)
        if manager is None:
            return
        jobs: list[tuple[str, str, object]] = []
        if (
            self._notebook is not None
            and bool(self._cfg("notebook_daily_summary_enable", True))
        ):
            jobs.append(
                (
                    "lifespan_theory_daily_summary",
                    str(self._cfg("notebook_daily_summary_cron", "0 4 * * *")),
                    self._cron_daily_summary,
                )
            )
        trigger = str(self._cfg("report_trigger", "both") or "both").lower()
        if self._yearbook is not None and bool(self._cfg("report_enable", True)) and trigger in ("cron", "both"):
            jobs.append(
                (
                    "lifespan_theory_birthday_report",
                    str(self._cfg("report_cron", "0 4 * * *")),
                    self._cron_birthday_report,
                )
            )
        if not jobs:
            return
        existing: dict[str, str] = {}
        try:
            for job in await manager.list_jobs("basic"):
                existing[str(getattr(job, "name", ""))] = str(getattr(job, "job_id", ""))
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 列出现有 cron 失败：{exc!r}")
        for name, expr, handler in jobs:
            try:
                if name in existing:
                    await manager.delete_job(existing[name])
                job = await manager.add_basic_job(
                    name=name,
                    cron_expression=expr,
                    handler=handler,
                    description="寿命论 v1.0.0 定时任务",
                    persistent=False,
                )
                self._cron_job_ids[name] = str(getattr(job, "job_id", ""))
                logger.info(f"[{PLUGIN_NAME}] 已注册定时任务 {name}（{expr}）")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 注册定时任务 {name} 失败：{exc!r}")

    async def _remove_cron(self) -> None:
        manager = getattr(self.context, "cron_manager", None)
        if manager is None:
            return
        for name, job_id in list(self._cron_job_ids.items()):
            try:
                await manager.delete_job(job_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[{PLUGIN_NAME}] 删除定时任务 {name} 失败：{exc!r}")
        self._cron_job_ids.clear()

    async def _init_webui(self) -> None:
        if webui_mod is None or self._templates is None:
            return
        try:
            routes = webui_mod.register_web_apis(self.context, self._templates, logger=logger)
            logger.info(f"[{PLUGIN_NAME}] 年报模板页已注册 {len(routes or [])} 个接口")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 年报模板页注册失败：{exc!r}")

    # ------------------------------------------------------------------ LLM / embedding
    async def _llm_generate_text(
        self,
        system: str,
        user: str,
        *,
        provider_id: str = "",
        timeout: float | None = None,
    ) -> str:
        pid = str(provider_id or "").strip()
        if not pid:
            try:
                pid = await self._resolve_provider_id("")
            except Exception:  # noqa: BLE001
                pid = ""
        if not pid:
            try:
                provider = await self.context.get_using_provider_async(None)
                config = getattr(provider, "provider_config", None) or {}
                pid = str(config.get("id") or "") if isinstance(config, dict) else ""
            except Exception:  # noqa: BLE001
                pid = ""
        if not pid:
            raise RuntimeError("找不到可用的模型 Provider")
        kwargs = {
            "chat_provider_id": pid,
            "prompt": user,
            "system_prompt": system,
            "contexts": [],
        }
        limit = timeout if timeout else float(self._cfg("generation_timeout", 90) or 90)
        response = await asyncio.wait_for(
            self.context.llm_generate(**kwargs), timeout=max(10.0, float(limit))
        )
        return str(getattr(response, "completion_text", "") or "")

    async def _notebook_llm_call(self, system: str, user: str) -> str:
        return await self._llm_generate_text(
            system,
            user,
            provider_id=str(self._cfg("notebook_summary_provider_id", "") or ""),
            timeout=float(self._cfg("report_timeout", 300) or 300),
        )

    async def _report_llm_call(self, system: str, user: str) -> str:
        return await self._llm_generate_text(
            system,
            user,
            provider_id=str(self._cfg("report_llm_provider_id", "") or ""),
            timeout=float(self._cfg("report_timeout", 300) or 300),
        )

    async def _embed_texts(self, texts: list[str]):
        """给 notebook 的 embedding 注入；没有可用 provider 返回 None（模块自行降级）。"""
        try:
            providers = self.context.get_all_embedding_providers() or []
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 取 embedding providers 失败：{exc!r}")
            return None
        for provider in providers:
            try:
                vectors = await provider.get_embeddings(list(texts))
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[{PLUGIN_NAME}] embedding 调用失败：{exc!r}")
                continue
            if vectors:
                self._embed_provider = provider
                return vectors
        return None

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------------ 只写小本本（LLM 工具）
    @filter.llm_tool(name="notebook_write")
    async def notebook_write(
        self,
        event: AstrMessageEvent,
        content: str,
        kind: str = "other",
        attach_media: bool = False,
    ) -> str:
        """主动记下长期有用的、和你自己有关的事，写进你自己的小本本。

        什么时候用：你遇到想长期记住的东西就主动写下来——你喜欢/讨厌的东西、你做过的事、
        你此刻的心情、你在意的人、你和对方约定好的事（约定要标 kind=commitment）。
        不要记：闲聊寒暄、一次性信息、命令、工具调用噪声、重复内容（插件会自动去重）、
        他人的隐私、你没把握的推测。想记之前不用问，直接写。

        Args:
            content(string): 要记下的内容，一两句具体的话。
            kind(string): 分类：preference=喜好 / event=做过的事 / mood=心情 / person=在意的人 / commitment=约定或计划 / other=其它。
            attach_media(boolean): 是否把当前这条消息里的图片/视频一起记下（只在同一条消息里有效）。
        """
        if self._notebook is None or not bool(self._cfg("notebook_enable", False)):
            return "小本本没有启用，这次不记。"
        try:
            text_value = str(content or "").strip()
            if not text_value:
                return "内容为空，没有记。"
            media: list[dict] = []
            if attach_media:
                try:
                    media = await self._notebook.capture_event_media(event)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[{PLUGIN_NAME}] 附加媒体失败：{exc!r}")
                    media = []
            result = await self._notebook.add(
                text_value,
                kind=str(kind or "other").strip().lower(),
                date=self._today(),
                media=media or None,
                source="agent",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 小本本写入失败：{exc!r}")
            return "小本本写入失败，这次没记住。"
        if result.get("duplicate"):
            merged = result.get("merged_into")
            if merged:
                return f"这条和我本子上已有的记录很像，已经并进 {merged}。"
            return "这条和我本子上已有的记录很像，不重复记了。"
        note_id = result.get("id") or "?"
        return f"记下了（id={note_id}，kind={result.get('kind') or kind}）。"

    # ------------------------------------------------------------------ cron：每日兜底总结
    async def _cron_daily_summary(self, **_kwargs) -> None:
        if self._notebook is None:
            return
        try:
            today = self._today()
            sessions = await self._collect_summary_sessions(today)
            if not sessions:
                logger.info(f"[{PLUGIN_NAME}] 每日兜底总结：白名单会话昨天没有可总结的对话")
                return
            notes = await self._notebook.daily_summary(today=today, sessions=sessions)
            logger.info(f"[{PLUGIN_NAME}] 每日兜底总结完成：写入 {len(notes or [])} 条")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 每日兜底总结失败：{exc!r}")

    async def _collect_summary_sessions(self, today: date) -> list[dict]:
        """只读白名单会话（report_whitelist）的对话历史，给每日兜底总结用。"""
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return []
        raw_ids = self._cfg("report_whitelist", []) or []
        if not isinstance(raw_ids, list):
            return []
        sessions: list[dict] = []
        for item in raw_ids:
            ident = str(item or "").strip()
            if not ident:
                continue
            for umo in (
                f"aiocqhttp:FriendMessage:{ident}",
                f"aiocqhttp:GroupMessage:{ident}",
            ):
                try:
                    cid = await manager.get_curr_conversation_id(umo)
                    if not cid:
                        continue
                    conversation = await manager.get_conversation(umo, cid)
                except Exception:  # noqa: BLE001
                    continue
                if conversation is None:
                    continue
                messages = self._history_to_messages(getattr(conversation, "history", ""))
                if messages:
                    sessions.append({"umo": umo, "messages": messages})
        return sessions

    @staticmethod
    def _history_to_messages(history) -> list[dict]:
        try:
            data = json.loads(history) if isinstance(history, str) else history
        except (TypeError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        out: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "")
            content = item.get("content")
            if isinstance(content, list):
                content = " ".join(
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") in (None, "text")
                )
            text_value = str(content or "").strip()
            if role in ("user", "assistant") and text_value:
                out.append({"role": role, "content": text_value})
        return out

    # ------------------------------------------------------------------ cron：生日年报
    async def _cron_birthday_report(self, **_kwargs) -> None:
        if self._yearbook is None or not bool(self._cfg("report_enable", True)):
            return
        persona_id, _ = await self._resolve_persona(None)
        info = self._resolve_info(persona_id)
        if info is None or not info.get("is_birthday"):
            return
        await self._run_report(info=info, persona_id=persona_id, event=None)

    def _maybe_report_first_message(self, event, info: dict, persona_id: str | None) -> None:
        """生日当天首条消息触发年报（幂等，一年一次）。"""
        if self._yearbook is None or not bool(self._cfg("report_enable", True)):
            return
        if not info.get("is_birthday"):
            return
        trigger = str(self._cfg("report_trigger", "both") or "both").lower()
        if trigger not in ("first_message", "both"):
            return
        notices = self._load_state().setdefault("notices", {})
        if int(notices.get("last_report_year", 0) or 0) == info["today"].year:
            return
        self._spawn(self._run_report(info=info, persona_id=persona_id, event=event))

    def _report_entries(self, info: dict) -> list[dict]:
        if self._notebook is None:
            return []
        today = info["today"]
        start = today - timedelta(days=365)
        raw: list[dict] = []
        for year in sorted({start.year, today.year}):
            try:
                raw.extend(self._notebook.load_year(year) or [])
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[{PLUGIN_NAME}] 读取 {year} 年小本本失败：{exc!r}")
        entries: list[dict] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            date_text = str(entry.get("date") or entry.get("ts") or "")[:10]
            try:
                entry_date = date.fromisoformat(date_text)
            except ValueError:
                continue
            if start <= entry_date <= today:
                entries.append(entry)
        entries.sort(key=lambda item: str(item.get("date") or ""))
        return entries

    def _report_media(self, entries: list[dict]) -> list[dict]:
        if not bool(self._cfg("report_include_media", True)):
            return []
        media: list[dict] = []
        for entry in entries:
            for item in entry.get("media") or []:
                if isinstance(item, dict):
                    media.append(item)
        try:
            limit = max(0, int(self._cfg("report_max_media", 12) or 12))
        except (TypeError, ValueError):
            limit = 12
        return media[:limit] if limit else media

    async def _build_yearbook(
        self, *, info: dict, persona_id: str | None
    ) -> dict:
        """生成年报（不发送、不记账），回交给 cron/命令复用。"""
        entries = self._report_entries(info)
        media = self._report_media(entries)
        render_stats = getattr(self._yearbook, "render_stats", None)
        stats = (
            render_stats(entries, media)
            if callable(render_stats)
            else {"total": len(entries), "media": len(media), "by_kind": {}}
        )
        theme_raw = str(self._cfg("report_theme", "random") or "random").strip()
        return await self._yearbook.build(
            character_name=str(self._cfg("character_name", "德吉可（Dejiko）")),
            age=info["age"],
            year=info["today"].year,
            stats=stats,
            entries=entries,
            media=media,
            today=info["today"],
            theme=None if theme_raw in ("", "random") else theme_raw,
        )

    async def _run_report(
        self,
        *,
        info: dict,
        persona_id: str | None,
        event=None,
        force: bool = False,
        mark: bool = True,
    ) -> dict | None:
        """生成+发送+存档生日年报；自动触发时一年只跑一次。"""
        notices = self._load_state().setdefault("notices", {})
        if not force and int(notices.get("last_report_year", 0) or 0) == info["today"].year:
            return None
        try:
            result = await self._build_yearbook(info=info, persona_id=persona_id)
            if mark:
                notices["last_report_year"] = info["today"].year
            notices["last_report_dir"] = str(result.get("dir") or "")
            self._save_state()
            logger.info(f"[{PLUGIN_NAME}] 生日年报已生成：{result.get('dir')}")
            self._prune_old_reports(info["today"].year)
            sent = await self._send_report(result, event=event)
            if mark and self._notebook is not None:
                try:
                    entries = self._report_entries(info)
                    ids = [str(entry.get("id")) for entry in entries if entry.get("id")]
                    if ids:
                        self._notebook.mark_used_in_report(ids, info["today"])
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"[{PLUGIN_NAME}] 标记年报引用失败：{exc!r}")
            logger.info(f"[{PLUGIN_NAME}] 生日年报发送目标 {len(sent)} 个：{sent}")
            return result
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 生日年报生成失败：{exc!r}")
            return None

    def _prune_old_reports(self, current_year: int) -> None:
        """report_keep_years：只保留最近 N 年的年报归档目录。"""
        try:
            keep = int(self._cfg("report_keep_years", 5) or 0)
        except (TypeError, ValueError):
            keep = 5
        if keep <= 0:
            return
        root = self._module_data_dir("yearbook")
        if not root.is_dir():
            return
        cutoff = current_year - keep + 1
        import shutil

        for child in root.iterdir():
            if not child.is_dir():
                continue
            try:
                year = int(child.name)
            except ValueError:
                continue
            if year < cutoff:
                try:
                    shutil.rmtree(child, ignore_errors=True)
                    logger.info(f"[{PLUGIN_NAME}] 已清理旧年报归档：{child.name}")
                except OSError as exc:  # noqa: BLE001
                    logger.debug(f"[{PLUGIN_NAME}] 清理旧年报失败：{exc!r}")

    async def _send_report(self, result: dict, *, event=None) -> list[str]:
        try:
            targets = self._yearbook.compute_targets(self.config, event) or []
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{PLUGIN_NAME}] 计算年报发送目标失败：{exc!r}")
            targets = []
        if not targets:
            logger.info(f"[{PLUGIN_NAME}] 年报只存档不发送（没有符合白黑名单的 OneBot 目标）")
            return []
        from astrbot.core.message.components import File, Image
        from astrbot.core.message.message_event_result import MessageChain

        chain_parts = []
        long_image = result.get("long_image")
        if long_image and Path(str(long_image)).exists():
            try:
                chain_parts.append(Image.fromFileSystem(str(long_image)))
            except Exception:  # noqa: BLE001
                pass
        for page in result.get("pages") or []:
            if len(chain_parts) >= 10:
                break
            if page and Path(str(page)).exists():
                try:
                    chain_parts.append(Image.fromFileSystem(str(page)))
                except Exception:  # noqa: BLE001
                    pass
        html_path = result.get("html")
        if html_path and Path(str(html_path)).exists():
            try:
                chain_parts.append(
                    File(name=Path(str(html_path)).name, file=str(html_path))
                )
            except Exception:  # noqa: BLE001
                pass
        if not chain_parts:
            return []
        chain = MessageChain(chain=chain_parts)
        sent: list[str] = []
        for umo in targets:
            try:
                ok = await self.context.send_message(umo, chain)
                if ok:
                    sent.append(umo)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{PLUGIN_NAME}] 年报发送到 {umo} 失败：{exc!r}")
        return sent

    # ------------------------------------------------------------------ v1 新指令实现
    async def _cmd_lock(self, event, arg: str) -> str:
        if not self._require_admin(event):
            return "只有管理员能锁定档位文案喵。"
        if extras_mod is None or self._lock_store is None:
            return "extras 模块未就绪，锁定功能不可用喵。"
        persona_id, persona_prompt = await self._resolve_persona(event)
        info = self._resolve_info(persona_id)
        if info is None:
            return "生日配置解析不了，没法锁定喵。"
        tier = info["tier"]
        span = tier_key(tier)
        style = None
        if persona_prompt:
            prompt_h = self._persona_key(persona_id, persona_prompt)
            style = self._cached_style(cache_key(persona_id or "", tier, prompt_h))
        if not style:
            style = {
                "tone": str(tier.get("tone") or ""),
                "manner": str(tier.get("manner") or ""),
                "avoid": str(tier.get("avoid") or ""),
                "continuity": "",
            }
        self._lock_store.set(persona_id or "", span, style)
        note = "（这一档当时还没有生成内容，锁定的是手写兜底文案）" if not self._cached_style(
            cache_key(persona_id or "", tier, self._persona_key(persona_id, persona_prompt or ""))
        ) else ""
        return f"已锁定 {span} 档的文案（人格 {persona_id or '未知'}）{note}。之后缓存/重新生成都不会覆盖它，/age unlock 解除喵。"

    async def _cmd_unlock(self, event, arg: str) -> str:
        if not self._require_admin(event):
            return "只有管理员能解除锁定喵。"
        if self._lock_store is None:
            return "锁定功能未就绪喵。"
        persona_id, _ = await self._resolve_persona(event)
        target = (arg or "").strip()
        if target in ("all", "全部"):
            count = self._lock_store.clear(persona_id or "", None)
        elif target:
            count = self._lock_store.clear(persona_id or "", target)
        else:
            info = self._resolve_info(persona_id)
            if info is None:
                return "生日配置解析不了，没法判断当前档位喵。"
            count = self._lock_store.clear(persona_id or "", tier_key(info["tier"]))
        return f"已解除 {count} 个锁定喵。"

    async def _cmd_timeline(self, event) -> str:
        if extras_mod is None:
            return "extras 模块未就绪，时间线不可用喵。"
        persona_id, persona_prompt = await self._resolve_persona(event)
        info = self._resolve_info(persona_id)
        if info is None:
            return "生日配置解析不了，没法生成时间线喵。"
        try:
            rows = extras_mod.timeline_entries(
                self._load_state(), self._profiles(), info["birth"], info["today"]
            )
        except Exception as exc:  # noqa: BLE001
            return f"时间线生成失败：{exc!r}"
        generated: dict = {}
        if persona_prompt:
            prompt_h = self._persona_key(persona_id, persona_prompt)
            generated = self._entries_for(persona_id or "", prompt_h)
        lines = [f"年龄时间线（生日 {info['birth'].isoformat()}，今天 {info['today'].isoformat()}）："]
        for row in rows:
            span = str(row.get("tier") or "")
            entry = generated.get(span) if isinstance(generated, dict) else None
            generated_at = str(entry.get("generated_at") or "") if isinstance(entry, dict) else ""
            locked = False
            try:
                locked = bool(self._lock_store is not None and self._lock_store.is_locked(persona_id or "", span))
            except Exception:  # noqa: BLE001
                locked = False
            is_current = bool(row.get("current"))
            if not is_current:
                try:
                    is_current = int(row.get("age") or -1) == info["age"]
                except (TypeError, ValueError):
                    is_current = False
            mark = " ← 当前" if is_current else ""
            note = "已生成" if generated_at else "未生成"
            if locked:
                note = "已锁定"
            lines.append(
                f"{row.get('age')} 岁 · {span} · {row.get('label') or ''} | {note}{mark}"
            )
        return "\n".join(lines)

    async def _cmd_export(self, event) -> str:
        if not self._require_admin(event):
            return "只有管理员能导出喵。"
        if extras_mod is None:
            return "extras 模块未就绪，导出不可用喵。"
        try:
            raw = extras_mod.export_styles(self._load_state(), self._profiles())
        except Exception as exc:  # noqa: BLE001
            return f"导出失败：{exc!r}"
        export_dir = self._data_dir() / "exports"
        try:
            export_dir.mkdir(parents=True, exist_ok=True)
            path = export_dir / f"age-styles-{self._today().isoformat()}.json"
            path.write_text(raw, encoding="utf-8")
        except OSError as exc:
            return f"写导出文件失败：{exc!r}"
        return f"已导出到：\n{path}\n（{len(raw)} 字符；/age import <路径> 可导入）"

    async def _cmd_import(self, event, arg: str) -> str:
        if not self._require_admin(event):
            return "只有管理员能导入喵。"
        if extras_mod is None:
            return "extras 模块未就绪，导入不可用喵。"
        target = (arg or "").strip().strip('"')
        if not target:
            return "用法：/age import <json 文件路径>（也支持直接贴 JSON）"
        try:
            path = Path(target)
            raw = path.read_text(encoding="utf-8") if path.exists() else target
        except OSError as exc:
            return f"读取导入文件失败：{exc!r}"
        try:
            imported = extras_mod.import_styles(raw)
        except ValueError as exc:
            return f"导入失败：{exc}"
        cache = imported.get("cache") if isinstance(imported, dict) else None
        if not isinstance(cache, dict):
            return "导入失败：文件里没有 cache 数据。"
        state = self._load_state()
        bucket = state.setdefault("cache", {})
        bucket.update(cache)
        self._save_state()
        return f"导入成功：合并 {len(cache)} 条，当前缓存 {len(bucket)} 条。"

    async def _cmd_notebook(self, event, arg: str) -> str:
        if not self._require_admin(event):
            return "小本本里的内容比较私密，只有管理员能看喵。"
        if self._notebook is None:
            return "小本本未启用（配置里打开 notebook_enable 后重启生效）喵。"
        sub, _, rest = (arg or "").strip().partition(" ")
        sub = sub.strip().lower()
        rest = rest.strip()
        year = int(rest) if rest.isdigit() else self._today().year
        if sub in ("", "stats", "统计"):
            try:
                data = self._notebook.stats(year)
            except Exception as exc:  # noqa: BLE001
                return f"统计失败：{exc!r}"
            return (
                f"{year} 年小本本：{data.get('total', 0)} 条；"
                f"分类 {data.get('by_kind') or {}}；媒体 {data.get('media', 0)} 个。"
            )
        if sub in ("list", "列表"):
            try:
                entries = self._notebook.load_year(year) or []
            except Exception as exc:  # noqa: BLE001
                return f"读取失败：{exc!r}"
            if not entries:
                return f"{year} 年还没有记录喵。"
            lines = [f"{year} 年小本本（最多显示 30 条）："]
            for entry in entries[:30]:
                lines.append(
                    f"- {entry.get('date')} [{entry.get('kind')}] {str(entry.get('content') or '')[:80]}"
                )
            if len(entries) > 30:
                lines.append(f"……还有 {len(entries) - 30} 条")
            return "\n".join(lines)
        if sub in ("delete", "删除"):
            if not rest:
                return "用法：/age notebook delete <YYYY-MM-DD 或 note_id>"
            try:
                if len(rest) == 10 and rest[4] == "-":
                    result = self._notebook.delete(day=date.fromisoformat(rest))
                else:
                    result = self._notebook.delete(note_id=rest)
            except Exception as exc:  # noqa: BLE001
                return f"删除失败：{exc!r}"
            return f"已删除 {result.get('deleted', 0)} 条喵。"
        if sub in ("cleanup", "清理"):
            if not bool(self._cfg("notebook_cleanup_enable", False)):
                return "清理功能默认关闭；先在配置里打开 notebook_cleanup_enable 喵。"
            dry_run = rest not in ("run", "执行", "confirm")
            try:
                result = self._notebook.cleanup(today=self._today(), dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001
                return f"清理失败：{exc!r}"
            mode = "预览（未动文件）" if dry_run else "已执行"
            return (
                f"清理{mode}：命中 {result.get('matched', 0)} 条，"
                f"归档 {result.get('archived', 0)}，删除 {result.get('deleted', 0)}，"
                f"保留 {result.get('kept', 0)}。"
                + ("\n确认执行：/age notebook cleanup run" if dry_run else "")
            )
        if sub in ("reindex", "重建索引"):
            try:
                result = await self._notebook.reindex()
            except Exception as exc:  # noqa: BLE001
                return f"重建索引失败：{exc!r}"
            return f"索引重建完成：{result}"
        if sub in ("export", "导出"):
            try:
                raw = self._notebook.export(year)
            except Exception as exc:  # noqa: BLE001
                return f"导出失败：{exc!r}"
            export_dir = self._data_dir() / "exports"
            try:
                export_dir.mkdir(parents=True, exist_ok=True)
                path = export_dir / f"notebook-{year}.json"
                path.write_text(raw, encoding="utf-8")
            except OSError as exc:
                return f"写导出文件失败：{exc!r}"
            return f"已导出小本本到：\n{path}"
        return "用法：/age notebook [stats|list|delete|cleanup|reindex|export] [年/日期/id]"

    async def _cmd_yearbook(self, event, arg: str) -> str:
        if self._yearbook is None:
            return "年报模块未就绪（检查 yearbook.py / templates 是否存在）喵。"
        sub, _, rest = (arg or "").strip().partition(" ")
        sub = sub.strip().lower()
        rest = rest.strip()
        if sub in ("templates", "模板"):
            try:
                rows = self._templates.list() if self._templates else []
            except Exception as exc:  # noqa: BLE001
                return f"读取模板失败：{exc!r}"
            lines = ["年报模板："]
            for row in rows:
                lines.append(f"- {row.get('name')}（{row.get('source')}）{row.get('title') or ''}")
            return "\n".join(lines)
        if sub in ("", "help", "帮助"):
            return (
                "用法：/age yearbook [preview|send|templates]\n"
                "preview=只生成不发送；send=生成并发送；模板在 WebUI「年报模板」页管理。"
            )
        if sub in ("preview", "预览", "send", "发送"):
            try:
                persona_id, _ = await self._resolve_persona(event)
                info = self._resolve_info(persona_id)
                if info is None:
                    return "年龄解析失败，做不了年报喵。"
                result = await self._build_yearbook(info=info, persona_id=persona_id)
                path = str(result.get("dir") or "")
                if sub in ("preview", "预览"):
                    return (
                        f"年报已生成（预览：不发送、不占用今年自动年报的名额）：\n{path}\n"
                        f"主题 {result.get('theme')} · 模式 {result.get('mode')}"
                    )
                sent = await self._send_report(result, event=event)
                return (
                    f"年报已生成并尝试发送（目标 {len(sent)} 个）：\n{path}\n"
                    f"主题 {result.get('theme')} · 模式 {result.get('mode')}"
                )
            except Exception as exc:  # noqa: BLE001
                return f"年报生成失败：{exc!r}"
        return "用法：/age yearbook [preview|send|templates]"

    def _notice(self, info: dict, generated: bool, key: str, persona_id: str) -> None:
        """生日当天只打一次日志 + 记下年份；年龄/档位变化时记账。"""
        state = self._load_state()
        notices = state.setdefault("notices", {})
        changed = False
        if info["is_birthday"] and int(notices.get("last_birthday_year", 0) or 0) != info["today"].year:
            notices["last_birthday_year"] = info["today"].year
            logger.info(f"[{PLUGIN_NAME}] 今天是生日：{info['age']} 岁，{info['today'].isoformat()}")
            changed = True
        last_age = notices.get("last_seen_age")
        if last_age != info["age"]:
            notices["last_seen_age"] = info["age"]
            notices["last_seen_tier"] = tier_key(info["tier"])
            logger.info(
                f"[{PLUGIN_NAME}] 年龄更新为 {info['age']} 岁（{info['tier'].get('label', '')}），"
                f"风格文案{'已就绪' if generated else '未就绪→用兜底'}"
            )
            changed = True
        if changed:
            self._save_state()

    # ------------------------------------------------------------------ 指令
    def _parse_args(self, event) -> tuple[str, str]:
        text = (getattr(event, "message_str", "") or "").strip()
        if not text:
            return "", ""
        tokens = text.replace("\u3000", " ").split()
        known = {COMMAND, *COMMAND_ALIAS}
        cut = None
        for index, token in enumerate(tokens):
            if token.lstrip("/!！/") in known:
                cut = index
                break
        rest = tokens[cut + 1 :] if cut is not None else tokens[1:]
        if not rest:
            return "", ""
        return rest[0].strip().lower(), " ".join(rest[1:]).strip()

    def _is_private(self, event) -> bool:
        try:
            return bool(event.is_private_chat())
        except Exception:  # noqa: BLE001
            return True

    def _require_admin(self, event) -> bool:
        try:
            return bool(event.is_admin())
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] is_admin 失败：{exc!r}")
            return False

    @filter.command(COMMAND, alias=COMMAND_ALIAS)
    async def cmd_age(self, event: AstrMessageEvent):
        """寿命论主指令：查看状态；子指令见 /age help。"""
        sub, arg = self._parse_args(event)
        if sub in {"help", "?", "帮助"}:
            yield event.plain_result(self._help_text())
            return
        if sub in {"on", "off"}:
            yield event.plain_result(await self._cmd_switch(event, sub))
            return
        if sub == "set":
            yield event.plain_result(await self._cmd_set_age(event, arg))
            return
        if sub == "reset":
            yield event.plain_result(await self._cmd_reset_age(event))
            return
        if sub == "show":
            yield event.plain_result(await self._cmd_show(event))
            return
        if sub == "tiers":
            yield event.plain_result(await self._cmd_tiers(event))
            return
        if sub in {"lint", "体检"}:
            yield event.plain_result(await self._cmd_lint(event))
            return
        if sub in {"preview", "预览"}:
            yield event.plain_result(await self._cmd_preview(event, arg))
            return
        if sub in {"compare", "对比"}:
            yield event.plain_result(await self._cmd_compare(event, arg))
            return
        if sub in {"prompt", "提示词"}:
            yield event.plain_result(await self._cmd_prompt(event))
            return
        if sub in {"gen", "生成"}:
            yield event.plain_result(await self._cmd_generate(event, arg))
            return
        if sub in {"clear", "清空"}:
            yield event.plain_result(await self._cmd_clear(event))
            return
        if sub == "mode":
            yield event.plain_result(await self._cmd_mode(event, arg))
            return
        if sub in {"persona", "人格"}:
            yield event.plain_result(await self._cmd_persona(event, arg))
            return
        if sub in {"lock", "锁定"}:
            yield event.plain_result(await self._cmd_lock(event, arg))
            return
        if sub in {"unlock", "解锁"}:
            yield event.plain_result(await self._cmd_unlock(event, arg))
            return
        if sub in {"timeline", "时间线"}:
            yield event.plain_result(await self._cmd_timeline(event))
            return
        if sub in {"export", "导出"}:
            yield event.plain_result(await self._cmd_export(event))
            return
        if sub in {"import", "导入"}:
            yield event.plain_result(await self._cmd_import(event, arg))
            return
        if sub in {"notebook", "小本本"}:
            yield event.plain_result(await self._cmd_notebook(event, arg))
            return
        if sub in {"yearbook", "年报"}:
            yield event.plain_result(await self._cmd_yearbook(event, arg))
            return
        yield event.plain_result(await self._cmd_status(event))

    # ---- 子指令实现
    async def _cmd_status(self, event) -> str:
        persona_id, persona_prompt = await self._resolve_persona(event)
        info = self._resolve_info(persona_id)
        if info is None:
            return "生日配置解析不了，看看 /age set 不了的话先去配置面板检查 birthday 是不是 YYYY-MM-DD 喵。"
        lines = [
            f"今天是 {info['today'].isoformat()}，年龄 {info['age']} 岁"
            + ("（手动覆盖）" if info["manual"] else "")
            + "。",
            f"生日 {info['birth'].month} 月 {info['birth'].day} 日，"
            f"距 {info['age'] + 1} 岁生日还有 {info['days_left']} 天"
            f"（{info['next_birthday'].isoformat()}）。",
            f"当前年龄档：{tier_key(info['tier'])} · {info['tier'].get('label', '')}；"
            f"总开关：{'开' if bool(self._cfg('enable', True)) else '关'}。",
        ]
        mode = str(self._cfg("generation_mode", "background")).strip().lower()
        if persona_prompt:
            prompt_h = self._persona_key(persona_id, persona_prompt)
            key = cache_key(persona_id or "", info["tier"], prompt_h)
            if self._cached_style(key):
                cache_state = "已生成"
            elif key in self._inflight:
                cache_state = "后台生成中，本轮先用兜底"
            elif self._cooldown_active(key):
                cache_state = "上次生成失败，冷却中（先用兜底）"
            else:
                cache_state = "未生成（先用兜底，下条消息会排队生成）"
            lines.append(f"人格：{persona_id or '未识别'}（提示词 {len(persona_prompt)} 字）；风格文案：{cache_state}。")
        else:
            lines.append("人格：未识别（检查 persona_id 或 prompt_override），当前只用年龄档手写兜底文。")
        lines.append(f"生成模式：{mode}；注入位置：{self._cfg('inject_mode', 'extra_user_content')}。")
        lines.append(
            f"小本本：{'开' if self._notebook else '关'}；"
            f"年报：{'开' if (self._yearbook and bool(self._cfg('report_enable', True))) else '关'}；"
            f"锁定：{'开' if self._lock_store else '不可用'}。"
        )
        lines.append("输入 /age help 看全部指令喵。")
        return "\n".join(lines)

    async def _cmd_show(self, event) -> str:
        persona_id, persona_prompt = await self._resolve_persona(event)
        info = self._resolve_info(persona_id)
        if info is None:
            return "生日配置解析不了，没法预览注入内容喵。"
        style = None
        pending = False
        key = ""
        if persona_prompt:
            prompt_h = self._persona_key(persona_id, persona_prompt)
            key = cache_key(persona_id or "", info["tier"], prompt_h)
            style = self._cached_style(key)
            pending = style is None
        text = self._render_block(info, style, pending=pending)
        header = "本轮注入预览（还没发送，不是要念出来的台词）：\n"
        if key and key in self._inflight:
            header += "（风格文案后台生成中，下面是兜底版）\n"
        elif key and self._cooldown_active(key):
            header += "（上次生成失败冷却中，下面是兜底版）\n"
        return header + text

    async def _cmd_tiers(self, event) -> str:
        persona_id, persona_prompt = await self._resolve_persona(event)
        info = self._resolve_info(persona_id)
        profiles = self._profiles()
        generated: dict = {}
        if persona_prompt:
            prompt_h = self._persona_key(persona_id, persona_prompt)
            generated = self._entries_for(persona_id or "", prompt_h)
        lines = ["年龄档一览（✔=已生成风格文案，○=用兜底）："]
        for tier in sorted(profiles.get("tiers") or [], key=lambda t: int(t.get("min", 0))):
            span = tier_key(tier)
            mark = "✔" if span in generated else "○"
            current = " ← 当前" if info is not None and tier is info["tier"] else ""
            lines.append(f"{mark} {span} · {tier.get('label', '')}{current}")
        return "\n".join(lines)

    async def _style_and_tier_for_age(self, event, age: int):
        """(style|None, tier, persona_id, age)。只查缓存：不调 LLM、不写状态。"""
        age = max(0, min(int(age), 150))
        tier = pick_tier(self._profiles(), age)
        persona_id, persona_prompt = await self._resolve_persona(event)
        style = None
        if persona_prompt:
            prompt_h = self._persona_key(persona_id, persona_prompt)
            style = self._cached_style(cache_key(persona_id or "", tier, prompt_h))
        return style, tier, persona_id, age

    async def _cmd_lint(self, event) -> str:
        if not self._require_admin(event):
            return "只有管理员能做人设体检喵（会读取人格提示词）。"
        persona_id, persona_prompt = await self._resolve_persona(event)
        if not persona_prompt:
            return "没取到人格提示词，没法体检。先用 /age prompt 排查 persona_id / prompt_override。"
        report = core.lint_persona_style(persona_prompt)
        info = self._resolve_info(persona_id)
        head = (
            f"人设年龄体检 · 人格：{persona_id or '未知（prompt_override）'} · "
            f"提示词 {len(persona_prompt)} 字\n"
        )
        if info:
            head += (
                f"当前：{info['age']} 岁 / {tier_key(info['tier'])} 档；"
                f"memory_note={'开' if bool(self._cfg('memory_note', True)) else '关'}\n"
            )
        findings = report.get("findings") or []
        if not findings:
            return head + "✅ 没发现写死的年龄或固定口吻，人设与插件不冲突喵。"
        if not self._is_private(event):
            return (
                head
                + f"发现 {len(findings)} 处疑似冲突；人设原文不适合在群里展开，"
                "请私聊我发 /age lint 看详情喵。"
            )
        lines = [head, f"发现 {len(findings)} 处疑似冲突："]
        for index, item in enumerate(findings, 1):
            lines.append("")
            lines.append(f"{index}. [{item['kind']}] 命中「{item['match']}」")
            lines.append(f"   上下文：…{item['context']}…")
            lines.append(f"   建议：{item['hint']}")
        lines.append("")
        lines.append("（体检只给建议，不会自动改人格；改完人设后生成缓存会自动失效。）")
        return "\n".join(lines)

    async def _cmd_preview(self, event, arg: str) -> str:
        persona_id, _ = await self._resolve_persona(event)
        info = self._resolve_info(persona_id)
        if info is None:
            return "生日配置解析不了，没法预览喵。"
        arg = (arg or "").strip()
        if arg and not arg.isdigit():
            return "用法：/age preview <年龄>（例如 /age preview 24）；不带参数预览当前年龄。"
        age = int(arg) if arg else info["age"]
        style, tier, _, age = await self._style_and_tier_for_age(event, age)
        preview_info = {**info, "age": age, "tier": tier, "manual": False}
        text = self._render_block(preview_info, style, pending=style is None)
        header = (
            f"预览 {age} 岁（{tier_key(tier)} · {tier.get('label', '')}）：\n"
            + ("" if style else "（该档还没有生成内容，下面是手写兜底文案；不会触发生成）\n")
            + "（只是预览，不会注入到对话）\n"
        )
        return header + text

    async def _cmd_compare(self, event, arg: str) -> str:
        parts = (arg or "").replace("，", " ").replace(",", " ").split()
        if len(parts) < 2 or not all(p.isdigit() for p in parts[:2]):
            return "用法：/age compare <年龄A> <年龄B>（例如 /age compare 14 60）"
        ages = [max(0, min(int(p), 150)) for p in parts[:2]]
        blocks = []
        for age in ages:
            style, tier, _, _ = await self._style_and_tier_for_age(event, age)
            lines = core.render_style_lines(style, tier)
            blocks.append((age, tier, style, lines))
        out = [f"年龄对比：{ages[0]} 岁 ↔ {ages[1]} 岁（只查缓存，不调模型、不注入）"]
        for age, tier, style, lines in blocks:
            mark = "" if style else "（手写兜底）"
            out.append("")
            out.append(f"【{age} 岁 · {tier_key(tier)} · {tier.get('label', '')}】{mark}")
            out.extend(lines or ["（这一档没有可用的风格文案）"])
        return "\n".join(out)

    async def _cmd_prompt(self, event) -> str:
        if not self._require_admin(event):
            return "这条只有管理员能看喵。"
        persona_id, persona_prompt = await self._resolve_persona(event, force=True)
        if not persona_prompt:
            return "没取到人格提示词。检查配置里的 persona_id，或者是不是填了 prompt_override 兜底。"
        prompt_h = self._persona_key(persona_id, persona_prompt)
        return (
            f"人格 id：{persona_id or '未知'}\n"
            f"提示词长度：{len(persona_prompt)} 字\n"
            f"提示词指纹：{prompt_h}\n"
            f"缓存模板版本：{TEMPLATE_VERSION}\n"
            f"来源：{'AstrBot 人格库（按 id 读取）' if persona_id else 'prompt_override'}\n"
            "（出于隐私，这里不回显人格原文；要看真正注入的内容用 /age show。）"
        )

    async def _cmd_generate(self, event, arg: str) -> str:
        if not self._require_admin(event):
            return "只有管理员能触发重新生成喵。"
        persona_id, persona_prompt = await self._resolve_persona(event)
        info = self._resolve_info(persona_id)
        if info is None:
            return "生日配置解析不了，没法生成喵。"
        if not persona_prompt:
            return "没取到人格提示词，生成不了。先用 /age prompt 排查一下。"
        profiles = self._profiles()
        tiers = sorted(profiles.get("tiers") or [], key=lambda t: int(t.get("min", 0)))
        raw_target = (arg or "current").strip().lower()
        want_wait = raw_target in {"wait", "等待"}
        target = "current" if want_wait else raw_target
        prompt_h = self._persona_key(persona_id, persona_prompt)

        def _payload(age: int, tier: dict) -> dict:
            key = cache_key(persona_id or "", tier, prompt_h)
            self._clear_failure(key)
            return {
                "event": event,
                "info": {**info, "age": age, "tier": tier},
                "persona_id": persona_id,
                "persona_prompt": persona_prompt,
                "prompt_h": prompt_h,
                "key": key,
                "force": True,
            }

        if target in {"all", "全部"}:
            current_min = int(info["tier"].get("min", 0) or 0)
            pending = [t for t in tiers if int(t.get("min", 0) or 0) >= current_min]
            if not pending:
                return "没有需要生成的档位喵。"
            task = asyncio.create_task(
                self._generate_all(
                    event=event,
                    info=info,
                    persona_id=persona_id,
                    persona_prompt=persona_prompt,
                    prompt_h=prompt_h,
                )
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return (
                f"已排队后台依次生成 {len(pending)} 个档位（当前及以后），"
                "用 /age tiers 看进度。"
            )

        if target in {"next", "下一档"}:
            current_min = int(info["tier"].get("min", 0) or 0)
            upcoming = [t for t in tiers if int(t.get("min", 0) or 0) > current_min]
            if not upcoming:
                return "已经是最后一档了，没有下一档喵。"
            tier = upcoming[0]
            payload = _payload(max(int(tier.get("min", 0) or 0), info["age"]), tier)
            if want_wait:
                entry = await self._generate(**payload)
                return f"下一档（{tier_key(tier)}）风格文案生成{'成功' if entry else '失败，看日志'}。"
            ok = self._schedule_generation(**payload)
            return "下一档风格文案已开始后台生成。" if ok else "下一档已在生成中喵。"

        if target.isdigit():
            age = int(target)
            tier = pick_tier(profiles, age)
        else:
            age, tier = info["age"], info["tier"]
        payload = _payload(age, tier)
        if want_wait:
            entry = await self._generate(**payload)
            style = (entry or {}).get("style")
            if style:
                return (
                    f"{age} 岁（{tier_key(tier)} 档）的风格文案已生成：\n"
                    f"口吻：{style.get('tone', '')}\n"
                    f"行为：{style.get('manner', '')}\n"
                    f"避免：{style.get('avoid', '')}"
                )
            return "生成失败或超时，看日志 / 先继续用兜底文案喵。"
        ok = self._schedule_generation(**payload)
        if not ok:
            return "这一档已在生成中喵，稍等再看 /age tiers。"
        return f"{age} 岁（{tier_key(tier)} 档）的风格文案已开始后台生成，稍后 /age show 看效果。"

    async def _cmd_clear(self, event) -> str:
        if not self._require_admin(event):
            return "只有管理员能清缓存喵。"
        state = self._load_state()
        count = len(state.get("cache") or {})
        state["cache"] = {}
        state["failures"] = {}
        self._save_state()
        self._prompt_cache.clear()
        return f"已清空 {count} 条生成缓存，下条消息会重新排队生成。"

    async def _cmd_mode(self, event, arg: str) -> str:
        if not self._require_admin(event):
            return "只有管理员能改生成模式喵。"
        mode = (arg or "").strip().lower()
        if mode not in GENERATION_MODES:
            return f"用法：/age mode {'|'.join(GENERATION_MODES)}（当前 {self._cfg('generation_mode', 'background')}）"
        self.config["generation_mode"] = mode
        saved = self._save_config()
        return f"生成模式已设为 {mode}{'' if saved else '（写配置失败，只在内存里生效）'}。"

    async def _cmd_persona(self, event, arg: str) -> str:
        if not self._require_admin(event):
            return "只有管理员能改人格绑定喵。"
        value = (arg or "").strip()
        if not value:
            persona_id, persona_prompt = await self._resolve_persona(event)
            return (
                f"当前自动识别的人格：{persona_id or '未识别'}"
                f"（提示词 {len(persona_prompt)} 字）。\n"
                "用法：/age persona <人格id> 指定；/age persona auto 回到自动识别。"
            )
        if value.lower() in {"auto", "自动"}:
            self.config["persona_id"] = ""
            saved = self._save_config()
            self._prompt_cache.clear()
            return f"已切回自动识别人格{'' if saved else '（写配置失败，只在内存里生效）'}。"
        self.config["persona_id"] = value
        saved = self._save_config()
        self._prompt_cache.clear()
        prompt = await self._fetch_persona_prompt(value, force=True)
        if not prompt:
            return (
                f"人格 id「{value}」没取到提示词，先照样存下了。检查 AstrBot 人格库里这个 id 是否存在"
                f"{'' if saved else '（写配置失败，只在内存里生效）'}。"
            )
        return f"已绑定人格「{value}」（提示词 {len(prompt)} 字）{'' if saved else '（写配置失败，只在内存里生效）'}。"

    async def _cmd_switch(self, event, sub: str) -> str:
        if not self._require_admin(event):
            return "这条只有管理员能改喵。"
        self.config["enable"] = sub == "on"
        saved = self._save_config()
        suffix = "" if saved else "（写配置失败，只在内存里生效）"
        return ("年龄流逝已开启喵。" if sub == "on" else "年龄流逝已关闭，不会再注入喵。") + suffix

    async def _cmd_set_age(self, event, arg: str) -> str:
        if not self._require_admin(event):
            return "这条只有管理员能改喵。"
        value = (arg or "").strip()
        if not value.lstrip("-").isdigit():
            return "用法：/age set 16（-1 表示取消手动覆盖）"
        age = int(value)
        self.config["manual_age"] = age
        saved = self._save_config()
        suffix = "" if saved else "（写配置失败，只在内存里生效）"
        if age < 0:
            return "手动年龄已取消，回到按生日现算。" + suffix
        return f"手动年龄设为 {age} 岁，下一条消息生效（测试用，记得 /age reset 收回）。" + suffix

    async def _cmd_reset_age(self, event) -> str:
        if not self._require_admin(event):
            return "这条只有管理员能改喵。"
        self.config["manual_age"] = -1
        saved = self._save_config()
        return "已复位：年龄回到按生日现算喵。" + ("" if saved else "（写配置失败，只在内存里生效）")

    def _help_text(self) -> str:
        return (
            "《寿命论》指令：\n"
            "/age 或 /寿命论 — 当前年龄、档位、生成状态\n"
            "/age show — 预览本轮注入的原文\n"
            "/age tiers — 年龄档一览与生成进度\n"
            "/age lint — 人设年龄体检：扫描写死的年龄/固定口吻并给建议（管理员，详情仅私聊）\n"
            "/age preview [年龄] — 预览某个年龄会注入的原文（不调模型、不注入）\n"
            "/age compare <年龄A> <年龄B> — 并排对比两个年龄的说话方式\n"
            "/age prompt — 看当前用的 AstrBot 人格提示词（管理员）\n"
            "/age gen [current|next|<年龄>|all|wait] — 重新生成风格文案（管理员）\n"
            "/age clear — 清空生成缓存（管理员）\n"
            "/age mode background|sync|manual|off — 生成模式（管理员）\n"
            "/age persona <id|auto> — 绑定/自动识别人格（管理员）\n"
            "/age lock [unlock] — 锁定/解除当前档位文案（管理员）\n"
            "/age timeline — 年龄时间线与各档生成/锁定状态\n"
            "/age export | /age import <路径> — 导出/导入生成缓存（管理员）\n"
            "/age notebook [stats|list|delete|cleanup|export] — 只写小本本管理（管理员）\n"
            "/age yearbook [preview|send|templates] — 生日年报（管理员）\n"
            "/age set <年龄> | /age reset — 手动覆盖年龄（管理员）\n"
            "/age on | /age off — 总开关（管理员）\n"
            "（年报模板在 AstrBot 插件页「寿命论 → 年报模板」里编辑/导入）"
        )
