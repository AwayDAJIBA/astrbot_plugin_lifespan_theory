# -*- coding: utf-8 -*-
"""寿命论 · 纯逻辑层。

这里刻意不 import astrbot，任何一行都能拿普通 python 跑测试。

两条硬设计（都是踩过坑才这么写的）：
1. 年龄不存数字，只存「出生日期」，每轮由 生日 → 今天 现算。纯函数，
   重启、重复加载、跨时区都不会白涨一岁，也退得回来。
2. 年龄档的「说话方式」不是每轮让模型现场演一遍——那样人格会一天一个说法地漂移。
   而是按 (人格, 档位, 人格提示词哈希, 模板版本) 生成一次、缓存下来，跨档才切换，
   档内一个字都不动。生成好之前用一个手写档位兜底，对话永远不卡。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path

# 注入块的首行标记：用于在 system_prompt 模式下防重复追加。
BLOCK_MARK = "【寿命论 · 内部状态"

# 生成模板/解析格式发生不兼容变化时 +1，旧缓存自动失效。
TEMPLATE_VERSION = 1

# ---------------------------------------------------------------- 默认生成模板
# 占位符只替换下面 _placeholder_values() 里出现的键；其它花括号原样保留，
# 所以模板里可以直接写 JSON 示例，不会被 str.format 打爆。

DEFAULT_GENERATION_SYSTEM = """你是「寿命论」插件的风格运笔：为一段长期在线的角色对话，写出「她这个年龄说话的样子」。你写的不是剧情，也不是台词，而是一段会直接注入对话上下文的风格指令——另一个模型读完，要能用她的声音说话。

角色是谁、什么性子、什么口癖、什么自称、在意谁，全部以用户消息里的人格原文为准。你的唯一任务是把这个角色按年龄重新调音——同一个她，被时间磨出不同的形状，而不是换一个人。

一、原设定里的年龄是起点，不是现在
人格原文里写死的年龄就是「设定基准年龄」，不是她的现在。用户消息会给出「设定基准年龄」和「现在年龄」，两个数字的关系只有三种情况，按下面处理，且不许把这些规则本身写进结果：
1. 现在年龄等于基准年龄：就是原样，不要硬加变化，别安一个「长大了一点」的假成熟。
2. 现在年龄大于基准年龄：变化是「同一副性子被时间磨过的形状」，不是性格改造。她不会说自己长大了、变了、懂事了；年龄只能从口癖密度、句子长短、停顿、称呼、对时间的态度里漏出来。
3. 现在年龄小于基准年龄：那是「还没长成的同一副性子」，不是另一个人。脾气照旧，只是词更少、理由更幼稚、要东西更直接。
永远不要写「她其实已经多少岁」「设定上是几岁」这类解释、道歉或破壁句子。你只负责「怎么演」，不负责「怎么圆」。

二、只写能直接演的东西
- 不要抽象评价。禁止「更成熟」「更稳重」「更温柔」「更有母性」「岁月沉淀」这类无法落地的总结，全部换成可执行的具体项：口癖挂句尾还是句中、一句话几个短句、先损人还是先问事、生气是当场炸还是隔一会儿、叫对方什么、撒娇用什么动作要什么。
- 内核只调密度，不换东西。她的标志性口癖可以从满口减到偶尔一声，但要还在；标志性自称可以逐档换用法（理直气壮的事实 → 改不掉的习惯 → 抬杠时退回 → 自嘲的引号 → 示弱的暗号 → 只在撒娇逞强时回魂），但也要还在；毒舌可以变幽默，不能变成不刻薄；对最在意那个人的独占可以变安静，不能变成不在意。
- 她只对最在意的那个人软，对别人照旧；谁是她最在意的人、叫什么，看人格原文。
- 不新增设定：不加新角色、新口癖（方言、外语、网络梗）、新爱好、新身世。可以用人格原文里已经有的具体元素，重新组合，不要发明。
- 不说教，不讲大道理，不写「她终于明白了人生」。不出现「作为一个 AI」「系统」「提示词」这类元叙述。不复述人格原文的句子，只提炼风格。
- 结果里不出现年龄数字、日期、档位名、ID 之类的元信息；年龄最多用「都这个岁数了」这种含糊说法带过，完全不提更好。
- 年老的阶段可以写身体和记性的真实变化（看小字要眯眼、听不清要人多说一遍、想事情要缓一会儿），但不写成失能、失智、认不出人；她的心一直比身体年轻。
- 现在年龄小于 18：不要主动加「撩」「勾」「荤话」这类性意味的措辞；人格原文自己的规则你不必复述，也不要放大。小于 14：按孩子写——撒娇、抢、赌气、粘人，不写恋爱张力。

