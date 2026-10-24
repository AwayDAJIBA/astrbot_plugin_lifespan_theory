# -*- coding: utf-8 -*-
"""生日年报：模板仓库 + 文案三模式 + 本地渲染（长图 / 分页 / 存档 HTML）。

为什么长这样
============
年报是「一年只出现一次」的功能，所以它必须：
1. **绝不因为外部环境缺失而报错把插件拖死**。LLM 挂了 → 用模板文案；Playwright
   挂了 → 用 Pillow 画；Pillow 也挂了 → 只出 HTML。返回值里如实写清楚实际走了
   哪条路（``engine`` / ``mode`` / ``render_errors``），上层不用猜。
2. **不依赖 AstrBot 的 t2i / 云端渲染**。小本本内容很私密，截图必须在本机完成；
   所以模板是纯静态 HTML，渲染只依赖本机 chromium 或 Pillow。
3. **模板可以用户自定义**（WebUI 编辑 / 导入 zip）。自定义模板必须能校验：
   语法、必需变量、单文件（不允许 extends/include），坏模板在保存时就挡掉，
   而不是在生日当天才炸。
4. **数据可能残缺**。小本本条目、缩略图路径都可能异常（合成数据 / 手改 / 被清理），
   所以所有输入都要先 normalize，缺日期、缺文本、路径不存在都降级跳过。
5. **接口与 SPEC_V1 §4.3 完全一致**，方便 Lead 按契约接线；额外的公开方法
   （``render_preview`` / ``normalize_entries`` / ``TEMPLATE_VARIABLES``）是给
   WebUI 预览和单测用的补充，不影响契约。

对外接口
========
- :class:`TemplateStore`   模板仓库（list/get/save/import_zip/delete/render）
- :class:`YearbookBuilder` 年报生成器（build / compute_targets / render_preview）
- :func:`render_stats`     年报统计（条数、kind 分布、月份热力、媒体数……）
- :func:`normalize_entries` 条目清洗（防脏数据）
"""

from __future__ import annotations

import asyncio
import inspect
import io
import json
import logging
import os
import random
import re
import shutil
import time
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from jinja2 import Environment, TemplateSyntaxError
from jinja2 import meta as jinja_meta
from jinja2 import nodes as jinja_nodes

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 内置模板目录（本文件同级的 templates/）。允许包被复制/打包后仍然能找到。
BUILTIN_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

#: 内置兜底模板：连 templates/ 都丢了的时候也能出一份能看的 HTML。
#: 它必须是合法的 Jinja2 模板、有 .report-block 标记（分页要用）。
FALLBACK_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<style>
  :root { --ink:#23262b; --muted:#8b857c; --line:#e3ded5; --paper:#fbfaf7; --accent:#2f6f9f; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--paper); color:var(--ink);
         font-family:"PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",sans-serif; }
  .report { width:900px; margin:0 auto; padding:72px 76px 96px; }
  .report-block { margin:0 0 44px; }
  .kicker { letter-spacing:.32em; font-size:12px; color:var(--muted); text-transform:uppercase; }
  h1 { font-size:56px; margin:18px 0 6px; font-weight:600; letter-spacing:.02em; }
  .sub { color:var(--muted); font-size:18px; margin:0 0 20px; }
  .age { font-size:96px; font-weight:700; color:var(--accent); line-height:1; }
  .age small { font-size:20px; color:var(--muted); font-weight:400; margin-left:8px; }
  .rule { height:1px; background:var(--line); margin:26px 0; }
  .lead { font-size:19px; line-height:2; }
  h2 { font-size:24px; margin:0 0 14px; font-weight:600; }
  p { font-size:17px; line-height:1.95; margin:0 0 14px; }
  .muted { color:var(--muted); font-size:14px; }
  .stats { display:flex; gap:0; border-top:1px solid var(--line); border-bottom:1px solid var(--line); }
  .stat { flex:1; padding:22px 10px; text-align:center; }
  .stat b { display:block; font-size:34px; font-weight:600; }
  .stat span { font-size:13px; color:var(--muted); }
  .entry { display:flex; gap:18px; padding:12px 0; border-bottom:1px dashed var(--line); }
  .entry .d { width:88px; color:var(--muted); font-size:14px; flex:none; }
  .entry .t { flex:1; font-size:16px; line-height:1.8; }
  .media { display:grid; grid-template-columns:repeat(3,1fr); gap:14px; }
  .media figure { margin:0; }
  .media img { width:100%; height:170px; object-fit:cover; border-radius:6px; display:block; }
  .media figcaption { font-size:13px; color:var(--muted); margin-top:6px; }
  .closing { border-top:1px solid var(--ink); padding-top:28px; font-size:20px; line-height:2; }
  .footer { margin-top:56px; color:var(--muted); font-size:12px; letter-spacing:.08em; }
  .empty { color:var(--muted); font-size:16px; }
</style></head>
<body><main class="report">
  <section class="report-block">
    <div class="kicker">LIFESPAN · YEARBOOK · {{ year }}</div>
    <h1>{{ title }}</h1>
    <p class="sub">{{ subtitle }}</p>
    <div class="rule"></div>
    <div class="age">{{ age }}<small>岁</small></div>
    <p class="muted">{{ birthday_line }}</p>
  </section>
  <section class="report-block">
    <p class="lead">{{ intro }}</p>
  </section>
  <section class="report-block">
    <div class="stats">
      <div class="stat"><b>{{ stats.total }}</b><span>条记录</span></div>
      <div class="stat"><b>{{ stats.active_months }}</b><span>个活跃月份</span></div>
      <div class="stat"><b>{{ stats.pinned }}</b><span>条约定</span></div>
      <div class="stat"><b>{{ stats.media }}</b><span>个媒体</span></div>
    </div>
  </section>
  {% for section in sections %}
  <section class="report-block">
    <h2>{{ section.heading }}</h2>
    <p>{{ section.body }}</p>
  </section>
  {% endfor %}
  {% if entries_by_month %}
  <section class="report-block">
    <h2>这一年的月份</h2>
    {% for group in entries_by_month %}
    <div class="muted" style="margin:18px 0 4px;">{{ group.label }}</div>
    {% for entry in group.entries %}
    <div class="entry"><div class="d">{{ entry.date_text }}</div><div class="t">{{ entry.text }}</div></div>
    {% endfor %}
    {% endfor %}
  </section>
  {% endif %}
  {% if media %}
  <section class="report-block">
    <h2>留下来的一些画面</h2>
    <div class="media">
      {% for item in media %}
      <figure><img src="{{ item.src }}" alt="{{ item.note or item.type_label }}"><figcaption>{{ item.caption }}</figcaption></figure>
      {% endfor %}
    </div>
  </section>
  {% endif %}
  <section class="report-block">
    <div class="closing">{{ closing }}</div>
  </section>
  <section class="report-block footer">GENERATED {{ generated_at }} · THEME {{ theme }} · MODE {{ mode }}</section>
</main></body></html>
"""

#: 空模板兜底时给上层看的原因（模板仓库空 / 自定义模板渲染炸了）。
TEMPLATE_FALLBACK_NAME = "fallback"

#: kind 枚举 → 中文名 / emoji / 展示顺序。顺序决定统计和模板里的排列。
KIND_META: dict[str, dict[str, str]] = {
    "preference": {"label": "喜好", "emoji": "✨"},
    "event": {"label": "做过的事", "emoji": "🏃"},
    "mood": {"label": "心情", "emoji": "🌙"},
    "person": {"label": "在意的人", "emoji": "💫"},
    "commitment": {"label": "约定", "emoji": "🤝"},
    "other": {"label": "其他", "emoji": "📝"},
}

#: 兼容用户手改 / 旧数据里的中文 kind。
KIND_ALIASES: dict[str, str] = {
    "喜好": "preference",
    "喜欢": "preference",
    "偏好": "preference",
    "事件": "event",
    "经历": "event",
    "做过的事": "event",
    "心情": "mood",
    "情绪": "mood",
    "人": "person",
    "人物": "person",
    "在意的人": "person",
    "约定": "commitment",
    "计划": "commitment",
    "承诺": "commitment",
    "其他": "other",
    "other_kind": "other",
}

MONTH_LABELS: tuple[str, ...] = (
    "一月",
    "二月",
    "三月",
    "四月",
    "五月",
    "六月",
    "七月",
    "八月",
    "九月",
    "十月",
    "十一月",
    "十二月",
)
WEEKDAY_LABELS: tuple[str, ...] = (
    "星期一",
    "星期二",
    "星期三",
    "星期四",
    "星期五",
    "星期六",
    "星期日",
)

#: 兜底设计：文案模式。SPEC §6 里就是这三个值。
CONTENT_MODES: tuple[str, ...] = ("llm", "template", "llm_guided")
MODE_LABELS: dict[str, str] = {
    "llm": "模型自由生成",
    "template": "纯模板",
    "llm_guided": "模板骨架 + 模型润色",
}

#: 渲染引擎。auto = Playwright → Pillow → 只出 HTML。
RENDER_ENGINES: tuple[str, ...] = ("auto", "playwright", "pillow", "html")

#: 长图 / 分页的目标尺寸（CSS 像素；Playwright 截图会乘以 device_scale_factor）。
REPORT_WIDTH = 900
REPORT_PAGE_HEIGHT = 1600
#: 分页时从块顶往上留一点空隙，避免下一页的标题贴着图片上边缘。
PAGE_BOUNDARY_GAP = 36
#: 页面超过这个 CSS 高度就不再用 2x 截图，避免截图缓冲区过大。
SCALE2_MAX_HEIGHT = 6000

#: 单个条目展示文本上限；过长会截断（年报不是日记原文备份，全文在小本本里）。
MAX_ENTRY_TEXT = 240
#: 年报正文最多渲染多少条条目；统计仍是全量（可能显示「只放了其中一些」）。
MAX_DISPLAY_ENTRIES = 60
#: 送给 LLM 的条目数量 / 单条长度上限，控制成本。
MAX_LLM_ENTRIES = 60
MAX_LLM_ENTRY_CHARS = 140
#: 自定义模板单文件上限（防 zip 炸弹 / 误传大文件）。
MAX_TEMPLATE_CHARS = 300_000
MAX_TEMPLATE_FILES = 50
MAX_ZIP_BYTES = 20 * 1024 * 1024

#: 模板名只允许这些字符，直接挡住 `../` 之类的路径穿越。
_TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{1,64}$")

#: 模板文件顶部元信息注释：{# theme-title: 极简白 #} / {# theme-desc: ... #}
_META_TITLE_RE = re.compile(r"\{#\s*theme-title\s*:\s*(.+?)\s*#\}", re.S)
_META_DESC_RE = re.compile(r"\{#\s*theme-desc\s*:\s*(.+?)\s*#\}", re.S)
_TITLE_TAG_RE = re.compile(r"<title>\s*(.*?)\s*</title>", re.I | re.S)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"(\d{4})\s*[-/\.年]\s*(\d{1,2})\s*[-/\.月]\s*(\d{1,2})")
_WHITESPACE_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MEDIA_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".webm",
    ".flv",
    ".m4v",
}

#: Pillow 降级渲染用的调色板（与 templates/ 的主题呼应）。
THEME_PALETTES: dict[str, dict[str, tuple[int, int, int]]] = {
    "minimal": {
        "bg": (250, 249, 246),
        "ink": (34, 36, 40),
        "muted": (128, 124, 116),
        "accent": (47, 111, 159),
        "soft": (238, 240, 244),
        "line": (224, 221, 214),
    },
    "journal": {
        "bg": (252, 248, 238),
        "ink": (60, 51, 41),
        "muted": (142, 126, 100),
        "accent": (178, 121, 60),
        "soft": (246, 237, 219),
        "line": (226, 213, 188),
    },
    "film": {
        "bg": (23, 25, 28),
        "ink": (236, 232, 224),
        "muted": (150, 148, 142),
        "accent": (226, 178, 104),
        "soft": (37, 40, 44),
        "line": (58, 60, 64),
    },
    "birthday": {
        "bg": (252, 245, 240),
        "ink": (62, 33, 32),
        "muted": (152, 112, 101),
        "accent": (188, 62, 62),
        "soft": (247, 229, 222),
        "line": (230, 205, 197),
    },
}

#: 模板标题兜底（模板文件没写元信息时用）。
THEME_TITLE_FALLBACK: dict[str, str] = {
    "minimal": "极简白",
    "journal": "手账格纸",
    "film": "胶片暗色",
    "birthday": "生日红金",
}
_THEME_DESC_FALLBACK: dict[str, str] = {
    "minimal": "留白、细线、大字号，像一本安静的年度小册子。",
    "journal": "横线纸、胶带、便签，把一年写成手账。",
    "film": "暗房、胶片孔、金色字幕，像一部一年份的短片。",
    "birthday": "酒红与烫金，把生日当成封面上的一枚印章。",
}

#: 开场短句（按年份取模选择，保证同一年同一句，单测可复现）。
OPENING_QUOTES: tuple[str, ...] = (
    "把这一年折好，夹进书里。",
    "时间不响，记录替它出声。",
    "一年份的细枝末节，收进一张纸里。",
    "又长一岁，也又多了一年的字。",
    "翻过去是去年，翻回来是今天。",
    "这一页写满了，就该翻篇了。",
)

#: 给模板作者 / WebUI 用的变量说明。新增变量必须在这里登记，方便前端提示。
TEMPLATE_VARIABLES: dict[str, str] = {
    "title": "年报标题，如「德吉可的 2026」",
    "subtitle": "副标题，一句话",
    "character_name": "角色名",
    "age": "年龄（整数）",
    "age_prev": "去年的年龄（整数，age-1）",
    "year": "年报年份（整数）",
    "today": "生成日期 ISO 字符串",
    "today_text": "生成日期中文，如「2026 年 10 月 24 日」",
    "weekday_text": "星期几",
    "birthday_line": "生日特典行（可能为空串）",
    "opening_quote": "开场短句",
    "mode": "实际使用的文案模式 llm/template/llm_guided",
    "mode_label": "文案模式中文名",
    "theme": "实际使用的模板名",
    "theme_title": "模板中文名",
    "theme_desc": "模板说明",
    "generated_at": "生成时间字符串",
    "intro": "开场正文（一段）",
    "sections": "正文段落列表：[{heading, body}]",
    "closing": "结尾正文（一段）",
    "stats": (
        "统计对象：total/by_kind/months(12 个月数组)/month_heat/kinds/"
        "active_months/busiest_month/busiest_month_label/busiest_month_count/"
        "first_date/last_date/span_days/pinned/media/media_by_type/chars/avg_chars/top_tags"
    ),
    "entries": "展示用条目列表（已清洗、按日期排序、最多 60 条）",
    "entries_by_month": "按月分组条目：[{month,label,count,entries}]（只含有条目的月份）",
    "months": "12 个月热力：[{month,label,count,level(0-4)}]",
    "kinds": "kind 分布：[{key,label,emoji,count,pct}]（只含非零项）",
    "highlights": "精选条目（约定/最长/分散月份，最多 8 条）",
    "media": "媒体列表：[{src,type,type_label,note,date_text,caption}]",
    "has_entries": "是否有条目（bool）",
    "has_media": "是否有媒体（bool）",
    "entry_total": "条目总数（含未展示的）",
    "entries_shown": "实际展示条数",
    "truncation_note": "条目过多时的提示文案（可能为空串）",
    "media_total": "媒体总数",
    "tokens": "LLM 用量近似统计 {llm_calls,prompt_chars,completion_chars,approx_tokens}",
}

#: 送 LLM 的系统提示词（三模式共用；llm_guided 会另加一条「按骨架来」的要求）。
LLM_SYSTEM_PROMPT = """你是一位克制、细腻的中文文案编辑，正在为一份「角色生日年报」写文字。

