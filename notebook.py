# -*- coding: utf-8 -*-
"""寿命论 · 只写小本本（notebook.py）。

这是「她自己的手账」，和 iris_memory 那类「记住并能随时想起」的记忆不同：
**只写不读**。agent 只能通过 main.py 注册的 `notebook_write` 工具往里写，插件不提供
任何读/搜工具；人和 agent 平时都翻不到，唯一出口是生日年报。这样它才不会变成第二个
"记忆库"，而是一年翻一次的私人物品。

设计要点（大多是为了"写歪了也能收拾"）：

1. 双份存储：`YYYY/YYYY-MM-DD.md` 是人能直接读/改的正文；`index.sqlite3` 只是索引
   （去重用的归一化文本、embedding、pinned、年报引用、媒体 kept 标记）。md 丢了结构化
   信息可以重建索引，DB 丢了可以从 md 全量重建——任一份都不许成为单点。
2. 去重是"先便宜后贵"：字面（NFKC 归一化 + 字符 n-gram Jaccard + SimHash 汉明距离）
   先拦掉绝大多数重复，再对剩下的候选做 embedding 余弦。命中就**合并**到旧条目
   （mention 计数 +1、标签/媒体并进来、更长的新版本可原位升级正文），绝不新增。
3. 约定类（commitment）自动 pinned。清理默认关闭；即使打开，pinned / commitment /
   含未来时间词或承诺词 / 被年报引用过 / 带"被选择媒体"的条目永不清理，且默认只归档。
4. 媒体只在「当前这条消息」里被 agent 显式选择时才会保存（不做全量捕获）：图片存
   缩略图（长边 ≤ `notebook_media_thumb_size`，顺带剥掉 EXIF 隐私），视频取封面帧；
   被选择过的媒体 `kept=True`，永不清理。原文件默认不保存（`notebook_media_keep_original`）。
5. 每日兜底总结走「分块 map → 多层 reduce → 终选」：先把当天会话按
   `notebook_summary_max_input_chars` 切块，每块只留与角色自身相关的素材，再合并压缩，
   最后只挑 1~2 条落盘。幂等靠写入去重兜底。LLM 不可用/调用失败时**静默跳过**，
   绝不因为总结失败影响对话或写坏数据。

本模块不 import astrbot：LLM 与 embedding 都由 main.py 以可调用对象注入，媒体组件只用
鸭子类型（`type` 属性/类名 + `convert_to_file_path()` / `get_file()`），所以单测不需要
真实 AstrBot 环境。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import re
import shutil
import sqlite3
import threading
import unicodedata
import uuid
from array import array
from datetime import date, datetime, timedelta
from pathlib import Path

from typing import Any, Awaitable, Callable, Iterator

try:  # 日志统一走 AstrBot 的 logger（平台安全审查要求，禁止使用标准库日志模块）
    from .log_bridge import get_logger
except ImportError:  # pragma: no cover - 单测直接 import notebook 的场景
    from log_bridge import get_logger  # type: ignore[no-redef]

__all__ = ["Notebook", "KINDS", "MEDIA_TYPES", "SCHEMA_VERSION"]


# 索引结构变化时 +1；仅用于诊断/未来迁移，不做自动迁移。
SCHEMA_VERSION = 1

DB_FILE = "index.sqlite3"

#: 小本本条目分类（与 SPEC_V1 §4.2 一致）。用于统计和"约定永不清理"。
KINDS = ("preference", "event", "mood", "person", "commitment", "other")

#: 可被 agent 选择附加的媒体类型。
MEDIA_TYPES = ("image", "video", "file", "record")

#: 中文 kind 别名（工具参数被模型写成中文时兜底，别让一条笔记掉成 other）。
KIND_ALIASES = {
    "喜好": "preference",
    "喜欢": "preference",
    "偏好": "preference",
    "事件": "event",
    "做过": "event",
    "经历": "event",
    "心情": "mood",
    "情绪": "mood",
    "状态": "mood",
    "人物": "person",
    "在意": "person",
    "关系": "person",
    "约定": "commitment",
    "承诺": "commitment",
    "计划": "commitment",
    "其他": "other",
    "其它": "other",
}

# ---------------------------------------------------------------- 配置默认值（SPEC §6）
DEFAULT_DUP_THRESHOLD = 0.92
DEFAULT_CLEANUP_IDLE_DAYS = 180
DEFAULT_CLEANUP_MODE = "archive"
DEFAULT_SUMMARY_MAX_INPUT_CHARS = 12000
DEFAULT_SUMMARY_MAX_CHARS_PER_NOTE = 600
DEFAULT_MEDIA_THUMB_SIZE = 720
DEFAULT_MEDIA_KEEP_ORIGINAL = False

# ---------------------------------------------------------------- 内部上限（避免单条写入把磁盘/上下文打爆）
MAX_NOTE_CHARS = 2000
MAX_TAGS = 8
MAX_TAG_CHARS = 24
MAX_MEDIA_COPY_BYTES = 100 * 1024 * 1024  # 单个文件超 100MB 不复制到插件数据目录
MAX_SUMMARY_MESSAGES = 3000
MAX_SUMMARY_MSG_CHARS = 800
MAX_MAP_CHUNKS = 24
MAX_REDUCE_LAYERS = 4
MAX_FACTS_PER_CHUNK = 8
DEDUPE_CANDIDATE_LIMIT = 300
EMBED_MAX_BATCH = 32
REINDEX_EMBED_LIMIT = 200

# 字面去重阈值：n-gram Jaccard 直接判重；SimHash 汉明距离只在对得上的前提下兜底
# （短中文串上 SimHash 很敏感：换一个词可能汉明距离就是 0，所以还要求 Jaccard 够高，
#  否则「她喜欢猫 / 她喜欢狗」会被误合并）。
LITERAL_JACCARD_THRESHOLD = 0.82
LITERAL_SIMHASH_DISTANCE = 2
LITERAL_SIMHASH_MIN_JACCARD = 0.6
LITERAL_CONTAINMENT_RATIO = 0.6

# 清理保护词：含未来时间词或承诺词的条目永不清（设计底稿 §二）。
_FUTURE_WORDS = (
    "明天", "下次", "下回", "下周", "下个月", "明年", "以后", "将来", "未来",
    "等我", "等我们", "总有一天", "到时候", "改天", "过几天", "过年",
    "生日那天", "退休", "长大", "变老", "老去",
)
_COMMITMENT_WORDS = (
    "答应", "约定", "说好", "约好", "记得", "要去", "想去", "计划",
    "承诺", "一定", "别忘", "别忘记", "会去",
)

# ---------------------------------------------------------------- 每日兜底总结提示词
# 提示词刻意角色无关、不含私人称呼/路径（插件要公开发布）。
SUMMARY_MAP_SYSTEM = """你是「寿命论」插件的小本本整理员：把一段聊天记录压成「角色自己的手账素材」。

只保留与角色本人有关、且长期有用的内容：
- 她的喜好、厌恶、习惯（preference）
- 她做过的事、经历过的事（event）
- 她的心情、状态、身体感受（mood）
- 她在意的人、和对方的关系（person）
- 她说定、答应或计划过的事（commitment）

一律丢弃：
- 对方自己的私事（除非内容直接关于她）；
- 寒暄、表情、语气词、一次性的指令；
- 命令、工具调用、系统提示、代码、JSON、链接；
- 他人的隐私（住址、电话、账号、真实姓名、证件号等）；
- 重复内容，以及没有把握的推测。

转成第三人称短句（用「她」），每条 15~80 字，不复述原话，不加评论。
只输出 JSON：{"facts": ["...", "..."]}；没有可保留的就输出 {"facts": []}。
不要 markdown 代码块，不要解释。"""

SUMMARY_REDUCE_SYSTEM = """你是「寿命论」插件的小本本整理员：把多段「角色手账素材」合并成更短的一版。

规则：合并重复、丢掉寒暄和一次性信息、只保留与角色本人有关且长期有用的内容；
每条 15~80 字，第三人称，用「她」。
只输出 JSON：{"facts": ["...", "..."]}。不要 markdown 代码块，不要解释。"""

SUMMARY_FINAL_SYSTEM = """你是「寿命论」插件的小本本整理员：从候选事实里挑出最值得写进她私人手账的 1~2 条。

要求：
- 只写与角色本人有关、长期有用、以后翻起来还有味道的事；
- 第三人称短句，用「她」，每条不超过 %MAX_CHARS% 字；不复述原话，不加评论；
- 同一天尽量一条「事/心情」+ 一条「喜好/在意的人/约定」，不要为凑数硬写；
- kind 只能是 preference/event/mood/person/commitment/other；
- tags 是 1~3 个短词，没有就给空数组。

只输出 JSON：{"notes": [{"content": "...", "kind": "event", "tags": ["..."]}]}；没有值得写的就输出 {"notes": []}。
不要 markdown 代码块，不要解释。"""

SUMMARY_MAP_USER = """【今天】%TODAY%
【片段】第 %INDEX%/%TOTAL% 块，共 %CHARS% 字
【聊天记录】
%CHUNK%"""

SUMMARY_REDUCE_USER = """【今天】%TODAY%
【待合并素材】第 %INDEX%/%TOTAL% 组
%CHUNK%"""

SUMMARY_FINAL_USER = """【今天】%TODAY%
【候选事实】
%FACTS%"""

# ---------------------------------------------------------------- 正则
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)
_JSON_ARR_RE = re.compile(r"\[.*\]", re.DOTALL)
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)
_FRONT_DATE_RE = re.compile(r"^date\s*:\s*(\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)
_NOTE_BLOCK_RE = re.compile(
    r"<!--\s*note\s*:\s*(\{.*?\})\s*-->\s*\n?(.*?)\n?\s*<!--\s*/note\s*-->",
    re.DOTALL,
)
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    day TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    content TEXT NOT NULL,
    normalized TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'other',
    tags TEXT NOT NULL DEFAULT '[]',
    pinned INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'agent',
    mentions INTEGER NOT NULL DEFAULT 1,
    archived INTEGER NOT NULL DEFAULT 0,
    embedding BLOB,
    media TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_notes_day ON notes(day);
CREATE INDEX IF NOT EXISTS idx_notes_archived ON notes(archived);
CREATE TABLE IF NOT EXISTS media (
    id TEXT PRIMARY KEY,
    note_id TEXT,
    day TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'file',
    path TEXT NOT NULL DEFAULT '',
    thumb TEXT NOT NULL DEFAULT '',
    kept INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_media_day ON media(day);
CREATE INDEX IF NOT EXISTS idx_media_note ON media(note_id);
CREATE TABLE IF NOT EXISTS note_uses (
    note_id TEXT NOT NULL,
    report_date TEXT NOT NULL,
    used_at TEXT NOT NULL,
    PRIMARY KEY (note_id, report_date)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# ==================================================================== 小工具（纯函数）


def _today_default() -> date:
    """模块内部兜底用的"今天"。

    模块不主动取系统时间：main.py 应该把 today/date 显示传进来（单测可注入）。
    只有 `add(date=None)` 和媒体捕获这种接口本身没有日期参数的地方才用这个兜底。
    """
    return datetime.now().date()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _coerce_day(value: Any) -> date:
    """把 date/datetime/'YYYY-MM-DD' 统一成 date；非法输入退回今天（永远不抛）。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is not None:
        text = str(value).strip()
        if _DAY_RE.match(text):
            try:
                return date.fromisoformat(text)
            except ValueError:
                pass
    return _today_default()