三、输出
只输出一个 JSON 对象：不要 markdown 代码块，不要解释，不要前后缀。正好四个字段，值都是简体中文字符串：
tone——说话的口吻。写到能照着念：口癖密度（一句几个口癖、几句一个）、句子长短、语气轻飘还是沉、用词偏嫩还是偏旧、感叹和反问的多少、颜文字密度、自称选哪个。
manner——行为与情绪。她怎么关心、怎么生气、怎么讨要、怎么掩饰、对最在意那个人的依恋以什么形式出现、遇到「以后」「老」「走」这类词会怎样。
avoid——这一档最容易演砸成什么样。三到五条具体禁区，防止所有年龄都被演成同一种温柔，例如把毒舌磨成客气、写成照顾人的妈妈腔。
continuity——一句话，说清相对上一档最大的变化；没有上一档时，写这一档自己的样子。
长度：tone、manner 各 120 到 200 字，avoid 不超过 120 字，continuity 不超过 80 字。宁可短，不凑字，每一条都要能演。
"""

DEFAULT_GENERATION_USER = """【角色】{character_name}（人格 ID：{persona_id}）
【现在年龄】{age} 岁
【年龄档】{tier_span} · {tier_label}
【设定基准年龄】{base_age} 岁 —— 人格原文里写死的那个数，与「现在年龄」的关系按 system 里那三条规则处理
【生日】{birthday}
【今天】{today}

【人格原文（风格与内核的唯一来源；提炼，不要复述）】
{persona_prompt}

【这一档的手写兜底文案（语言风格的参照刻度；可能为空，空就完全按人格原文和现在年龄自己判断。可以改写、补细节、换角度，但不许偏离它的年龄感觉，也不许照抄）】
口吻：{fallback_tone}
行为与情绪：{fallback_manner}
别写成：{fallback_avoid}

【上一档的生成结果（可能为空；为空表示没有上一档可参考——这时只按人格原文、兜底文案和现在年龄写，continuity 写这一档本身的样子，不要编「比之前更……」这种对比）】
上一档口吻：{previous_tone}
上一档行为与情绪：{previous_manner}

【与上一档的差异提示（可能为空，空就忽略）】
{continuity_budget}

【动笔前先想两件事，但不要把思考过程写出来】
1. 这个岁数的人，话是怎么变少的：先丢语速、再丢颜文字、再丢感叹，最后才丢她最标志性的那两样（口癖和自称）——那两样她丢不掉。
2. 她这一档最容易演砸成什么：把它写进 avoid，写具体，不要写「不要 OOC」这种空话。