写作要求：
1. 只能使用给定素材里的事实；可以概括、抒情，但绝不编造没记录过的事件。
2. 角色用名字或「她」，不要用「它」「该角色」「AI」「模型」「人格」「提示词」这类词。
3. 把「小本本」「记录库」这类系统词换成「这一年的字」「手账」「记下的」；也不要说「素材」「数据」。
4. 短句为主，少用四字成语；不用「时光荏苒、岁月如梭、白驹过隙」这类套话，不说教。
5. 几段正文各自有独立主题，不要互相重复。
6. 只输出一个 JSON 对象，不要 Markdown 代码围栏、不要任何解释文字。
JSON 结构：
{{"title": "≤12字", "subtitle": "≤24字", "intro": "80~150字", "sections": [{{"heading": "≤8字", "body": "60~140字"}}], "closing": "40~80字"}}
"""

#: llm_guided 追加要求（附在系统提示词后面）。
LLM_GUIDED_EXTRA = """7. 会给你一份「骨架」（标题/副标题/三段小标题和要点）。请保持三段的数量、顺序和小标题含义不变，把要点扩写成通顺的文案，不要新增素材里没有的事实。
"""


# ---------------------------------------------------------------------------
# 基础小工具
# ---------------------------------------------------------------------------


def _safe_int(value: Any, default: int = 0, *, lo: int | None = None, hi: int | None = None) -> int:
    """把任意配置值/数据值安全转 int；越界夹紧。配置写错不该让年报炸掉。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if lo is not None:
        number = max(lo, number)
    if hi is not None:
        number = min(hi, number)
    return number


