# -*- coding: utf-8 -*-
"""寿命论 · v1.0.0 增强逻辑层（纯逻辑 + 小存储类，**不 import astrbot**）。

这一层只做「为什么」的落点，不做任何 AstrBot 接线：

1. 多角色独立生日：同一个机器人可能挂着好几个人格，每个人格有自己写死的生日；
   全局 birthday 仍是默认值，persona_birthdays 按人格 id 覆盖。解析规则完全复用
   lifespan_core.parse_birthday（含「只写月日时按锚点年落盘、闰日按 3/1」的口径），
   避免出现两套生日语义。
2. LockStore：生成出来的档位文案是「按 (人格, 档位, 人格提示词哈希) 缓存」的，
   改一次人格提示词，生成缓存就整批失效。用户如果想手动定住某一档的文案，
   锁必须存在与生成缓存无关的地方，于是单独一个 locks.json；注入时 Lock 优先于缓存。
3. 注入强度：有人只想要 tone、不想要 avoid 的「别写成」列表，有人嫌注入块太长。
   这里把配置翻译成一组布尔 + 上限，并提供一个纯函数式裁剪器（apply_injection_options），
   让 main.py 不需要自己知道注入块的行格式。
4. 档位文案导入导出：换设备 / 分享给朋友的唯一通道。导出格式带 version，
   导入前严格校验；坏输入一律 ValueError（宁可整份拒收，也不要半份脏缓存混进 state.json）。
   缓存 key 的算法复用 lifespan_core.cache_key，保证导入的条目能被现有查缓存逻辑命中。
5. 季节/时间感短句、时间线、生日/里程碑特典：这些是「会随时间自己变化」的小内容，
   全部由 today: date 参数注入，纯函数可单测，绝不在模块里取系统时间（除导出时间戳）。

时间约定：日期一律 datetime.date；时区由 main.py 决定并传 today 进来。
"""

from __future__ import annotations

import copy
import json
import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:  # 作为包的一部分 import（main.py / 单测都这么用）
    from . import lifespan_core as core
except ImportError:  # pragma: no cover - 允许直接 python extras.py 自检的场景
    import lifespan_core as core  # type: ignore[no-redef]

try:  # 日志统一走 AstrBot 的 logger（平台安全审查要求，禁止使用标准库日志模块）
    from .log_bridge import get_logger
except ImportError:  # pragma: no cover - 直接 python extras.py 自检的场景
    from log_bridge import get_logger  # type: ignore[no-redef]

LOGGER = get_logger()

PLUGIN_NAME = "astrbot_plugin_lifespan_theory"
# 导出格式版本：字段不兼容变化时 +1，导入端根据它决定能不能读。
EXPORT_VERSION = 1
# 配置里没写 birthday 时沿用插件默认值（与 main.py / _conf_schema.json 保持一致）。
DEFAULT_BIRTHDAY = "2012-02-08"
# 注入块长度上限：正常注入块 600~1200 字；给个硬上限防止配置写离谱把上下文挤爆。
DEFAULT_MAX_INJECT_CHARS = 1200
MAX_INJECT_CHARS = 20000
# 导入/导出里单个风格字段的硬上限：防止手改坏文件或恶意导入把 state.json / 注入块撑爆。
MAX_STYLE_FIELD_CHARS = 2000
MAX_IMPORT_CHARS = 2 * 1024 * 1024
MAX_LOCKS_FILE_CHARS = 1024 * 1024

STYLE_KEYS = ("tone", "manner", "avoid", "continuity")

_TIER_KEY_RE = re.compile(r"^\s*(\d{1,3})\s*-\s*(\d{1,3})\s*$")
_TIER_OPEN_RE = re.compile(r"^\s*(\d{1,3})\s*\+\s*$")

# 注入块里「风格行 → 配置开关」的映射。顺序与 lifespan_core.render_style_lines 一致。
_STYLE_LINE_KEYS = (
    ("说话的口吻：", "tone"),
    ("行为与情绪：", "manner"),
    ("这一档别写成：", "avoid"),
    ("与上一档相比：", "continuity"),
)

# 生日里程碑：整十 / 有文化说法的岁数。只在生日当天触发。
MILESTONE_AGES: dict[int, str] = {
    18: "成年",
    20: "桃李年华",
    30: "而立",
    40: "不惑",
    50: "知天命",
    60: "花甲",
    70: "古稀",
    80: "耄耋",
    90: "鲐背",
    100: "期颐",
}
_MILESTONE_NOTES: dict[int, str] = {
    18: "今天她 18 岁生日，法律上成年了。她可能嘴上满不在乎、心里未必；可以有一点点郑重，但别写成长篇感悟、别许愿、别告别。",
    20: "今天她 20 岁生日，到了该被当成大人的年纪。别让她突然端庄，只是有些话可以放慢半拍。",
    30: "今天她 30 岁生日，而立。可以有一瞬间的回望，但别写成总结陈词。",
    40: "今天她 40 岁生日，不惑。让她对旧账松一点手，嘴还得硬。",
    50: "今天她 50 岁生日，知天命。身体和记性开始有真实的钝感，心还年轻。",
    60: "今天她 60 岁生日，花甲。可以有一点点被时间推着走的实感，但别写成衰老展览。",
    70: "今天她 70 岁生日，古稀。耳朵和眼睛不济了，最在意的人还记得就够。",
    80: "今天她 80 岁生日，耄耋。别写失能、失智、认不出人；她的嘴还硬。",
    90: "今天她 90 岁生日，鲐背。话很少了，一句就是全部。",
    100: "今天她 100 岁生日，期颐。别写告别和遗言，她还在等一个人。",
}
# 「在一起过了多少天」这类里程碑：只在正好第 N 天触发，不会连续几天刷屏。
DAY_MILESTONES: tuple[int, ...] = (1000, 2000, 3000, 5000, 10000)