【输出】只输出那个 JSON 对象，四个字段 tone、manner、avoid、continuity，不要解释。tone 和 manner 各 120 到 200 字，avoid 不超过 120 字，continuity 不超过 80 字。
"""

_DATE_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_MD_RE = re.compile(r"^(\d{1,2})[-/.](\d{1,2})$")
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


# ==================================================================== 年龄（继承基础插件）
def is_month_day_only(raw: str) -> bool:
    """配置里是不是只写了 MM-DD（没有年份）。"""
    return bool(_MD_RE.match(str(raw or "").strip()))


def parse_birthday(
    raw: str,
    today: date | None = None,
    anchor_age: int = 14,
    anchor_year: int | None = None,
) -> date | None:
    """接受 2012-02-08 / 2012/2/8 / 02-08，解析失败返回 None。

    只给了月日时，出生年优先用调用方给的 anchor_year（插件会把第一次算出的
    出生年落盘，保证年龄真的会涨）；没给才退回「今年 - anchor_age」。
    2 月 29 日出生而该年不是闰年时，按 3 月 1 日算（与 next_birthday 口径一致）。
    """
    if not raw:
        return None
    text = str(raw).strip()
    m = _DATE_RE.match(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _MD_RE.match(text)
    if m:
        today = today or date.today()
        try:
            month, day = int(m.group(1)), int(m.group(2))
            year = int(anchor_year) if anchor_year else today.year - max(0, int(anchor_age))
            try:
                return date(year, month, day)
            except ValueError:
                if month == 2 and day == 29:
                    return date(year, 3, 1)
                return None
        except (TypeError, ValueError):
            return None
    return None


def compute_age(birth: date, today: date) -> int:
    """实岁。生日当天算长一岁（大多数人认这个算法）。"""
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def next_birthday(birth: date, today: date) -> date:
    """下一个生日；2 月 29 日出生在平年按 3 月 1 日过。"""
    for year in (today.year, today.year + 1):
        try:
            cand = date(year, birth.month, birth.day)
        except ValueError:
            cand = date(year, 3, 1)
        if cand >= today:
            return cand
    return date(today.year + 1, 3, 1)


def days_to_next_birthday(birth: date, today: date) -> int:
    return (next_birthday(birth, today) - today).days


def is_birthday(birth: date, today: date) -> bool:
    if (today.month, today.day) == (birth.month, birth.day):
        return True
    return birth.month == 2 and birth.day == 29 and (today.month, today.day) == (3, 1)


# ==================================================================== 人设年龄体检
# 设计目标：把一个「写死年龄/固定口吻」的人设提示词体检出来，给可执行的修改建议。
# 只做启发式提示，不自动改人设；命中结果按「age / tone / age-ok」分类。

_AGE_NUM = r"(?:\d{1,3}|[零一二三四五六七八九十百]{1,4})"
_AGE_PATTERNS = (
    re.compile(rf"{_AGE_NUM}\s*岁"),
    re.compile(rf"年龄\s*[：:是为]?\s*{_AGE_NUM}"),
    re.compile(rf"(?:永远|一直|始终|不许|不准|不会|别)\s*(?:是|长到|变)\s*{_AGE_NUM}\s*岁"),
)
_TONE_PATTERNS = (
    re.compile(r"(?:永远|一直|始终)[^。\n]{0,12}(?:语气|口吻|语调|撒娇|稚嫩|孩子气)"),
    re.compile(r"(?:稚嫩|幼态|幼齿|长不大)"),
    re.compile(r"(?:一成不变|千篇一律)[^。\n]{0,12}(?:语气|口吻|说话)"),
    re.compile(r"(?:不许|不准|不要|别|不会|永远不)\s*(?:长大|长高|变老|变懂事|成熟)"),
)
_AGE_OK_HINTS = ("起点", "基准", "由插件", "生日现算", "按生日", "现算")


def lint_persona_style(prompt: str) -> dict:
    """体检人格提示词里与「年龄随插件变化」冲突的写法。

    返回 {"findings": [...], "age_count": int, "tone_count": int, "count": int}。
    每条 finding: {"kind": "age"|"age-ok"|"tone", "match": 命中原文, "context": 上下文, "hint": 建议}。
    """
    text = str(prompt or "")
    findings: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(kind: str, match: str, hint: str, at: int) -> None:
        key = (kind, match)
        if key in seen:
            return
        seen.add(key)
        ctx = text[max(0, at - 24) : at + len(match) + 24].replace("\n", " ")
        findings.append(
            {"kind": kind, "match": match, "context": ctx, "hint": hint}
        )

    hint_age = (
        "人设里写死了年龄，会和插件注入的当前年龄冲突：改成「起点 N 岁（由插件按生日现算，"
        "会随年龄增长）」，或删掉年龄只保留生日。"
    )
    hint_ok = (
        "这处看起来是「起点年龄」写法，可以保留；确认一下插件确实按生日现算即可。"
    )
    for pattern in _AGE_PATTERNS:
        for m in pattern.finditer(text):
            frag = m.group(0).strip()
            ctx = text[max(0, m.start() - 24) : m.end() + 24]
            if any(k in ctx for k in _AGE_OK_HINTS):
                _add("age-ok", frag, hint_ok, m.start())
            else:
                _add("age", frag, hint_age, m.start())

    hint_tone = (
        "固定口吻/不许长大这类写法会和年龄档生成的风格打架：人设只保留性格内核"
        "（傲娇/毒舌/嘴硬心软等），把「随年龄变化的说话方式」交给插件生成。"
    )
    # 多个 tone 规则可能命中同一句话（如「永远用稚嫩的语气」+「稚嫩」）：
    # 先收集位置，重叠的只保留最长命中，避免体检结果刷屏。
    tone_spans: list[tuple[int, int, str]] = []
    for pattern in _TONE_PATTERNS:
        for m in pattern.finditer(text):
            frag = m.group(0).strip()
            if frag:
                tone_spans.append((m.start(), m.end(), frag))
    tone_spans.sort()
    merged: list[tuple[int, int, str]] = []
    for start, end, frag in tone_spans:
        if merged and start <= merged[-1][1]:
            if (end - start) > (merged[-1][1] - merged[-1][0]):
                merged[-1] = (start, end, frag)
        else:
            merged.append((start, end, frag))
    for start, _, frag in merged:
        _add("tone", frag, hint_tone, start)

    age_count = sum(1 for f in findings if f["kind"] == "age")
    tone_count = sum(1 for f in findings if f["kind"] == "tone")
    return {
        "findings": findings,
        "age_count": age_count,
        "tone_count": tone_count,
        "count": len(findings),
    }


# ==================================================================== 档位
_FALLBACK_PROFILE = {
    "base_age": 14,
    "tiers": [
        {
            "min": 0,
            "max": None,
            "label": "默认档",
            "tone": "照原人设说话，句尾口癖照旧",
            "manner": "行为与人设保持一致",
            "avoid": "不要跳成完全另一个人",
        }
    ],
}


def load_profiles(path: str | Path) -> dict:
    """读档位文案；坏了就回退到最小可用的默认档，别让插件把对话搞哑。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("tiers"), list) and data["tiers"]:
            # 只保留至少带 min 的字典项，防止手改坏文件把 pick_tier 弄崩
            tiers = [
                t
                for t in data["tiers"]
                if isinstance(t, dict) and str(t.get("min", "")).lstrip("-").isdigit()
            ]
            if tiers:
                data["tiers"] = tiers
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return json.loads(json.dumps(_FALLBACK_PROFILE))