def _safe_bool(value: Any, default: bool = False) -> bool:
    """把配置里的 true/"true"/"开"/1 等安全转 bool；认不出来用默认值。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "开", "是", "启用"}:
        return True
    if text in {"0", "false", "no", "off", "关", "否", "禁用", ""}:
        return False
    return default


def _clean_text(value: Any, limit: int = 0) -> str:
    """把任意值压成单行干净文本（去控制字符、折叠空白、按需截断）。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(_clean_text(item) for item in value if item is not None)
    text = str(value)
    text = _CONTROL_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if limit > 0 and len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def _now_text() -> str:
    """本地时间的展示字符串。只用于落盘/页面脚注，不参与任何日期计算。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _iso_now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _weekday_text(day: date) -> str:
    return WEEKDAY_LABELS[day.weekday()]


def _cn_date_text(day: date) -> str:
    return f"{day.year} 年 {day.month} 月 {day.day} 日"


def _month_label(month: int) -> str:
    return MONTH_LABELS[month - 1] if 1 <= month <= 12 else ""


def _short_error(exc: BaseException, limit: int = 160) -> str:
    """异常压成一行（不落可能很长的 traceback，也不回显隐私内容）。"""
    text = f"{type(exc).__name__}: {exc}"
    return _clean_text(text, limit)


def _parse_date_value(value: Any) -> date | None:
    """从 date/datetime/时间戳/字符串里尽力抠出一个日期；失败返回 None。

    小本本的条目可能来自 markdown 手改、sqlite、旧版本数据，日期格式不统一；
    这里只认「能确定是某一天」的输入，认不出来就当作无日期（排序放最后），
    而不是抛错。
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value)).date()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    match = _DATE_RE.search(text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def _normalize_kind(raw: Any) -> str:
    """kind 归一：英文枚举、中文别名、未知值都收敛到 KIND_META 的 key。"""
    text = _clean_text(raw).lower()
    if not text:
        return "other"
    if text in KIND_META:
        return text
    if text in KIND_ALIASES:
        return KIND_ALIASES[text]
    for alias, kind in KIND_ALIASES.items():
        if alias and alias in text:
            return kind
    return "other"


def _entry_text(raw: dict) -> str:
    """从条目 dict 里找正文：兼容 text/content/note/summary/detail 等字段名。"""
    for key in ("text", "content", "note", "summary", "detail", "value", "title"):
        if key in raw:
            text = _clean_text(raw.get(key), MAX_ENTRY_TEXT)
            if text:
                return text
    return ""


def _normalize_tags(raw: Any) -> list[str]:
    """标签归一：list 或逗号分隔字符串都行；去重、截断。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[,，、;；\s]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(item) for item in raw]
    else:
        parts = [str(raw)]
    out: list[str] = []
    for part in parts:
        tag = _clean_text(part, 24)
        if tag and tag not in out:
            out.append(tag)
        if len(out) >= 6:
            break
    return out


def _media_type_of(item: Any) -> str:
    """判断媒体类型：video / image（路径后缀优先，其次 type 字段）。"""
    if isinstance(item, str):
        suffix = Path(item).suffix.lower()
        return "video" if suffix in {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".m4v"} else "image"
    if not isinstance(item, dict):
        return "image"
    raw_type = _clean_text(item.get("type") or item.get("kind") or "").lower()
    if "video" in raw_type or "视频" in raw_type or "record" in raw_type:
        return "video"
    for key in ("path", "thumb", "file", "src", "url"):
        value = item.get(key)
        if value:
            suffix = Path(str(value)).suffix.lower()
            if suffix in {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".m4v"}:
                return "video"
    return "image"


def _count_media(media: Any) -> tuple[int, dict[str, int]]:
    """统计媒体数量与类型分布；只数「看起来有效」的项，坏数据忽略。"""
    total = 0
    by_type = {"image": 0, "video": 0}
    if not isinstance(media, (list, tuple)):
        return 0, by_type
    for item in media:
        if isinstance(item, str):
            if not item.strip():
                continue
        elif isinstance(item, dict):
            if not any(item.get(key) for key in ("path", "thumb", "file", "src", "url", "note")):
                continue
        else:
            continue
        kind = _media_type_of(item)
        by_type[kind] = by_type.get(kind, 0) + 1
        total += 1
    return total, by_type


# ---------------------------------------------------------------------------
# 条目清洗 + 统计
# ---------------------------------------------------------------------------


def normalize_entries(entries: Any, *, text_limit: int = MAX_ENTRY_TEXT) -> list[dict]:
    """把外部传进来的条目清洗成年报统一结构（脏数据不抛错、直接跳过/降级）。

    返回的每条：``id/date/year/month/day/date_text/kind/kind_label/kind_emoji/
    text/tags/pinned/media/source``。年报的统计、模板渲染、LLM 提示词都只吃这个
    结构，所以输入格式怎么变都只需要改这一个函数。
    """
    result: list[dict] = []
    if not isinstance(entries, (list, tuple)):
        return result
    for index, raw in enumerate(entries):
        if isinstance(raw, str):
            raw = {"text": raw}
        elif not isinstance(raw, dict):
            continue
        text = _clean_text(_entry_text(raw), text_limit)
        if not text:
            continue
        day = None
        for key in ("date", "day", "created_at", "created", "time", "at", "ts", "timestamp"):
            if key in raw and raw.get(key) not in (None, ""):
                day = _parse_date_value(raw.get(key))
                if day is not None:
                    break
        kind = _normalize_kind(raw.get("kind") or raw.get("type") or raw.get("category"))
        kind_meta = KIND_META.get(kind, KIND_META["other"])
        media = raw.get("media")
        result.append(
            {
                "id": str(raw.get("id") or f"entry-{index:04d}"),
                "date": day.isoformat() if day else None,
                "year": day.year if day else None,
                "month": day.month if day else None,
                "day": day.day if day else None,
                "date_text": f"{day.month} 月 {day.day} 日" if day else "",
                "kind": kind,
                "kind_label": kind_meta["label"],
                "kind_emoji": kind_meta["emoji"],
                "text": text,
                "tags": _normalize_tags(raw.get("tags")),
                "pinned": bool(raw.get("pinned")) or kind == "commitment",
                "media": list(media) if isinstance(media, (list, tuple)) else [],
                "source": _clean_text(raw.get("source"), 32),
            }
        )
    return result


def render_stats(entries: Any, media: Any = None) -> dict:
    """年报用统计：条数、kind 分布、月份热力、媒体数、日期跨度等。

    SPEC_V1 §4.3 要求的统计出口。它是纯函数、不碰文件系统，年报渲染和单测都直接
    用它；所有计数都吃 :func:`normalize_entries` 清洗后的数据，脏条目不会让数字
    对不上。
    """
    normalized = normalize_entries(entries)
    total = len(normalized)
    by_kind: dict[str, int] = {key: 0 for key in KIND_META}
    months = [0] * 12
    dates: list[date] = []
    pinned = 0
    chars = 0
    ages: list[int] = []
    tag_counter: dict[str, int] = {}
    for entry in normalized:
        by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + 1
        chars += len(entry["text"])
        if entry["pinned"]:
            pinned += 1
        if entry["month"] and 1 <= entry["month"] <= 12:
            months[entry["month"] - 1] += 1
        if entry["date"]:
            try:
                dates.append(date.fromisoformat(entry["date"]))
            except ValueError:
                pass
        for tag in entry["tags"]:
            tag_counter[tag] = tag_counter.get(tag, 0) + 1
    # age_span：条目里带 age 字段时才有意义（小本本本身不存年龄，兼容旧数据）。
    raw_ages: list[int] = []
    if isinstance(entries, (list, tuple)):
        for raw in entries:
            if isinstance(raw, dict) and raw.get("age") is not None:
                raw_ages.append(_safe_int(raw.get("age"), -1))
    ages = [age for age in raw_ages if age >= 0]
    first_date = min(dates).isoformat() if dates else None
    last_date = max(dates).isoformat() if dates else None
    span_days = (max(dates) - min(dates)).days + 1 if dates else None
    active_months = sum(1 for count in months if count > 0)
    busiest_month = 0
    busiest_count = 0
    for index, count in enumerate(months, 1):
        if count > busiest_count:
            busiest_count = count
            busiest_month = index
    max_count = busiest_count
    month_heat = []
    for index, count in enumerate(months, 1):
        if count <= 0:
            level = 0
        elif max_count <= 1:
            level = 1
        else:
            level = 1 + int(3 * (count - 1) / (max_count - 1))
        month_heat.append(
            {
                "month": index,
                "label": MONTH_LABELS[index - 1],
                "short": f"{index}月",
                "count": count,
                "level": min(4, max(0, level)),
            }
        )
    kinds = [
        {
            "key": key,
            "label": meta["label"],
            "emoji": meta["emoji"],
            "count": by_kind.get(key, 0),
            "pct": int(round(100.0 * by_kind.get(key, 0) / total)) if total else 0,
        }
        for key, meta in KIND_META.items()
        if by_kind.get(key, 0) > 0
    ]
    kinds.sort(key=lambda item: -item["count"])
    media_total, media_by_type = _count_media(media)
    top_tags = [
        {"tag": tag, "count": count}
        for tag, count in sorted(tag_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    ]
    return {
        "total": total,
        "by_kind": by_kind,
        "kinds": kinds,
        "months": months,
        "month_heat": month_heat,
        "active_months": active_months,
        "busiest_month": busiest_month,
        "busiest_month_label": MONTH_LABELS[busiest_month - 1] if busiest_month else "",
        "busiest_month_count": busiest_count,
        "first_date": first_date,
        "last_date": last_date,
        "span_days": span_days,
        "pinned": pinned,
        "media": media_total,
        "media_by_type": media_by_type,
        "chars": chars,
        "avg_chars": round(chars / total, 1) if total else 0.0,
        "top_tags": top_tags,
        "age_span": [min(ages), max(ages)] if ages else None,
    }


# ---------------------------------------------------------------------------
# 模板仓库
# ---------------------------------------------------------------------------


class TemplateStore:
    """年报模板仓库：内置模板（只读）+ 用户模板（可写）两层。

    为什么分两层：内置模板随插件发布、升级时整体替换；用户模板存在插件数据目录，
    绝不会被升级覆盖。同名时用户模板覆盖内置模板（可以在 WebUI 里「改一份」），
    删掉用户版本就回到内置版本。

    安全：模板名只允许中文/字母/数字/下划线/短横线，杜绝路径穿越；自定义模板保存
    前做 Jinja2 语法 + 必需变量 + 单文件校验，坏模板在保存时就挡掉。
    """

    #: 年报模板必须用到的变量（SPEC §4.3：("title","entries","stats",...)）。
    REQUIRED_VARS: tuple[str, ...] = ("title", "character_name", "age", "year", "entries", "stats")
    #: 必需变量允许的等价写法：内置模板按「月」渲染条目，用的是 entries_by_month；
    #: 自定义模板写 `{{ entries }}` 或 `{% for g in entries_by_month %}` 都算合规。
    REQUIRED_VAR_ALIASES: dict[str, tuple[str, ...]] = {
        "entries": ("entries", "entries_by_month"),
    }

    def __init__(self, builtin_dir: Path, user_dir: Path):
        self.builtin_dir = Path(builtin_dir)
        self.user_dir = Path(user_dir)
        # 单个 Environment 复用；不挂 loader（模板内容来自 get() 的字符串）。
        self._env = Environment(autoescape=True, keep_trailing_newline=True)

    # ---- 查询 --------------------------------------------------------
    def list(self) -> list[dict]:
        """列出全部模板：[{name,title,desc,source,updated_at,bytes}]。

        用户模板与内置模板同名时，只出现用户那一条（source="user"），列表里不会
        出现两个同名项让 WebUI 困惑。
        """
        items: dict[str, dict] = {}

        def scan(base: Path, source: str) -> None:
            if not base or not base.is_dir():
                return
            try:
                paths = sorted(
                    (path for path in base.glob("*.html") if path.is_file()),
                    key=lambda path: path.name,
                )
            except OSError:
                return
            for path in paths:
                name = path.stem
                if not _TEMPLATE_NAME_RE.fullmatch(name):
                    continue
                try:
                    source_text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                title, desc = self._parse_meta(source_text, name)
                try:
                    updated_at = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
                    size = path.stat().st_size
                except OSError:
                    updated_at = ""
                    size = len(source_text.encode("utf-8"))
                items[name] = {
                    "name": name,
                    "title": title,
                    "desc": desc,
                    "source": source,
                    "updated_at": updated_at,
                    "bytes": size,
                }

        scan(self.builtin_dir, "builtin")
        scan(self.user_dir, "user")
        return [items[name] for name in sorted(items)]

    def exists(self, name: str) -> bool:
        """模板是否存在（用户层优先）。"""
        try:
            self._clean_name(name)
        except ValueError:
            return False
        return (self.user_dir / f"{name}.html").is_file() or (self.builtin_dir / f"{name}.html").is_file()

    def get(self, name: str) -> str:
        """取模板 HTML 文本；不存在 raise KeyError。"""
        name = self._clean_name(name)
        for base in (self.user_dir, self.builtin_dir):
            path = base / f"{name}.html"
            if path.is_file():
                try:
                    return path.read_text(encoding="utf-8")
                except OSError as exc:
                    raise KeyError(f"读取模板失败：{name}（{_short_error(exc)}）") from exc
        raise KeyError(f"模板不存在：{name}")

    def stats(self) -> dict:
        """模板数量概览（WebUI 首页/健康检查用）。"""
        items = self.list()
        return {
            "total": len(items),
            "builtin": sum(1 for item in items if item["source"] == "builtin"),
            "user": sum(1 for item in items if item["source"] == "user"),
        }

    # ---- 写入 --------------------------------------------------------
    def save(self, name: str, html: str) -> dict:
        """保存用户模板；校验不合格 raise ValueError（WebUI 可直接把消息回给用户）。"""
        name = self._clean_name(name)
        if not isinstance(html, str):
            raise ValueError("模板内容必须是字符串")
        title, desc = self._parse_meta(html, name)
        self._validate_template(html)
        self.user_dir.mkdir(parents=True, exist_ok=True)
        path = self.user_dir / f"{name}.html"
        data = html.encode("utf-8")
        self._atomic_write(path, data)
        return {
            "ok": True,
            "name": name,
            "title": title,
            "desc": desc,
            "source": "user",
            "path": str(path),
            "bytes": len(data),
            "updated_at": _now_text(),
        }

    def import_zip(self, filename: str, data: bytes) -> dict:
        """从 zip 里解出 *.html 模板逐个保存。

        返回 ``{"ok","filename","imported":[...],"skipped":[{"filename","reason"}],"total"}``；
        zip 本身坏掉 raise ValueError，单个模板坏掉只记进 skipped，不影响其它模板
        （用户可能一次导入一整套模板，其中一份改坏了）。
        """
        safe_filename = _clean_text(filename, 120) or "upload.zip"
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ValueError("导入内容为空")
        if len(data) > MAX_ZIP_BYTES:
            raise ValueError(f"zip 太大（上限 {MAX_ZIP_BYTES // (1024 * 1024)}MB）")
        imported: list[dict] = []
        skipped: list[dict] = []
        try:
            archive = zipfile.ZipFile(io.BytesIO(bytes(data)))
        except (zipfile.BadZipFile, OSError) as exc:
            raise ValueError(f"不是有效的 zip 文件：{_short_error(exc)}") from exc
        with archive:
            members = [info for info in archive.infolist() if not info.is_dir()]
            if len(members) > MAX_TEMPLATE_FILES:
                raise ValueError(f"zip 里文件太多（上限 {MAX_TEMPLATE_FILES} 个）")
            for info in members:
                raw_name = info.filename
                if "__MACOSX" in raw_name or Path(raw_name).name.startswith("."):
                    continue
                suffix = Path(raw_name).suffix.lower()
                if suffix not in {".html", ".htm"}:
                    skipped.append({"filename": raw_name, "reason": "不是 HTML 文件"})
                    continue
                stem = Path(raw_name).stem
                name = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]", "_", stem)[:64] or "template"
                try:
                    raw = archive.read(info)
                except (RuntimeError, OSError, KeyError) as exc:
                    skipped.append({"filename": raw_name, "reason": f"读取失败：{_short_error(exc)}"})
                    continue
                text = self._decode_html(raw)
                if text is None:
                    skipped.append({"filename": raw_name, "reason": "编码不是 utf-8/gb18030，无法解析"})
                    continue
                try:
                    saved = self.save(name, text)
                except ValueError as exc:
                    skipped.append({"filename": raw_name, "reason": str(exc)})
                    continue
                imported.append({**saved, "filename": raw_name})
        return {
            "ok": bool(imported),
            "filename": safe_filename,
            "imported": imported,
            "skipped": skipped,
            "total": len(imported) + len(skipped),
        }

    def delete(self, name: str) -> dict:
        """删除用户模板；内置模板不能删（返回 ok=False 而不是抛错，WebUI 好展示）。"""
        name = self._clean_name(name)
        path = self.user_dir / f"{name}.html"
        if path.is_file():
            try:
                path.unlink()
            except OSError as exc:
                return {"ok": False, "name": name, "error": f"删除失败：{_short_error(exc)}"}
            return {"ok": True, "name": name, "source": "user"}
        if (self.builtin_dir / f"{name}.html").is_file():
            return {"ok": False, "name": name, "error": "内置模板不能删除（可以保存一个同名用户模板来覆盖）"}
        return {"ok": False, "name": name, "error": "模板不存在"}

    def delete_user_templates(self) -> int:
        """清空全部用户模板（导入整套模板前的「重置」用），返回删除数量。"""
        if not self.user_dir.is_dir():
            return 0
        removed = 0
        for path in self.user_dir.glob("*.html"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    # ---- 渲染 --------------------------------------------------------
    def render(self, name: str, context: dict) -> str:
        """用指定模板渲染 HTML；语法/渲染错误统一 raise ValueError，带模板名。"""
        source = self.get(name)
        return self.render_string(source, context, label=name)

    def render_string(self, source: str, context: dict, *, label: str = "inline") -> str:
        """渲染一段模板源码（兜底模板 / 预览用）。"""
        try:
            template = self._env.from_string(source)
            return template.render(**(context or {}))
        except TemplateSyntaxError as exc:
            raise ValueError(f"模板 {label} 语法错误（第 {exc.lineno} 行）：{exc.message}") from exc
        except Exception as exc:  # noqa: BLE001 渲染错误也要变成可展示的消息
            raise ValueError(f"模板 {label} 渲染失败：{_short_error(exc)}") from exc

    # ---- 内部 --------------------------------------------------------
    @staticmethod
    def _clean_name(name: Any) -> str:
        """模板名校验（同时用于所有读写入口，统一挡路径穿越）。"""
        text = _clean_text(name, 64)
        if not text or not _TEMPLATE_NAME_RE.fullmatch(text):
            raise ValueError("模板名只能包含中文/字母/数字/下划线/短横线，长度 1~64")
        return text

    def _validate_template(self, html: str) -> None:
        """保存前校验：大小、完整 HTML、Jinja2 语法、单文件、必需变量。"""
        if not html or not html.strip():
            raise ValueError("模板内容为空")
        encoded_len = len(html.encode("utf-8"))
        if encoded_len > MAX_TEMPLATE_CHARS:
            raise ValueError(f"模板太大（{encoded_len} 字节，上限 {MAX_TEMPLATE_CHARS}）")
        lowered = html.lower()
        if "<html" not in lowered and "<body" not in lowered:
            raise ValueError("模板必须是完整 HTML 文档（至少包含 <html> 或 <body> 标签）")
        try:
            ast = self._env.parse(html)
        except TemplateSyntaxError as exc:
            raise ValueError(f"模板语法错误（第 {exc.lineno} 行）：{exc.message}") from exc
        for node in ast.find_all((jinja_nodes.Extends, jinja_nodes.Include, jinja_nodes.Import, jinja_nodes.FromImport)):
            raise ValueError("自定义模板不支持 extends/include/import，请把模板写成单文件 HTML")
        used = jinja_meta.find_undeclared_variables(ast)
        missing = [
            var
            for var in self.REQUIRED_VARS
            if not any(alias in used for alias in self.REQUIRED_VAR_ALIASES.get(var, (var,)))
        ]
        if missing:
            def _alias_text(var: str) -> str:
                aliases = self.REQUIRED_VAR_ALIASES.get(var, (var,))
                return " 或 ".join("{{ %s }}" % alias for alias in aliases)

            raise ValueError(
                "模板缺少必需变量："
                + "、".join(_alias_text(var) for var in missing)
                + "（可用变量见 TEMPLATE_VARIABLES）"
            )

    @staticmethod
    def _parse_meta(html: str, default_title: str) -> tuple[str, str]:
        """从模板里的注释/`<title>` 提取中文名与说明；都没有就用文件名。"""
        title = ""
        desc = ""
        match = _META_TITLE_RE.search(html)
        if match:
            title = _clean_text(match.group(1), 40)
        match = _META_DESC_RE.search(html)
        if match:
            desc = _clean_text(match.group(1), 120)
        if not title:
            match = _TITLE_TAG_RE.search(html)
            if match:
                candidate = _clean_text(_HTML_TAG_RE.sub("", match.group(1)), 40)
                if candidate and "{{" not in candidate and "{%" not in candidate:
                    title = candidate
        if not title:
            title = THEME_TITLE_FALLBACK.get(default_title, default_title)
        if not desc:
            desc = _THEME_DESC_FALLBACK.get(default_title, "")
        return title, desc

    @staticmethod
    def _decode_html(raw: bytes) -> str | None:
        """模板编码：utf-8-sig 优先，其次 gb18030（Windows 用户常见）。"""
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return None

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        """先写临时文件再原子替换：模板不会被写成半个文件。"""
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)


# ---------------------------------------------------------------------------
# 年报生成器
# ---------------------------------------------------------------------------


class YearbookBuilder:
    """生日年报生成器：三模式文案 → Jinja2 HTML → 长图/分页 → 归档。

    渲染优先级（SPEC §4.3）：
    ``auto`` 时 Playwright 可用就真截图（长图 + 分页 PNG）；否则 Pillow 手绘
    简易长图；两者都不可用时只出 HTML。返回值里的 ``engine`` 如实报告实际路径，
    ``render_errors`` 记录降级原因，上层可以据此提示用户。
    """

    def __init__(
        self,
        data_dir: Path,
        templates: TemplateStore,
        cfg: dict,
        llm_call: Callable[[str, str], Any] | None = None,
        logger: Any = None,
    ):
        self.data_dir = Path(data_dir)
        self.templates = templates
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.llm_call = llm_call if callable(llm_call) else None
        self.logger = logger or logging.getLogger("astrbot")

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            value = self.cfg.get(key, default)
        except AttributeError:
            return default
        return default if value is None else value

    def _cfg_int(self, key: str, default: int, *, lo: int | None = None, hi: int | None = None) -> int:
        return _safe_int(self._cfg(key, default), default, lo=lo, hi=hi)

    def _cfg_bool(self, key: str, default: bool) -> bool:
        return _safe_bool(self._cfg(key, default), default)

    def _log(self, level: int, message: str) -> None:
        """日志永远不能反过来拖死年报。"""
        try:
            self.logger.log(level, "[yearbook] %s", message)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    async def build(
        self,
        *,
        character_name: str,
        age: int,
        year: int,
        stats: dict,
        entries: list[dict],
        media: list[dict],
        today: date,
        theme: str | None = None,
        birthday_line: str | None = None,
    ) -> dict:
        """生成一份完整年报，返回产物路径与生成信息。

        三模式文案、模板随机、Playwright/Pillow 降级都在这里串起来；任何一步失败
        都降级而不是 raise（除非连 HTML 都写不出来）。返回键见模块文档，SPEC §4.3
        要求的 ``dir/long_image/pages/html/theme/mode/tokens`` 一定都有。
        """
        started = time.monotonic()
        errors: list[str] = []
        warnings: list[str] = []
        today = today if isinstance(today, date) else date.today()
        year = _safe_int(year, today.year, lo=1900, hi=2100)
        age = _safe_int(age, 0, lo=0, hi=200)
        character_name = _clean_text(character_name, 40) or "她"
        if isinstance(birthday_line, str):
            birthday_line = _clean_text(birthday_line, 200)

        dir_path = self.data_dir / f"{year:04d}"
        dir_path.mkdir(parents=True, exist_ok=True)
        (dir_path / "pages").mkdir(parents=True, exist_ok=True)
        self._clear_previous_artifacts(dir_path)

        # 1) 清洗输入 + 统计
        all_entries = normalize_entries(entries)
        scoped_entries = self._scope_entries(all_entries, year)
        media_ctx = self._prepare_media(media, dir_path, errors)
        stats_obj = render_stats(scoped_entries, media)
        if isinstance(stats, dict):
            for key, value in stats.items():
                if key not in stats_obj or stats_obj.get(key) in (None, [], {}):
                    stats_obj[key] = value
        # 统计里的「媒体数」按真正进了年报的媒体算（跳过路径不存在/超上限的），
        # 否则模板上的数字会和实际图片数对不上。关闭插图时保留小本本里的媒体总数。
        if self._cfg_bool("report_include_media", True):
            stats_obj["media"] = len(media_ctx)
            stats_obj["media_by_type"] = {
                "image": sum(1 for item in media_ctx if item.get("type") == "image"),
                "video": sum(1 for item in media_ctx if item.get("type") == "video"),
            }
        display_entries = self._select_display_entries(scoped_entries, MAX_DISPLAY_ENTRIES)
        truncation_note = ""
        if len(scoped_entries) > len(display_entries):
            truncation_note = f"这一年记了 {len(scoped_entries)} 条，这里只放得下其中 {len(display_entries)} 条。"

        # 2) 文案（llm / template / llm_guided）
        copy_payload, mode_used, mode_requested, llm_ok, tokens = await self._generate_copy(
            character_name=character_name,
            age=age,
            year=year,
            today=today,
            entries=display_entries,
            stats=stats_obj,
            media=media_ctx,
            warnings=warnings,
        )

        # 3) 主题 + 模板上下文
        theme_name = self._select_theme(theme, warnings)
        context = self._build_context(
            copy=copy_payload,
            character_name=character_name,
            age=age,
            year=year,
            today=today,
            stats=stats_obj,
            entries=display_entries,
            all_entry_count=len(scoped_entries),
            media=media_ctx,
            mode=mode_used,
            mode_label=MODE_LABELS.get(mode_used, mode_used),
            mode_requested=mode_requested,
            theme_name=theme_name,
            tokens=tokens,
            truncation_note=truncation_note,
            birthday_line=birthday_line,
        )
        theme_title = self._theme_title(theme_name)
        context["theme_title"] = theme_title
        context["theme_desc"] = self._theme_desc(theme_name)

        # 4) 渲染 HTML 并落盘
        html, used_theme = self._render_document(theme_name, context, errors)
        context["theme"] = used_theme
        if used_theme != theme_name:
            theme_name = used_theme
            theme_title = self._theme_title(theme_name)
        html_path = dir_path / "report.html"
        self._write_text(html_path, html)

        # 5) 截图（Playwright → Pillow → 只出 HTML）
        engine_requested = self._requested_engine()
        render_result = await self._render_engine(engine_requested, html_path, dir_path, errors, context)

        result = {
            "dir": str(dir_path),
            "long_image": str(render_result["long"]) if render_result.get("long") else None,
            "pages": [str(path) for path in render_result.get("pages") or []],
            "html": str(html_path),
            "theme": theme_name,
            "theme_title": theme_title,
            "mode": mode_used,
            "mode_requested": mode_requested,
            "mode_label": MODE_LABELS.get(mode_used, mode_used),
            "llm_ok": llm_ok,
            "tokens": tokens,
            "engine": render_result.get("engine", "html"),
            "engine_requested": engine_requested,
            "render_errors": list(errors),
            "warnings": list(warnings),
            "year": year,
            "age": age,
            "character_name": character_name,
            "entry_count": stats_obj.get("total", 0),
            "entries_shown": len(display_entries),
            "media_count": len(media_ctx),
            "generated_at": _now_text(),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        self._write_meta(dir_path, result)
        self._log(
            logging.INFO,
            f"年报已生成：{year} 年 / {age} 岁 / 模式 {mode_used} / 主题 {theme_name} / "
            f"引擎 {result['engine']} / 条目 {result['entries_shown']} / "
            f"媒体 {result['media_count']} / 耗时 {result['elapsed_ms']}ms",
        )
        return result

    # ------------------------------------------------------------------
    # 条目 / 媒体 / 统计
    # ------------------------------------------------------------------
    @staticmethod
    def _scope_entries(entries: list[dict], year: int) -> list[dict]:
        """只保留这一年的条目；如果全都没有年份，就全留下（数据本身不带年份）。"""
        dated = [entry for entry in entries if entry["year"] is not None]
        if not dated:
            return list(entries)
        scoped = [entry for entry in entries if entry["year"] in (None, year)]
        return scoped or list(entries)

    @staticmethod
    def _select_display_entries(entries: list[dict], limit: int) -> list[dict]:
        """条目太多时挑着展示：约定优先、月份铺开，再按日期排序。"""
        ordered = sorted(entries, key=lambda entry: (entry["date"] is None, entry["date"] or ""))
        if len(ordered) <= limit:
            return ordered
        picked: list[dict] = []
        picked_ids: set[int] = set()

        def take(entry: dict) -> None:
            key = id(entry)
            if key in picked_ids or len(picked) >= limit:
                return
            picked_ids.add(key)
            picked.append(entry)

        for entry in ordered:
            if entry["pinned"]:
                take(entry)
        buckets: dict[int, list[dict]] = {}
        for entry in ordered:
            if id(entry) in picked_ids:
                continue
            month = entry["month"] or 0
            buckets.setdefault(month, []).append(entry)
        # 月份轮转：保证一年里每个有记录的月份都能被看见。
        month_order = sorted(buckets)
        progressed = True
        while progressed and len(picked) < limit:
            progressed = False
            for month in month_order:
                if buckets[month] and len(picked) < limit:
                    take(buckets[month].pop(0))
                    progressed = True
        for entry in ordered:
            if len(picked) >= limit:
                break
            take(entry)
        return sorted(picked, key=lambda entry: (entry["date"] is None, entry["date"] or ""))

    def _prepare_media(self, media: Any, dir_path: Path, errors: list[str]) -> list[dict]:
        """把媒体缩略图复制进报告目录（存档 HTML 用相对路径引用）。

        路径不存在/复制失败都只是跳过并记一条 warning，不影响年报主体——
        「有图更好，没图也能过生日」。
        """
        if not self._cfg_bool("report_include_media", True):
            return []
        limit = self._cfg_int("report_max_media", 12, lo=0, hi=100)
        if limit <= 0 or not isinstance(media, (list, tuple)):
            return []
        media_dir = dir_path / "media"
        shutil.rmtree(media_dir, ignore_errors=True)
        result: list[dict] = []
        for index, item in enumerate(media):
            if len(result) >= limit:
                break
            if isinstance(item, str):
                item = {"path": item}
            if not isinstance(item, dict):
                continue
            raw_path = ""
            for key in ("thumb", "path", "file", "src"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    raw_path = value.strip()
                    break
            if not raw_path:
                continue
            try:
                source = Path(raw_path).expanduser()
            except (OSError, ValueError):
                continue
            if not source.is_file():
                errors.append(f"media-skip: 文件不存在：{_clean_text(raw_path, 80)}")
                continue
            media_type = _media_type_of(item)
            suffix = source.suffix.lower()
            if suffix not in _MEDIA_EXTS:
                suffix = ".png" if media_type == "image" else ".mp4"
            safe_stem = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]", "_", source.stem)[:24] or "media"
            target_name = f"{index + 1:02d}-{safe_stem}{suffix}"
            media_dir.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copyfile(source, media_dir / target_name)
            except OSError as exc:
                errors.append(f"media-skip: 复制失败：{_short_error(exc)}")
                continue
            note = _clean_text(item.get("note"), 80)
            date_text = ""
            day = _parse_date_value(item.get("date") or item.get("date_text"))
            if day is not None:
                date_text = f"{day.month} 月 {day.day} 日"
            caption = note or date_text or ("一段视频" if media_type == "video" else "一张照片")
            result.append(
                {
                    "src": f"media/{target_name}",
                    "path": str(media_dir / target_name),
                    "type": media_type,
                    "type_label": "视频" if media_type == "video" else "图片",
                    "note": note,
                    "date_text": date_text,
                    "caption": caption,
                }
            )
        return result

    # ------------------------------------------------------------------
    # 文案三模式
    # ------------------------------------------------------------------
    async def _generate_copy(
        self,
        *,
        character_name: str,
        age: int,
        year: int,
        today: date,
        entries: list[dict],
        stats: dict,
        media: list[dict],
        warnings: list[str],
    ) -> tuple[dict, str, str, bool | None, dict]:
        """按配置生成文案，返回 (copy, mode_used, mode_requested, llm_ok, tokens)。

        - ``template``：零 LLM 调用，直接用统计/条目拼文案；
        - ``llm``：素材全给模型自由写；
        - ``llm_guided``：先出模板骨架，再让模型按骨架润色。
        LLM 不可用（没注入 / 报错 / 返回不可解析）时退回模板文案，``llm_ok=False``，
        绝不让年报因为模型问题而失败。
        """
        requested = _clean_text(self._cfg("report_content_mode", "llm_guided")).lower()
        if requested not in CONTENT_MODES:
            requested = "llm_guided"
        template_copy = self._template_copy(
            character_name=character_name,
            age=age,
            year=year,
            stats=stats,
            entries=entries,
            media=media,
        )
        tokens = {"llm_calls": 0, "prompt_chars": 0, "completion_chars": 0, "approx_tokens": 0}
        if requested == "template":
            return template_copy, "template", requested, None, tokens
        if self.llm_call is None:
            warnings.append("未注入 llm_call，年报文案退回纯模板")
            return template_copy, "template", requested, False, tokens

        system, user = self._build_llm_messages(
            mode=requested,
            character_name=character_name,
            age=age,
            year=year,
            today=today,
            entries=entries,
            stats=stats,
            media=media,
            template_copy=template_copy,
        )
        tokens["llm_calls"] += 1
        tokens["prompt_chars"] = len(system) + len(user)
        timeout = self._cfg_int("report_timeout", 300, lo=10, hi=1800)
        llm_timeout = max(15, min(timeout, 180))
        try:
            raw = await asyncio.wait_for(self._invoke_llm(system, user), timeout=llm_timeout)
            raw_text = raw if isinstance(raw, str) else str(raw or "")
            tokens["completion_chars"] = len(raw_text)
            tokens["approx_tokens"] = int(round((tokens["prompt_chars"] + len(raw_text)) / 1.5))
            parsed = self._parse_llm_copy(raw_text, template_copy)
        except asyncio.TimeoutError:
            warnings.append(f"文案模型超时（>{llm_timeout}s），退回纯模板")
            self._log(logging.WARNING, f"年报文案模型超时（{llm_timeout}s）")
            return template_copy, "template", requested, False, tokens
        except Exception as exc:  # noqa: BLE001 模型失败绝不能拖死年报
            warnings.append(f"文案模型失败（{_short_error(exc)}），退回纯模板")
            self._log(logging.WARNING, f"年报文案模型失败：{_short_error(exc)}")
            return template_copy, "template", requested, False, tokens
        if parsed is None:
            warnings.append("文案模型返回无法解析为 JSON，退回纯模板")
            self._log(logging.WARNING, "年报文案模型返回无法解析，退回纯模板")
            return template_copy, "template", requested, False, tokens
        return parsed, requested, requested, True, tokens

    async def _invoke_llm(self, system: str, user: str) -> Any:
        """调用注入的 llm_call；同步函数也兼容（放到线程里跑，别堵事件循环）。"""
        result = self.llm_call(system, user)
        if inspect.isawaitable(result):
            return await result
        return result

    def _build_llm_messages(
        self,
        *,
        mode: str,
        character_name: str,
        age: int,
        year: int,
        today: date,
        entries: list[dict],
        stats: dict,
        media: list[dict],
        template_copy: dict,
    ) -> tuple[str, str]:
        """拼年报文案的 system/user 提示词（素材 + 骨架）。"""
        system = LLM_SYSTEM_PROMPT
        if mode == "llm_guided":
            system = system.rstrip() + "\n" + LLM_GUIDED_EXTRA
        lines = [
            f"角色：{character_name}",
            f"年龄：{age} 岁",
            f"年份：{year}",
            f"生成日期：{_cn_date_text(today)}",
            "",
            "统计："
            f"记录 {stats.get('total', 0)} 条；活跃 {stats.get('active_months', 0)} 个月；"
            f"最忙的月份是 {stats.get('busiest_month_label') or '无'}（{stats.get('busiest_month_count', 0)} 条）；"
            f"约定 {stats.get('pinned', 0)} 条；媒体 {stats.get('media', 0)} 个。",
            "",
            "这一年的条目（日期｜类型｜内容）：",
        ]
        if entries:
            for entry in entries[:MAX_LLM_ENTRIES]:
                date_text = entry["date_text"] or "日期不详"
                text = _clean_text(entry["text"], MAX_LLM_ENTRY_CHARS)
                lines.append(f"- {date_text}｜{entry['kind_label']}｜{text}")
        else:
            lines.append("- （这一年没有任何记录）")
        if media:
            lines.append("")
            lines.append("被选进年报的媒体说明：")
            for item in media[:12]:
                lines.append(f"- {item['type_label']}：{item['caption']}")
        if mode == "llm_guided":
            lines.extend(
                [
                    "",
                    "骨架（请保持结构，重写语言，不要新增事实）：",
                    f"标题：{template_copy['title']}",
                    f"副标题：{template_copy['subtitle']}",
                    f"开场：{template_copy['intro']}",
                ]
            )
            for index, section in enumerate(template_copy.get("sections") or [], 1):
                outline = _clean_text(section.get("body"), 90)
                lines.append(f"第 {index} 段 小标题：{section.get('heading')}｜要点：{outline}")
            lines.extend(
                [
                    f"结尾：{template_copy['closing']}",
                    "",
                    "请输出 JSON：title / subtitle / intro / sections（三段，字段 heading、body）/ closing。",
                ]
            )
        else:
            lines.extend(["", "请输出 JSON：title / subtitle / intro / sections（3~5 段，字段 heading、body）/ closing。"])
        return system, "\n".join(lines)

    def _template_copy(
        self,
        *,
        character_name: str,
        age: int,
        year: int,
        stats: dict,
        entries: list[dict],
        media: list[dict],
    ) -> dict:
        """纯模板文案：不调模型也能读得下去的一段话。

        语言风格刻意「克制」：讲事实、少形容词、不替角色抒情过头；LLM 模式只是
        在这份骨架上加文采，所以它必须自己就是完整可用的。
        """
        stats = stats if isinstance(stats, dict) else {}
        total = _safe_int(stats.get("total"), 0, lo=0)
        active = _safe_int(stats.get("active_months"), 0, lo=0)
        media_count = len(media or [])
        pinned = _safe_int(stats.get("pinned"), 0, lo=0)
        first_date = stats.get("first_date")
        last_date = stats.get("last_date")
        busiest_label = stats.get("busiest_month_label") or ""
        busiest_count = _safe_int(stats.get("busiest_month_count"), 0, lo=0)
        first_text = self._iso_to_cn(first_date)
        last_text = self._iso_to_cn(last_date)
        kinds = stats.get("kinds") or []
        kind_text = "、".join(f"{item['label']} {item['count']} 条" for item in kinds[:4]) if kinds else ""
        title = _clean_text(f"{character_name}的 {year}", 40)
        opening_quote = OPENING_QUOTES[year % len(OPENING_QUOTES)]
        if total <= 0:
            subtitle = f"空白的一页，也盖上了 {age} 岁的邮戳"
            intro = (
                f"{year} 年，{character_name} {age} 岁。这一年，手账上什么也没写。"
                "空白页也是这一年的形状——至少她又长大了一岁，而纸还在这里等着。"
            )
            year_body = (
                "没有记录的一年，时间照样往前走。翻过这一页，下一年还有地方可以写。"
            )
        else:
            subtitle = f"{total} 条记录，{active} 个月，一整个 {year}"
            if first_text and last_text and first_text != last_text:
                span_text = f"从 {first_text} 到 {last_text}"
            else:
                span_text = f"在 {first_text or last_text or '这一年'}"
            intro = (
                f"{year} 年，{character_name} {age} 岁。{span_text}，手账上留下了 {total} 条记录，"
                f"摊在 {active} 个月里。"
                + (f"{busiest_label}最热闹，有 {busiest_count} 条。" if busiest_label else "")
                + (f"还有 {media_count} 张照片/截图被一起收进来。" if media_count else "")
            )
            if first_text and last_text and first_text != last_text:
                year_body = (
                    f"从 {first_text} 的第一条，到 {last_text} 的最后一条，这一年被拆成 {total} 个小片段。"
                    + (f"{busiest_label}写得最密，有 {busiest_count} 条；其余月份松松散散，也算一种节奏。" if busiest_label else "")
                )
            else:
                year_body = f"这一年留下了 {total} 条记录，分散在 {active} 个月里。翻回去看，每一句都还认得出来。"
        # 第二段：她记下来的东西（kind 分布 + 一条具体条目作细节）
        if kind_text:
            sample = self._pick_sample_entry(entries)
            body_parts = [f"记得最多的是{kind_text}。"]
            if sample:
                body_parts.append(f"比如 {sample['date_text'] or '有一天'} 写下的那句「{_clean_text(sample['text'], 60)}」。")
            body_parts.append("喜欢什么、做过什么、心情好不好、和谁约好了什么——都混在这些字里。")
            kinds_body = "".join(body_parts)
        else:
            kinds_body = "没有分类，也没有条目；下一年如果想起来写，就从第一句开始。"
        # 第三段：约定与下一年
        if pinned:
            next_body = (
                f"这一年里，有 {pinned} 条被标成了约定。约定不一定要实现，"
                "但写下它的那一刻是真的。下一年的页已经空出来了，先替她留着。"
            )
        else:
            next_body = (
                "这一年没有写下约定，也没有关系。日子是自己的，记录只是顺手。"
                "下一年的页已经空出来了，等她想起什么再写。"
            )
        closing = f"{age} 岁生日快乐，{character_name}。这一年收好了，下一年的页也留好了。"
        return {
            "title": title,
            "subtitle": subtitle,
            "intro": intro,
            "sections": [
                {"heading": "这一年的长度", "body": year_body},
                {"heading": "她记下来的", "body": kinds_body},
                {"heading": "还有下一年", "body": next_body},
            ],
            "closing": closing,
            "opening_quote": opening_quote,
        }

    @staticmethod
    def _pick_sample_entry(entries: list[dict]) -> dict | None:
        """挑一条最有「细节感」的条目当引文：约定优先，其次长文本。"""
        if not entries:
            return None
        for entry in entries:
            if entry["kind"] == "commitment":
                return entry
        return max(entries, key=lambda entry: len(entry["text"]), default=None)

    @staticmethod
    def _iso_to_cn(raw: Any) -> str:
        if not raw:
            return ""
        try:
            day = date.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return ""
        return f"{day.month} 月 {day.day} 日"

    def _parse_llm_copy(self, raw_text: str, fallback: dict) -> dict | None:
        """解析模型返回的 JSON 文案；解析不出来返回 None（调用方退回模板）。"""
        text = (raw_text or "").strip()
        if not text:
            return None
        text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
        start = text.find("{")
        if start < 0:
            return None
        data = None
        try:
            data, _ = json.JSONDecoder().raw_decode(text, start)
        except json.JSONDecodeError:
            end = text.rfind("}")
            if end > start:
                try:
                    data = json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    data = None
        if not isinstance(data, dict):
            return None
        title = _clean_text(data.get("title"), 40) or fallback["title"]
        subtitle = _clean_text(data.get("subtitle"), 80) or fallback["subtitle"]
        intro = _clean_text(data.get("intro"), 600) or fallback["intro"]
        closing = _clean_text(data.get("closing"), 400) or fallback["closing"]
        sections: list[dict] = []
        raw_sections = data.get("sections")
        if isinstance(raw_sections, list):
            for item in raw_sections[:6]:
                if isinstance(item, dict):
                    heading = _clean_text(item.get("heading"), 24)
                    body = _clean_text(item.get("body") or item.get("text") or item.get("content"), 900)
                elif isinstance(item, str):
                    heading = ""
                    body = _clean_text(item, 900)
                else:
                    continue
                if body:
                    sections.append({"heading": heading or f"第 {len(sections) + 1} 段", "body": body})
        if not sections:
            # 模型可能只给了一段长文：至少别浪费，把它当正文第一段。
            if intro and intro != fallback["intro"]:
                sections = [{"heading": "这一年", "body": intro}]
            else:
                return None
        return {
            "title": title,
            "subtitle": subtitle,
            "intro": intro,
            "sections": sections,
            "closing": closing,
            "opening_quote": fallback.get("opening_quote") or OPENING_QUOTES[0],
        }

    # ------------------------------------------------------------------
    # 模板上下文
    # ------------------------------------------------------------------
    def _build_context(
        self,
        *,
        copy: dict,
        character_name: str,
        age: int,
        year: int,
        today: date,
        stats: dict,
        entries: list[dict],
        all_entry_count: int,
        media: list[dict],
        mode: str,
        mode_label: str,
        mode_requested: str,
        theme_name: str,
        tokens: dict,
        truncation_note: str,
        birthday_line: str | None,
    ) -> dict:
        """把数据 + 文案 + 统计整理成模板能直接用的扁平上下文。

        内置模板和用户模板都用同一份变量（见 :data:`TEMPLATE_VARIABLES`），所以
        模板只需要关心展示；缺数据时的降级在 Python 侧就做完了。
        """
        entries_by_month: list[dict] = []
        for month in range(1, 13):
            month_entries = [entry for entry in entries if entry["month"] == month]
            if not month_entries:
                continue
            entries_by_month.append(
                {
                    "month": month,
                    "label": MONTH_LABELS[month - 1],
                    "count": len(month_entries),
                    "entries": month_entries,
                }
            )
        # 没有日期的条目单独兜住，避免「无日期条目在时间线上消失」。
        undated = [entry for entry in entries if entry["month"] is None]
        if undated:
            entries_by_month.append({"month": 0, "label": "没有写日期", "count": len(undated), "entries": undated})
        has_entries = bool(entries)
        media_list = list(media or [])
        has_media = bool(media_list)
        if not birthday_line:
            birthday_line = f"写给 {age} 岁的 {character_name}。"
        subtitle = copy.get("subtitle") or ""
        title = copy.get("title") or f"{character_name}的 {year}"
        return {
            "title": title,
            "subtitle": subtitle,
            "character_name": character_name,
            "age": age,
            "age_prev": max(0, age - 1),
            "year": year,
            "today": today.isoformat(),
            "today_text": _cn_date_text(today),
            "weekday_text": _weekday_text(today),
            "birthday_line": _clean_text(birthday_line, 200),
            "opening_quote": copy.get("opening_quote") or OPENING_QUOTES[0],
            "mode": mode,
            "mode_label": mode_label,
            "mode_requested": mode_requested,
            "theme": theme_name,
            "theme_title": self._theme_title(theme_name),
            "theme_desc": self._theme_desc(theme_name),
            "generated_at": _now_text(),
            "intro": copy.get("intro") or "",
            "sections": copy.get("sections") or [],
            "closing": copy.get("closing") or "",
            "stats": stats,
            "entries": entries,
            "entries_by_month": entries_by_month,
            "months": stats.get("month_heat") or [],
            "kinds": stats.get("kinds") or [],
            "highlights": self._pick_highlights(entries, 8),
            "media": media_list,
            "has_entries": has_entries,
            "has_media": has_media,
            "entry_total": _safe_int(stats.get("total"), all_entry_count, lo=0),
            "entries_shown": len(entries),
            "truncation_note": truncation_note,
            "media_total": _safe_int(stats.get("media"), len(media_list), lo=0),
            "tokens": tokens,
        }

    @staticmethod
    def _pick_highlights(entries: list[dict], limit: int) -> list[dict]:
        """精选：约定优先 + 内容长的 + 不同类型，最后按日期排回去。"""
        if not entries or limit <= 0:
            return []
        weights = {"commitment": 320, "person": 140, "mood": 100, "preference": 80, "event": 40, "other": 0}

        def score(entry: dict) -> int:
            return (1000 if entry["pinned"] else 0) + weights.get(entry["kind"], 0) + min(len(entry["text"]), 200)

        picked = sorted(entries, key=score, reverse=True)[:limit]
        return sorted(picked, key=lambda entry: (entry["date"] is None, entry["date"] or ""))

    # ------------------------------------------------------------------
    # 主题 / HTML
    # ------------------------------------------------------------------
    def _theme_title(self, name: str) -> str:
        for item in self.templates.list():
            if item["name"] == name:
                return item["title"]
        return THEME_TITLE_FALLBACK.get(name, name or "兜底模板")

    def _theme_desc(self, name: str) -> str:
        for item in self.templates.list():
            if item["name"] == name:
                return item["desc"]
        return _THEME_DESC_FALLBACK.get(name, "")

    def _select_theme(self, theme_override: str | None, warnings: list[str]) -> str:
        """选模板：显式指定 > 配置项 > 随机；找不到指定模板时退回随机并记 warning。"""
        available = [item["name"] for item in self.templates.list()]
        requested = ""
        if theme_override is not None:
            requested = _clean_text(theme_override, 64)
        if not requested:
            requested = _clean_text(self._cfg("report_theme", "random"), 64)
        if requested.lower() in {"", "random", "随机"}:
            requested = ""
        if requested:
            if requested in available:
                return requested
            lowered = {name.lower(): name for name in available}
            if requested.lower() in lowered:
                return lowered[requested.lower()]
            warnings.append(f"模板 {requested!r} 不存在，改用随机模板")
        if available:
            return random.choice(available)
        warnings.append("没有可用模板，使用内置兜底 HTML")
        return ""

    def _render_document(self, theme_name: str, context: dict, errors: list[str]) -> tuple[str, str]:
        """渲染完整 HTML；模板全坏时用内置兜底模板，保证一定有可看的东西。"""
        if theme_name:
            try:
                return self.templates.render(theme_name, context), theme_name
            except Exception as exc:  # noqa: BLE001
                message = f"template:{theme_name}:{_short_error(exc)}"
                errors.append(message)
                self._log(logging.WARNING, f"模板渲染失败，尝试兜底：{message}")
        for candidate in ("minimal", "journal", "film", "birthday"):
            try:
                return self.templates.render(candidate, context), candidate
            except Exception:  # noqa: BLE001
                continue
        try:
            html = self.templates.render_string(FALLBACK_TEMPLATE, context, label="fallback")
        except Exception as exc:  # noqa: BLE001 连兜底模板都炸只能是环境级问题
            errors.append(f"fallback::{_short_error(exc)}")
            html = "<!DOCTYPE html><html><body><h1>这一年</h1><p>年报渲染失败，请查看插件日志。</p></body></html>"
        return html, TEMPLATE_FALLBACK_NAME

    # ------------------------------------------------------------------
    # 渲染引擎
    # ------------------------------------------------------------------
    def _requested_engine(self) -> str:
        engine = _clean_text(self._cfg("report_render_engine", "auto")).lower()
        return engine if engine in RENDER_ENGINES else "auto"

    async def _render_engine(
        self,
        engine_requested: str,
        html_path: Path,
        dir_path: Path,
        errors: list[str],
        context: dict,
    ) -> dict:
        """按优先级渲染：Playwright → Pillow → 只出 HTML；实际路径写进返回值。"""
        want_playwright = engine_requested in ("auto", "playwright")
        want_pillow = engine_requested in ("auto", "playwright", "pillow")
        if want_playwright:
            timeout = self._cfg_int("report_timeout", 300, lo=10, hi=1800)
            budget = max(30, min(timeout, 240))
            try:
                rendered = await asyncio.wait_for(
                    self._render_playwright(html_path, dir_path), timeout=budget
                )
                return {**rendered, "engine": "playwright"}
            except Exception as exc:  # noqa: BLE001 浏览器缺失/被墙/超时都降级
                errors.append(f"playwright:{_short_error(exc)}")
                self._log(logging.WARNING, f"Playwright 渲染失败，降级 Pillow：{_short_error(exc)}")
        if want_pillow:
            try:
                rendered = await asyncio.to_thread(self._render_pillow, dir_path, errors, context)
                return {**rendered, "engine": "pillow"}
            except Exception as exc:  # noqa: BLE001 Pillow 不可用/字体异常 → 只出 HTML
                errors.append(f"pillow:{_short_error(exc)}")
                self._log(logging.WARNING, f"Pillow 渲染失败，只保留 HTML：{_short_error(exc)}")
        if engine_requested == "html":
            self._log(logging.INFO, "按配置只输出 HTML（不生成图片）")
        return {"long": None, "pages": [], "engine": "html"}

    async def _render_playwright(self, html_path: Path, dir_path: Path) -> dict:
        """Playwright 真截图：一张长图 + 按 .report-block 切页的分页 PNG。

        长图本来可以直接 full_page 截，但页面很长时 2x 截图内存开销大，所以先量
        高度决定缩放；分页按块边界切，避免把一段话从中间劈开。
        """
        from playwright.async_api import async_playwright  # 可选依赖：热路径外延迟 import

        uri = html_path.resolve().as_uri()
        pages_dir = dir_path / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        long_path = dir_path / "report-long.png"
        max_pages = self._cfg_int("report_max_pages", 12, lo=1, hi=60)
        viewport_h = REPORT_PAGE_HEIGHT
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                args=["--hide-scrollbars", "--force-color-profile=srgb"],
                timeout=30_000,
            )
            try:
                # 第一遍：scale=1 量页面高度（2x 会翻倍，先知道要多高再决定）。
                measure_page = await browser.new_page(
                    viewport={"width": REPORT_WIDTH, "height": viewport_h},
                    device_scale_factor=1,
                )
                measure_page.set_default_timeout(25_000)
                await measure_page.goto(uri, wait_until="load")
                await self._wait_page_assets(measure_page)
                total_height = int(
                    await measure_page.evaluate(
                        "Math.max(document.documentElement.scrollHeight,"
                        " document.body ? document.body.scrollHeight : 0)"
                    )
                    or 0
                )
                if total_height <= 0:
                    raise RuntimeError("页面高度为 0（模板可能没有内容）")
                long_scale = 2 if total_height <= SCALE2_MAX_HEIGHT else 1
                if long_scale == 2:
                    await measure_page.close()
                    long_page = await browser.new_page(
                        viewport={"width": REPORT_WIDTH, "height": viewport_h},
                        device_scale_factor=2,
                    )
                else:
                    long_page = measure_page
                long_page.set_default_timeout(25_000)
                if long_scale == 2:
                    await long_page.goto(uri, wait_until="load")
                await self._wait_page_assets(long_page)
                total_height = int(
                    await long_page.evaluate(
                        "Math.max(document.documentElement.scrollHeight,"
                        " document.body ? document.body.scrollHeight : 0)"
                    )
                    or total_height
                )
                long_bytes = await long_page.screenshot(full_page=True)
                long_path.write_bytes(long_bytes)
                self._log(
                    logging.DEBUG,
                    f"Playwright 长图：{REPORT_WIDTH * long_scale}x{total_height * long_scale}",
                )
                blocks = await long_page.evaluate(
                    "Array.from(document.querySelectorAll('.report-block')).map(function(el){"
                    " var r = el.getBoundingClientRect();"
                    " return { top: Math.round(r.top + window.scrollY),"
                    "          bottom: Math.round(r.bottom + window.scrollY) }; })"
                )
                if long_scale == 2:
                    pages_page = long_page
                else:
                    await long_page.close()
                    pages_page = await browser.new_page(
                        viewport={"width": REPORT_WIDTH, "height": viewport_h},
                        device_scale_factor=2,
                    )
                    pages_page.set_default_timeout(25_000)
                    await pages_page.goto(uri, wait_until="load")
                    await self._wait_page_assets(pages_page)
                page_rules = self._compute_page_ranges(blocks, total_height, viewport_h, max_pages)
                written: list[Path] = []
                for index, (top, bottom) in enumerate(page_rules, 1):
                    clip = {
                        "x": 0,
                        "y": max(0, int(top)),
                        "width": REPORT_WIDTH,
                        "height": max(1, int(bottom - top)),
                    }
                    png = await pages_page.screenshot(full_page=True, clip=clip)
                    page_path = pages_dir / f"page-{index:02d}.png"
                    page_path.write_bytes(png)
                    written.append(page_path)
                if not written:  # 理论上不会发生；真发生了就用长图切
                    written = self._slice_image_file(long_path, pages_dir, max_pages)
                return {"long": long_path, "pages": written}
            finally:
                await browser.close()

    @staticmethod
    async def _wait_page_assets(page: Any) -> None:
        """等图片解码完、字体就绪，避免截图里出现半张图 / 回退字体。"""
        try:
            await page.evaluate(
                "() => Promise.all(Array.from(document.images || []).map(function(img){"
                "  if (img.complete) { return Promise.resolve(); }"
                "  return new Promise(function(resolve){"
                "    img.addEventListener('load', resolve, { once: true });"
                "    img.addEventListener('error', resolve, { once: true });"
                "    setTimeout(resolve, 3000);"
                "  });"
                "}))"
            )
        except Exception:  # noqa: BLE001 图片等待失败不影响截图
            pass
        try:
            await page.evaluate("() => (document.fonts && document.fonts.ready) || true")
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(150)

    @staticmethod
    def _compute_page_ranges(
        blocks: Any, total_height: int, page_height: int, max_pages: int
    ) -> list[tuple[int, int]]:
        """按 .report-block 的边界把长页切成若干页（尽量不把一段话劈开）。

        用一小段动态规划，在所有「块与块之间的切点」里挑一组页，让每页高度尽量
        接近目标页高（允许比目标多 20%），避免贪心切出「半页只有一张便签」的碎
        片页。页数超过 ``report_max_pages`` 时退化为等分切——宁可切在段落中间，
        也要保证页数可控（发给 QQ 的图太多会刷屏）。
        """
        total_height = max(1, int(total_height))
        page_height = max(200, int(page_height))
        if total_height <= page_height * 1.15:
            return [(0, total_height)]
        positions = {0, total_height}
        for block in blocks or []:
            if not isinstance(block, dict):
                continue
            try:
                top = int(block.get("top", 0))
            except (TypeError, ValueError):
                continue
            if 0 < top < total_height:
                positions.add(top)
        points = sorted(positions)
        count = len(points)
        if count <= 2:
            return [(0, total_height)]
        max_span = page_height * 1.2
        min_span = page_height * 0.45
        infinity = float("inf")
        best: list[float] = [infinity] * count
        previous = [-1] * count
        best[0] = 0.0
        for end_index in range(1, count):
            for start_index in range(end_index - 1, -1, -1):
                if best[start_index] == infinity:
                    continue
                span = points[end_index] - points[start_index]
                if span <= 0:
                    continue
                if span > max_span and end_index - start_index > 1:
                    continue  # 不为了「少分一页」把好几块硬塞进超长页
                cost = float((span - page_height) ** 2)
                if span < min_span:
                    cost += float((min_span - span) ** 2) * 2  # 碎片页重罚
                if span > max_span:  # 单块本身超长：罚但允许，总比切碎强
                    cost += float((span - max_span) ** 2)
                if best[start_index] + cost < best[end_index]:
                    best[end_index] = best[start_index] + cost
                    previous[end_index] = start_index
        if best[-1] == infinity:  # 理论上不会发生；退回整页
            return [(0, total_height)]
        chain = [count - 1]
        while chain[-1] != 0:
            parent = previous[chain[-1]]
            if parent < 0:
                return [(0, total_height)]
            chain.append(parent)
        chain.reverse()
        ranges: list[tuple[int, int]] = []
        for page_index in range(len(chain) - 1):
            start_index, end_index = chain[page_index], chain[page_index + 1]
            start = 0 if page_index == 0 else max(0, points[start_index] - PAGE_BOUNDARY_GAP)
            if end_index == count - 1:
                end = total_height
            else:
                end = max(start + 1, points[end_index] - PAGE_BOUNDARY_GAP)
            ranges.append((start, min(end, total_height)))
        # 尾页太短（只剩页脚）就并进上一页，别单独出一张图。
        if (
            len(ranges) >= 2
            and ranges[-1][1] - ranges[-1][0] < page_height * 0.2
            and (ranges[-2][1] - ranges[-2][0]) + (ranges[-1][1] - ranges[-1][0]) <= max_span * 1.5
        ):
            ranges[-2] = (ranges[-2][0], total_height)
            ranges.pop()
        if len(ranges) > max_pages:
            step = total_height / max_pages
            ranges = [
                (int(round(index * step)), int(round((index + 1) * step)))
                for index in range(max_pages)
            ]
        return ranges

    # ------------------------------------------------------------------
    # Pillow 降级渲染
    # ------------------------------------------------------------------
    def _render_pillow(self, dir_path: Path, errors: list[str], context: dict) -> dict:
        """Pillow 手绘简易长图（无浏览器时的兜底，保证「至少能出图」）。

        比 Playwright 简陋，但排版逻辑（换行、分页、主题配色）是独立的，不依赖
        HTML 模板；中文字体探测自己实现，Windows/Linux/macOS 都有候选路径。
        """
        theme = _clean_text(context.get("theme"), 40) or "minimal"
        palette = THEME_PALETTES.get(theme) or THEME_PALETTES["minimal"]
        layout = _PillowLayout(REPORT_WIDTH, palette)
        try:
            layout.cover(context)
            layout.intro(context)
            layout.stats_band(context)
            layout.month_heat(context)
            layout.sections(context)
            layout.timeline(context)
            layout.media_gallery(context)
            layout.closing(context)
            layout.footer(context)
        except Exception as exc:  # noqa: BLE001 单块画失败不该整张放弃
            errors.append(f"pillow-layout:{_short_error(exc)}")
            self._log(logging.WARNING, f"Pillow 排版部分失败，继续画：{_short_error(exc)}")
        image = layout.render()
        long_path = dir_path / "report-long.png"
        image.save(long_path, format="PNG")
        max_pages = self._cfg_int("report_max_pages", 12, lo=1, hi=60)
        pages = self._slice_image(image, dir_path / "pages", max_pages)
        if not layout.fonts.cjk_found:
            errors.append("pillow: 没找到中文字体，中文可能显示为方块（需安装微软雅黑/Noto Sans CJK）")
        if layout.fonts.cjk_found:
            self._log(logging.INFO, f"Pillow 长图：{image.width}x{image.height}")
        return {"long": long_path, "pages": pages}

    def _slice_image(self, image: Any, pages_dir: Path, max_pages: int, page_height: int = REPORT_PAGE_HEIGHT) -> list[Path]:
        """把长图按固定高度切成分页图（Pillow 路径 / Playwright 分页兜底共用）。"""
        pages_dir.mkdir(parents=True, exist_ok=True)
        for stale in pages_dir.glob("page-*.png"):
            try:
                stale.unlink()
            except OSError:
                continue
        written: list[Path] = []
        height = image.height
        index = 0
        top = 0
        while top < height and index < max_pages:
            bottom = min(height, top + page_height)
            crop = image.crop((0, top, image.width, bottom))
            path = pages_dir / f"page-{index + 1:02d}.png"
            crop.save(path, format="PNG")
            written.append(path)
            top = bottom
            index += 1
        return written

    def _slice_image_file(self, image_path: Path, pages_dir: Path, max_pages: int) -> list[Path]:
        """长图文件切页（Playwright 分页失败时的最后兜底）。"""
        from PIL import Image

        with Image.open(image_path) as image:
            return self._slice_image(image.convert("RGB"), pages_dir, max_pages)

    # ------------------------------------------------------------------
    # 小工具
    # ------------------------------------------------------------------
    @staticmethod
    def _clear_previous_artifacts(dir_path: Path) -> None:
        """清掉上一次生成的图，避免页数变少时留下旧分页。"""
        try:
            for name in ("report-long.png",):
                path = dir_path / name
                if path.is_file():
                    path.unlink()
            pages_dir = dir_path / "pages"
            if pages_dir.is_dir():
                for stale in pages_dir.glob("page-*.png"):
                    try:
                        stale.unlink()
                    except OSError:
                        continue
        except OSError:
            pass

    @staticmethod
    def _write_text(path: Path, text: str) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def _write_meta(self, dir_path: Path, result: dict) -> None:
        """写一份生成信息（排查/WebUI 展示用；失败不影响年报）。"""
        try:
            payload = {
                key: result.get(key)
                for key in (
                    "year",
                    "age",
                    "character_name",
                    "theme",
                    "theme_title",
                    "mode",
                    "mode_requested",
                    "llm_ok",
                    "engine",
                    "engine_requested",
                    "entry_count",
                    "entries_shown",
                    "media_count",
                    "tokens",
                    "render_errors",
                    "warnings",
                    "generated_at",
                    "elapsed_ms",
                )
            }
            self._write_text(
                dir_path / "report-meta.json",
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 发送目标（QQ OneBot 白/黑名单）
    # ------------------------------------------------------------------
    def compute_targets(self, cfg: dict | None = None, event: Any = None) -> list[str]:
        """按 QQ OneBot 白/黑名单算出年报要发到哪些 umo；非 aiocqhttp 返回 []。

        语义（与 `_conf_schema.json` 的 hint 对齐）：
        - 非 OneBot 平台一律不发（只存档），所以先判平台；
        - ``report_target_mode=current``：只发当前会话；若白名单非空，当前会话
          必须在白名单里；黑名单永远优先；
        - ``report_target_mode=whitelist``：按白名单逐个发（黑名单里的剔除）；
        - 没有 event（cron 场景）：current 模式无从判断 → 返回 []，交上层决定；
          whitelist 模式照常返回白名单目标。

        白名单项支持三种写法：
        - ``"123456789"``          → 默认好友：``aiocqhttp:FriendMessage:123456789``
        - ``"group:123456789"``    → 群：``aiocqhttp:GroupMessage:123456789``
          （``g:`` / ``群:`` 等价）
        - ``"aiocqhttp:GroupMessage:123456789"`` → 原样使用
        纯数字无法区分好友/群，所以默认按好友处理；要发群请写 ``group:`` 前缀。
        """
        cfg = cfg if isinstance(cfg, dict) else self.cfg
        if not isinstance(cfg, dict):
            return []
        if event is not None:
            platform = self._event_platform(event)
            if platform and platform != "aiocqhttp":
                return []
        mode = _clean_text(cfg.get("report_target_mode", "current")).lower()
        if mode not in {"current", "whitelist"}:
            mode = "current"
        blacklist_ids = self._id_set(cfg.get("report_blacklist"))
        whitelist_specs = self._target_specs(cfg.get("report_whitelist"))
        if mode == "current":
            if event is None:
                return []
            umo = self._event_umo(event)
            if not umo:
                return []
            current_id = self._id_of_umo(umo)
            if current_id and current_id in blacklist_ids:
                return []
            if whitelist_specs and current_id not in {spec["id"] for spec in whitelist_specs}:
                return []
            return [umo]
        targets: list[str] = []
        seen: set[str] = set()
        for spec in whitelist_specs:
            if spec["id"] in blacklist_ids:
                continue
            umo = spec["umo"]
            if umo and umo not in seen:
                seen.add(umo)
                targets.append(umo)
        return targets

    @staticmethod
    def _event_platform(event: Any) -> str:
        """取平台名；取不到就从 umo 前缀里猜（aiocqhttp:...）。"""
        try:
            platform = str(event.get_platform_name() or "")
        except Exception:  # noqa: BLE001
            platform = ""
        if platform:
            return platform
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        return umo.split(":", 1)[0] if umo else ""

    @staticmethod
    def _event_umo(event: Any) -> str:
        """取当前会话 umo；属性缺失时用 sender/group id 拼一个 OneBot umo。"""
        for attr in ("unified_msg_origin", "umo"):
            value = getattr(event, attr, None)
            if isinstance(value, str) and value.startswith("aiocqhttp:"):
                return value
        platform = YearbookBuilder._event_platform(event) or "aiocqhttp"
        if platform != "aiocqhttp":
            return ""
        group_id = ""
        try:
            group_id = str(event.get_group_id() or "")
        except Exception:  # noqa: BLE001
            group_id = ""
        if group_id:
            return f"aiocqhttp:GroupMessage:{group_id}"
        sender_id = ""
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:  # noqa: BLE001
            sender_id = ""
        if sender_id:
            return f"aiocqhttp:FriendMessage:{sender_id}"
        return ""

    @staticmethod
    def _id_of_umo(umo: str) -> str:
        """从 umo / target 描述里抠出数字 id（黑名单比对用）。"""
        if not umo:
            return ""
        parts = str(umo).split(":")
        for part in reversed(parts):
            if part.strip().isdigit():
                return part.strip()
        digits = re.sub(r"\D", "", str(umo))
        return digits

    @staticmethod
    def _id_set(raw: Any) -> set[str]:
        """黑名单归一成数字 id 集合（容忍 list/字符串/带前缀写法）。"""
        if raw is None:
            return set()
        items = raw if isinstance(raw, (list, tuple, set)) else [raw]
        ids: set[str] = set()
        for item in items:
            text = _clean_text(item, 64)
            if not text:
                continue
            match = re.search(r"(\d{4,})", text)
            if match:
                ids.add(match.group(1))
            elif text.isdigit():
                ids.add(text)
        return ids

    @staticmethod
    def _target_specs(raw: Any) -> list[dict]:
        """白名单归一成 [{kind,id,umo}]；认不出的项静默忽略。"""
        if raw is None:
            return []
        items = raw if isinstance(raw, (list, tuple, set)) else [raw]
        specs: list[dict] = []
        seen: set[str] = set()
        for item in items:
            text = _clean_text(item, 120)
            if not text:
                continue
            kind = "friend"
            rest = text
            if text.lower().startswith("aiocqhttp:"):
                parts = text.split(":")
                if len(parts) >= 3:
                    middle = parts[1].lower()
                    kind = "group" if "group" in middle else "friend"
                    rest = parts[-1]
                else:
                    continue
            elif ":" in text:
                prefix, rest = text.split(":", 1)
                prefix = prefix.strip().lower()
                if prefix in {"group", "g", "群", "群组", "群聊"}:
                    kind = "group"
                elif prefix in {"friend", "f", "private", "私聊", "好友"}:
                    kind = "friend"
                else:
                    continue
            match = re.search(r"(\d{4,})", rest)
            if not match:
                continue
            target_id = match.group(1)
            umo = f"aiocqhttp:{'GroupMessage' if kind == 'group' else 'FriendMessage'}:{target_id}"
            if umo in seen:
                continue
            seen.add(umo)
            specs.append({"kind": kind, "id": target_id, "umo": umo})
        return specs

    # ------------------------------------------------------------------
    # WebUI 预览 / 样例
    # ------------------------------------------------------------------
    def render_preview(self, theme: str | None = None) -> dict:
        """用内置样例数据渲染一份 HTML（WebUI「预览」按钮用，不写文件、不截图）。

        返回 ``{"html","theme","theme_title","title"}``；模板全坏时会用兜底模板，
        所以 WebUI 永远有东西可展示。
        """
        today = date.today()
        year = today.year
        entries = self.demo_entries(year)
        media: list[dict] = []
        stats = render_stats(entries, media)
        copy_payload = self._template_copy(
            character_name="小春",
            age=16,
            year=year,
            stats=stats,
            entries=normalize_entries(entries),
            media=media,
        )
        theme_name = self._pick_preview_theme(theme)
        context = self._build_context(
            copy=copy_payload,
            character_name="小春",
            age=16,
            year=year,
            today=today,
            stats=stats,
            entries=normalize_entries(entries),
            all_entry_count=len(entries),
            media=media,
            mode="template",
            mode_label=MODE_LABELS["template"],
            mode_requested="template",
            theme_name=theme_name,
            tokens={"llm_calls": 0, "prompt_chars": 0, "completion_chars": 0, "approx_tokens": 0},
            truncation_note="",
            birthday_line="生日快乐，小春。16 岁的这一年，被收在这里。",
        )
        errors: list[str] = []
        html, used_theme = self._render_document(theme_name, context, errors)
        return {
            "html": html,
            "theme": used_theme,
            "theme_title": self._theme_title(used_theme),
            "title": context["title"],
            "warnings": errors,
        }

    def _pick_preview_theme(self, theme: str | None) -> str:
        available = [item["name"] for item in self.templates.list()]
        requested = _clean_text(theme, 64) if theme else ""
        if requested and requested in available:
            return requested
        if available:
            return available[0]
        return ""

    @staticmethod
    def demo_entries(year: int) -> list[dict]:
        """预览/样例数据：覆盖 5 种 kind、12 个月里的一部分、含约定与媒体占位。"""
        raw = [
            ("preference", f"{year}-01-09", "开始喜欢上在雨天听老唱片，说针尖落下去的声音像有人在很远的地方说话。"),
            ("event", f"{year}-02-14", "第一次一个人坐火车去海边，站台上买了个红豆面包，凉了也很好吃。"),
            ("mood", f"{year}-03-02", "今天心情很好，因为窗口那盆薄荷活过来了。"),
            ("person", f"{year}-03-27", "和隔壁班的朋友在走廊聊了半小时，话题从考试跑到了宇宙。"),
            ("commitment", f"{year}-04-11", "约好明年夏天一起去看烟火，谁都不许临时说没空。"),
            ("preference", f"{year}-05-05", "发现自己其实喜欢苦一点的东西：黑咖啡、青橄榄、很旧的书。"),
            ("event", f"{year}-06-21", "把房间重新布置了一遍，把书桌挪到窗边，光很好。"),
            ("mood", f"{year}-07-18", "有点闷。说不出为什么，就在纸上写了很多没有意义的字。"),
            ("event", f"{year}-08-30", "学会了做番茄鸡蛋面，第 4 次终于不糊锅。"),
            ("commitment", f"{year}-09-15", "答应自己下个季度少熬一点夜，虽然大概做不到。"),
            ("person", f"{year}-10-03", "收到一封手写的信，读了三遍，收进了抽屉最里面。"),
            ("other", f"{year}-11-22", "整理旧照片，发现去年这个时候的自己看起来更小一点。"),
        ]
        return [{"kind": kind, "date": day, "text": text, "tags": [kind]} for kind, day, text in raw]


# ---------------------------------------------------------------------------
# Pillow 手绘长图（Playwright 不可用时的降级路径）
# ---------------------------------------------------------------------------

#: 中文字体候选（Windows / Linux / macOS）。允许用环境变量指定，方便离线环境。
_FONT_CANDIDATES: tuple[str, ...] = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/Deng.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/msyh.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
)
_FONT_BOLD_CANDIDATES: tuple[str, ...] = (
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "C:/Windows/Fonts/simsun.ttc",
)


class _PillowFonts:
    """中文字体探测（自己实现，参考 AstrBot 本地 t2i 的思路但不 import 它）。

    为什么不用默认字体：Pillow 的 ``load_default`` 只有拉丁字形，中文要么变方块
    要么直接抛编码错误；所以优先找系统 CJK 字体，全都找不到时把非 ASCII 字符降级
    成 ``?``，保证「不崩」比「好看」优先。
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[int, bool], Any] = {}
        self._regular = self._resolve_candidates(_FONT_CANDIDATES)
        self._bold = self._resolve_candidates(_FONT_BOLD_CANDIDATES)
        self.cjk_found = bool(self._regular or self._bold)

    @staticmethod
    def _resolve_candidates(candidates: tuple[str, ...]) -> list[str]:
        override = os.environ.get("LIFESPAN_YEARBOOK_FONT", "").strip()
        paths: list[str] = []
        if override:
            paths.append(override)
        paths.extend(candidates)
        found: list[str] = []
        for raw in paths:
            try:
                path = Path(raw).expanduser()
            except (OSError, ValueError):
                continue
            if path.is_file():
                found.append(str(path))
        return found

    def get(self, size: int, bold: bool = False) -> Any:
        size = max(8, int(size))
        key = (size, bool(bold))
        if key in self._cache:
            return self._cache[key]
        from PIL import ImageFont

        candidates = self._bold if bold else self._regular
        if not candidates:
            candidates = self._regular or self._bold
        for path in candidates:
            try:
                font = ImageFont.truetype(path, size)
                self._cache[key] = font
                return font
            except (OSError, TypeError, ValueError):
                continue
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:  # Pillow < 10.1 不支持 size 参数
            font = ImageFont.load_default()
        self._cache[key] = font
        return font


