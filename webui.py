# -*- coding: utf-8 -*-
"""寿命论 · v1.0.0 年报模板管理页后端（SPEC_V1 §4.4）。

页面栈是 AstrBot 4.29.1 的插件 Pages：
- 静态资源在 `pages/yearbook/`（index.html + app.js + style.css），由框架以受限 iframe 加载；
- 页面脚本通过 `window.AstrBotPluginPage` bridge 调用本文件注册的 Web API；
- 路由必须带插件名前缀（`/<plugin_name>/...`），bridge 端的 endpoint 不带前缀。

为什么后端这么写：
1. **不自己实现模板仓库**：所有读写都走 yearbook.TemplateStore（SPEC §4.3 契约），
   本文件只做 HTTP 层的参数校验、错误翻译、预览渲染。这样模板格式/校验规则只有一处。
2. **请求对象双兼容**：优先用新版 `astrbot.api.web.request`，旧版 AstrBot 退回
   `quart.request`；两者都不可用时返回空 dict，让本模块在没装 AstrBot 的测试里也能 import。
3. **错误统一返回 `{"status": "error", "message": ...}`**：bridge 收到这个形状会 reject，
   页面 catch 后弹 toast；成功时返回普通业务 dict（bridge 原样 resolve）。
4. **导入走 JSON + base64**：`bridge.upload()` 的 multipart 在旧版 AstrBot / 不同前端
   实现里行为不一致，而 zip 只有几百 KB，页面用 FileReader 读成 base64 再 apiPost 最稳。
   后端严格校验文件名/大小/解码，导入仍然调用 TemplateStore.import_zip。
5. **预览在沙箱 iframe 里渲染**：预览接口用样例数据 + jinja2 真渲染，页面把结果塞进
   `<iframe sandbox>` 的 srcdoc；模板是用户内容，不能让它拿到 dashboard 的凭据。

重要：本模块**顶层不 import astrbot**，AstrBot 相关 import 全部延迟到请求发生时才做，
单测可以直接 import 本模块 + fake TemplateStore / fake context。
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:  # AstrBot 自带 jinja2；测试环境缺了就降级为「只回显模板」
    import jinja2
except Exception:  # pragma: no cover - 只在极端环境触发
    jinja2 = None  # type: ignore[assignment]

LOGGER = logging.getLogger("astrbot_plugin_lifespan_theory.webui")

PLUGIN_NAME = "astrbot_plugin_lifespan_theory"
PAGE_DIR = Path(__file__).parent / "pages" / "yearbook"

# 模板名白名单：与 yearbook.TemplateStore._clean_name 保持一致（中文/字母/数字/下划线/连字符，
# 不含点——模板名对应 `templates/<name>.html` 的文件名主干，带点会变成 xxx.html.html）。
TEMPLATE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{1,64}$")
# 页面上限：模板是手写 HTML，512 KB 足够；导入 zip 4 MB。
MAX_TEMPLATE_CHARS = 512 * 1024
MAX_IMPORT_BYTES = 4 * 1024 * 1024
# 预览结果别超过这个量级，否则 bridge 传输和 iframe srcdoc 都会变慢。
MAX_PREVIEW_CHARS = 1024 * 1024

_HTML_VAR_RE = re.compile(r"{{-?\s*([A-Za-z_][A-Za-z0-9_]*)")
_HTML_FOR_RE = re.compile(r"{%-?\s*for\s+[A-Za-z_][A-Za-z0-9_]*\s+in\s+([A-Za-z_][A-Za-z0-9_]*)")
_HTML_BLOCK_VAR_RE = re.compile(
    r"{%-?\s*(?:if|elif|for|set|with|include|extends|import|from)\s+([A-Za-z_][A-Za-z0-9_]*)"
)
_JINJA_META = None
_JINJA_META_CHECKED = False

# 预览用的样例数据：尽量覆盖年报模板会用到的一切字段。
# 值是给模板看的示意内容，不落盘、不注入、不发送。
# 占位图用内联 PNG（320x200，~0.7KB）：预览离线可用、不依赖任何外部 URL / CDN。
_PLACEHOLDER_IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAUAAAADICAIAAAAWZq/8AAACh0lEQVR42u3ZMaqDQBiFUQ3uwcre/a/Gws5qIGAgG3ALCQTi/eec+sHLiB93Qsaz7QOQ6eERgIABAQMCBgEDAgYEDAgYBAwIGBAwCBgQMCBgQMAgYEDAgIABAYOAAQEDAgYBAwIGBAwIGAQMCBgQMCBgEDAgYEDAIGBAwICAAQGDgAEBA78weQSVtGP75M/mZfWsahjPtnsKPUQrZgFTp1slC5gK3SpZwBRJV8YCJj5dGQ9+RqJAvX//71hg6ZpiC0z302eKBUx2LRp2haZCJK7TFpjgiTPFAia7Cg0LmOweNCxgQMDm1ycXMBrQsIDp7u3XsIABARsuZxEw3ngnEjAgYPPrXAgYBIyZMsICBgQMCNgN0xkFDAgYELC7pZMKGBAwIGBAwL4WOq+AAQEDAgYBAwIGBAwIGAQMCBgQMCBgEDAg4E7My+q8CBgQMAgYELCvhU6KgEHAgIDp927p/ixgQMAgYNwwnU7AgIDNlHMhYBAwxsr8CljDzoKAQcAYLvMrYLp5+9UrYA2vPjkCBgFjysyvgKndg3oFTGoV6r2P8Wy7p3A37dikiwU2xeoVMGpRrys0oddp6VpgUvtRrwUmcoqlK2AiM5augMkrWbcCJq9k3QqYsJhFK2Bg8DMSIGAQMCBgQMCAgEHAgIABAQMCBgEDAgYEDAIGBAwIGBAwCBgQMCBgELBHAAIGBAwIGAQMCBgQMCBgEDAgYEDAIGBAwICAAQGDgAEBAwIGBAwCBgQMCBgEDAgYEDAgYChrer+engJYYEDAgIBBwICAAQEDAgYBAwIGBAwCBgQMCBgQMAgYEDAgYEDAIGBAwICAQcCAgAEBAwIGAQMCBgQMCBgEDAgY+M4FiFu8lFmXu+AAAAAASUVORK5CYII="
)


_MONTH_LABELS = (
    "一月", "二月", "三月", "四月", "五月", "六月",
    "七月", "八月", "九月", "十月", "十一月", "十二月",
)
_KIND_META = {
    "preference": ("喜好", "✨"),
    "event": ("做过的事", "🏃"),
    "mood": ("心情", "🌙"),
    "person": ("人", "👤"),
    "commitment": ("约定", "🤝"),
    "other": ("其它", "📝"),
}


def _sample_entries(year: int) -> list[dict]:
    """预览用条目：字段与 yearbook.normalize_entries 的输出对齐（text/kind_label/emoji…）。"""
    raw = [
        ("event", "2026-03-02", "她说只是顺路，结果在楼下等了四十分钟。", ["日常", "嘴硬"], False),
        ("preference", "2026-07-15", "记住了不加香菜。点单时先说了「不要香菜」，说完还嫌我大惊小怪。", ["偏好"], True),
        ("commitment", "2026-11-08", "「下次一定」——她说了三次，前两次都做到了。", ["约定"], True),
        ("mood", "", "没写日期的一句碎碎念：今天她比平时安静。", ["日常"], False),
    ]
    entries: list[dict] = []
    for index, (kind, date_text, content, tags, pinned) in enumerate(raw):
        label, emoji = _KIND_META.get(kind, _KIND_META["other"])
        month = day = None
        if date_text:
            month = int(date_text[5:7])
            day = int(date_text[8:10])
            date_display = f"{month} 月 {day} 日"
        else:
            date_display = ""
        entries.append(
            {
                "id": f"entry-{index:04d}",
                "date": date_text,
                "year": year if date_text else None,
                "month": month,
                "day": day,
                "date_text": date_display,
                "kind": kind,
                "kind_label": label,
                "kind_emoji": emoji,
                "text": content,
                "content": content,
                "tags": tags,
                "pinned": pinned,
                "media": [],
                "source": "agent",
            }
        )
    return entries


def _sample_stats(entries: list[dict]) -> dict:
    """预览用统计：字段形状与 yearbook.render_stats 对齐（kinds/month_heat 是对象列表）。"""
    by_kind = {key: 0 for key in _KIND_META}
    for entry in entries:
        by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + 1
    kinds: list[dict] = []
    total = sum(by_kind.values()) or 1
    for key, count in by_kind.items():
        if not count:
            continue
        label, emoji = _KIND_META[key]
        kinds.append(
            {
                "key": key,
                "label": label,
                "emoji": emoji,
                "count": count,
                "pct": round(count * 100 / total, 1),
            }
        )
    months = [0] * 12
    for entry in entries:
        month = entry.get("month")
        if month:
            months[int(month) - 1] += 1
    month_heat = []
    for index, count in enumerate(months, start=1):
        month_heat.append(
            {
                "month": index,
                "label": _MONTH_LABELS[index - 1],
                "short": f"{index}月",
                "count": count,
                "level": 0 if count <= 0 else min(4, 1 + count),
            }
        )
    dated = [entry for entry in entries if entry.get("date")]
    return {
        "total": 128,
        "by_kind": by_kind,
        "kinds": kinds,
        "months": months,
        "month_heat": month_heat,
        "active_months": sum(1 for count in months if count),
        "busiest_month": 7,
        "busiest_month_label": "七月",
        "busiest_month_count": 15,
        "first_date": dated[0]["date"] if dated else "",
        "last_date": dated[-1]["date"] if dated else "",
        "span_days": 252,
        "pinned": 23,
        "media": 9,
        "media_by_type": {"image": 8, "video": 1},
        "chars": 8421,
        "avg_chars": 65.8,
        "top_tags": [
            {"tag": "日常", "count": 18},
            {"tag": "嘴硬", "count": 12},
            {"tag": "约定", "count": 9},
        ],
        "age_span": None,
    }


def _sample_context(today: date | None = None) -> dict:
    """预览样例上下文：字段名/形状与 yearbook.YearbookBuilder._build_context 对齐。

    为什么自己造一份而不 import yearbook：webui 只依赖 TemplateStore 契约（SPEC §4.4）。
    预览的意义是「模板能不能渲染、长什么样」，所以样例必须覆盖内置/用户模板会用到的
    全部变量（entries 条目、entries_by_month 按月分组、month_heat 月热力、kinds 分布、
    media 媒体），并且形状与真实年报一致，否则 `{% for m in months %}{{ m.label }}`
    这类循环会静默渲染成空白，预览就失去参考价值。
    """
    today = today or date.today()
    year = today.year
    entries = _sample_entries(year)
    stats = _sample_stats(entries)
    entries_by_month: list[dict] = []
    for month in range(1, 13):
        month_entries = [entry for entry in entries if entry["month"] == month]
        if not month_entries:
            continue
        entries_by_month.append(
            {
                "month": month,
                "label": _MONTH_LABELS[month - 1],
                "count": len(month_entries),
                "entries": month_entries,
            }
        )
    media = [
        {
            "src": _PLACEHOLDER_IMAGE,
            "thumb": _PLACEHOLDER_IMAGE,
            "type": "image",
            "type_label": "图片",
            "note": "样例照片",
            "date_text": "5 月 1 日",
            "caption": "样例照片",
        },
        {
            "src": _PLACEHOLDER_IMAGE,
            "thumb": _PLACEHOLDER_IMAGE,
            "type": "image",
            "type_label": "图片",
            "note": "样例照片 2",
            "date_text": "9 月 20 日",
            "caption": "样例照片 2",
        },
    ]
    return {
        "title": f"德吉可的 {year} 年报",
        "subtitle": "一起走过的这一年",
        "character_name": "德吉可",
        "age": 14,
        "age_prev": 13,
        "year": year,
        "today": today.isoformat(),
        "today_text": f"{year} 年 {today.month} 月 {today.day} 日",
        "weekday_text": "星期六",
        "birthday_line": "写给 14 岁的德吉可。",
        "opening_quote": "她不说的话，比说出口的多。",
        "mode": "llm_guided",
        "mode_label": "LLM 引导",
        "theme": "minimal",
        "theme_title": "极简白",
        "theme_desc": "留白多、字少、适合只有几件小事的年份。",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "intro": "这一年被记下来的，多半是些不起眼的小事：等过的人、记住的口味、说漏嘴的约定。",
        "sections": [
            {"heading": "小事情", "body": "有些事当时没觉得重要，翻出来才发现都还在。"},
            {"heading": "说过的话", "body": "她答应过的事不多，但记住的都会做到。"},
        ],
        "closing": "下一年也请多指教。",
        "stats": stats,
        "entries": entries,
        "entries_by_month": entries_by_month,
        "months": stats["month_heat"],
        "kinds": stats["kinds"],
        "highlights": entries[:2],
        "media": media,
        "has_entries": True,
        "has_media": True,
        "entry_total": stats["total"],
        "entries_shown": len(entries),
        "truncation_note": "",
        "media_total": len(media),
        "tokens": {
            "llm_calls": 1,
            "prompt_chars": 1200,
            "completion_chars": 800,
            "approx_tokens": 700,
        },
        # 非 yearbook 官方变量，但自定义模板常用到的别名，预览时也给个合理值
        "character": "德吉可",
        "name": "德吉可",
        "nickname": "德吉可",
        "birthday": "2012-02-08",
        "date": today.isoformat(),
        "plugin": PLUGIN_NAME,
        "report_dir": "",
    }



# ==================================================================== 请求适配
def _current_request() -> Any | None:
    """取当前插件 Web API 请求代理：新版 astrbot.api.web → 旧版 quart → None。"""
    try:
        from astrbot.api.web import request as web_request  # 新版（4.29+）

        return web_request
    except Exception:  # noqa: BLE001 旧版没有这个模块，或没装 astrbot
        pass
    try:
        from quart import request as quart_request  # 旧版 Quart 兼容通道

        return quart_request
    except Exception:  # noqa: BLE001
        return None


async def _request_json(req: Any = None, default: dict | None = None) -> dict:
    """把请求体读成 dict；读不到/不是 JSON 对象时返回 default。

    `_req` 是为了单测可注入：生产代码不传，走 bridge/框架绑定的请求代理。
    """
    if default is None:
        default = {}
    req = req if req is not None else _current_request()
    if req is None:
        return default

    payload: Any = None
    try:
        json_method = getattr(req, "json", None)
    except Exception:  # noqa: BLE001 astrbot 的请求代理在非请求上下文里 __getattr__ 会抛错
        json_method = None
    if callable(json_method):
        try:
            payload = json_method(default=default)  # astrbot.api.web.PluginRequest.json
        except TypeError:
            try:
                payload = json_method()
            except Exception:  # noqa: BLE001
                payload = None
        except Exception:  # noqa: BLE001
            payload = None
        try:
            payload = await payload
        except Exception:  # noqa: BLE001 没有请求上下文 / 非 JSON 请求体
            payload = None
        if isinstance(payload, dict):
            return payload
    try:
        get_json = getattr(req, "get_json", None)  # quart.Request.get_json
    except Exception:  # noqa: BLE001
        get_json = None
    if callable(get_json):
        try:
            payload = await get_json()
        except Exception:  # noqa: BLE001
            payload = None
        if isinstance(payload, dict):
            return payload
    return default


def _request_query(req: Any = None) -> Any:
    req = req if req is not None else _current_request()
    if req is None:
        return {}
    query = getattr(req, "query", None)
    return query if query is not None else {}


def _query_get(req: Any, key: str, default: Any = None) -> Any:
    query = _request_query(req)
    try:
        value = query.get(key, default)
    except TypeError:
        value = query.get(key)
    return default if value is None else value


def _request_username(req: Any = None) -> str:
    req = req if req is not None else _current_request()
    try:
        return str(getattr(req, "username", "") or "")
    except Exception:  # noqa: BLE001
        return ""


# ==================================================================== 参数校验
def _error(message: str) -> dict:
    """bridge 认这个形状：status=error → 页面 Promise reject。"""
    return {"status": "error", "message": str(message)}


def _jsonable(value: Any) -> Any:
    """把 TemplateStore 返回值洗成 JSON 可序列化结构（Path/datetime 等都转字符串）。"""
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def safe_template_name(raw: Any) -> str:
    """校验模板名：只能是一个安全的短文件名，防止路径逃逸/URL 注入。"""
    name = str(raw or "").strip()
    if not name:
        raise ValueError("模板名不能为空")
    if len(name) > 64:
        raise ValueError("模板名过长（最多 64 字符）")
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError("模板名不能包含路径分隔符或 . / ..")
    if name.startswith("."):
        raise ValueError("模板名不能以 . 开头")
    if any(ord(char) < 32 for char in name):
        raise ValueError("模板名不能包含控制字符")
    if not TEMPLATE_NAME_RE.match(name):
        raise ValueError("模板名只能包含中文/字母/数字/下划线/连字符（不含扩展名，1~64 字符）")
    return name


def safe_filename(raw: Any) -> str:
    """导入文件名：只取 basename，去掉任何路径成分。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    return Path(text.replace("\\", "/")).name.strip()