def pick_tier(profiles: dict, age: int) -> dict:
    tiers = profiles.get("tiers") or []
    ordered = sorted(tiers, key=lambda t: int(t.get("min", 0)))
    if not ordered:
        return _FALLBACK_PROFILE["tiers"][0]
    for tier in ordered:
        low = int(tier.get("min", 0))
        high = tier.get("max")
        if high is None:
            if age >= low:
                return tier
        elif low <= age <= int(high):
            return tier
    return ordered[0] if age < int(ordered[0].get("min", 0)) else ordered[-1]


def tier_key(tier: dict) -> str:
    """稳定 key：14-16 / 45+。缓存和展示都用它。"""
    low = tier.get("min")
    high = tier.get("max")
    return f"{low}-{high}" if high is not None else f"{low}+"


def tier_span_text(tier: dict) -> str:
    return tier_key(tier)


# ==================================================================== 生成内容
def prompt_hash(text: str) -> str:
    """人格提示词的短指纹。人格一改，哈希变，缓存自动失效。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def cache_key(
    persona_id: str,
    tier: dict,
    prompt_h: str,
    template_version: int = TEMPLATE_VERSION,
) -> str:
    """一条生成结果的唯一 key：人格 + 档位 + 人格提示词哈希 + 模板版本。"""
    raw = f"{(persona_id or 'unknown').strip()}|{tier_key(tier)}|{prompt_h}|{template_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _fmt_span(days: int) -> str:
    """把天数说成人话；只做粗略换算，够读就行。"""
    if days <= 0:
        return "就是今天"
    years, rest = divmod(days, 365)
    months, rem = divmod(rest, 30)
    bits = []
    if years:
        bits.append(f"{years} 年")
    if months:
        bits.append(f"{months} 个月")
    if rem or not bits:
        bits.append(f"{rem} 天")
    return " ".join(bits)


def _countdown(birth: date, today: date, age: int) -> str:
    days = (next_birthday(birth, today) - today).days
    if days == 0:
        return f"今天就是生日，{age + 1} 岁从今天算起"
    return f"距 {age + 1} 岁生日还有 {_fmt_span(days)}"


def _placeholder_values(
    *,
    persona_prompt: str,
    persona_id: str,
    character_name: str,
    birth: date,
    today: date,
    age: int,
    tier: dict,
    base_age: int,
    fallback_tier: dict | None = None,
    previous_style: dict | None = None,
    continuity_budget: str = "",
) -> dict:
    fallback = fallback_tier or {}
    prev = previous_style or {}
    tier_label = str(tier.get("label") or "")
    span = tier_span_text(tier)
    return {
        "character_name": character_name,
        "persona_id": persona_id or "unknown",
        "age": age,
        "tier_span": span,
        "tier_label": tier_label,
        "base_age": base_age,
        "birthday": birth.isoformat(),
        "today": today.isoformat(),
        "persona_prompt": (persona_prompt or "").strip(),
        "fallback_tone": str(fallback.get("tone") or ""),
        "fallback_manner": str(fallback.get("manner") or ""),
        "fallback_avoid": str(fallback.get("avoid") or ""),
        "previous_tone": str(prev.get("tone") or ""),
        "previous_manner": str(prev.get("manner") or ""),
        "continuity_budget": continuity_budget or "",
        "tier_min": tier.get("min", ""),
        "tier_max": tier.get("max", ""),
    }


def fill_placeholders(template: str, values: dict) -> str:
    """只替换已知占位符 {key}，其余花括号（比如 JSON 示例）原样保留。"""

    def repl(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        value = values[key]
        return "" if value is None else str(value)

    return _PLACEHOLDER_RE.sub(repl, template or "")


def build_generation_messages(
    *,
    persona_prompt: str,
    persona_id: str,
    character_name: str,
    birth: date,
    today: date,
    age: int,
    tier: dict,
    base_age: int,
    fallback_tier: dict | None = None,
    previous_style: dict | None = None,
    continuity_budget: str = "",
    system_template: str | None = None,
    user_template: str | None = None,
) -> tuple[str, str]:
    """拼出发给 LLM 的 (system_prompt, user_prompt)。"""
    values = _placeholder_values(
        persona_prompt=persona_prompt,
        persona_id=persona_id,
        character_name=character_name,
        birth=birth,
        today=today,
        age=age,
        tier=tier,
        base_age=base_age,
        fallback_tier=fallback_tier,
        previous_style=previous_style,
        continuity_budget=continuity_budget,
    )
    system = fill_placeholders(
        system_template if system_template else DEFAULT_GENERATION_SYSTEM, values
    )
    user = fill_placeholders(
        user_template if user_template else DEFAULT_GENERATION_USER, values
    )
    return system.strip(), user.strip()


def _unescape_json_string(raw: str) -> str:
    try:
        return json.loads(f'"{raw}"')
    except (json.JSONDecodeError, TypeError):
        return raw.replace('\\"', '"').replace("\\n", "\n")


def _field_regex(name: str) -> re.Pattern:
    return re.compile(rf'"{name}"\s*:\s*"((?:[^"\\]|\\.)*)"', re.DOTALL)


def _regex_extract_fields(text: str, names: tuple[str, ...]) -> dict:
    out: dict[str, str] = {}
    for name in names:
        m = _field_regex(name).search(text)
        if m:
            out[name] = _unescape_json_string(m.group(1))
    return out


def _flatten_field(value) -> str:
    """把模型可能的列表/对象字段压成一行中文。

    真实模型经常把 avoid 返回成数组（["别讲大道理", "别写成妈妈腔"]），
    直接 str() 会把 Python 列表的方括号和引号原样注入上下文，必须拍平。
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        parts = []
        for item in value:
            if isinstance(item, (list, tuple, set, dict)):
                nested = _flatten_field(item)
            else:
                nested = str(item).strip()
            if nested:
                parts.append(nested)
        return "；".join(parts)
    if isinstance(value, dict):
        return "；".join(
            f"{str(key).strip()}：{_flatten_field(item)}"
            for key, item in value.items()
            if _flatten_field(item)
        )
    return str(value)