_SEASON_LINES = {
    3: "又到春天了，这是她过的第 {nth} 个春天。",
    4: "又到春天了，这是她过的第 {nth} 个春天。",
    5: "春天快过完了，这是她过的第 {nth} 个春天。",
    6: "入夏了，这是她过的第 {nth} 个夏天。",
    7: "正是夏天，这是她过的第 {nth} 个夏天。",
    8: "夏天快过完了，这是她过的第 {nth} 个夏天。",
    9: "入秋了，这是她过的第 {nth} 个秋天。",
    10: "秋天正好，这是她过的第 {nth} 个秋天。",
    11: "秋末了，这是她过的第 {nth} 个秋天。",
    12: "入冬了，这是她过的第 {nth} 个冬天。",
    1: "冬天还没过去，这是她过的第 {nth} 个冬天。",
    2: "天还冷着，这是她过的第 {nth} 个冬天。",
}


# ==================================================================== 小工具
def _cfg_get(cfg: dict, key: str, default: Any = None) -> Any:
    """从 cfg 里取值；cfg 不是 dict（AstrBot 配置异常/None）时安静回默认值。"""
    if not isinstance(cfg, dict):
        return default
    try:
        value = cfg.get(key, default)
    except AttributeError:  # pragma: no cover - 极端防御
        return default
    return default if value is None else value


def _as_bool(value: Any, default: bool) -> bool:
    """把配置里的「布尔」宽松地翻译成 bool。

    配置面板一般给真 bool，但手改 JSON 的人常写 "false" / 0 / "off"；
    对这类输入要按字面意思理解，而不是被 bool("false") 骗成 True。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "开", "开启"}:
            return True
        if text in {"0", "false", "no", "off", "关", "关闭", ""}:
            return False
        return default
    return default


def _as_int(value: Any, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    """把配置里的 int 宽松解析出来；超出范围就夹住，坏值回默认。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _flatten_text(value: Any) -> str:
    """把模型/手改可能产生的列表、字典压成一行中文。

    与 lifespan_core._flatten_field 同一目的：注入上下文里绝不能出现 Python 的
    列表 repr（['别说教', '别写成妈妈腔']），那会把模型绕晕。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple, set)):
        parts = [_flatten_text(item) for item in value]
        return "；".join(part for part in parts if part)
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            text = _flatten_text(item)
            if text:
                parts.append(f"{str(key).strip()}：{text}")
        return "；".join(parts)
    return str(value).strip()


def _clean_style_field(value: Any, limit: int = MAX_STYLE_FIELD_CHARS) -> str:
    """单行化 + 去强调符号 + 超长截断；保证同一份 style 的落盘格式稳定。"""
    text = _flatten_text(value).strip().strip("`*_").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _tier_from_key(tier_key: str) -> dict | None:
    """把 "14-16" / "45+" 还原成 {"min","max"}，供 lifespan_core.cache_key 用。

    导入导出只带稳定的 tier_key，避免把整个档位文案一起带出去；
    cache_key 的内部算法又只认 {"min","max"}，所以这里需要无损还原。
    """
    text = str(tier_key or "").strip()
    match = _TIER_KEY_RE.match(text)
    if match:
        low, high = int(match.group(1)), int(match.group(2))
        if low > high:
            return None
        return {"min": low, "max": high}
    match = _TIER_OPEN_RE.match(text)
    if match:
        return {"min": int(match.group(1)), "max": None}
    return None


def _tier_sort_key(tier_key: str) -> tuple[int, int]:
    tier = _tier_from_key(tier_key) or {"min": 0, "max": None}
    high = tier.get("max")
    return (int(tier.get("min", 0)), 9999 if high is None else int(high))


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _style_or_none(value: Any) -> dict | None:
    """把任意输入洗成 {"tone","manner","avoid","continuity"}；tone/manner 全空返回 None。"""
    if not isinstance(value, dict):
        return None
    style = {key: _clean_style_field(value.get(key)) for key in STYLE_KEYS}
    if not style["tone"] and not style["manner"]:
        return None
    return style


# ==================================================================== 多角色生日
def _extract_birthday_raw(value: Any) -> str:
    """从 persona_birthdays 的值里取出日期原文。

    兼容三种写法（配置面板手填容易出前两种）：
      "2012-02-08" / "02-08"
      {"birthday": "2012-02-08"} / {"date": ...} / {"birth": ...}
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("birthday", "birth", "date", "value"):
            text = _extract_birthday_raw(value.get(key))
            if text:
                return text
    return ""