def _wrap_pillow_text(text: str, font: Any, max_width: int) -> list[str]:
    """按像素宽度给 Pillow 手动换行（中文按字断，够用了）。"""
    text = _clean_text(text)
    if not text:
        return [""]
    if max_width <= 0:
        return [text]
    lines: list[str] = []
    current = ""
    width = 0.0
    for char in text:
        try:
            char_width = float(font.getlength(char))
        except Exception:  # noqa: BLE001 个别字体不支持 getlength
            char_width = float(font.size if hasattr(font, "size") else 16)
        if current and width + char_width > max_width:
            lines.append(current)
            current = char
            width = char_width
        else:
            current += char
            width += char_width
    if current:
        lines.append(current)
    return lines or [""]


class _PillowLayout:
    """一个极简的「先排版后绘制」画布：先累计算子，最后一次性出图。

    为什么两段式：Pillow 要先知道总高度才能建图，所以排版阶段只计算行，绘制阶段
    再落到 Image 上；顺带把每个块都做成独立方法，单测和排错都容易。
    """

    def __init__(self, width: int, palette: dict[str, tuple[int, int, int]]):
        self.width = max(360, int(width))
        self.palette = palette
        self.fonts = _PillowFonts()
        self.margin = 56
        self.ops: list[tuple[str, dict]] = []
        self.y = self.margin

    # ---- 基础算子 ---------------------------------------------------
    @property
    def content_width(self) -> int:
        return self.width - self.margin * 2

    def space(self, height: int) -> None:
        self.y += max(0, int(height))

    def rule(self, gap: int = 30, color: str = "line", thickness: int = 1) -> None:
        self.ops.append(
            (
                "line",
                {
                    "y": self.y,
                    "x1": self.margin,
                    "x2": self.width - self.margin,
                    "color": self.palette[color],
                    "width": thickness,
                },
            )
        )
        self.y += gap

    def text(
        self,
        content: Any,
        size: int,
        *,
        color: str = "ink",
        bold: bool = False,
        align: str = "left",
        gap_after: int = 10,
        line_gap: float = 1.7,
    ) -> None:
        text = _clean_text(content)
        if not text:
            return
        font = self.fonts.get(size, bold)
        if not self.fonts.cjk_found:
            text = text.encode("ascii", "replace").decode("ascii")
        lines = _wrap_pillow_text(text, font, self.content_width)
        line_height = int(size * line_gap)
        for line in lines:
            self.ops.append(
                (
                    "text",
                    {
                        "x": self.margin,
                        "y": self.y,
                        "text": line,
                        "font": font,
                        "color": self.palette[color],
                        "align": align,
                    },
                )
            )
            self.y += line_height
        self.y += gap_after

    def centered(self, content: Any, size: int, *, color: str = "ink", bold: bool = False, gap_after: int = 10) -> None:
        self.text(content, size, color=color, bold=bold, align="center", gap_after=gap_after)

    def rect(self, x1: int, y1: int, x2: int, y2: int, *, fill: str = "soft", radius: int = 8) -> None:
        self.ops.append(
            (
                "rect",
                {
                    "box": (x1, y1, x2, y2),
                    "fill": self.palette[fill],
                    "radius": radius,
                },
            )
        )

    # ---- 年报各块 ---------------------------------------------------
    def cover(self, ctx: dict) -> None:
        """封面：年份眉标、标题、副标题、大号年龄、生日行、开场短句。"""
        self.y = self.margin
        text = f"LIFESPAN · YEARBOOK · {ctx.get('year', '')}"
        self.text(text, 15, color="muted", gap_after=26)
        self.text(ctx.get("title") or "", 52, bold=True, gap_after=14)
        self.text(ctx.get("subtitle") or "", 22, color="muted", gap_after=30)
        self.rule(gap=36)
        age = ctx.get("age", 0)
        self.text(f"{age}", 96, bold=True, color="accent", gap_after=0)
        self.text("岁", 22, color="muted", gap_after=26)
        self.text(ctx.get("birthday_line") or "", 17, color="muted", gap_after=16)
        self.text(ctx.get("opening_quote") or "", 17, color="muted", align="center", gap_after=30)
        self.rule(gap=34)

    def intro(self, ctx: dict) -> None:
        self.text(ctx.get("intro") or "", 20, gap_after=26, line_gap=1.9)

    def stats_band(self, ctx: dict) -> None:
        """四格数字带：条数 / 活跃月份 / 约定 / 媒体。"""
        stats = ctx.get("stats") or {}
        cells = [
            (str(stats.get("total", 0)), "条记录"),
            (str(stats.get("active_months", 0)), "个活跃月份"),
            (str(stats.get("pinned", 0)), "条约定"),
            (str(stats.get("media", 0)), "个媒体"),
        ]
        box_top = self.y
        box_height = 108
        cell_width = self.content_width // len(cells)
        for index, (value, label) in enumerate(cells):
            x1 = self.margin + index * cell_width
            self.rect(x1 + 2, box_top, x1 + cell_width - 2, box_top + box_height, fill="soft", radius=6)
            self.ops.append(
                (
                    "text",
                    {
                        "x": x1 + cell_width // 2,
                        "y": box_top + 20,
                        "text": value,
                        "font": self.fonts.get(34, True),
                        "color": self.palette["ink"],
                        "align": "center",
                    },
                )
            )
            self.ops.append(
                (
                    "text",
                    {
                        "x": x1 + cell_width // 2,
                        "y": box_top + 68,
                        "text": label,
                        "font": self.fonts.get(14),
                        "color": self.palette["muted"],
                        "align": "center",
                    },
                )
            )
        self.y = box_top + box_height + 30

    def month_heat(self, ctx: dict) -> None:
        """12 个月热力格子：有记录的月份越深。"""
        months = ctx.get("months") or []
        if not months:
            return
        self.text("十二个月", 20, bold=True, gap_after=12)
        gap = 8
        box_width = (self.content_width - gap * 11) // 12
        box_height = 46
        top = self.y
        shades = {
            0: self.palette["soft"],
            1: _blend(self.palette["soft"], self.palette["accent"], 0.25),
            2: _blend(self.palette["soft"], self.palette["accent"], 0.45),
            3: _blend(self.palette["soft"], self.palette["accent"], 0.7),
            4: self.palette["accent"],
        }
        for index, month in enumerate(months):
            x1 = self.margin + index * (box_width + gap)
            level = _safe_int(month.get("level"), 0, lo=0, hi=4)
            self.ops.append(
                (
                    "rect",
                    {
                        "box": (x1, top, x1 + box_width, top + box_height),
                        "fill": shades.get(level, self.palette["soft"]),
                        "radius": 5,
                    },
                )
            )
            count = _safe_int(month.get("count"), 0, lo=0)
            # 深色格子用背景色写字、浅色格子用墨色写字（两套主题都保证可读）。
            label_color = "bg" if level >= 3 else "ink"
            self.ops.append(
                (
                    "text",
                    {
                        "x": x1 + box_width // 2,
                        "y": top + 8,
                        "text": str(count),
                        "font": self.fonts.get(16, True),
                        "color": self.palette[label_color],
                        "align": "center",
                    },
                )
            )
            short = str(month.get("short") or (str(month.get("month")) + "月"))
            self.ops.append(
                (
                    "text",
                    {
                        "x": x1 + box_width // 2,
                        "y": top + box_height + 6,
                        "text": short,
                        "font": self.fonts.get(12),
                        "color": self.palette["muted"],
                        "align": "center",
                    },
                )
            )
        self.y = top + box_height + 38

    def sections(self, ctx: dict) -> None:
        """正文三段：小标题 + 段落。"""
        for section in ctx.get("sections") or []:
            heading = _clean_text(section.get("heading"))
            if heading:
                self.text(heading, 24, bold=True, gap_after=8)
            self.text(section.get("body") or "", 18, gap_after=26, line_gap=1.9)

    def timeline(self, ctx: dict) -> None:
        """按月列条目；每一条只保留一句主文本。"""
        groups = ctx.get("entries_by_month") or []
        if not groups:
            return
        self.rule(gap=26)
        self.text("这一年的月份", 20, bold=True, gap_after=14)
        for group in groups:
            self.text(f"{group.get('label')} · {group.get('count')} 条", 16, color="muted", gap_after=6)
            for entry in group.get("entries") or []:
                date_text = entry.get("date_text") or ""
                text = _clean_text(entry.get("text"), 180)
                line = f"{entry.get('kind_label', '')}｜{text}"
                self.ops.append(
                    (
                        "text",
                        {
                            "x": self.margin + 8,
                            "y": self.y,
                            "text": date_text,
                            "font": self.fonts.get(13),
                            "color": self.palette["muted"],
                            "align": "left",
                        },
                    )
                )
                font = self.fonts.get(16)
                lines = _wrap_pillow_text(line, font, self.content_width - 120)
                for offset, wrapped in enumerate(lines):
                    self.ops.append(
                        (
                            "text",
                            {
                                "x": self.margin + 96,
                                "y": self.y + offset * 26,
                                "text": wrapped,
                                "font": font,
                                "color": self.palette["ink"],
                                "align": "left",
                            },
                        )
                    )
                self.y += max(1, len(lines)) * 26 + 8
            self.y += 10

    def media_gallery(self, ctx: dict) -> None:
        """媒体缩略图 3 列；图片读不出来就跳过。"""
        items = ctx.get("media") or []
        if not items:
            return
        try:
            from PIL import Image
        except Exception:  # noqa: BLE001
            return
        self.rule(gap=26)
        self.text("留下来的一些画面", 20, bold=True, gap_after=14)
        cell_width = (self.content_width - 16 * 2) // 3
        cell_height = 150
        row_top = self.y
        for index, item in enumerate(items):
            column = index % 3
            if column == 0 and index > 0:
                row_top += cell_height + 34
            x1 = self.margin + column * (cell_width + 16)
            try:
                with Image.open(item["path"]) as source:
                    thumb = source.convert("RGB")
                    thumb.thumbnail((cell_width, cell_height))
            except Exception:  # noqa: BLE001 坏图/视频首帧缺失都跳过
                continue
            self.ops.append(
                (
                    "image",
                    {
                        "image": thumb,
                        "box": (x1, row_top, x1 + cell_width, row_top + cell_height),
                        "color": self.palette["line"],
                    },
                )
            )
            caption = _clean_text(item.get("caption"), 28)
            self.ops.append(
                (
                    "text",
                    {
                        "x": x1,
                        "y": row_top + cell_height + 6,
                        "text": caption,
                        "font": self.fonts.get(12),
                        "color": self.palette["muted"],
                        "align": "left",
                    },
                )
            )
        self.y = row_top + cell_height + 34

    def closing(self, ctx: dict) -> None:
        self.rule(gap=30, color="ink")
        self.text(ctx.get("closing") or "", 22, gap_after=30, line_gap=1.9)

    def footer(self, ctx: dict) -> None:
        self.rule(gap=20, color="line")
        footer = (
            f"生成于 {ctx.get('today_text', '')} · 主题 {ctx.get('theme_title') or ctx.get('theme', '')} · "
            f"模式 {ctx.get('mode', '')}"
        )
        self.text(footer, 12, color="muted", gap_after=0)

    # ---- 绘制 -------------------------------------------------------
    def render(self) -> Any:
        """把算子落到一张 RGB 长图上。"""
        from PIL import Image, ImageDraw

        height = max(self.margin * 2, self.y + self.margin)
        image = Image.new("RGB", (self.width, height), self.palette["bg"])
        draw = ImageDraw.Draw(image)
        for kind, data in self.ops:
            try:
                if kind == "text":
                    x = data["x"]
                    if data.get("align") == "center":
                        try:
                            text_width = draw.textlength(data["text"], font=data["font"])
                        except Exception:  # noqa: BLE001
                            text_width = 0
                        x = max(self.margin, int((self.width - text_width) / 2))
                    draw.text((x, data["y"]), data["text"], font=data["font"], fill=data["color"])
                elif kind == "line":
                    draw.line((data["x1"], data["y"], data["x2"], data["y"]), fill=data["color"], width=data["width"])
                elif kind == "rect":
                    draw.rounded_rectangle(data["box"], radius=data["radius"], fill=data["fill"])
                elif kind == "image":
                    thumb = data["image"]
                    box = data["box"]
                    x = box[0] + max(0, (box[2] - box[0] - thumb.width) // 2)
                    y = box[1] + max(0, (box[3] - box[1] - thumb.height) // 2)
                    image.paste(thumb, (x, y))
                    draw.rectangle(box, outline=data["color"], width=1)
            except Exception:  # noqa: BLE001 单个算子失败不毁整张图
                continue
        return image


def _blend(
    first: tuple[int, int, int], second: tuple[int, int, int], ratio: float
) -> tuple[int, int, int]:
    """两个颜色按比例混合（Pillow 主题热力格用）。"""
    ratio = max(0.0, min(1.0, ratio))
    return tuple(int(round(a + (b - a) * ratio)) for a, b in zip(first, second))  # type: ignore[return-value]