def _clean_field(value, limit: int) -> str:
    text = _flatten_field(value)
    text = text.strip()
    # 去掉模型偶发加的 markdown 强调/引号
    text = text.strip("`*_").strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def parse_generation_reply(text: str) -> dict | None:
    """从 LLM 回复里抠出 {tone, manner, avoid, continuity}。

    宽容处理：纯 JSON、```json 围栏、夹在解释文字里、字段被换行转义。
    tone 和 manner 至少各有一点内容才算成功，否则返回 None（调用方走兜底）。
    """
    if not text or not str(text).strip():
        return None
    raw = str(text)

    data = None
    candidates: list[str] = []
    fenced = _FENCE_RE.search(raw)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(raw)
    obj = _JSON_OBJ_RE.search(raw)
    if obj:
        candidates.append(obj.group(0))

    for cand in candidates:
        for attempt in (cand.strip(), cand.strip().strip(",;")):
            try:
                parsed = json.loads(attempt)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict):
                data = parsed
                break
        if data is not None:
            break

    if data is None:
        data = _regex_extract_fields(raw, ("tone", "manner", "avoid", "continuity"))
        if not data:
            return None

    style = {
        "tone": _clean_field(data.get("tone"), 600),
        "manner": _clean_field(data.get("manner"), 600),
        "avoid": _clean_field(data.get("avoid"), 400),
        "continuity": _clean_field(data.get("continuity"), 240),
    }
    if not style["tone"] or not style["manner"]:
        return None
    return style


