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
from datetime import datetime
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
    "0.1.3",
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

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self) -> None:
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

    async def terminate(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._inflight.clear()

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

    def _birth_anchor_year(self, today: date, anchor_age: int) -> int:
        """只写 MM-DD 时，出生年在第一次确定后落盘复用。

        否则每年都按「今年 - 锚点年龄」重算出生年，年龄会永远停在锚点岁数，
        跟「年龄增长」正好相反。配置变了（signature 变化）才重新锚定。
        """
        raw = str(self._cfg("birthday", "2012-02-08")).strip()
        signature = f"{raw}|{anchor_age}"
        notices = self._load_state().setdefault("notices", {})
        record = notices.get("birth_anchor")
        if isinstance(record, dict) and record.get("signature") == signature:
            try:
                return int(record.get("year"))
            except (TypeError, ValueError):
                pass
        year = today.year - anchor_age
        notices["birth_anchor"] = {"signature": signature, "year": year}
        self._save_state()
        return year

    def _birthday(self, today: date | None = None) -> date | None:
        today = today or self._today()
        raw = str(self._cfg("birthday", "2012-02-08"))
        anchor_age = self._anchor_age()
        anchor_year = (
            self._birth_anchor_year(today, anchor_age)
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

    def _resolve_info(self) -> dict | None:
        """算出这一轮该用的全部信息，算不出来返回 None（配置再离谱也不许让指令/注入连坐）。"""
        try:
            today = self._today()
            birth = self._birthday(today)
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
        self, event, req, info: dict
    ) -> tuple[dict | None, str, str]:
        """返回 (style, key, persona_id)。没就绪时 style=None（用兜底）。"""
        persona_id, persona_prompt = await self._resolve_persona(event, req)
        if not persona_prompt:
            return None, "", persona_id or ""
        prompt_h = self._persona_key(persona_id, persona_prompt)
        key = cache_key(persona_id or "", info["tier"], prompt_h)
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
        info = self._resolve_info()
        if info is None:
            return

        style, key, persona_id = await self._prepare_style(event, req, info)
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
        yield event.plain_result(await self._cmd_status(event))

    # ---- 子指令实现
    async def _cmd_status(self, event) -> str:
        info = self._resolve_info()
        if info is None:
            return "生日配置解析不了，看看 /age set 不了的话先去配置面板检查 birthday 是不是 YYYY-MM-DD 喵。"
        persona_id, persona_prompt = await self._resolve_persona(event)
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
        lines.append("输入 /age help 看全部指令喵。")
        return "\n".join(lines)

    async def _cmd_show(self, event) -> str:
        info = self._resolve_info()
        if info is None:
            return "生日配置解析不了，没法预览注入内容喵。"
        persona_id, persona_prompt = await self._resolve_persona(event)
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
        info = self._resolve_info()
        profiles = self._profiles()
        persona_id, persona_prompt = await self._resolve_persona(event)
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
        info = self._resolve_info()
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
        info = self._resolve_info()
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
        info = self._resolve_info()
        if info is None:
            return "生日配置解析不了，没法生成喵。"
        persona_id, persona_prompt = await self._resolve_persona(event)
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
            "/age set <年龄> | /age reset — 手动覆盖年龄（管理员）\n"
            "/age on | /age off — 总开关（管理员）"
        )