def html_import_name(filename: str) -> str:
    """从导入的 .html 文件名推出一个安全的模板名（与 TemplateStore 命名规则一致）。

    TemplateStore 的名字是「不含扩展名的文件名主干」，只接受中文/字母/数字/下划线/
    连字符；用户本机文件名可能带空格、括号、点、emoji，这里统一压成 "_"，
    去首尾符号，空/超长兜底，保证导入不会因为文件名奇怪而失败。
    """
    stem = Path(safe_filename(filename)).stem
    cleaned = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]", "_", stem)
    cleaned = cleaned.strip("_-")
    if not cleaned:
        cleaned = "template"
    return cleaned[:64]


def validate_html(raw: Any) -> str:
    if not isinstance(raw, str):
        raise ValueError("模板内容必须是字符串")
    html = raw.strip()
    if not html:
        raise ValueError("模板内容为空")
    if len(html) > MAX_TEMPLATE_CHARS:
        raise ValueError(f"模板内容过大（上限 {MAX_TEMPLATE_CHARS} 字符）")
    return raw


def required_vars(templates: Any) -> list[str]:
    """读 TemplateStore.REQUIRED_VARS（SPEC §4.3），兼容缺失/坏定义。"""
    raw = getattr(templates, "REQUIRED_VARS", ())
    try:
        items = list(raw)
    except TypeError:
        return []
    return [item for item in items if isinstance(item, str) and item.strip()]