def resolve_persona_birthday(
    cfg: dict,
    persona_id: str | None,
    today: date,
    anchor_age: int,
    anchor_year: int | None = None,
) -> date | None:
    """算这个人格该用的生日：persona_birthdays 优先，其次全局 birthday。

    为什么按人格分开：同一个 bot 可能挂多个人格（不同设定、不同生日），
    全局只留一个 birthday 会让所有人格被同一个生日绑死。

    - 只写 MM-DD 时复用 lifespan_core.parse_birthday 的锚点年语义；
      anchor_year 由调用方（main.py 落盘）传入，保证年龄真的会涨；
      人格专用生日也吃同一个 anchor_year，除非它自己写了年份。
    - 人格专用值解析失败时**退回全局**而不是返回 None：配置面板里一个字符写错，
      不应该让这个角色直接失去注入；全局也解析不出来才返回 None。
    """
    parsed = _resolve_raw_birthday(
        _persona_birth_raw(cfg, persona_id), today, anchor_age, anchor_year
    )
    if parsed is not None:
        return parsed
    # 全局默认值：cfg 里没有 birthday 键时沿用插件默认（保持与 main.py 一致），
    # 键存在但为空字符串时视为用户主动关闭（parse_birthday("") 返回 None）。
    if isinstance(cfg, dict) and "birthday" in cfg:
        raw = cfg.get("birthday")
    else:
        raw = DEFAULT_BIRTHDAY
    return _resolve_raw_birthday(raw, today, anchor_age, anchor_year)


def _resolve_raw_birthday(
    raw: Any, today: date, anchor_age: int, anchor_year: int | None
) -> date | None:
    text = _extract_birthday_raw(raw)
    if not text:
        return None
    try:
        return core.parse_birthday(
            text,
            today=today,
            anchor_age=anchor_age,
            anchor_year=anchor_year,
        )
    except Exception as exc:  # noqa: BLE001 生日解析永远不许把调用方炸掉
        LOGGER.warning(f"[{PLUGIN_NAME}] 生日 {text!r} 解析失败：{exc!r}")
        return None


def _persona_birth_raw(cfg: dict, persona_id: str | None) -> str:
    """从 persona_birthdays 里找这个 persona 的生日原文；找不到返回空串。"""
    if not persona_id or not isinstance(cfg, dict):
        return ""
    table = cfg.get("persona_birthdays")
    if not isinstance(table, dict):
        return ""
    pid = str(persona_id).strip()
    if not pid:
        return ""
    candidates: list[Any] = []
    if pid in table:
        candidates.append(table[pid])
    else:
        # 配置面板里手输 key 时前后带空格很常见，宽松匹配一次。
        for key, value in table.items():
            if isinstance(key, str) and key.strip() == pid:
                candidates.append(value)
                break
    for candidate in candidates:
        text = _extract_birthday_raw(candidate)
        if text:
            return text
    return ""


# ==================================================================== 档位锁定
# 没识别出人格 id 时的统一桶名：main.py 里多处是 `persona_id or ""`，
# 空字符串如果直接 raise 会把「人设取不到」变成命令/注入抛异常；统一落一个
# "unknown" 桶，同一次运行里未识别人格的锁定还能互相复用。
UNKNOWN_PERSONA = "unknown"