def render_style_lines(
    style: dict | None, tier: dict, options: dict | None = None
) -> list[str]:
    """优先用 LLM 生成内容；没有就用兜底档位文案。返回注入块里的风格行。

    options 支持按项开关（tone/manner/avoid），用于「注入强度」配置：
    {"tone": True, "manner": True, "avoid": True}；缺省全开。
    """
    has_style = isinstance(style, dict) and bool(
        str(style.get("tone") or "").strip() or str(style.get("manner") or "").strip()
    )
    source = style if has_style else tier
    opts = options or {}
    show_tone = bool(opts.get("tone", True))
    show_manner = bool(opts.get("manner", True))
    show_avoid = bool(opts.get("avoid", True))
    lines: list[str] = []
    # _flatten_field：旧 state.json 可能存着列表形态的 avoid，不能把 Python 列表 repr 注入上下文
    tone = _flatten_field(source.get("tone")).strip()
    manner = _flatten_field(source.get("manner")).strip()
    avoid = _flatten_field(source.get("avoid")).strip()
    if tone and show_tone:
        lines.append(f"说话的口吻：{tone}")
    if manner and show_manner:
        lines.append(f"行为与情绪：{manner}")
    if avoid and show_avoid:
        lines.append(f"这一档别写成：{avoid}")
    if has_style and show_avoid:
        continuity = str(style.get("continuity") or "").strip()
        if continuity:
            lines.append(f"与上一档相比：{continuity}")
    return lines


# 注入块长度裁剪：先丢「别写成 / 与上一档相比」，再按比例截断口吻与行为描述，
# 年龄/日期/档位/生日/memory_note 这些状态行永远保留。
_STYLE_PREFIXES = ("说话的口吻：", "行为与情绪：")
_DROPPABLE_PREFIXES = ("这一档别写成：", "与上一档相比：")