def template_vars(html: str) -> set[str]:
    """提取模板里引用的顶层变量名（优先级：jinja2 语法树 → 正则粗提取）。

    用 jinja2.meta 的 find_undeclared_variables 是为了正确处理
    `{% for e in entries %}`：entries 是外部变量、e 是循环局部变量，不能混为一谈；
    没有 jinja2 时才退回正则粗提取（宁可多算，也不要漏报必需变量）。
    """
    text = str(html or "")
    meta = _jinja_meta()
    if meta is not None:
        try:
            environment = jinja2.Environment()
            return set(meta.find_undeclared_variables(environment.parse(text)))
        except Exception:  # noqa: BLE001 语法坏的时候退回正则，让预览接口去报语法错误
            pass
    used = set(_HTML_VAR_RE.findall(text))
    used.update(_HTML_FOR_RE.findall(text))
    used.update(_HTML_BLOCK_VAR_RE.findall(text))
    return used


def _jinja_meta():
    """延迟 import jinja2.meta；没有 jinja2 时返回 None（只影响变量提示的精度）。"""
    global _JINJA_META, _JINJA_META_CHECKED
    if not _JINJA_META_CHECKED:
        _JINJA_META_CHECKED = True
        try:
            from jinja2 import meta as jinja2_meta

            _JINJA_META = jinja2_meta
        except Exception:  # pragma: no cover - AstrBot 环境一定有 jinja2
            _JINJA_META = None
    return _JINJA_META