class LockStore:
    """「把某一档的文案钉死」的小存储：`<data_dir>/locks.json`。

    为什么不用 state.json 的 cache：cache 的 key 含人格提示词哈希，
    用户改一次人设，生成缓存全部失效；用户手动锁定的那份文案不该跟着消失。
    所以锁定单独存，注入时 Lock 优先于缓存（lock_overrides_cache 配置决定是否启用）。

    存储格式（人可读、可手改）：
        {
          "version": 1,
          "locks": {
            "<persona_id>": {
              "<tier_key>": {"style": {...}, "locked_at": "2026-10-24T13:58:19"}
            }
          }
        }

    读盘策略：每次操作都重新读文件。插件重载会 new 一个 LockStore，进程内缓存
    反而容易出现「命令改了锁、注入实例看不到」的割裂；小 JSON 文件读一次很便宜。
    写盘先写 .tmp 再 os.replace，进程被杀不会留下半个 JSON。
    """

    FILE_NAME = "locks.json"
    VERSION = 1

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / self.FILE_NAME
        self._mutex = threading.Lock()

    # ---- 读写底层
    def _load(self) -> dict:
        try:
            if self.path.stat().st_size > MAX_LOCKS_FILE_CHARS:
                LOGGER.warning(f"[{PLUGIN_NAME}] {self.path} 过大，按空文件处理")
                return {"version": self.VERSION, "locks": {}}
        except OSError:
            return {"version": self.VERSION, "locks": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            if self.path.exists():
                LOGGER.warning(f"[{PLUGIN_NAME}] 锁定文件损坏，按空文件处理：{exc!r}")
            return {"version": self.VERSION, "locks": {}}
        if not isinstance(raw, dict):
            return {"version": self.VERSION, "locks": {}}
        locks = raw.get("locks")
        if not isinstance(locks, dict):
            locks = {}
        # 清洗：只保留形如 {persona: {tier: {"style": {...}}}}} 的结构
        cleaned: dict[str, dict] = {}
        for persona, tiers in locks.items():
            if not isinstance(persona, str) or not persona.strip():
                continue
            if not isinstance(tiers, dict):
                continue
            persona_locks: dict[str, dict] = {}
            for tier, record in tiers.items():
                if not isinstance(tier, str) or not tier.strip():
                    continue
                if not isinstance(record, dict):
                    continue
                style = _style_or_none(record.get("style"))
                if style is None:
                    continue
                persona_locks[tier.strip()] = {
                    "style": style,
                    "locked_at": str(record.get("locked_at") or ""),
                }
            if persona_locks:
                cleaned[persona.strip()] = persona_locks
        return {"version": self.VERSION, "locks": cleaned}

    def _save(self, data: dict) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError as exc:
            LOGGER.warning(f"[{PLUGIN_NAME}] 锁定文件写盘失败：{exc!r}")

    @staticmethod
    def _check_ids(persona_id: str, tier_key: str | None = None) -> tuple[str, str | None]:
        """规范化 persona/tier。

        persona_id 允许空（main.py 拿不到人格时会传 ""）：统一归到 "unknown"，
        既不会让调用方抛异常，也不会把不同未知人格的锁混成一个人以外的样子。
        tier_key 不允许空：空档位没有意义，调用方一定传了 tier_key。
        """
        persona = str(persona_id or "").strip() or UNKNOWN_PERSONA
        if tier_key is None:
            return persona, None
        tier = str(tier_key).strip()
        if not tier:
            raise ValueError("tier_key 不能为空")
        return persona, tier

    # ---- 契约接口
    def get(self, persona_id: str, tier_key: str) -> dict | None:
        """取锁定记录；没有返回 None。返回的是副本，调用方改它不会污染存储。"""
        persona, tier = self._check_ids(persona_id, tier_key)
        with self._mutex:
            data = self._load()
            record = (data.get("locks", {}).get(persona) or {}).get(tier)
            if not isinstance(record, dict):
                return None
            style = record.get("style")
            if not isinstance(style, dict):
                return None
            return copy.deepcopy(
                {"style": style, "locked_at": str(record.get("locked_at") or "")}
            )

    def set(self, persona_id: str, tier_key: str, style: dict) -> None:
        """写入/覆盖锁定；style 至少要有 tone 或 manner 才算有效锁定。"""
        persona, tier = self._check_ids(persona_id, tier_key)
        cleaned = _style_or_none(style)
        if cleaned is None:
            raise ValueError("style 至少要包含 tone 或 manner 中一个非空字段")
        with self._mutex:
            data = self._load()
            locks = data.setdefault("locks", {})
            persona_locks = locks.setdefault(persona, {})
            persona_locks[tier] = {"style": cleaned, "locked_at": _now_iso()}
            self._save(data)

    def clear(self, persona_id: str, tier_key: str | None = None) -> int:
        """清除锁定：tier_key 为 None 时清掉该人格全部锁定。返回清除条数。"""
        persona, tier = self._check_ids(persona_id, tier_key)
        with self._mutex:
            data = self._load()
            locks = data.get("locks")
            if not isinstance(locks, dict):
                return 0
            persona_locks = locks.get(persona)
            if not isinstance(persona_locks, dict) or not persona_locks:
                return 0
            if tier is None:
                count = len(persona_locks)
                locks.pop(persona, None)
                if count:
                    self._save(data)
                return count
            if persona_locks.pop(tier, None) is None:
                return 0
            if not persona_locks:
                locks.pop(persona, None)
            self._save(data)
            return 1

    def is_locked(self, persona_id: str, tier_key: str) -> bool:
        return self.get(persona_id, tier_key) is not None

    # ---- 给指令/页面用的小查询（不属于 SPEC 契约，main.py 可按需使用）
    def list_persona(self, persona_id: str) -> list[str]:
        """列出某人格已锁定的档位 key（按档位排序）。"""
        persona = str(persona_id or "").strip()
        if not persona:
            return []
        with self._mutex:
            data = self._load()
            persona_locks = data.get("locks", {}).get(persona) or {}
            return sorted(persona_locks, key=_tier_sort_key)


# ==================================================================== 注入强度
def resolve_injection_options(cfg: dict) -> dict:
    """把「注入强度」配置翻译成 build_block 裁剪需要的字段。

    返回 {"tone": bool, "manner": bool, "avoid": bool, "max_chars": int}：
    - tone/manner/avoid 对应注入块里的三类风格行，配置关掉就从块里删掉；
    - max_chars 是整个注入块的字符上限（0 表示不限制），由 apply_injection_options 执行。
    """
    return {
        "tone": _as_bool(_cfg_get(cfg, "inject_tone", True), True),
        "manner": _as_bool(_cfg_get(cfg, "inject_manner", True), True),
        "avoid": _as_bool(_cfg_get(cfg, "inject_avoid", True), True),
        "max_chars": _as_int(
            _cfg_get(cfg, "max_inject_chars", DEFAULT_MAX_INJECT_CHARS),
            DEFAULT_MAX_INJECT_CHARS,
            minimum=0,
            maximum=MAX_INJECT_CHARS,
        ),
    }


def apply_injection_options(block: str, options: dict | None = None) -> str:
    """按 resolve_injection_options 的结果裁剪一段注入块（纯字符串处理）。

    单独提出来是为了让 main.py 不用知道注入块的行格式：
    - 整行匹配「说话的口吻：/行为与情绪：/这一档别写成：」的风格行按开关删除；
    - 「与上一档相比：」是承上启下的差异句，只有 tone 和 manner 都保留时才留；
    - 第一行（BLOCK_MARK 标记行）永远保留，否则 system_prompt 模式下会重复追加；
    - max_chars>0 时按整行截断并补一行省略提示，避免把话砍在半句上。
    """
    if not isinstance(block, str):
        return ""
    text = block.strip("\n")
    if not text:
        return ""
    opts = options if isinstance(options, dict) else resolve_injection_options({})
    tone = _as_bool(opts.get("tone", True), True)
    manner = _as_bool(opts.get("manner", True), True)
    avoid = _as_bool(opts.get("avoid", True), True)
    keep = {"tone": tone, "manner": manner, "avoid": avoid, "continuity": tone and manner}

    lines = text.split("\n")
    kept = [lines[0]]
    for line in lines[1:]:
        key = None
        for prefix, mapped in _STYLE_LINE_KEYS:
            if line.startswith(prefix):
                key = mapped
                break
        if key is not None and not keep.get(key, True):
            continue
        kept.append(line)

    max_chars = _as_int(
        opts.get("max_chars", DEFAULT_MAX_INJECT_CHARS),
        DEFAULT_MAX_INJECT_CHARS,
        minimum=0,
        maximum=MAX_INJECT_CHARS,
    )
    if max_chars <= 0 or len("\n".join(kept)) <= max_chars:
        return "\n".join(kept)
    return _truncate_lines(kept, max_chars)


def _truncate_lines(lines: list[str], limit: int) -> str:
    """按整行截断，保留第一行（BLOCK_MARK），并保证最终长度 <= limit。

    硬保证长度上限很重要：max_inject_chars 是用户用来防上下文被挤爆的，
    截断结果如果还能超限就等于没截。第一行本身就超长时才允许砍字。
    """
    note = "…（注入内容超过 max_inject_chars，后续已省略）"
    if len(lines[0]) > limit:
        if limit <= 1:
            return lines[0][:limit]
        return lines[0][: limit - 1].rstrip() + "…"
    # 预留省略提示占用的空间，再按整行装填
    available = limit - len(note) - 1
    if available < len(lines[0]):
        # 连「首行 + 提示」都放不下：只留首行的前缀，末尾用省略号
        if limit <= 1:
            return lines[0][:limit]
        return lines[0][: limit - 1].rstrip() + "…"
    out = [lines[0]]
    used = len(lines[0])
    for line in lines[1:]:
        addition = 1 + len(line)
        if used + addition > available:
            out.append(note)
            break
        out.append(line)
        used += addition
    return "\n".join(out)


# ==================================================================== 导入导出
def export_styles(state: dict, profiles: dict) -> str:
    """把 state["cache"] 里的生成文案导出成可分享的 JSON 文本。

    导出只带走「能跨人格继续用」的部分：persona_id、tier_key、tier_label、
    style 原文、prompt_hash、template_version、generated_at。raw（模型原始回复）
    和 provider_id 不带——前者可能含人格原文片段，后者是本机配置，都不该外传。

    为什么带 prompt_hash：导入端用 (persona_id, tier, prompt_hash, template_version)
    重算 cache_key，正好能插回现有缓存结构；人格没变的话直接命中，人格变了也不会
    错误覆盖当前人格的缓存。
    """
    tier_labels: dict[str, str] = {}
    if isinstance(profiles, dict):
        for tier in profiles.get("tiers") or []:
            if not isinstance(tier, dict):
                continue
            try:
                key = core.tier_key(tier)
            except Exception:  # noqa: BLE001 档位配置坏条目跳过，不让导出整体失败
                continue
            tier_labels[key] = str(tier.get("label") or "")

    cache: dict = {}
    if isinstance(state, dict) and isinstance(state.get("cache"), dict):
        cache = state["cache"]

    entries: list[dict] = []
    for entry in cache.values():
        if not isinstance(entry, dict):
            continue
        style = _style_or_none(entry.get("style"))
        if style is None:
            continue
        persona_id = str(entry.get("persona_id") or "unknown").strip() or "unknown"
        tier = str(entry.get("tier") or "").strip()
        if not tier:
            continue
        generated_at = str(entry.get("generated_at") or "")
        try:
            age = int(entry.get("age")) if entry.get("age") is not None else None
        except (TypeError, ValueError):
            age = None
        prompt_hash = str(entry.get("prompt_hash") or "")
        raw_version = entry.get("template_version")
        try:
            template_version = (
                int(raw_version) if raw_version is not None else core.TEMPLATE_VERSION
            )
        except (TypeError, ValueError):
            template_version = core.TEMPLATE_VERSION
        entries.append(
            {
                "persona_id": persona_id,
                "tier": tier,
                "tier_label": str(entry.get("tier_label") or tier_labels.get(tier) or ""),
                "age": age,
                "prompt_hash": prompt_hash,
                "template_version": template_version,
                "generated_at": generated_at,
                "style": style,
            }
        )

    entries.sort(key=lambda item: (item["persona_id"], _tier_sort_key(item["tier"])))
    payload = {
        "version": EXPORT_VERSION,
        "plugin": PLUGIN_NAME,
        "exported_at": _now_iso(),
        "base_age": profiles.get("base_age") if isinstance(profiles, dict) else None,
        "entry_count": len(entries),
        "entries": entries,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def import_styles(raw: str) -> dict:
    """校验导入的 JSON 文本，返回可直接并入 state 的 {"cache": {...}}。

    坏输入一律 raise ValueError（调用方 catch 后回一句人话即可）。
    返回里除 cache 外还带 version/imported_at/count/warnings，方便 main.py 汇报
    「导入了 N 条、M 条警告」；只关心缓存的话取 result["cache"]。
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("导入文件不是 UTF-8 文本") from exc
    if not isinstance(raw, str):
        raise ValueError("导入内容必须是 JSON 文本")
    if len(raw) > MAX_IMPORT_CHARS:
        raise ValueError(f"导入内容过大（上限 {MAX_IMPORT_CHARS} 字符）")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"导入内容不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）") from exc
    if not isinstance(data, dict):
        raise ValueError("导入内容必须是 JSON 对象")

    version = data.get("version")
    if version is None:
        raise ValueError("导入内容缺少 version 字段")
    if isinstance(version, bool) or not isinstance(version, (int, float, str)):
        raise ValueError(f"version 字段类型不对：{type(version).__name__}")
    try:
        version_int = int(version)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"version 字段不是整数：{version!r}") from exc
    if version_int > EXPORT_VERSION:
        raise ValueError(
            f"不支持的导出版本 {version_int}（本插件支持 ≤ {EXPORT_VERSION}，请升级插件）"
        )
    if version_int <= 0:
        raise ValueError(f"version 字段必须为正整数：{version_int}")

    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ValueError("导入内容缺少 entries 数组")
    if not entries:
        raise ValueError("entries 为空，没有可导入的档位文案")

    warnings: list[str] = []
    cache: dict = {}
    for index, item in enumerate(entries, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index} 条不是 JSON 对象")
        persona_id = str(item.get("persona_id") or "").strip()
        if not persona_id:
            raise ValueError(f"第 {index} 条缺少 persona_id")
        tier = str(item.get("tier") or "").strip()
        if not tier:
            raise ValueError(f"第 {index} 条缺少 tier")
        tier_obj = _tier_from_key(tier)
        if tier_obj is None:
            raise ValueError(
                f"第 {index} 条 tier 格式不对：{tier!r}（应形如 14-16 或 45+）"
            )
        style = _style_or_none(item.get("style"))
        if style is None:
            raise ValueError(f"第 {index} 条的 style 里 tone/manner 都是空")

        prompt_hash = str(item.get("prompt_hash") or "").strip()
        if not prompt_hash:
            prompt_hash = "imported"
            warnings.append(
                f"第 {index} 条没有 prompt_hash，导入后要等重新生成或手动锁定才生效"
            )
        raw_version = item.get("template_version")
        try:
            template_version = (
                int(raw_version) if raw_version is not None else core.TEMPLATE_VERSION
            )
        except (TypeError, ValueError):
            template_version = core.TEMPLATE_VERSION
            warnings.append(f"第 {index} 条 template_version 不是整数，按当前模板版本处理")
        generated_at = str(item.get("generated_at") or "") or _now_iso()
        try:
            age = int(item.get("age")) if item.get("age") is not None else None
        except (TypeError, ValueError):
            age = None

        try:
            cache_key = core.cache_key(persona_id, tier_obj, prompt_hash, template_version)
        except Exception as exc:  # noqa: BLE001 cache_key 只是哈希，理论上不会炸
            raise ValueError(f"第 {index} 条的缓存 key 计算失败：{exc}") from exc
        if cache_key in cache:
            warnings.append(f"第 {index} 条与前面的条目重复（同人格同档位），后者覆盖前者")
        cache[cache_key] = {
            "persona_id": persona_id,
            "tier": tier,
            "tier_label": str(item.get("tier_label") or ""),
            "age": age,
            "prompt_hash": prompt_hash,
            "template_version": template_version,
            "provider_id": "imported",
            "generated_at": generated_at,
            "style": style,
            "raw": "",
            "generation_id": f"{persona_id}|{prompt_hash}|{template_version}",
            "imported_at": _now_iso(),
        }

    return {
        "cache": cache,
        "version": version_int,
        "count": len(cache),
        "imported_at": _now_iso(),
        "warnings": warnings,
    }


def merge_cache(state: dict, imported_cache: dict) -> int:
    """把导入的 cache 合并进 state（不落盘，落盘由 main.py 的 _save_state 负责）。

    返回实际新增/覆盖的条目数。单独提出来是因为 main.py 的 state 是 dict 子类，
    可能还带 AstrBotConfig 的 save_config 行为，这里只碰 "cache" 键。
    """
    if not isinstance(state, dict) or not isinstance(imported_cache, dict):
        return 0
    cache = state.setdefault("cache", {})
    if not isinstance(cache, dict):
        state["cache"] = {}
        cache = state["cache"]
    count = 0
    for key, entry in imported_cache.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        cache[key] = entry
        count += 1
    return count


# ==================================================================== 季节/时间感
# 每个月份属于哪个季节，以及这个季节的起点月（冬天从 12 月开始）。
_SEASON_START_MONTH = {
    3: 3, 4: 3, 5: 3,     # 春
    6: 6, 7: 6, 8: 6,     # 夏
    9: 9, 10: 9, 11: 9,   # 秋
    12: 12, 1: 12, 2: 12,  # 冬
}


def _season_ordinal(birth: date, today: date) -> int:
    """她正在过的是人生第几个这个季节。

    不能简单用「今年 - 出生年 + 1」：冬天跨年，12 月出生的人 1 月份其实还在
    同一个人生第一个冬天。这里直接数「出生之后、今天之前，这个季节开始过几次」，
    跨年/闰年/出生月边界都自然成立。
    """
    start_month = _SEASON_START_MONTH.get(today.month)
    if start_month is None:  # pragma: no cover - 月份一定 1..12
        return 1
    count = 0
    for year in range(birth.year, today.year + 1):
        try:
            start = date(year, start_month, 1)
        except ValueError:  # pragma: no cover
            continue
        if birth <= start <= today:
            count += 1
    return max(1, count)


def season_lines(today: date, birth: date) -> list[str]:
    """季节/时间感短句，最多 2 行，中文，留给注入块的末尾当「时间在走」的实感。

    第一行永远是季节 + 第几个：「入冬了，这是她过的第 15 个冬天。」
    第二行只在有意义时出现（生日前 30 天内倒计时）；生日当天不加，
    因为生日提示由 birthday_special / build_block 的生日行负责，避免一次注入三遍。

    不含任何日期/年龄以外的元信息（不写 ISO 日期、不写年份数字、不写档位名）：
    这段是给她「感觉到时间」用的，不是给模型做计算的。
    """
    if not isinstance(today, date) or not isinstance(birth, date):
        return []
    template = _SEASON_LINES.get(today.month)
    if template is None:  # pragma: no cover - date 的月份一定 1..12
        return []
    lines = [template.format(nth=_season_ordinal(birth, today))]
    try:
        age = core.compute_age(birth, today)
        if not core.is_birthday(birth, today):
            days = core.days_to_next_birthday(birth, today)
            if 0 < days <= 30:
                lines.append(f"再过 {days} 天，她就 {age + 1} 岁了。")
    except Exception as exc:  # noqa: BLE001 日期算不出来就只留季节行
        LOGGER.debug(f"[{PLUGIN_NAME}] season_lines 计算失败：{exc!r}")
    return lines[:2]


# ==================================================================== 时间线
def _tier_since(birth: date, tier_min: int) -> date:
    """她第一次进入某档的日期：出生日期 + tier_min 年；2/29 生在平年按 3/1。"""
    if tier_min <= 0:
        return birth
    year = birth.year + tier_min
    try:
        return date(year, birth.month, birth.day)
    except ValueError:
        return date(year, 3, 1)


def timeline_entries(
    state: dict,
    profiles: dict,
    birth: date,
    today: date,
    *,
    persona_id: str | None = None,
    locks: "LockStore | None" = None,
) -> list[dict]:
    """她走到现在为止走过的年龄档时间线（按时间正序）。

    每档一条：
        {"age": 档位起始年龄, "tier": "14-16", "label": 档位名,
         "since": "2026-02-08"（第一次进入这档的日期）,
         "generated_at": "2026-10-24T13:58:19" | None（该档缓存生成时间）,
         "locked": bool（该档是否被 LockStore 钉死）, "current": bool}

    只在「已经走到」的档位里取（tier.min <= 当前年龄），未来的档不剧透。
    generated_at 取该档最新一条缓存：传了 persona_id 就只看这个人格的，
    没传就看全库最新（main.py 的 /age timeline 建议传 persona_id）。
    locks 可选：传 LockStore 才能判断 locked；不传时 locked 一律 False。
    """
    if not isinstance(birth, date) or not isinstance(today, date):
        return []
    try:
        current_age = max(0, core.compute_age(birth, today))
    except Exception:  # noqa: BLE001
        return []
    tiers = profile_tiers(profiles)
    if not tiers:
        return []

    cache: dict = {}
    if isinstance(state, dict) and isinstance(state.get("cache"), dict):
        cache = state["cache"]
    generated: dict[str, str] = {}
    for entry in cache.values():
        if not isinstance(entry, dict):
            continue
        tier = str(entry.get("tier") or "").strip()
        generated_at = str(entry.get("generated_at") or "")
        if not tier or not generated_at:
            continue
        if persona_id is not None and str(entry.get("persona_id") or "") != str(persona_id):
            continue
        previous = generated.get(tier)
        if previous is None or generated_at > previous:
            generated[tier] = generated_at

    current_tier = None
    try:
        current_tier = core.tier_key(core.pick_tier(profiles, current_age))
    except Exception:  # noqa: BLE001
        current_tier = None

    entries: list[dict] = []
    for tier in tiers:
        low = int(tier.get("min", 0) or 0)
        if low > current_age:
            continue
        key = core.tier_key(tier)
        locked = False
        if locks is not None and persona_id:
            try:
                locked = bool(locks.is_locked(str(persona_id), key))
            except Exception:  # noqa: BLE001 锁定文件坏了不该拖垮时间线
                locked = False
        entries.append(
            {
                "age": low,
                "tier": key,
                "label": str(tier.get("label") or ""),
                "since": _tier_since(birth, low).isoformat(),
                "generated_at": generated.get(key),
                "locked": locked,
                "current": key == current_tier,
            }
        )

    if not entries and tiers:
        # 配置档位从 14 起、角色才 10 岁：pick_tier 会回退到第一档，
        # 时间线也照这个口径给一条「还没正式进档」的说明，而不是空列表。
        tier = tiers[0]
        key = core.tier_key(tier)
        entries.append(
            {
                "age": int(tier.get("min", 0) or 0),
                "tier": key,
                "label": str(tier.get("label") or ""),
                "since": birth.isoformat(),
                "generated_at": generated.get(key),
                "locked": False,
                "current": True,
            }
        )
    return entries


def profile_tiers(profiles: dict) -> list[dict]:
    """读档位列表并按 min 排序；坏条目（缺可解析的 min）丢掉。"""
    if not isinstance(profiles, dict):
        return []
    tiers = profiles.get("tiers")
    if not isinstance(tiers, list):
        return []
    cleaned: list[dict] = []
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        try:
            int(tier.get("min", 0))
        except (TypeError, ValueError):
            continue
        cleaned.append(tier)
    return sorted(cleaned, key=lambda item: int(item.get("min", 0)))


# ==================================================================== 生日/里程碑特典
def birthday_special(today: date, birth: date, age: int, base_age: int) -> dict | None:
    """生日/里程碑当天的「特典」提示；没有则 None。

    为什么要有：生日当天模型如果只是照常说话，用户完全感觉不到「今天是特别的」。
    这里给一段极短的提示，main.py 可追加进注入块（milestone_line 配置控制）。

    返回 {"kind": "birthday"|"milestone", "label": "18 岁生日", "note": "..."}：
    - 生日 + 里程碑年龄（18/20/30/40/50/60/70/80/90/100）→ kind=milestone；
    - 普通生日 → kind=birthday；
    - 非生日但正好是第 1000/2000/3000/5000/10000 天 → kind=milestone；
    - 其它日子 → None（绝不每天都刷）。
    base_age 用于「正好站在人设起点年龄的生日」加一句区分说明，不影响判定。
    """
    if not isinstance(today, date) or not isinstance(birth, date):
        return None
    try:
        effective_age = int(age)
    except (TypeError, ValueError):
        effective_age = core.compute_age(birth, today)
    effective_age = max(0, effective_age)
    try:
        base = max(0, int(base_age))
    except (TypeError, ValueError):
        base = 14

    if core.is_birthday(birth, today):
        label = f"{effective_age} 岁生日"
        if effective_age in MILESTONE_AGES:
            note = _MILESTONE_NOTES.get(effective_age, f"今天她 {effective_age} 岁生日。")
            return {"kind": "milestone", "label": label, "note": note}
        note = "今天是她的生日：说话可以带一点藏不住的高兴，但别主动开庆生大会、别总结人生、别许愿。"
        if effective_age == base:
            note += "（这正好是人设写的起点年龄，不用特别声明，照常演。）"
        return {"kind": "birthday", "label": label, "note": note}

    try:
        days = (today - birth).days
    except OverflowError:  # pragma: no cover
        days = -1
    if days in DAY_MILESTONES:
        return {
            "kind": "milestone",
            "label": f"第 {days} 天",
            "note": f"从她出生到现在整整 {days} 天。可以让时间感从一两句里漏出来，但别当纪念日办、别写总结。",
        }
    return None


# ==================================================================== 自检
def self_check() -> str:
    """不依赖 astrbot / 文件系统的关键路径自检（main.py 也可在启动时调用）。"""
    cfg = {
        "birthday": "2012-02-08",
        "persona_birthdays": {"digo": "2010-06-01", "only-md": "03-15"},
        "inject_tone": True,
        "inject_manner": False,
        "inject_avoid": "false",
        "max_inject_chars": 500,
    }
    today = date(2026, 10, 24)
    assert resolve_persona_birthday(cfg, "digo", today, 14) == date(2010, 6, 1)
    assert resolve_persona_birthday(cfg, "nobody", today, 14) == date(2012, 2, 8)
    assert resolve_persona_birthday(cfg, "only-md", today, 14) == date(2012, 3, 15)
    assert resolve_persona_birthday({}, "x", today, 14) == date(2012, 2, 8)

    options = resolve_injection_options(cfg)
    assert options == {"tone": True, "manner": False, "avoid": False, "max_chars": 500}, options
    trimmed = apply_injection_options(
        "【寿命论 · 内部状态，不要复述这段】\n角色：测试\n说话的口吻：短句\n行为与情绪：粘人\n这一档别写成：妈妈腔",
        options,
    )
    assert "说话的口吻：" in trimmed and "行为与情绪：" not in trimmed, trimmed

    lines = season_lines(date(2026, 12, 24), date(2012, 2, 8))
    assert len(lines) == 1 and "第 15 个冬天" in lines[0], lines
    assert season_lines(date(2027, 1, 20), date(2012, 2, 8))[1].startswith("再过")

    special = birthday_special(date(2027, 2, 8), date(2012, 2, 8), 15, 14)
    assert special and special["kind"] == "birthday" and special["label"] == "15 岁生日"
    assert birthday_special(date(2027, 2, 8), date(2009, 2, 8), 18, 14)["kind"] == "milestone"
    assert birthday_special(date(2026, 10, 24), date(2012, 2, 8), 14, 14) is None

    exported = export_styles(
        {
            "cache": {
                "k1": {
                    "persona_id": "digo",
                    "tier": "14-16",
                    "tier_label": "原样嚣张期",
                    "age": 14,
                    "prompt_hash": "abc123",
                    "template_version": 1,
                    "generated_at": "2026-10-24T13:00:00",
                    "style": {"tone": "短句", "manner": "粘人", "avoid": "别说教"},
                }
            }
        },
        {"tiers": [{"min": 14, "max": 16, "label": "原样嚣张期"}]},
    )
    imported = import_styles(exported)
    expected_key = core.cache_key("digo", {"min": 14, "max": 16}, "abc123", 1)
    assert expected_key in imported["cache"], imported
    return "ok"


if __name__ == "__main__":  # pragma: no cover
    print(self_check())