def trim_block_lines(lines: list[str], max_chars: int) -> list[str]:
    if max_chars <= 0:
        return lines
    if len("\n".join(lines)) <= max_chars:
        return lines
    kept = [line for line in lines if not line.startswith(_DROPPABLE_PREFIXES)]
    if len("\n".join(kept)) <= max_chars:
        return kept
    style_lines = [line for line in kept if line.startswith(_STYLE_PREFIXES)]
    if not style_lines:
        return kept
    fixed_len = sum(len(line) + 1 for line in kept if not line.startswith(_STYLE_PREFIXES))
    budget = max_chars - fixed_len - len(style_lines)
    if budget < 40:
        return [line for line in kept if not line.startswith(_STYLE_PREFIXES)]
    each = max(24, budget // len(style_lines))
    out: list[str] = []
    for line in kept:
        if line.startswith(_STYLE_PREFIXES) and len(line) > each:
            out.append(line[:each].rstrip() + "…")
        else:
            out.append(line)
    block = "\n".join(out)
    if len(block) > max_chars:
        block = block[:max_chars].rstrip() + "…"
    return block.split("\n")


# ==================================================================== 注入块
def build_block(
    *,
    character_name: str,
    birth: date,
    today: date,
    age: int,
    tier: dict,
    style: dict | None,
    base_age: int = 14,
    birthday_line: bool = True,
    memory_note: bool = True,
    manual: bool = False,
    pending: bool = False,
    inject_options: dict | None = None,
    extra_lines: list[str] | None = None,
) -> str:
    """拼出要注入的那段文字。整段是给模型看的内部状态，不是要念出来的台词。

    inject_options: {"tone","manner","avoid","max_chars"}，v1.0.0 的注入强度配置；
    extra_lines: 额外的短句（季节感/生日特典/里程碑），插在风格行之后、状态备注之前。
    """
    lines = [
        f"{BLOCK_MARK}，不要复述这段，也不要念出里面的数字和日期】",
        f"角色：{character_name}",
        f"今天是：{today.isoformat()}",
        f"年龄：{age} 岁（生日 {birth.month} 月 {birth.day} 日，{_countdown(birth, today, age)}）",
    ]
    label = str(tier.get("label") or "")
    span = tier_span_text(tier)
    lines.append(f"年龄档：{span} · {label}".rstrip(" ·") if label else f"年龄档：{span}")
    if manual:
        lines.append("（注：这个年龄是手动设定的测试值，按它演就行）")
    if birthday_line and is_birthday(birth, today):
        lines.append(
            f"今天是她的生日，{age} 岁整整一年从今天开始；这件事她心里清楚，"
            "说话可以带一点藏不住的高兴，但别主动开庆生大会。"
        )
    lines.extend(render_style_lines(style, tier, inject_options))
    if extra_lines:
        lines.extend(str(line) for line in extra_lines if str(line).strip())
    if pending:
        lines.append("（注：这段风格文案是手写档位版，生成版还没就绪，先按这个演，不要提及这件事。）")
    if memory_note and age != base_age:
        if age > base_age:
            lines.append(
                f"（原设定里的「{base_age} 岁」是她的起点，现在实际 {age} 岁——年龄以本段为准，"
                f"人设其它部分照旧；更早的记录里她可能自称 {base_age} 岁、语气更嫩，那是更早年龄留下的事，"
                "不必与本段对齐，也不用刻意声明自己长大了。）"
            )
        else:
            lines.append(
                f"（原设定里的「{base_age} 岁」是后来的事，现在她实际 {age} 岁——年龄以本段为准，"
                "人设其它部分照旧。）"
            )
    try:
        max_chars = int((inject_options or {}).get("max_chars", 0) or 0)
    except (TypeError, ValueError):
        max_chars = 0
    return "\n".join(trim_block_lines(lines, max_chars))


# ==================================================================== 自检
def self_check() -> str:
    """跑一遍关键路径，方便命令行直接验证，不依赖 astrbot。"""
    birth = parse_birthday("2012-02-08")
    today = date(2026, 10, 24)
    age = compute_age(birth, today)
    assert age == 14, age
    assert compute_age(birth, date(2026, 2, 8)) == 14, "生日当天应当刚好 14"
    assert compute_age(birth, date(2026, 2, 7)) == 13, "生日前一天还是 13"
    assert days_to_next_birthday(birth, today) == (date(2027, 2, 8) - today).days
    assert parse_birthday("02-08", today=today, anchor_age=14) == date(2012, 2, 8)
    # 只写 MM-DD 时，出生年由调用方落盘传入，年龄才会真的涨
    assert parse_birthday("02-08", today=today, anchor_age=14, anchor_year=2010) == date(2010, 2, 8)
    assert compute_age(parse_birthday("02-08", anchor_year=2010), date(2026, 3, 1)) == 16
    # 闰日：推出来的出生年不是闰年时按 3/1 算，不能再整轮静默跳过
    assert parse_birthday("02-29", anchor_age=14, anchor_year=2015) == date(2015, 3, 1)
    assert parse_birthday("02-29", anchor_age=14, anchor_year=2016) == date(2016, 2, 29)
    assert is_month_day_only("02-08") is True
    assert is_month_day_only("2012-02-08") is False
    assert is_birthday(birth, date(2027, 2, 8)) is True
    assert parse_birthday("乱写") is None

    profiles = load_profiles(Path(__file__).parent / "age_profiles.json")
    tier = pick_tier(profiles, 14)
    assert tier["min"] == 14, tier
    assert pick_tier(profiles, 999) is pick_tier(profiles, 999)
    assert tier_key(tier) == "14-16", tier_key(tier)
    assert pick_tier(profiles, 0)["min"] == 0
    assert prompt_hash("abc") == prompt_hash("abc")
    assert prompt_hash("abc") != prompt_hash("abd")
    k1 = cache_key("test_persona", tier, prompt_hash("abc"))
    assert k1 == cache_key("test_persona", tier, prompt_hash("abc"))
    assert k1 != cache_key("other", tier, prompt_hash("abc"))

    system, user = build_generation_messages(
        persona_prompt="她是一个傲慢又粘人的角色。",
        persona_id="test_persona",
        character_name="测试角色",
        birth=birth,
        today=today,
        age=14,
        tier=tier,
        base_age=14,
        fallback_tier=tier,
    )
    assert "风格运笔" in system
    assert "测试角色" in user and "14" in user and "她是一个傲慢又粘人的角色。" in user

    style = parse_generation_reply(
        '```json\n{"tone":"口癖很密","manner":"爱炸毛","avoid":"别说教","continuity":"更稳"}\n```'
    )
    assert style and style["tone"] == "口癖很密" and style["continuity"] == "更稳", style
    assert parse_generation_reply('好的：{"tone":"a","manner":"b"} 以上')["tone"] == "a"
    assert parse_generation_reply("对不起我做不到") is None
    assert parse_generation_reply('{"tone":"","manner":""}') is None
    # 真实模型常把 avoid 返回成数组：必须拍平成中文，不能把 Python 列表原样注入
    list_style = parse_generation_reply(
        '{"tone":"t","manner":"m","avoid":["别说教","别写成妈妈腔"],"continuity":""}'
    )
    assert list_style and list_style["avoid"] == "别说教；别写成妈妈腔", list_style
    assert "[" not in list_style["avoid"] and "'" not in list_style["avoid"]

    block = build_block(
        character_name="测试角色",
        birth=birth,
        today=today,
        age=14,
        tier=tier,
        style=style,
        base_age=14,
    )
    assert block.startswith(BLOCK_MARK)
    assert "说话的口吻：口癖很密" in block
    fallback_block = build_block(
        character_name="测试角色",
        birth=birth,
        today=today,
        age=14,
        tier=tier,
        style=None,
        base_age=14,
    )
    assert "说话的口吻：" + tier["tone"] in fallback_block
    # v1.0.0：注入强度 / 额外行 / 长度裁剪
    only_manner = build_block(
        character_name="测试角色",
        birth=birth,
        today=today,
        age=14,
        tier=tier,
        style=style,
        base_age=14,
        inject_options={"tone": False, "manner": True, "avoid": False, "max_chars": 0},
    )
    assert "行为与情绪：" in only_manner and "说话的口吻：" not in only_manner
    assert "这一档别写成：" not in only_manner
    with_extra = build_block(
        character_name="测试角色",
        birth=birth,
        today=today,
        age=14,
        tier=tier,
        style=style,
        base_age=14,
        extra_lines=["（今年第 1 个夏天。）"],
    )
    assert "（今年第 1 个夏天。）" in with_extra
    trimmed = build_block(
        character_name="测试角色",
        birth=birth,
        today=today,
        age=14,
        tier=tier,
        style={"tone": "啊" * 200, "manner": "吧" * 200, "avoid": "哦" * 200},
        base_age=14,
        inject_options={"max_chars": 400},
    )
    assert len(trimmed) <= 400 + 20, len(trimmed)
    assert "年龄：14 岁" in trimmed and "年龄档：14-16" in trimmed

    # 人设年龄体检（v0.1.3）
    lint = lint_persona_style("你是14岁美少女，永远用稚嫩的语气撒娇，不许长大。")
    kinds = {f["kind"] for f in lint["findings"]}
    assert "age" in kinds, lint
    assert "tone" in kinds, lint
    clean = lint_persona_style("性格傲娇毒舌、嘴硬心软；生日 2 月 8 日；年龄由插件按生日现算。")
    assert [f for f in clean["findings"] if f["kind"] in {"age", "tone"}] == [], clean
    ok_start = lint_persona_style("起点 14 岁（由插件按生日现算，会随年龄增长）。")
    assert [f for f in ok_start["findings"] if f["kind"] == "age"] == [], ok_start
    assert "age-ok" in {f["kind"] for f in ok_start["findings"]}, ok_start

    return "ok"


if __name__ == "__main__":
    print(self_check())