def missing_required_vars(html: str, required: list[str]) -> list[str]:
    used = template_vars(html)
    return [name for name in required if name not in used]


def build_preview_context(
    required: list[str] | None = None, overrides: dict | None = None
) -> tuple[dict, list[str]]:
    """拼预览样例数据；返回 (context, 补空串的变量名列表)。"""
    context = copy.deepcopy(_sample_context())
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            key_text = str(key or "").strip()
            if key_text and key_text.isidentifier():
                context[key_text] = _jsonable(value)
    filled: list[str] = []
    for name in required or []:
        if name not in context:
            context[name] = ""
            filled.append(name)
    return context, filled


def render_preview_html(
    html: str,
    required: list[str] | None = None,
    overrides: dict | None = None,
    renderer: Any = None,
) -> dict:
    """用样例数据渲染模板 HTML。

    返回 {"html": 渲染结果, "warnings": [...], "missing_vars": [...],
          "filled_vars": [...], "error": ""}。

    renderer 可选：优先用 TemplateStore.render_string 渲染，保证预览环境与真实年报
    渲染完全一致（同一个 Jinja Environment/autoescape/过滤器）；没有 renderer 时
    退回本地 jinja2.Template。两者都失败时只把错误写进 error，不抛给框架。
    """
    result: dict = {
        "html": html,
        "warnings": [],
        "missing_vars": [],
        "filled_vars": [],
        "error": "",
    }
    # 页面发来的 sample 覆盖只保留一层基本类型，避免把任意对象塞进渲染上下文
    if not isinstance(html, str):
        result["error"] = "模板内容必须是字符串"
        result["warnings"].append(result["error"])
        return result
    context, filled = build_preview_context(required, overrides)
    result["filled_vars"] = filled
    if required:
        result["missing_vars"] = missing_required_vars(html, required)
        if result["missing_vars"]:
            result["warnings"].append(
                "模板里没有引用这些必需变量：" + "、".join(result["missing_vars"])
            )

    if callable(renderer):
        try:
            rendered = renderer(html, context)
            if not isinstance(rendered, str):
                raise ValueError("渲染器没有返回字符串")
        except ValueError as exc:  # TemplateStore.render_string 把错误包成 ValueError
            result["error"] = f"模板渲染失败：{exc}"
            result["warnings"].append(result["error"])
            return result
        except Exception as exc:  # noqa: BLE001 渲染失败不允许把接口打成 500
            result["error"] = f"渲染失败：{type(exc).__name__}: {exc}"
            result["warnings"].append(result["error"])
            return result
        return _finish_preview(result, rendered)

    if jinja2 is None:  # pragma: no cover - AstrBot 环境一定有 jinja2
        result["warnings"].append("当前环境没有 jinja2，预览只回显模板原文")
        return result
    try:
        template = jinja2.Template(html, autoescape=True)
        rendered = template.render(**context)
    except jinja2.TemplateSyntaxError as exc:
        result["error"] = f"模板语法错误（第 {exc.lineno} 行）：{exc.message}"
        result["warnings"].append(result["error"])
        return result
    except Exception as exc:  # noqa: BLE001 渲染失败不允许把接口打成 500
        result["error"] = f"渲染失败：{type(exc).__name__}: {exc}"
        result["warnings"].append(result["error"])
        return result
    return _finish_preview(result, rendered)