def _truncate(text: str, limit: int) -> str:
    text = str(text or "")
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _normalize_text(text: str) -> str:
    """去重用的归一化：全半角统一、小写、去空白/标点/符号/控制字符。

    中文按字保留；英文/数字保留。这样「她、喜欢猫」和「她喜欢猫！」会变成同一串。
    """
    raw = unicodedata.normalize("NFKC", str(text or "")).lower()
    out: list[str] = []
    for ch in raw:
        if ch in " \t\r\n\u3000":
            continue
        try:
            category = unicodedata.category(ch)[0]
        except (TypeError, ValueError):
            continue
        # P=标点 Z=分隔 S=符号 C=控制/格式；其余（字母/数字/组合符号）保留
        if category in ("P", "Z", "S", "C"):
            continue
        out.append(ch)
    return "".join(out)


def _char_ngrams(text: str, n: int = 2) -> set[str]:
    if not text:
        return set()
    if len(text) <= n:
        return {text}
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    inter = len(left & right)
    if not inter:
        return 0.0
    return inter / float(len(left | right))


def _simhash64(text: str) -> int:
    """64 位 SimHash：字符 bigram 的加权投票。纯 Python，够小体量用。"""
    tokens = _char_ngrams(text) or ({text} if text else set())
    if not tokens:
        return 0
    weights = [0] * 64
    for token in tokens:
        digest = hashlib_blake2b(token)
        for bit in range(64):
            weights[bit] += 1 if (digest >> bit) & 1 else -1
    result = 0
    for bit, weight in enumerate(weights):
        if weight > 0:
            result |= 1 << bit
    return result


def hashlib_blake2b(token: str) -> int:
    """bigram → 64 位整数。独立成函数便于测试替换。"""
    import hashlib

    return int.from_bytes(
        hashlib.blake2b(token.encode("utf-8", "ignore"), digest_size=8).digest(),
        "big",
    )


def _hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def _literal_similarity(
    new_norm: str,
    candidate_norm: str,
    new_grams: set[str] | None = None,
    new_hash: int | None = None,
) -> tuple[float, bool]:
    """字面预筛。返回 (相似度, 是否判重)。

    判断顺序：
    1. 归一化后完全一样 → 1.0；
    2. 短串完整包含在长串里且长度比 ≥ 0.6（"她喜欢草莓" ⊂ "她喜欢草莓，尤其是冬天"）→ 0.92；
    3. 字符 bigram Jaccard ≥ 0.82 → 判重；
    4. SimHash 汉明距离 ≤ 2 且 Jaccard ≥ 0.45 → 判重（兜拼音/换词序类改写）。
    """
    if not new_norm or not candidate_norm:
        return 0.0, False
    if new_norm == candidate_norm:
        return 1.0, True
    short, long_ = sorted((new_norm, candidate_norm), key=len)
    if (
        len(short) >= 8
        and short in long_
        and (len(short) / float(len(long_))) >= LITERAL_CONTAINMENT_RATIO
    ):
        return 0.92, True
    grams = new_grams if new_grams is not None else _char_ngrams(new_norm)
    jac = _jaccard(grams, _char_ngrams(candidate_norm))
    if jac >= LITERAL_JACCARD_THRESHOLD:
        return jac, True
    if (
        new_hash is not None
        and jac >= LITERAL_SIMHASH_MIN_JACCARD
        and _hamming(new_hash, _simhash64(candidate_norm)) <= LITERAL_SIMHASH_DISTANCE
    ):
        return max(jac, 0.9), True
    return jac, False


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = norm_left = norm_right = 0.0
    for a, b in zip(left, right):
        dot += a * b
        norm_left += a * a
        norm_right += b * b
    if norm_left <= 0.0 or norm_right <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_left * norm_right)


def _pack_vector(vector: list[float]) -> bytes | None:
    try:
        return array("f", [float(x) for x in vector]).tobytes()
    except (TypeError, ValueError, OverflowError):
        return None


def _unpack_vector(blob: Any) -> list[float] | None:
    if not blob:
        return None
    try:
        values = array("f")
        values.frombytes(bytes(blob))
    except (TypeError, ValueError):
        return None
    result = list(values)
    if not result or any(not math.isfinite(x) for x in result):
        return None
    return result


def _normalize_kind(kind: Any) -> str:
    text = str(kind or "").strip().lower()
    if text in KINDS:
        return text
    if not text:
        return "other"
    if text in KIND_ALIASES:
        return KIND_ALIASES[text]
    return "other"


def _clean_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        raw_items = re.split(r"[,，、;；\s]+", tags)
    elif isinstance(tags, (list, tuple, set)):
        raw_items = list(tags)
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = "".join(
            ch for ch in str(item or "").replace("<!--", "").replace("-->", "")
            if ch not in "\r\n\t"
        ).strip()
        if not text:
            continue
        text = _truncate(text, MAX_TAG_CHARS)
        key = _normalize_text(text)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= MAX_TAGS:
            break
    return out


def _cfg_int(cfg: dict, key: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        value = int(cfg.get(key, default))
    except (AttributeError, TypeError, ValueError):
        value = default
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def _cfg_float(cfg: dict, key: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    try:
        value = float(cfg.get(key, default))
    except (AttributeError, TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def _cfg_bool(cfg: dict, key: str, default: bool) -> bool:
    try:
        value = cfg.get(key, default)
    except AttributeError:
        return default
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "开", "开启", "是"}


def _cfg_str(cfg: dict, key: str, default: str) -> str:
    try:
        value = cfg.get(key, default)
    except AttributeError:
        return default
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _fill_template(template: str, values: dict[str, str]) -> str:
    """用 %KEY% 占位符填模板，避免 str.format 被正文里的花括号打爆。"""
    text = str(template or "")
    for key, value in values.items():
        text = text.replace(f"%{key}%", str(value))
    return text


def _extract_json(text: Any) -> Any:
    """从模型回复里尽量抠出 JSON：纯 JSON / ```json 围栏 / 夹在解释文字里。"""
    if not text or not str(text).strip():
        return None
    raw = str(text).strip()
    candidates: list[str] = []
    fenced = _FENCE_RE.search(raw)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(raw)
    obj = _JSON_OBJ_RE.search(raw)
    if obj:
        candidates.append(obj.group(0))
    arr = _JSON_ARR_RE.search(raw)
    if arr:
        candidates.append(arr.group(0))
    for candidate in candidates:
        try:
            return json.loads(candidate.strip())
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _split_text(text: str, budget: int) -> list[str]:
    """把一段文本切成 ≤ budget 的碎片，优先在换行处切。"""
    budget = max(1, int(budget))
    text = str(text or "")
    if len(text) <= budget:
        return [text] if text else []
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(line) > budget:
            if current:
                chunks.append(current)
                current = ""
            for start in range(0, len(line), budget):
                chunks.append(line[start : start + budget])
            continue
        if not current:
            current = line
        elif len(current) + 1 + len(line) <= budget:
            current = current + "\n" + line
        else:
            chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


def _chunk_blocks(blocks: list[str], budget: int) -> list[str]:
    """把若干块文本按 budget 紧凑装箱（块内部超长会先拆开）。"""
    budget = max(1, int(budget))
    chunks: list[str] = []
    current = ""
    for block in blocks:
        for piece in _split_text(str(block or "").strip(), budget):
            if not current:
                current = piece
            elif len(current) + 2 + len(piece) <= budget:
                current = current + "\n\n" + piece
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


def _resolve_local_ref(value: Any) -> str:
    """把 file:// URI / 本地路径 / url 统一成本地绝对路径；取不到就返回空串。

    只认本地文件：http(s)/base64/data 一律返回空串（本模块不在捕获阶段做网络下载，
    网络素材由 AstrBot 组件的 convert_to_file_path() 自己负责落盘）。
    """
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://", "base64://", "data:")):
        return ""
    if text.startswith("file://"):
        from urllib.parse import unquote, urlparse

        parsed = urlparse(text)
        path_text = unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:/", path_text):
            path_text = path_text[1:]
        candidate = Path(path_text)
    else:
        candidate = Path(text)
    try:
        if candidate.is_file():
            return str(candidate.resolve())
    except OSError:
        return ""
    return ""


_COMPONENT_KIND_MAP = {
    "image": "image",
    "img": "image",
    "picture": "image",
    "photo": "image",
    "video": "video",
    "file": "file",
    "record": "record",
    "voice": "record",
    "audio": "record",
}


def _component_kind(component: Any) -> str | None:
    """鸭子类型判断消息组件类型：优先 type 属性（ComponentType 是 str Enum），再看类名。"""
    raw = getattr(component, "type", None)
    value = getattr(raw, "value", raw)
    for candidate in (value, type(component).__name__):
        if not candidate:
            continue
        key = str(candidate).strip().lower()
        if key in _COMPONENT_KIND_MAP:
            return _COMPONENT_KIND_MAP[key]
    return None


async def _component_local_path(component: Any) -> str:
    """尽量把消息组件变成稳定的本地文件路径。

    AstrBot 4.29.1 源码事实（2026-10-24 经 v1-verifier 核实）：
    - Image / Video / Record 有 `async convert_to_file_path()`；
    - File 没有 convert_to_file_path，只有 `async get_file()`；
    - 平台适配后组件常常已经带本地 `path` / `file_`，优先用它们，省一次下载。

    所以这里先探属性、再探 async 转换，全部 try/except 兜住，并保持鸭子类型
    （不 import astrbot），single 媒体失败不影响整次捕获。
    """
    # 1) 组件已带本地路径：直接拿，避免无谓的下载/转换。
    #    注意 cover 是视频封面，不是媒体文件本身，不能在这里当主路径。
    for attr in ("path", "file_", "file", "url"):
        path = _resolve_local_ref(getattr(component, attr, None))
        if path:
            return path
    # 2) 按源码事实探测 async 转换：convert_to_file_path()（Image/Video/Record）
    #    → get_file()（File）。两者都可能不存在或抛异常，逐个兜。
    for attr in ("convert_to_file_path", "get_file"):
        func = getattr(component, attr, None)
        if not callable(func):
            continue
        try:
            result = func()
        except Exception:  # noqa: BLE001 组件取文件失败不该拖垮整次捕获
            continue
        if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
            try:
                result = await result
            except Exception:  # noqa: BLE001
                continue
        path = _resolve_local_ref(result)
        if path:
            return path
    return ""


def _safe_filename(name: Any, suffix: str) -> str:
    text = str(name or "").strip()
    cleaned = "".join(ch for ch in text if ch not in '\\/:*?"<>|\x00\r\n\t')
    cleaned = cleaned.strip().strip(".")
    cleaned = _truncate(cleaned, 60) or "file"
    if suffix and not cleaned.lower().endswith(suffix.lower()):
        cleaned += suffix
    return cleaned


def _merge_media_lists(existing: list[dict], incoming: list[dict]) -> list[dict]:
    """按 path/thumb 去重合并媒体列表（保持旧顺序，新条目追加）。"""
    merged: list[dict] = []
    seen: set[str] = set()
    for entry in list(existing or []) + list(incoming or []):
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("path") or entry.get("thumb") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    return merged


# ==================================================================== Notebook


class Notebook:
    """只写小本本。构造一次放在插件里复用；所有公开方法都是"失败也不炸对话"的。"""

    def __init__(
        self,
        data_dir: Path,
        cfg: dict,
        llm_call: Callable[[str, str], Awaitable[str]] | None = None,
        embed_fn: Callable[[list[str]], Awaitable[list[list[float]]]] | None = None,
        logger: Any | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.llm_call = llm_call
        self.embed_fn = embed_fn
        self.logger = get_logger(logger)
        self._lock = asyncio.Lock()
        self._db_lock = threading.RLock()
        self._bootstrapped = False
        self._warned_missing_llm = False
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # noqa: BLE001 数据目录建不出来时后续操作会各自报错并降级
            self.logger.warning("[notebook] 数据目录创建失败：%r", exc)

    # ------------------------------------------------------------------ 配置
    def _dup_threshold(self) -> float:
        return _cfg_float(self.cfg, "notebook_dup_threshold", DEFAULT_DUP_THRESHOLD, 0.0, 1.0)

    def _thumb_size(self) -> int:
        return _cfg_int(self.cfg, "notebook_media_thumb_size", DEFAULT_MEDIA_THUMB_SIZE, 64, 4096)

    def _keep_original(self) -> bool:
        return _cfg_bool(self.cfg, "notebook_media_keep_original", DEFAULT_MEDIA_KEEP_ORIGINAL)

    def _summary_max_input(self) -> int:
        return _cfg_int(
            self.cfg,
            "notebook_summary_max_input_chars",
            DEFAULT_SUMMARY_MAX_INPUT_CHARS,
            200,
            500_000,
        )

    def _summary_note_limit(self) -> int:
        return _cfg_int(
            self.cfg,
            "notebook_summary_max_chars_per_note",
            DEFAULT_SUMMARY_MAX_CHARS_PER_NOTE,
            20,
            MAX_NOTE_CHARS,
        )

    # ------------------------------------------------------------------ SQLite 基础
    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """每次操作开一个短连接：少一点跨线程/文件锁的坑，条目量小完全够用。"""
        with self._db_lock:
            conn = self._connect()
            try:
                yield conn
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    def _connect(self) -> sqlite3.Connection:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.data_dir / DB_FILE), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(_SCHEMA_SQL)
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            if not self._bootstrapped:
                self._bootstrapped = True
                count = conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()["n"]
                if not count:
                    self._bootstrap_from_markdown(conn)
            conn.commit()
        except Exception:
            conn.close()
            raise
        return conn

    def _bootstrap_from_markdown(self, conn: sqlite3.Connection) -> None:
        """DB 是空的但磁盘上还有 md：按 md 重建索引（用户手动备份/迁移后也能自愈）。"""
        try:
            active = sorted(self.data_dir.glob("[0-9][0-9][0-9][0-9]/*.md"))
            archived = sorted(self.data_dir.glob("archive/[0-9][0-9][0-9][0-9]/*.md"))
        except OSError:
            return
        for is_archived, files in ((False, active), (True, archived)):
            for path in files:
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                for note in self._parse_markdown(text):
                    try:
                        self._insert_note_row(conn, note, archived=is_archived)
                    except sqlite3.Error:
                        continue
        if active or archived:
            self.logger.info(
                "[notebook] 已从 markdown 重建索引：%d 个活跃文件 / %d 个归档文件",
                len(active),
                len(archived),
            )

    # ------------------------------------------------------------------ markdown 读/写
    def _md_path(self, day: str) -> Path:
        return self.data_dir / day[:4] / f"{day}.md"

    def _archive_md_path(self, day: str) -> Path:
        return self.data_dir / "archive" / day[:4] / f"{day}.md"

    @staticmethod
    def _escape_comment(text: str) -> str:
        return str(text or "").replace("<!--", "&lt;!--").replace("-->", "--&gt;")

    @staticmethod
    def _unescape_comment(text: str) -> str:
        return str(text or "").replace("&lt;!--", "<!--").replace("--&gt;", "-->")

    def _render_entry(self, row: sqlite3.Row) -> str:
        try:
            tags = json.loads(row["tags"] or "[]")
        except (json.JSONDecodeError, TypeError):
            tags = []
        try:
            media = json.loads(row["media"] or "[]")
        except (json.JSONDecodeError, TypeError):
            media = []
        meta = {
            "id": row["id"],
            "day": row["day"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "kind": row["kind"],
            "tags": tags if isinstance(tags, list) else [],
            "pinned": bool(row["pinned"]),
            "source": row["source"],
            "mentions": int(row["mentions"] or 1),
            "media": media if isinstance(media, list) else [],
        }
        payload = self._escape_comment(
            json.dumps(meta, ensure_ascii=False, sort_keys=True)
        )
        body = self._escape_comment(str(row["content"] or ""))
        return f"<!-- note: {payload} -->\n{body}\n<!-- /note -->"

    def _write_text_atomic(self, path: Path, text: str) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(str(text), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            self.logger.warning("[notebook] 写 markdown 失败（%s）：%r", path.name, exc)

    def _unlink_quietly(self, path: Path) -> None:
        try:
            if path.is_file():
                path.unlink()
        except OSError as exc:
            self.logger.debug("[notebook] 删除文件失败（%s）：%r", path, exc)

    def _write_day_markdown(self, conn: sqlite3.Connection, day: str) -> None:
        """把某天的活跃条目重写成一份人读 md；没有条目就删掉空文件。"""
        try:
            rows = conn.execute(
                "SELECT * FROM notes WHERE day = ? AND archived = 0 "
                "ORDER BY created_at ASC, id ASC",
                (day,),
            ).fetchall()
        except sqlite3.Error:
            return
        path = self._md_path(day)
        if not rows:
            self._unlink_quietly(path)
            return
        parts = [
            "---",
            f"date: {day}",
            f"count: {len(rows)}",
            f"updated_at: {_now_iso()}",
            "generator: lifespan_theory notebook v1",
            "---",
            "",
            f"# {day} · 小本本",
            "",
        ]
        for row in rows:
            parts.append(self._render_entry(row))
            parts.append("")
        self._write_text_atomic(path, "\n".join(parts).rstrip() + "\n")

    def _write_archive_markdown(self, conn: sqlite3.Connection, day: str) -> None:
        try:
            rows = conn.execute(
                "SELECT * FROM notes WHERE day = ? AND archived = 1 "
                "ORDER BY created_at ASC, id ASC",
                (day,),
            ).fetchall()
        except sqlite3.Error:
            return
        path = self._archive_md_path(day)
        if not rows:
            self._unlink_quietly(path)
            return
        parts = [
            "---",
            f"date: {day}",
            f"count: {len(rows)}",
            f"updated_at: {_now_iso()}",
            "archived: true",
            "generator: lifespan_theory notebook v1",
            "---",
            "",
            f"# {day} · 小本本（已归档）",
            "",
        ]
        for row in rows:
            parts.append(self._render_entry(row))
            parts.append("")
        self._write_text_atomic(path, "\n".join(parts).rstrip() + "\n")

    def _parse_markdown(self, text: str) -> list[dict]:
        """解析一份 md。坏条目（JSON 坏了/缺 id）跳过，不让一条脏数据毁掉整个索引。"""
        raw = str(text or "")
        front_date = ""
        front = _FRONTMATTER_RE.match(raw)
        if front:
            match = _FRONT_DATE_RE.search(front.group(1))
            if match:
                front_date = match.group(1)
        notes: list[dict] = []
        for match in _NOTE_BLOCK_RE.finditer(raw):
            meta_text = self._unescape_comment(match.group(1))
            try:
                meta = json.loads(meta_text)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(meta, dict):
                continue
            content = self._unescape_comment(match.group(2)).strip()
            if not content:
                continue
            day = str(meta.get("day") or front_date or "").strip()
            if not _DAY_RE.match(day):
                continue
            note_id = str(meta.get("id") or "").strip()
            if not note_id:
                continue
            tags = _clean_tags(meta.get("tags"))
            media = []
            raw_media = meta.get("media")
            if isinstance(raw_media, list):
                for entry in raw_media:
                    if not isinstance(entry, dict):
                        continue
                    media.append(
                        {
                            "type": str(entry.get("type") or "file")[:16],
                            "path": str(entry.get("path") or ""),
                            "thumb": str(entry.get("thumb") or ""),
                            "note": str(entry.get("note") or "")[:200],
                        }
                    )
            created_at = str(meta.get("created_at") or f"{day}T00:00:00")
            notes.append(
                {
                    "id": note_id,
                    "day": day,
                    "created_at": created_at,
                    "updated_at": str(meta.get("updated_at") or created_at),
                    "content": content,
                    "normalized": _normalize_text(content),
                    "kind": _normalize_kind(meta.get("kind")),
                    "tags": tags,
                    "pinned": 1 if meta.get("pinned") or _normalize_kind(meta.get("kind")) == "commitment" else 0,
                    "source": str(meta.get("source") or "agent")[:40],
                    "mentions": max(1, int(meta.get("mentions") or 1)),
                    "media": media,
                }
            )
        return notes

    def _insert_note_row(self, conn: sqlite3.Connection, note: dict, archived: bool) -> bool:
        note_id = str(note.get("id") or "").strip()
        if not note_id:
            return False
        try:
            conn.execute(
                "INSERT OR IGNORE INTO notes("
                "id, day, created_at, updated_at, content, normalized, kind, tags, "
                "pinned, source, mentions, archived, embedding, media"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
                (
                    note_id,
                    str(note.get("day") or ""),
                    str(note.get("created_at") or _now_iso()),
                    str(note.get("updated_at") or note.get("created_at") or _now_iso()),
                    str(note.get("content") or ""),
                    str(note.get("normalized") or _normalize_text(note.get("content"))),
                    _normalize_kind(note.get("kind")),
                    json.dumps(_clean_tags(note.get("tags")), ensure_ascii=False),
                    1 if note.get("pinned") else 0,
                    str(note.get("source") or "agent")[:40],
                    max(1, int(note.get("mentions") or 1)),
                    1 if archived else 0,
                    json.dumps(note.get("media") or [], ensure_ascii=False),
                ),
            )
        except (sqlite3.Error, TypeError, ValueError):
            return False
        for entry in note.get("media") or []:
            if not isinstance(entry, dict):
                continue
            self._insert_media_row(
                conn,
                note_id=note_id,
                day=str(note.get("day") or ""),
                media_type=str(entry.get("type") or "file"),
                path=str(entry.get("path") or ""),
                thumb=str(entry.get("thumb") or ""),
                kept=True,
                note=str(entry.get("note") or ""),
            )
        return True

    def _insert_media_row(
        self,
        conn: sqlite3.Connection,
        *,
        note_id: str | None,
        day: str,
        media_type: str,
        path: str,
        thumb: str,
        kept: bool,
        note: str = "",
        media_id: str | None = None,
    ) -> str:
        row_id = media_id or f"{_now_iso()}-{uuid.uuid4().hex[:12]}"
        try:
            conn.execute(
                "INSERT OR REPLACE INTO media("
                "id, note_id, day, type, path, thumb, kept, note, created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    row_id,
                    note_id,
                    day,
                    media_type if media_type in MEDIA_TYPES else "file",
                    str(path or ""),
                    str(thumb or ""),
                    1 if kept else 0,
                    str(note or "")[:500],
                    _now_iso(),
                ),
            )
        except sqlite3.Error:
            return ""
        return row_id

    # ------------------------------------------------------------------ 路径工具
    def _rel_path(self, ref: Any) -> str:
        """把 data_dir 内的路径转成相对 posix 路径；外部路径返回空串。"""
        text = str(ref or "").strip()
        if not text:
            return ""
        candidate = Path(text)
        if not candidate.is_absolute():
            try:
                if (self.data_dir / candidate).is_file():
                    return candidate.as_posix()
            except OSError:
                pass
            return ""
        local = _resolve_local_ref(text)
        if not local:
            return ""
        try:
            return Path(local).relative_to(self.data_dir.resolve()).as_posix()
        except (ValueError, OSError):
            return ""

    def _abs_path(self, ref: Any) -> str:
        text = str(ref or "").strip()
        if not text:
            return ""
        path = Path(text)
        if not path.is_absolute():
            path = self.data_dir / path
        try:
            return str(path.resolve())
        except OSError:
            return str(path)

    def _safe_unlink(self, ref: Any) -> bool:
        """只删 data_dir 之内的文件，防止误删原始素材/系统文件。"""
        text = str(ref or "").strip()
        if not text:
            return False
        path = Path(text)
        if not path.is_absolute():
            safe_rel = self._rel_path(text)
            if not safe_rel:
                return False
            path = self.data_dir / safe_rel
        try:
            resolved = path.resolve()
            resolved.relative_to(self.data_dir.resolve())
        except (ValueError, OSError):
            return False
        try:
            if resolved.is_file():
                resolved.unlink()
                return True
        except OSError as exc:
            self.logger.debug("[notebook] 删除媒体文件失败（%s）：%r", resolved, exc)
        return False

    # ------------------------------------------------------------------ 公开读接口（仅内部用，不注册给 agent）

    def _row_to_public(self, row: sqlite3.Row) -> dict:
        try:
            tags = json.loads(row["tags"] or "[]")
        except (json.JSONDecodeError, TypeError):
            tags = []
        if not isinstance(tags, list):
            tags = []
        media: list[dict] = []
        try:
            entries = json.loads(row["media"] or "[]")
        except (json.JSONDecodeError, TypeError):
            entries = []
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                item = {
                    "type": str(entry.get("type") or "file"),
                    "path": self._abs_path(entry.get("path")),
                    "thumb": self._abs_path(entry.get("thumb")),
                }
                note_text = str(entry.get("note") or "").strip()
                if note_text:
                    item["note"] = note_text
                media.append(item)
        return {
            "id": str(row["id"]),
            "date": str(row["day"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "content": str(row["content"]),
            "kind": str(row["kind"]),
            "tags": [str(tag) for tag in tags],
            "pinned": bool(row["pinned"]),
            "source": str(row["source"]),
            "mentions": int(row["mentions"] or 1),
            "archived": bool(row["archived"]),
            "media": media,
        }

    def _row_by_id(self, conn: sqlite3.Connection, note_id: str) -> sqlite3.Row | None:
        try:
            return conn.execute(
                "SELECT * FROM notes WHERE id = ? LIMIT 1", (str(note_id),)
            ).fetchone()
        except sqlite3.Error:
            return None

    def load_year(self, year: int) -> list[dict]:
        """读某一年的条目（年报/管理命令用）。按创建时间升序；不读归档。"""
        try:
            year_int = int(year)
        except (TypeError, ValueError):
            return []
        start = f"{year_int:04d}-01-01"
        end = f"{year_int:04d}-12-31"
        try:
            with self._db() as conn:
                rows = conn.execute(
                    "SELECT * FROM notes WHERE archived = 0 AND day >= ? AND day <= ? "
                    "ORDER BY created_at ASC, id ASC",
                    (start, end),
                ).fetchall()
        except (sqlite3.Error, OSError) as exc:
            self.logger.warning("[notebook] 读 %s 年小本本失败：%r", year_int, exc)
            return []
        if rows:
            return [self._row_to_public(row) for row in rows]
        # DB 里没有但磁盘有 md（比如用户手改过）→ 直接读 md 兜底
        return self._load_year_from_markdown(year_int)

    def _load_year_from_markdown(self, year: int) -> list[dict]:
        notes: list[dict] = []
        try:
            files = sorted((self.data_dir / f"{year:04d}").glob("*.md"))
        except OSError:
            return []
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for note in self._parse_markdown(text):
                notes.append(
                    {
                        "id": note["id"],
                        "date": note["day"],
                        "created_at": note["created_at"],
                        "updated_at": note["updated_at"],
                        "content": note["content"],
                        "kind": note["kind"],
                        "tags": list(note["tags"]),
                        "pinned": bool(note["pinned"]),
                        "source": note["source"],
                        "mentions": int(note["mentions"]),
                        "archived": False,
                        "media": [
                            {
                                "type": str(entry.get("type") or "file"),
                                "path": self._abs_path(entry.get("path")),
                                "thumb": self._abs_path(entry.get("thumb")),
                            }
                            for entry in note["media"]
                            if isinstance(entry, dict)
                        ],
                    }
                )
        notes.sort(key=lambda item: (item["created_at"], item["id"]))
        return notes

    def stats(self, year: int) -> dict:
        """年报/命令用统计：{"total", "by_kind", "months", "media"}。

        - by_kind 包含全部 6 个 kind（没有的补 0），前端不用再补键；
        - months 是 {1..12: 条数}，方便直接画热力；
        - media 是该年「被选择并保留」的媒体数。
        """
        try:
            year_int = int(year)
        except (TypeError, ValueError):
            year_int = _today_default().year
        start = f"{year_int:04d}-01-01"
        end = f"{year_int:04d}-12-31"
        by_kind = {kind: 0 for kind in KINDS}
        months = {month: 0 for month in range(1, 13)}
        total = 0
        media_count = 0
        try:
            with self._db() as conn:
                rows = conn.execute(
                    "SELECT day, kind FROM notes WHERE archived = 0 AND day >= ? AND day <= ?",
                    (start, end),
                ).fetchall()
                media_row = conn.execute(
                    "SELECT COUNT(*) AS n FROM media WHERE kept = 1 AND day >= ? AND day <= ? "
                    "AND (path != '' OR thumb != '')",
                    (start, end),
                ).fetchone()
            for row in rows:
                total += 1
                kind = str(row["kind"])
                if kind not in by_kind:
                    kind = "other"
                by_kind[kind] += 1
                try:
                    month = int(str(row["day"])[5:7])
                except (TypeError, ValueError):
                    continue
                if 1 <= month <= 12:
                    months[month] += 1
            media_count = int(media_row["n"] if media_row is not None else 0)
        except (sqlite3.Error, OSError) as exc:
            self.logger.warning("[notebook] 统计 %s 年失败：%r", year_int, exc)
        return {
            "total": total,
            "by_kind": by_kind,
            "months": months,
            "media": media_count,
        }

    def recent(self, limit: int = 10) -> list[dict]:
        """最近写的条目（管理命令/调试用）；不返回归档条目。"""
        try:
            count = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            count = 10
        try:
            with self._db() as conn:
                rows = conn.execute(
                    "SELECT * FROM notes WHERE archived = 0 "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (count,),
                ).fetchall()
        except (sqlite3.Error, OSError) as exc:
            self.logger.warning("[notebook] 读最近条目失败：%r", exc)
            return []
        return [self._row_to_public(row) for row in rows]

    def list_media(self, year: int) -> list[dict]:
        """某年「被选择过」的媒体（年报嵌图用），路径一律返回绝对路径。"""
        try:
            year_int = int(year)
        except (TypeError, ValueError):
            year_int = _today_default().year
        start = f"{year_int:04d}-01-01"
        end = f"{year_int:04d}-12-31"
        try:
            with self._db() as conn:
                rows = conn.execute(
                    "SELECT * FROM media WHERE kept = 1 AND day >= ? AND day <= ? "
                    "ORDER BY day ASC, created_at ASC, id ASC",
                    (start, end),
                ).fetchall()
        except (sqlite3.Error, OSError) as exc:
            self.logger.warning("[notebook] 读 %s 年媒体失败：%r", year_int, exc)
            return []
        result: list[dict] = []
        for row in rows:
            path = self._abs_path(row["path"])
            thumb = self._abs_path(row["thumb"])
            if not path and not thumb:
                continue
            result.append(
                {
                    "media_id": str(row["id"]),
                    "note_id": str(row["note_id"]) if row["note_id"] else "",
                    "type": str(row["type"]),
                    "path": path,
                    "thumb": thumb,
                    "day": str(row["day"]),
                    "kept": True,
                    "note": str(row["note"] or ""),
                }
            )
        return result

    # ------------------------------------------------------------------ 去重

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        """调注入的 embed_fn。任何异常/形状不对都返回 []（降级成字面去重）。"""
        if not texts or not callable(self.embed_fn):
            return []
        try:
            result = self.embed_fn(list(texts))
        except Exception as exc:  # noqa: BLE001 embedding 失败绝不能阻断写入
            self.logger.warning("[notebook] embedding 调用失败，降级字面去重：%r", exc)
            return []
        if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
            try:
                result = await result
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[notebook] embedding 调用失败，降级字面去重：%r", exc)
                return []
        if result is None:
            # main.py 在没有可用 embedding provider 时返回 None —— 属于正常降级，不刷警告
            return []
        if not isinstance(result, (list, tuple)) or len(result) != len(texts):
            self.logger.warning("[notebook] embedding 返回条数不对，降级字面去重")
            return []
        vectors: list[list[float]] = []
        for vector in result:
            try:
                floats = [float(value) for value in vector]
            except (TypeError, ValueError):
                return []
            if not floats or any(not math.isfinite(value) for value in floats):
                return []
            vectors.append(floats)
        return vectors

    async def _semantic_duplicate(
        self,
        candidates: list[sqlite3.Row],
        text: str,
    ) -> tuple[list[float] | None, sqlite3.Row | None, float]:
        """候选条目里有 embedding 的才参与语义比较；新条目向量顺便返回用于入库。"""
        vectors = await self._embed([text])
        if not vectors:
            return None, None, 0.0
        new_vector = vectors[0]
        best_row: sqlite3.Row | None = None
        best_similarity = 0.0
        for row in candidates:
            vector = _unpack_vector(row["embedding"])
            if not vector:
                continue
            similarity = _cosine(new_vector, vector)
            if similarity > best_similarity:
                best_similarity = similarity
                best_row = row
        return new_vector, best_row, best_similarity

    # ------------------------------------------------------------------ 写入

    async def add(
        self,
        content: str,
        *,
        kind: str = "other",
        tags: list[str] | None = None,
        date: date | None = None,
        media: list[dict] | None = None,
        source: str = "agent",
    ) -> dict:
        """去重后写入一条小本本。agent 的唯一写入入口（工具层由 main.py 注册）。

        返回：{"ok", "id", "duplicate", "merged_into", "similarity", "kind"}。
        - 命中重复：`duplicate=True`、`id`/`merged_into` 都是旧条目 id，`ok=True`（内容已归并）；
        - 内容为空：`ok=False`，其余字段给安全默认值。
        """
        normalized_kind = _normalize_kind(kind)
        cleaned = str(content or "").strip()
        if not cleaned:
            return {
                "ok": False,
                "id": None,
                "duplicate": False,
                "merged_into": None,
                "similarity": 0.0,
                "kind": normalized_kind,
            }
        async with self._lock:
            day = _coerce_day(date)
            text = _truncate(cleaned, MAX_NOTE_CHARS)
            tag_list = _clean_tags(tags)
            source_text = _truncate(str(source or "agent").strip() or "agent", 40)
            new_norm = _normalize_text(text)
            new_grams = _char_ngrams(new_norm)
            new_hash = _simhash64(new_norm) if new_norm else 0
            try:
                with self._db() as conn:
                    candidates = conn.execute(
                        "SELECT * FROM notes WHERE archived = 0 "
                        "ORDER BY created_at DESC, id DESC LIMIT ?",
                        (DEDUPE_CANDIDATE_LIMIT,),
                    ).fetchall()
                    duplicate_row: sqlite3.Row | None = None
                    best_similarity = 0.0
                    for row in candidates:
                        similarity, hit = _literal_similarity(
                            new_norm,
                            str(row["normalized"] or ""),
                            new_grams,
                            new_hash,
                        )
                        if similarity > best_similarity:
                            best_similarity = similarity
                        if hit:
                            duplicate_row = row
                            break
                    new_vector: list[float] | None = None
                    if duplicate_row is None and callable(self.embed_fn) and new_norm:
                        new_vector, semantic_row, semantic_similarity = (
                            await self._semantic_duplicate(candidates, text)
                        )
                        if semantic_similarity > best_similarity:
                            best_similarity = semantic_similarity
                        if (
                            semantic_row is not None
                            and semantic_similarity >= self._dup_threshold()
                        ):
                            duplicate_row = semantic_row
                    if duplicate_row is not None:
                        merged_kind = self._merge_note(
                            conn,
                            duplicate_row,
                            text=text,
                            new_norm=new_norm,
                            tags=tag_list,
                            media=media,
                            day=day,
                            kind=normalized_kind,
                        )
                        conn.commit()
                        return {
                            "ok": True,
                            "id": str(duplicate_row["id"]),
                            "duplicate": True,
                            "merged_into": str(duplicate_row["id"]),
                            "similarity": round(float(best_similarity), 4),
                            "kind": merged_kind,
                        }
                    note_id = self._new_id(conn, day)
                    media_entries = self._attach_media(conn, note_id, day, media)
                    now = _now_iso()
                    created_at = f"{day.isoformat()}T{datetime.now():%H:%M:%S}"
                    conn.execute(
                        "INSERT INTO notes("
                        "id, day, created_at, updated_at, content, normalized, kind, tags, "
                        "pinned, source, mentions, archived, embedding, media"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                        (
                            note_id,
                            day.isoformat(),
                            created_at,
                            now,
                            text,
                            new_norm,
                            normalized_kind,
                            json.dumps(tag_list, ensure_ascii=False),
                            1 if normalized_kind == "commitment" else 0,
                            source_text,
                            1,
                            _pack_vector(new_vector) if new_vector else None,
                            json.dumps(media_entries, ensure_ascii=False),
                        ),
                    )
                    self._write_day_markdown(conn, day.isoformat())
                    conn.commit()
            except Exception as exc:  # noqa: BLE001 磁盘满/权限/坏索引都不能把对话搞炸
                self.logger.warning("[notebook] 写入失败：%r", exc)
                return {
                    "ok": False,
                    "id": None,
                    "duplicate": False,
                    "merged_into": None,
                    "similarity": 0.0,
                    "kind": normalized_kind,
                }
            return {
                "ok": True,
                "id": note_id,
                "duplicate": False,
                "merged_into": None,
                "similarity": 0.0,
                "kind": normalized_kind,
            }

    def _new_id(self, conn: sqlite3.Connection, day: date) -> str:
        """条目 id：YYYYMMDD-HHMM-xxxx（设计底稿 §二），撞了就重摇。"""
        now = datetime.now()
        prefix = f"{day:%Y%m%d}-{now:%H%M}"
        for _ in range(50):
            candidate = f"{prefix}-{uuid.uuid4().hex[:4]}"
            try:
                exists = conn.execute(
                    "SELECT 1 FROM notes WHERE id = ? LIMIT 1", (candidate,)
                ).fetchone()
            except sqlite3.Error:
                exists = None
            if not exists:
                return candidate
        return f"{prefix}-{uuid.uuid4().hex[:12]}"

    def _merge_note(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        text: str,
        new_norm: str,
        tags: list[str],
        media: list[dict] | None,
        day: date,
        kind: str,
    ) -> str:
        """重复内容合并进旧条目：mention+1、标签/媒体并入、更长的新版本可升级正文。"""
        existing_kind = _normalize_kind(row["kind"])
        existing_content = str(row["content"] or "")
        existing_norm = str(row["normalized"] or _normalize_text(existing_content))
        content = existing_content
        # 新写的版本更长（信息更全）→ 原位升级正文；只是标点/语气词变体则保留原文。
        if len(new_norm) > len(existing_norm) and new_norm != existing_norm:
            content = text
        try:
            existing_tags = json.loads(row["tags"] or "[]")
        except (json.JSONDecodeError, TypeError):
            existing_tags = []
        merged_tags = _clean_tags(list(existing_tags or []) + list(tags or []))
        try:
            existing_media = json.loads(row["media"] or "[]")
        except (json.JSONDecodeError, TypeError):
            existing_media = []
        new_media = self._attach_media(conn, str(row["id"]), _coerce_day(row["day"]), media)
        merged_media = _merge_media_lists(
            existing_media if isinstance(existing_media, list) else [],
            new_media,
        )
        pinned = bool(row["pinned"]) or kind == "commitment"
        mentions = int(row["mentions"] or 1) + 1
        try:
            conn.execute(
                "UPDATE notes SET content = ?, normalized = ?, tags = ?, pinned = ?, "
                "mentions = ?, updated_at = ?, media = ? WHERE id = ?",
                (
                    content,
                    _normalize_text(content),
                    json.dumps(merged_tags, ensure_ascii=False),
                    1 if pinned else 0,
                    mentions,
                    _now_iso(),
                    json.dumps(merged_media, ensure_ascii=False),
                    str(row["id"]),
                ),
            )
            self._write_day_markdown(conn, str(row["day"]))
        except (sqlite3.Error, OSError) as exc:
            self.logger.warning("[notebook] 合并重复条目失败：%r", exc)
        return existing_kind

    # ------------------------------------------------------------------ 媒体捕获

    def _media_dir(self, day: date) -> Path:
        return self.data_dir / "media" / f"{day:%Y}"

    def _make_thumbnail(self, source: Path, target: Path, size: int) -> bool:
        """Pillow 缩略图（长边 ≤ size、转 RGB、输出 JPEG）。顺带剥离 EXIF/GPS 等隐私。"""
        try:
            from PIL import Image, ImageOps
        except Exception:  # noqa: BLE001 Pillow 不可用时降级为保存原文件副本
            self.logger.info("[notebook] Pillow 不可用，媒体缩略图降级为原文件副本")
            return False
        try:
            with Image.open(source) as image:
                image = ImageOps.exif_transpose(image)
                if image.mode in ("RGBA", "LA") or (
                    image.mode == "P" and "transparency" in image.info
                ):
                    image = image.convert("RGBA")
                    background = Image.new("RGB", image.size, (255, 255, 255))
                    background.paste(image, mask=image.getchannel("A"))
                    image = background
                elif image.mode != "RGB":
                    image = image.convert("RGB")
                image.thumbnail((size, size), Image.LANCZOS)
                target.parent.mkdir(parents=True, exist_ok=True)
                image.save(target, "JPEG", quality=85, optimize=True)
            return target.is_file()
        except Exception as exc:  # noqa: BLE001 坏图/不支持格式 → 交给调用方降级
            self.logger.debug("[notebook] 生成缩略图失败（%s）：%r", source.name, exc)
            return False

    def _copy_media_file(self, source: Path, target: Path) -> bool:
        try:
            if not source.is_file():
                return False
            if source.stat().st_size > MAX_MEDIA_COPY_BYTES:
                self.logger.info(
                    "[notebook] 媒体文件超过 %d MB，跳过保存：%s",
                    MAX_MEDIA_COPY_BYTES // (1024 * 1024),
                    source.name,
                )
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            return target.is_file()
        except OSError as exc:
            self.logger.debug("[notebook] 复制媒体失败（%s）：%r", source, exc)
            return False

    def _store_media(self, media_type: str, source: str, component: Any = None) -> dict | None:
        """把一个已落盘的素材存进 media/，返回 {"type","path","thumb","note"}（绝对路径）。"""
        source_path = Path(source)
        if not source_path.is_file():
            return None
        today = _today_default()
        target_dir = self._media_dir(today)
        base = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        suffix = source_path.suffix.lower()[:8]
        keep_original = self._keep_original()
        thumb_size = self._thumb_size()
        entry: dict = {"type": media_type, "path": "", "thumb": "", "note": ""}
        stored_path = ""
        stored_thumb = ""
        note_bits: list[str] = []
        if media_type == "image":
            thumb_path = target_dir / f"{base}.jpg"
            if self._make_thumbnail(source_path, thumb_path, thumb_size):
                stored_path = str(thumb_path)
                stored_thumb = str(thumb_path)
            else:
                fallback = target_dir / f"{base}{suffix or '.img'}"
                if self._copy_media_file(source_path, fallback):
                    stored_path = str(fallback)
                    stored_thumb = str(fallback)
                else:
                    note_bits.append("无法保存图片副本")
            if keep_original and stored_thumb:
                original_path = target_dir / f"{base}-orig{suffix or '.img'}"
                if self._copy_media_file(source_path, original_path):
                    stored_path = str(original_path)
        elif media_type == "video":
            # 首帧：优先用组件自带封面；没有封面就让 Pillow 直接试着打开原视频
            # （大多数视频格式打不开 → 跳过首帧，不为此引入 ffmpeg/额外依赖）。
            cover_ref = (
                _resolve_local_ref(getattr(component, "cover", ""))
                if component is not None
                else ""
            )
            cover_thumb = target_dir / f"{base}-cover.jpg"
            thumb_ok = bool(cover_ref) and self._make_thumbnail(
                Path(cover_ref), cover_thumb, thumb_size
            )
            if not thumb_ok:
                thumb_ok = self._make_thumbnail(source_path, cover_thumb, thumb_size)
            if thumb_ok:
                stored_thumb = str(cover_thumb)
                if not keep_original:
                    stored_path = stored_thumb
            if keep_original:
                original_path = target_dir / f"{base}{suffix or '.mp4'}"
                if self._copy_media_file(source_path, original_path):
                    stored_path = str(original_path)
            duration = getattr(component, "duration", None) if component is not None else None
            try:
                if duration is not None:
                    seconds = float(duration)
                    if math.isfinite(seconds) and seconds > 0:
                        note_bits.append(f"时长 {seconds:g} 秒")
            except (TypeError, ValueError):
                pass
            if not stored_thumb:
                note_bits.append("未取得视频首帧（不引入 ffmpeg，跳过首帧）")
            if not stored_path:
                # 不复制原视频：至少把原视频引用记下来，年报知道"这里有过一段视频"。
                stored_path = str(source_path)
                note_bits.append("未复制原视频，仅记录引用")
        else:  # file / record
            if keep_original:
                raw_name = getattr(component, "name", "") if component is not None else ""
                filename = _safe_filename(raw_name, suffix or ".bin")
                original_path = target_dir / f"{base}-{filename}"
                if self._copy_media_file(source_path, original_path):
                    stored_path = str(original_path)
                else:
                    note_bits.append("文件过大或复制失败，未保存")
            else:
                stored_path = str(source_path)
                note_bits.append("仅登记引用，未保存文件")
        entry["path"] = stored_path
        entry["thumb"] = stored_thumb
        entry["note"] = "；".join(note_bits)
        if not stored_path and not stored_thumb:
            # 没有任何可展示文件：返回带 note 的条目让 agent 知道发生了什么，但不落 media 行
            return entry
        # data_dir 内的文件存相对路径（可迁移）；外部引用（如临时视频/文件）按绝对路径记。
        relative_path = self._rel_path(stored_path) or (
            stored_path if Path(stored_path).is_absolute() else ""
        )
        relative_thumb = self._rel_path(stored_thumb) or (
            stored_thumb if Path(stored_thumb).is_absolute() else ""
        )
        try:
            with self._db() as conn:
                self._insert_media_row(
                    conn,
                    note_id=None,
                    day=today.isoformat(),
                    media_type=media_type,
                    path=relative_path,
                    thumb=relative_thumb,
                    kept=False,
                    note=entry["note"],
                )
                conn.commit()
        except (sqlite3.Error, OSError) as exc:
            self.logger.warning("[notebook] 媒体索引写入失败：%r", exc)
        return entry

    async def capture_event_media(self, event) -> list[dict]:
        """从当前消息取图片/视频/文件 → 存缩略图/封面到 media/。

        只处理「这条消息里真的带了」的组件，不做历史消息/网络全量捕获。
        返回 [{"type","path","thumb","note"?}]，path/thumb 为绝对路径；
        这些条目应原样传给 `add(media=...)`，被选择过的媒体会被标记 kept=True 永不清理。
        """
        if event is None:
            return []
        get_messages = getattr(event, "get_messages", None)
        if not callable(get_messages):
            return []
        try:
            components = list(get_messages() or [])
        except Exception as exc:  # noqa: BLE001 事件对象异常时安静跳过
            self.logger.debug("[notebook] 读取消息组件失败：%r", exc)
            return []
        entries: list[dict] = []
        for component in components:
            media_type = _component_kind(component)
            if media_type is None:
                continue
            try:
                local_path = await _component_local_path(component)
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("[notebook] 解析媒体组件失败：%r", exc)
                continue
            if not local_path:
                self.logger.debug("[notebook] 媒体组件没有本地文件，跳过（type=%s）", media_type)
                continue
            try:
                entry = self._store_media(media_type, local_path, component)
            except Exception as exc:  # noqa: BLE001 单个媒体失败不影响其它
                self.logger.warning("[notebook] 保存媒体失败：%r", exc)
                entry = None
            if entry:
                entries.append(entry)
        return entries

    def _attach_media(
        self,
        conn: sqlite3.Connection,
        note_id: str,
        day: date,
        media: list[dict] | None,
    ) -> list[dict]:
        """把 agent 选择附加的媒体挂到条目上，标 kept=True（被选择过的媒体永不清理）。

        兼容两种来源：capture_event_media() 的返回值（能按路径找回 pending 行），
        或外部直接给的 {"type","path","thumb"}；后者会尽量复制/缩略到 media/ 下。
        """
        if not media:
            return []
        stored: list[dict] = []
        for raw in media:
            if not isinstance(raw, dict):
                continue
            media_type = str(raw.get("type") or "file").strip().lower()
            if media_type not in MEDIA_TYPES:
                media_type = "file"
            path_ref = str(raw.get("path") or "").strip()
            thumb_ref = str(raw.get("thumb") or "").strip()
            note_text = _truncate(str(raw.get("note") or ""), 200)
            # 先按原始引用/相对路径/绝对路径找回 capture 阶段留下的 pending 行（含外部引用）；
            # 找不到再把外部文件缩略/复制进 media/ 并新建索引。
            representative = ""
            row = self._find_media_row(conn, (thumb_ref, path_ref))
            if row is not None:
                representative = (
                    self._rel_path(row["thumb"])
                    or self._rel_path(row["path"])
                    or str(row["thumb"] or row["path"] or "")
                )
                if representative:
                    try:
                        conn.execute(
                            "UPDATE media SET note_id = ?, day = ?, kept = 1, type = ?, "
                            "note = ? WHERE id = ?",
                            (
                                note_id,
                                day.isoformat(),
                                media_type,
                                note_text or str(row["note"] or ""),
                                str(row["id"]),
                            ),
                        )
                    except sqlite3.Error:
                        continue
                    stored_path = self._rel_path(row["path"]) or str(
                        row["path"] or representative
                    )
                    stored_thumb = self._rel_path(row["thumb"]) or str(
                        row["thumb"] or representative
                    )
                else:
                    row = None
            if row is None:
                relative = self._media_rel_for_ref(thumb_ref) or self._media_rel_for_ref(
                    path_ref
                )
                if not relative:
                    continue
                self._insert_media_row(
                    conn,
                    note_id=note_id,
                    day=day.isoformat(),
                    media_type=media_type,
                    path=relative,
                    thumb=relative,
                    kept=True,
                    note=note_text,
                )
                stored_path = relative
                stored_thumb = relative
            entry = {
                "type": media_type,
                "path": stored_path or representative,
                "thumb": stored_thumb or representative,
            }
            if note_text:
                entry["note"] = note_text
            stored.append(entry)
        return _merge_media_lists([], stored)

    def _find_media_row(
        self, conn: sqlite3.Connection, refs: tuple[str, ...]
    ) -> sqlite3.Row | None:
        """按媒体引用找回索引行：原始引用、相对路径、解析后的绝对路径都试一遍。

        capture_event_media() 写 pending 行时存的是相对路径；agent 回传的是绝对路径。
        优先匹配 pending（kept=0 且未挂笔记）的行，让"选择附加"能把 kept 置 1。
        """

        def _candidates(ref: str) -> list[str]:
            text = str(ref or "").strip()
            if not text:
                return []
            values = [text]
            relative = self._rel_path(text)
            if relative:
                values.append(relative)
            local = _resolve_local_ref(text)
            if local:
                values.append(local)
            return values

        candidates: list[str] = []
        for ref in refs:
            for value in _candidates(ref):
                if value not in candidates:
                    candidates.append(value)
        if not candidates:
            return None
        placeholders = ",".join("?" for _ in candidates)
        try:
            return conn.execute(
                f"SELECT * FROM media WHERE path IN ({placeholders}) "
                f"OR thumb IN ({placeholders}) "
                "ORDER BY CASE WHEN kept = 0 AND note_id IS NULL THEN 0 ELSE 1 END, "
                "created_at DESC LIMIT 1",
                tuple(candidates) * 2,
            ).fetchone()
        except sqlite3.Error:
            return None

    def _media_rel_for_ref(self, ref: str) -> str:
        """把引用转成 data_dir 内的相对路径；外部文件（如测试/命令传入）复制/缩略进来。"""
        if not ref:
            return ""
        relative = self._rel_path(ref)
        if relative:
            return relative
        local = _resolve_local_ref(ref)
        if not local:
            return ""
        source = Path(local)
        today = _today_default()
        target_dir = self._media_dir(today)
        base = f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
        suffix = source.suffix.lower()[:8] or ".bin"
        thumb_path = target_dir / f"{base}.jpg"
        if self._make_thumbnail(source, thumb_path, self._thumb_size()):
            return self._rel_path(str(thumb_path))
        copy_path = target_dir / f"{base}{suffix}"
        if self._copy_media_file(source, copy_path):
            return self._rel_path(str(copy_path))
        return ""

    # ------------------------------------------------------------------ 每日兜底总结

    @staticmethod
    def _render_sessions(sessions: list[dict]) -> list[str]:
        """把 sessions 渲染成「按会话分块」的文本，只保留 user/assistant 正文。"""
        blocks: list[str] = []
        messages_seen = 0
        for session in sessions or []:
            if not isinstance(session, dict):
                continue
            umo = str(session.get("umo") or "").strip()
            raw_messages = session.get("messages")
            if not isinstance(raw_messages, list):
                continue
            lines: list[str] = []
            for message in raw_messages:
                if messages_seen >= MAX_SUMMARY_MESSAGES:
                    break
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role") or "").strip().lower()
                if role in ("system", "tool", "developer"):
                    continue
                content = str(message.get("content") or "").strip()
                if not content:
                    continue
                messages_seen += 1
                speaker = "她" if role == "assistant" else "对方"
                lines.append(f"{speaker}：{_truncate(content, MAX_SUMMARY_MSG_CHARS)}")
            if lines:
                header = f"【会话 {umo}】" if umo else "【会话】"
                blocks.append(header + "\n" + "\n".join(lines))
            if messages_seen >= MAX_SUMMARY_MESSAGES:
                break
        return blocks

    async def _call_llm(self, system: str, user: str) -> str:
        """包一层：LLM 不可用/超时/报错都只返回空串，由调用方走"跳过"。"""
        if not callable(self.llm_call):
            return ""
        try:
            result = self.llm_call(system, user)
            if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                result = await result
        except Exception as exc:  # noqa: BLE001 总结失败绝不能影响别人
            self.logger.warning("[notebook] 每日总结 LLM 调用失败：%r", exc)
            return ""
        return str(result or "")

    @staticmethod
    def _parse_facts(text: str) -> list[str]:
        data = _extract_json(text)
        raw: Any = None
        if isinstance(data, dict):
            for key in ("facts", "items", "notes", "summary"):
                if isinstance(data.get(key), list):
                    raw = data[key]
                    break
        elif isinstance(data, list):
            raw = data
        if not isinstance(raw, list):
            return []
        facts: list[str] = []
        seen: set[str] = set()
        for item in raw:
            if isinstance(item, str):
                content = item
            elif isinstance(item, dict):
                content = str(item.get("content") or item.get("fact") or item.get("text") or "")
            else:
                continue
            content = _truncate(content.strip(), 200)
            key = _normalize_text(content)
            if len(content) < 2 or not key or key in seen:
                continue
            seen.add(key)
            facts.append(content)
            if len(facts) >= MAX_FACTS_PER_CHUNK:
                break
        return facts

    @staticmethod
    def _parse_final_notes(text: str, max_chars: int) -> list[dict]:
        data = _extract_json(text)
        raw: Any = None
        if isinstance(data, dict):
            for key in ("notes", "items", "memories"):
                if isinstance(data.get(key), list):
                    raw = data[key]
                    break
        elif isinstance(data, list):
            raw = data
        if not isinstance(raw, list):
            return []
        notes: list[dict] = []
        seen: set[str] = set()
        for item in raw:
            if isinstance(item, str):
                content = item
                kind = "other"
                tags: list[str] = []
            elif isinstance(item, dict):
                content = str(
                    item.get("content") or item.get("note") or item.get("text") or ""
                )
                kind = _normalize_kind(item.get("kind"))
                tags = _clean_tags(item.get("tags"))
            else:
                continue
            content = _truncate(content.strip(), max_chars)
            key = _normalize_text(content)
            if len(content) < 6 or not key or key in seen:
                continue
            seen.add(key)
            notes.append({"content": content, "kind": kind, "tags": tags})
            if len(notes) >= 2:
                break
        return notes

    async def daily_summary(self, *, today: date, sessions: list[dict]) -> list[dict]:
        """每日兜底：把当天会话做「map → 多层 reduce → 终选」，写 1~2 条笔记并返回。

        - sessions=[{"umo":str,"messages":[{"role","content","ts"}]}]；
        - 只保留与角色自身相关的内容（提示词约束 + 最终只挑 1~2 条）；
        - 输入超过 `notebook_summary_max_input_chars` 才分块；分块后仍超预算才继续 reduce；
        - LLM 不可用/失败/没素材 → 返回 []（安静跳过，不写脏数据）；
        - 命中已有笔记时去重合并，返回 []（幂等）——同一天重复跑不会长出新条目。
        """
        if not callable(self.llm_call):
            if not self._warned_missing_llm:
                self._warned_missing_llm = True
                self.logger.info("[notebook] 没有可用的 llm_call，每日兜底总结跳过")
            return []
        day = _coerce_day(today)
        blocks = self._render_sessions(sessions)
        if not blocks:
            return []
        budget = self._summary_max_input()
        max_chars = self._summary_note_limit()
        chunks = _chunk_blocks(blocks, budget)
        if not chunks:
            return []
        if len(chunks) > MAX_MAP_CHUNKS:
            self.logger.info(
                "[notebook] 当天输入过长，只总结前 %d 块（共 %d 块）", MAX_MAP_CHUNKS, len(chunks)
            )
            chunks = chunks[:MAX_MAP_CHUNKS]
        facts: list[str] = []
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            user = _fill_template(
                SUMMARY_MAP_USER,
                {
                    "TODAY": day.isoformat(),
                    "INDEX": str(index),
                    "TOTAL": str(total),
                    "CHARS": str(len(chunk)),
                    "CHUNK": chunk,
                },
            )
            text = await self._call_llm(SUMMARY_MAP_SYSTEM, user)
            facts.extend(self._parse_facts(text))
        if not facts:
            self.logger.info("[notebook] 每日总结没有提取到与角色相关的素材，跳过")
            return []
        # 跨块去重
        deduped: list[str] = []
        seen: set[str] = set()
        for fact in facts:
            key = _normalize_text(fact)
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(fact)
        facts = deduped
        # 多层 reduce：只要还有超预算就继续合并，最多 MAX_REDUCE_LAYERS 层
        layer = 0
        while len("\n".join(facts)) > budget and len(facts) > 1 and layer < MAX_REDUCE_LAYERS:
            groups = _chunk_blocks(facts, budget)
            merged_facts: list[str] = []
            for index, group in enumerate(groups, start=1):
                user = _fill_template(
                    SUMMARY_REDUCE_USER,
                    {
                        "TODAY": day.isoformat(),
                        "INDEX": str(index),
                        "TOTAL": str(len(groups)),
                        "CHUNK": group,
                    },
                )
                text = await self._call_llm(SUMMARY_REDUCE_SYSTEM, user)
                parsed = self._parse_facts(text)
                merged_facts.extend(parsed if parsed else [group])
            if not merged_facts or merged_facts == facts:
                break
            facts = merged_facts
            layer += 1
        facts_text = "\n".join(f"- {fact}" for fact in facts)
        final_user = _fill_template(
            SUMMARY_FINAL_USER, {"TODAY": day.isoformat(), "FACTS": facts_text}
        )
        final_system = _fill_template(
            SUMMARY_FINAL_SYSTEM, {"MAX_CHARS": str(max_chars)}
        )
        final_text = await self._call_llm(final_system, final_user)
        picked = self._parse_final_notes(final_text, max_chars)
        if not picked:
            self.logger.info("[notebook] 每日总结没有挑出值得写的内容，跳过")
            return []
        written: list[dict] = []
        for note in picked:
            result = await self.add(
                note["content"],
                kind=note["kind"],
                tags=note["tags"],
                date=day,
                source="daily_summary",
            )
            if not result.get("ok") or result.get("duplicate"):
                continue
            try:
                with self._db() as conn:
                    row = self._row_by_id(conn, str(result.get("id") or ""))
            except sqlite3.Error:
                row = None
            if row is not None:
                written.append(self._row_to_public(row))
        if written:
            self.logger.info("[notebook] 每日兜底总结写入 %d 条笔记", len(written))
        return written

    # ------------------------------------------------------------------ 清理 / 删除

    @staticmethod
    def _has_future_or_commitment(content: str, tags_json: str = "") -> bool:
        haystack = f"{content or ''} {tags_json or ''}"
        return any(word in haystack for word in _FUTURE_WORDS) or any(
            word in haystack for word in _COMMITMENT_WORDS
        )

    def _remove_media_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
        """删掉一条未保留的媒体索引 + 文件；只删 data_dir 内的。"""
        deleted_file = False
        for field in ("path", "thumb"):
            value = row[field]
            if value and self._safe_unlink(str(value)):
                deleted_file = True
        try:
            conn.execute("DELETE FROM media WHERE id = ?", (str(row["id"]),))
        except sqlite3.Error:
            return deleted_file
        return True

    def cleanup(self, *, today: date, dry_run: bool = True) -> dict:
        """清理旧条目。是否开启由 main.py 的 `notebook_cleanup_enable` 控制，本方法只执行。

        永不清理：pinned / kind=commitment / 正文含未来时间词或承诺词 /
        被年报引用过 / 带「被选择媒体」的条目。
        归档模式只把条目移到 archive/（默认）；delete 模式才真删索引和文件。
        `dry_run=True` 只报告候选，不动任何数据。
        """
        day = _coerce_day(today)
        mode = _cfg_str(self.cfg, "notebook_cleanup_mode", DEFAULT_CLEANUP_MODE).lower()
        if mode not in ("archive", "delete"):
            mode = "archive"
        idle_days = _cfg_int(
            self.cfg, "notebook_cleanup_idle_days", DEFAULT_CLEANUP_IDLE_DAYS, 1, 36500
        )
        cutoff = (day - timedelta(days=idle_days)).isoformat()
        protected = {
            "pinned": 0,
            "commitment": 0,
            "future": 0,
            "used_in_report": 0,
            "media": 0,
        }
        result: dict = {
            "dry_run": bool(dry_run),
            "mode": mode,
            "idle_days": idle_days,
            "candidate_ids": [],
            "candidate_count": 0,
            "media_candidate_ids": [],
            "media_candidate_count": 0,
            "protected": protected,
            "archived": 0,
            "deleted": 0,
            "media_deleted": 0,
        }
        try:
            with self._db() as conn:
                rows = conn.execute(
                    "SELECT * FROM notes WHERE archived = 0 AND day < ? ORDER BY day ASC",
                    (cutoff,),
                ).fetchall()
                used_ids = {
                    str(row["note_id"])
                    for row in conn.execute("SELECT note_id FROM note_uses").fetchall()
                }
                kept_media_ids = {
                    str(row["note_id"])
                    for row in conn.execute(
                        "SELECT DISTINCT note_id FROM media "
                        "WHERE kept = 1 AND note_id IS NOT NULL"
                    ).fetchall()
                }
                candidates: list[sqlite3.Row] = []
                for row in rows:
                    note_id = str(row["id"])
                    if bool(row["pinned"]):
                        protected["pinned"] += 1
                        continue
                    if str(row["kind"]) == "commitment":
                        protected["commitment"] += 1
                        continue
                    if self._has_future_or_commitment(
                        str(row["content"] or ""), str(row["tags"] or "")
                    ):
                        protected["future"] += 1
                        continue
                    if note_id in used_ids:
                        protected["used_in_report"] += 1
                        continue
                    if note_id in kept_media_ids:
                        protected["media"] += 1
                        continue
                    candidates.append(row)
                media_rows = conn.execute(
                    "SELECT * FROM media WHERE kept = 0 AND day < ? ORDER BY day ASC",
                    (cutoff,),
                ).fetchall()
                result["candidate_ids"] = [str(row["id"]) for row in candidates]
                result["candidate_count"] = len(candidates)
                result["media_candidate_ids"] = [str(row["id"]) for row in media_rows]
                result["media_candidate_count"] = len(media_rows)
                # main.py 的 /age notebook cleanup 展示用的是 matched/kept 两个字段
                # （命中待清理数 / 被保护保留数），跟 candidate_count 同义。
                result["matched"] = len(candidates)
                result["kept"] = sum(int(value) for value in protected.values())
                if dry_run:
                    return result
                affected_days: set[str] = set()
                if mode == "archive":
                    for row in candidates:
                        try:
                            conn.execute(
                                "UPDATE notes SET archived = 1, updated_at = ? WHERE id = ?",
                                (_now_iso(), str(row["id"])),
                            )
                        except sqlite3.Error:
                            continue
                        affected_days.add(str(row["day"]))
                    result["archived"] = len(candidates)
                else:
                    for row in candidates:
                        note_id = str(row["id"])
                        affected_days.add(str(row["day"]))
                        try:
                            conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
                            conn.execute("DELETE FROM note_uses WHERE note_id = ?", (note_id,))
                            conn.execute(
                                "UPDATE media SET note_id = NULL WHERE note_id = ?", (note_id,)
                            )
                        except sqlite3.Error:
                            continue
                        result["deleted"] += 1
                media_deleted = 0
                for row in media_rows:
                    if self._remove_media_row(conn, row):
                        media_deleted += 1
                result["media_deleted"] = media_deleted
                for affected in sorted(affected_days):
                    self._write_day_markdown(conn, affected)
                    self._write_archive_markdown(conn, affected)
                conn.commit()
        except (sqlite3.Error, OSError) as exc:
            self.logger.warning("[notebook] 清理失败：%r", exc)
        return result

    def delete(self, *, note_id: str | None = None, day: date | None = None) -> dict:
        """手动删除：按 id 删单条；按日期删当天的非 pinned 条目。

        显式删除单条不受 pinned 保护（用户主动要求）；按日期批量删会跳过 pinned，
        并在 `skipped_pinned` 里报告数量。被选择过的媒体文件不跟着删（永不清理）。
        """
        result = {
            "deleted": 0,
            "ids": [],
            "days": [],
            "media_deleted": 0,
            "media_detached": 0,
            "skipped_pinned": 0,
        }
        if not note_id and day is None:
            return result
        try:
            with self._db() as conn:
                targets: list[sqlite3.Row] = []
                if note_id:
                    row = self._row_by_id(conn, str(note_id))
                    if row is None:
                        return result
                    targets.append(row)
                else:
                    day_str = _coerce_day(day).isoformat()
                    rows = conn.execute(
                        "SELECT * FROM notes WHERE day = ? AND archived = 0", (day_str,)
                    ).fetchall()
                    for row in rows:
                        if bool(row["pinned"]):
                            result["skipped_pinned"] += 1
                            continue
                        targets.append(row)
                affected_days: set[str] = set()
                for row in targets:
                    target_id = str(row["id"])
                    try:
                        conn.execute("DELETE FROM notes WHERE id = ?", (target_id,))
                        conn.execute(
                            "DELETE FROM note_uses WHERE note_id = ?", (target_id,)
                        )
                        pending = conn.execute(
                            "SELECT * FROM media WHERE note_id = ? AND kept = 0", (target_id,)
                        ).fetchall()
                        for media_row in pending:
                            if self._remove_media_row(conn, media_row):
                                result["media_deleted"] += 1
                        cursor = conn.execute(
                            "UPDATE media SET note_id = NULL WHERE note_id = ?", (target_id,)
                        )
                        result["media_detached"] += max(0, int(cursor.rowcount or 0))
                    except sqlite3.Error:
                        continue
                    result["deleted"] += 1
                    result["ids"].append(target_id)
                    affected_days.add(str(row["day"]))
                for affected in sorted(affected_days):
                    self._write_day_markdown(conn, affected)
                    self._write_archive_markdown(conn, affected)
                result["days"] = sorted(affected_days)
                conn.commit()
        except (sqlite3.Error, OSError) as exc:
            self.logger.warning("[notebook] 删除条目失败：%r", exc)
        return result

    # ------------------------------------------------------------------ 年报引用 / 导出 / 重建索引

    def mark_used_in_report(self, ids: list[str], report_date: date) -> int:
        """记一笔「这些条目被某天的年报引用过」。返回本次新增标记数（幂等）。"""
        if not ids:
            return 0
        day = _coerce_day(report_date).isoformat()
        now = _now_iso()
        marked = 0
        try:
            with self._db() as conn:
                for note_id in ids:
                    value = str(note_id or "").strip()
                    if not value:
                        continue
                    try:
                        cursor = conn.execute(
                            "INSERT OR IGNORE INTO note_uses(note_id, report_date, used_at) "
                            "VALUES(?,?,?)",
                            (value, day, now),
                        )
                    except sqlite3.Error:
                        continue
                    if cursor.rowcount and cursor.rowcount > 0:
                        marked += 1
                conn.commit()
        except (sqlite3.Error, OSError) as exc:
            self.logger.warning("[notebook] 标记年报引用失败：%r", exc)
        return marked

    def _export_note(self, row: sqlite3.Row) -> dict:
        try:
            tags = json.loads(row["tags"] or "[]")
        except (json.JSONDecodeError, TypeError):
            tags = []
        try:
            media = json.loads(row["media"] or "[]")
        except (json.JSONDecodeError, TypeError):
            media = []
        return {
            "id": str(row["id"]),
            "date": str(row["day"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "content": str(row["content"]),
            "kind": str(row["kind"]),
            "tags": tags if isinstance(tags, list) else [],
            "pinned": bool(row["pinned"]),
            "source": str(row["source"]),
            "mentions": int(row["mentions"] or 1),
            "archived": bool(row["archived"]),
            "media": media if isinstance(media, list) else [],
        }

    def export(self, year: int | None = None) -> str:
        """导出成 JSON 文本（本地备份/用户查看用，路径用相对路径，方便迁移）。"""
        try:
            with self._db() as conn:
                if year is None:
                    note_rows = conn.execute(
                        "SELECT * FROM notes ORDER BY day ASC, created_at ASC, id ASC"
                    ).fetchall()
                    media_rows = conn.execute(
                        "SELECT * FROM media ORDER BY day ASC, created_at ASC"
                    ).fetchall()
                else:
                    year_int = int(year)
                    start = f"{year_int:04d}-01-01"
                    end = f"{year_int:04d}-12-31"
                    note_rows = conn.execute(
                        "SELECT * FROM notes WHERE day >= ? AND day <= ? "
                        "ORDER BY day ASC, created_at ASC, id ASC",
                        (start, end),
                    ).fetchall()
                    media_rows = conn.execute(
                        "SELECT * FROM media WHERE day >= ? AND day <= ? "
                        "ORDER BY day ASC, created_at ASC",
                        (start, end),
                    ).fetchall()
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            self.logger.warning("[notebook] 导出失败：%r", exc)
            return json.dumps(
                {"version": 1, "exported_at": _now_iso(), "year": year, "count": 0,
                 "notes": [], "media": []},
                ensure_ascii=False,
                indent=2,
            )
        payload = {
            "version": 1,
            "exported_at": _now_iso(),
            "source": "astrbot_plugin_lifespan_theory/notebook",
            "year": year,
            "count": len(note_rows),
            "notes": [self._export_note(row) for row in note_rows],
            "media": [
                {
                    "media_id": str(row["id"]),
                    "note_id": str(row["note_id"]) if row["note_id"] else "",
                    "day": str(row["day"]),
                    "type": str(row["type"]),
                    "path": str(row["path"]),
                    "thumb": str(row["thumb"]),
                    "kept": bool(row["kept"]),
                    "note": str(row["note"] or ""),
                }
                for row in media_rows
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    async def reindex(self) -> dict:
        """用磁盘上的 markdown 重建/校正索引（用户手改 md 后由管理命令调用）。

        以 md 为正文真源：新增/更新/删除索引条目、恢复媒体引用；顺手给缺 embedding
        的条目补向量（embed_fn 可用时），让语义去重继续生效。
        """
        stats = {
            "files": 0,
            "notes": 0,
            "inserted": 0,
            "updated": 0,
            "removed": 0,
            "embedded": 0,
        }
        async with self._lock:
            try:
                with self._db() as conn:
                    parsed: dict[str, dict] = {}
                    for pattern, archived in (
                        ("[0-9][0-9][0-9][0-9]/*.md", False),
                        ("archive/[0-9][0-9][0-9][0-9]/*.md", True),
                    ):
                        for path in sorted(self.data_dir.glob(pattern)):
                            try:
                                text = path.read_text(encoding="utf-8")
                            except OSError:
                                continue
                            stats["files"] += 1
                            for note in self._parse_markdown(text):
                                note["archived"] = archived
                                parsed[note["id"]] = note
                    stats["notes"] = len(parsed)
                    existing_rows = conn.execute("SELECT * FROM notes").fetchall()
                    existing = {str(row["id"]): row for row in existing_rows}
                    for note_id, note in parsed.items():
                        row = existing.get(note_id)
                        archived = 1 if note.get("archived") else 0
                        media_json = json.dumps(note.get("media") or [], ensure_ascii=False)
                        if row is None:
                            if self._insert_note_row(conn, note, archived=bool(archived)):
                                stats["inserted"] += 1
                            continue
                        content_changed = str(row["content"]) != str(note["content"])
                        try:
                            if content_changed:
                                conn.execute(
                                    "UPDATE notes SET content = ?, normalized = ?, embedding = NULL "
                                    "WHERE id = ?",
                                    (note["content"], note["normalized"], note_id),
                                )
                            conn.execute(
                                "UPDATE notes SET day = ?, updated_at = ?, kind = ?, tags = ?, "
                                "pinned = ?, source = ?, mentions = ?, archived = ?, media = ? "
                                "WHERE id = ?",
                                (
                                    note["day"],
                                    note["updated_at"],
                                    note["kind"],
                                    json.dumps(note["tags"], ensure_ascii=False),
                                    1 if note["pinned"] else 0,
                                    note["source"],
                                    note["mentions"],
                                    archived,
                                    media_json,
                                    note_id,
                                ),
                            )
                        except sqlite3.Error:
                            continue
                        stats["updated"] += 1
                        for entry in note.get("media") or []:
                            if not isinstance(entry, dict):
                                continue
                            self._insert_media_row(
                                conn,
                                note_id=note_id,
                                day=note["day"],
                                media_type=str(entry.get("type") or "file"),
                                path=str(entry.get("path") or ""),
                                thumb=str(entry.get("thumb") or ""),
                                kept=True,
                                note=str(entry.get("note") or ""),
                            )
                    for note_id in existing:
                        if note_id in parsed:
                            continue
                        try:
                            conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
                            conn.execute(
                                "UPDATE media SET note_id = NULL WHERE note_id = ?", (note_id,)
                            )
                        except sqlite3.Error:
                            continue
                        stats["removed"] += 1
                    conn.commit()
                    if callable(self.embed_fn):
                        stats["embedded"] = await self._refresh_missing_embeddings(conn)
            except (sqlite3.Error, OSError) as exc:
                self.logger.warning("[notebook] 重建索引失败：%r", exc)
        return stats

    async def _refresh_missing_embeddings(self, conn: sqlite3.Connection) -> int:
        try:
            rows = conn.execute(
                "SELECT id, content FROM notes WHERE archived = 0 AND embedding IS NULL "
                "AND content != '' LIMIT ?",
                (REINDEX_EMBED_LIMIT,),
            ).fetchall()
        except sqlite3.Error:
            return 0
        embedded = 0
        for start in range(0, len(rows), EMBED_MAX_BATCH):
            batch = rows[start : start + EMBED_MAX_BATCH]
            vectors = await self._embed([str(row["content"]) for row in batch])
            if len(vectors) != len(batch):
                break
            for row, vector in zip(batch, vectors):
                blob = _pack_vector(vector)
                if not blob:
                    continue
                try:
                    conn.execute(
                        "UPDATE notes SET embedding = ? WHERE id = ?",
                        (blob, str(row["id"])),
                    )
                except sqlite3.Error:
                    continue
                embedded += 1
        return embedded