def _finish_preview(result: dict, rendered: str) -> dict:
    """预览结果的收尾：裁到上限并写入 html。"""
    if len(rendered) > MAX_PREVIEW_CHARS:
        rendered = rendered[:MAX_PREVIEW_CHARS]
        result["warnings"].append("渲染结果过长，预览只保留前 1MB")
    result["html"] = rendered
    return result


def _find_meta(items: list[dict], name: str) -> dict:
    for item in items or []:
        if isinstance(item, dict) and str(item.get("name") or "") == name:
            return item
    return {}


def _store_failure(result: Any) -> str:
    """TemplateStore 的部分方法用 {"ok": False, "error": ...} 表达失败而不抛异常。

    比如 delete(): 内置模板不能删、模板不存在都会返回 ok=False；import_zip(): zip 里
    一个可用模板都没有也是 ok=False。webui 必须认这个形状，否则会把失败当成功回给页面。
    """
    if isinstance(result, dict) and result.get("ok") is False:
        return str(result.get("error") or "操作失败")
    return ""


def _skipped_summary(result: Any, limit: int = 3) -> str:
    """把 import_zip 的 skipped 列表压成一句人话，方便直接 toast。"""
    if not isinstance(result, dict):
        return ""
    skipped = result.get("skipped")
    if not isinstance(skipped, list) or not skipped:
        return ""
    parts = []
    for item in skipped[:limit]:
        if isinstance(item, dict):
            name = str(item.get("filename") or "").strip()
            reason = str(item.get("reason") or "").strip()
            parts.append(f"{name}（{reason}）" if name else reason)
    if len(skipped) > limit:
        parts.append(f"等 {len(skipped)} 个")
    return "；".join(part for part in parts if part)


# ==================================================================== 页面 API
class YearbookPageApi:
    """年报模板管理页的所有后端处理函数。

    做成类而不是闭包：单测可以直接 `api = YearbookPageApi(fake_store)`，
    对每个 handler 传 fake `_req` 真跑，不需要伪造整个 AstrBot context。
    """

    def __init__(self, templates: Any, logger: Any = None):
        self.templates = templates
        self.logger = logger or LOGGER

    # ---- 内部小工具
    def _warn(self, message: str) -> None:
        try:
            self.logger.warning(message)
        except Exception:  # noqa: BLE001 日志失败不能影响接口
            pass

    def _list_meta(self) -> list[dict]:
        items = self.templates.list()
        if not isinstance(items, list):
            return []
        return [_jsonable(item) for item in items if isinstance(item, dict)]

    def _template_renderer(self):
        """优先借 TemplateStore.render_string 渲染预览，保证和真实年报同一个 Jinja 环境。

        SPEC §4.3 只要求 list/get/save/import_zip/delete；render_string 是 yearbook 的
        扩展方法。有就用（自定义模板可能依赖那边的 Environment 配置），没有就返回 None
        让 render_preview_html 退回本地 jinja2。
        """
        render = getattr(self.templates, "render_string", None)
        if not callable(render):
            return None

        def _call(source: str, context: dict) -> str:
            try:
                return render(source, context, label="preview")
            except TypeError:  # 兼容只接受两个参数的实现
                return render(source, context)

        return _call

    # ---- GET /templates
    async def list_templates(self, _req: Any = None) -> dict:
        """模板列表 + 页面要用的元信息（必需变量、样例变量、当前用户）。"""
        try:
            items = self._list_meta()
        except Exception as exc:  # noqa: BLE001
            self._warn(f"[{PLUGIN_NAME}] 读取模板列表失败：{exc!r}")
            return _error(f"读取模板列表失败：{exc}")
        required = required_vars(self.templates)
        try:
            sample_vars = sorted(_sample_context().keys())
        except Exception:  # pragma: no cover
            sample_vars = []
        payload: dict = {
            "templates": items,
            "required_vars": required,
            "sample_vars": sample_vars,
            "plugin": PLUGIN_NAME,
            "page": "yearbook",
            "username": _request_username(_req),
        }
        stats_method = getattr(self.templates, "stats", None)
        if callable(stats_method):
            try:
                payload["stats"] = _jsonable(stats_method())
            except Exception:  # noqa: BLE001 统计失败不影响列表
                payload["stats"] = {}
        return payload

    # ---- GET /templates/<name>
    async def get_template(self, name: str = "", _req: Any = None) -> dict:
        """读单个模板原文（给编辑器 textarea 用）。"""
        try:
            safe_name = safe_template_name(name)
        except ValueError as exc:
            return _error(str(exc))
        try:
            html = self.templates.get(safe_name)
        except KeyError:
            return _error(f"模板不存在：{safe_name}")
        except ValueError as exc:
            return _error(f"模板名不合法：{exc}")
        except Exception as exc:  # noqa: BLE001
            self._warn(f"[{PLUGIN_NAME}] 读取模板 {safe_name} 失败：{exc!r}")
            return _error(f"读取模板失败：{exc}")
        if not isinstance(html, str):
            return _error(f"模板 {safe_name} 的内容不是 HTML 文本")
        meta = _find_meta(self._list_meta(), safe_name)
        required = required_vars(self.templates)
        return {
            "name": safe_name,
            "html": html,
            "source": str(meta.get("source") or ""),
            "title": str(meta.get("title") or ""),
            "desc": str(meta.get("desc") or ""),
            "updated_at": _jsonable(meta.get("updated_at")),
            "required_vars": required,
            "missing_vars": missing_required_vars(html, required),
        }

    # ---- POST /templates（保存）
    async def save_template(self, _req: Any = None) -> dict:
        body = await _request_json(_req)
        try:
            name = safe_template_name(body.get("name"))
            html = validate_html(body.get("html"))
        except ValueError as exc:
            return _error(str(exc))
        try:
            result = self.templates.save(name, html)
        except ValueError as exc:
            # TemplateStore 的校验错误（缺必需变量等）原样回给用户，他们才知道改哪里
            return _error(f"模板校验失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            self._warn(f"[{PLUGIN_NAME}] 保存模板 {name} 失败：{exc!r}")
            return _error(f"保存模板失败：{exc}")
        failure = _store_failure(result)
        if failure:
            return _error(f"模板校验失败：{failure}")
        try:
            meta = _find_meta(self._list_meta(), name)
        except Exception:  # noqa: BLE001
            meta = {}
        return {
            "saved": True,
            "name": name,
            "template": _jsonable(meta),
            "result": _jsonable(result),
        }

    # ---- POST /templates/import（zip 或 html）
    async def import_template(self, _req: Any = None) -> dict:
        body = await _request_json(_req)
        filename = safe_filename(body.get("filename"))
        if not filename:
            filename = "import.html"
        lower = filename.lower()
        try:
            if lower.endswith(".zip"):
                data = self._decode_import_bytes(body)
                result = self.templates.import_zip(filename, data)
                failure = _store_failure(result)
                if failure:
                    skipped = _skipped_summary(result)
                    detail = f"（{skipped}）" if skipped else ""
                    return _error(f"导入失败：{failure}{detail}")
                payload = {
                    "imported": True,
                    "kind": "zip",
                    "filename": filename,
                    "result": _jsonable(result),
                }
                skipped = _skipped_summary(result)
                if skipped:
                    payload["warnings"] = [f"部分文件被跳过：{skipped}"]
                return payload
            if lower.endswith((".html", ".htm")):
                html = body.get("html")
                if not isinstance(html, str) or not html.strip():
                    # 兼容前端只发 base64 的情况
                    data = self._decode_import_bytes(body)
                    html = data.decode("utf-8-sig", errors="replace")
                html = validate_html(html)
                name = html_import_name(filename)
                result = self.templates.save(name, html)
                failure = _store_failure(result)
                if failure:
                    return _error(f"导入失败：模板校验没通过：{failure}")
                return {
                    "imported": True,
                    "kind": "html",
                    "filename": filename,
                    "name": name,
                    "result": _jsonable(result),
                }
            return _error("只支持导入 .html / .htm / .zip 文件")
        except (ValueError, KeyError) as exc:
            return _error(f"导入失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            self._warn(f"[{PLUGIN_NAME}] 导入模板 {filename} 失败：{exc!r}")
            return _error(f"导入失败：{type(exc).__name__}: {exc}")

    def _decode_import_bytes(self, body: dict) -> bytes:
        """从请求体里解出 zip 二进制：支持 data_base64 / base64 / data URL 前缀。"""
        encoded = body.get("data_base64") or body.get("base64") or body.get("data")
        if not isinstance(encoded, str) or not encoded.strip():
            raise ValueError("缺少文件内容 data_base64")
        payload = encoded.strip()
        if payload.startswith("data:") and "," in payload:
            payload = payload.split(",", 1)[1]
        try:
            data = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("文件内容的 base64 编码不合法") from exc
        if not data:
            raise ValueError("文件内容为空")
        if len(data) > MAX_IMPORT_BYTES:
            raise ValueError(f"文件过大（上限 {MAX_IMPORT_BYTES} 字节）")
        return data

    # ---- POST /templates/delete
    async def delete_template(self, _req: Any = None) -> dict:
        body = await _request_json(_req)
        try:
            name = safe_template_name(body.get("name"))
        except ValueError as exc:
            return _error(str(exc))
        try:
            result = self.templates.delete(name)
        except KeyError:
            return _error(f"模板不存在：{name}")
        except ValueError as exc:
            # TemplateStore 约定：内置模板不能删
            return _error(f"不能删除：{exc}")
        except Exception as exc:  # noqa: BLE001
            self._warn(f"[{PLUGIN_NAME}] 删除模板 {name} 失败：{exc!r}")
            return _error(f"删除模板失败：{exc}")
        failure = _store_failure(result)
        if failure:
            # TemplateStore 用 ok=False 表达「不存在 / 内置不能删」；原样转给人看，
            # 但补一句「不能删除」让前端 toast 一眼能懂发生了什么。
            if "不存在" in failure:
                return _error(failure)
            return _error(f"不能删除：{failure}")
        return {"deleted": True, "name": name, "result": _jsonable(result)}

    # ---- POST /preview
    async def preview_template(self, _req: Any = None) -> dict:
        body = await _request_json(_req)
        name = str(body.get("name") or "").strip()
        html = body.get("html")
        if isinstance(html, str) and html.strip():
            try:
                html = validate_html(html)
            except ValueError as exc:
                return _error(str(exc))
        elif name:
            try:
                safe_name = safe_template_name(name)
                html = self.templates.get(safe_name)
            except ValueError as exc:
                return _error(str(exc))
            except KeyError:
                return _error(f"模板不存在：{name}")
            except Exception as exc:  # noqa: BLE001
                self._warn(f"[{PLUGIN_NAME}] 预览读取模板 {name} 失败：{exc!r}")
                return _error(f"读取模板失败：{exc}")
        else:
            return _error("预览需要 name 或 html 之一")
        if not isinstance(html, str) or not html.strip():
            return _error("模板内容为空，无法预览")
        overrides = body.get("sample")
        rendered = render_preview_html(
            html,
            required_vars(self.templates),
            overrides if isinstance(overrides, dict) else None,
            renderer=self._template_renderer(),
        )
        rendered["name"] = name
        return rendered


# ==================================================================== 注册入口
def build_routes(api: YearbookPageApi | None = None, templates: Any = None) -> list[tuple]:
    """生成 (route, handler, methods, desc) 列表；便于单测检查路由表。"""
    page_api = api if api is not None else YearbookPageApi(templates)
    prefix = f"/{PLUGIN_NAME}/yearbook"
    return [
        (
            f"{prefix}/templates",
            page_api.list_templates,
            ["GET"],
            "列出年报模板（内置 + 用户）",
        ),
        (
            f"{prefix}/templates/<path:name>",
            page_api.get_template,
            ["GET"],
            "读取单个年报模板 HTML",
        ),
        (
            f"{prefix}/templates",
            page_api.save_template,
            ["POST"],
            "保存年报模板（写入用户模板目录）",
        ),
        (
            f"{prefix}/templates/import",
            page_api.import_template,
            ["POST"],
            "导入 .zip / .html 年报模板",
        ),
        (
            f"{prefix}/templates/delete",
            page_api.delete_template,
            ["POST"],
            "删除用户年报模板",
        ),
        (
            f"{prefix}/preview",
            page_api.preview_template,
            ["POST"],
            "用样例数据渲染年报模板预览",
        ),
    ]


def register_web_apis(context: Any, templates: Any, logger: Any = None) -> list[tuple[str, str]]:
    """向 AstrBot 注册年报模板管理页的全部后端接口。

    返回已注册的 (route, method) 列表，方便单测/冒烟断言。
    单条失败只记日志、不影响其它路由（与 astrbot_plugin_lazy_tools 的做法一致）。
    """
    log = logger or LOGGER
    page_api = YearbookPageApi(templates, log)
    registered: list[tuple[str, str]] = []
    for route, handler, methods, desc in build_routes(page_api):
        try:
            context.register_web_api(route, handler, methods, desc)
        except Exception as exc:  # noqa: BLE001
            try:
                log.warning(f"[{PLUGIN_NAME}] 注册页面路由 {route} 失败：{exc!r}")
            except Exception:  # noqa: BLE001
                pass
            continue
        for method in methods:
            registered.append((route, str(method).upper()))
    return registered


# ==================================================================== 自检
def self_check() -> str:
    """不依赖 AstrBot 的自检：参数校验、预览渲染、路由表。"""
    assert safe_template_name("极简白") == "极简白"
    assert safe_template_name("my-template_1") == "my-template_1"
    for bad in ("", "..", ".hidden", "a/b", "a\\b", "x" * 65, "带.点", "带 空格"):
        try:
            safe_template_name(bad)
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"模板名 {bad!r} 应该被拒绝")
    assert safe_filename("C:\\tmp\\a.zip") == "a.zip"
    assert safe_filename("../../a.html") == "a.html"
    assert html_import_name("自定义 模板.html") == "自定义_模板"

    html = "<html><body><h1>{{ title }}</h1>{{ stats.total }}<ul>{% for e in entries %}<li>{{ e.content }}</li>{% endfor %}</ul></body></html>"
    preview = render_preview_html(html, ["title", "entries", "stats", "missing_one"])
    assert "德吉可" in preview["html"], preview
    assert "128" in preview["html"], preview
    assert "missing_one" in preview["missing_vars"], preview
    rendered_by_store = render_preview_html(html, [], renderer=lambda source, ctx: "STORE:" + ctx["title"])
    assert rendered_by_store["html"].startswith("STORE:德吉可"), rendered_by_store
    bad_preview = render_preview_html("{% if %}", [])
    assert bad_preview["error"], bad_preview

    class _FakeCtx:
        def __init__(self):
            self.calls = []

        def register_web_api(self, route, handler, methods, desc):
            self.calls.append((route, handler, methods, desc))

    class _FakeStore:
        REQUIRED_VARS = ("title",)

        def list(self):
            return [{"name": "极简白", "title": "极简白", "source": "builtin", "updated_at": ""}]

        def get(self, name):
            if name != "极简白":
                raise KeyError(name)
            return "<h1>{{ title }}</h1>"

        def save(self, name, html):
            return {"name": name}

        def import_zip(self, filename, data):
            return {"imported": []}

        def delete(self, name):
            return {"deleted": name}

    ctx = _FakeCtx()
    routes = register_web_apis(ctx, _FakeStore())
    assert len(ctx.calls) == 6 and len(routes) == 6, routes
    return "ok"


if __name__ == "__main__":  # pragma: no cover
    print(self_check())
