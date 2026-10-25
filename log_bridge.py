# -*- coding: utf-8 -*-
"""统一日志入口：只使用 AstrBot 的 logger（平台安全审查要求）。

平台规则：插件内日志必须且只能 ``from astrbot.api import logger``，禁止使用 Python
内置的标准库日志模块。各模块不直接 import astrbot，而是通过本文件拿 logger：

1. 生产路径：main.py 把 ``astrbot.api.logger`` 注入给 notebook / yearbook / webui；
2. 模块自身兜底：惰性 ``from astrbot.api import logger``（绝不使用标准库日志模块）；
3. 脱离 AstrBot 单独跑模块单测：导入不到 astrbot 时退回丢日志的 NullLogger。

本文件只做「取 logger / 级别常量 / 测试兜底」三件事，不配置日志 handler。
"""

from __future__ import annotations

from typing import Any

# 日志级别常量：只为让模块内 ``_log(LEVEL, ...)`` 调用不依赖标准库日志模块。
DEBUG = 10
INFO = 20
WARNING = 30
ERROR = 40


class _NullLogger:
    """脱离 AstrBot 独立单测时的唯一兜底：丢弃日志（不引入任何日志框架）。"""

    def debug(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        pass

    def info(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        pass

    def warning(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        pass

    def error(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        pass

    def exception(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        pass

    def log(self, level: int, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        pass


_NULL_LOGGER = _NullLogger()


def get_logger(injected: Any = None) -> Any:
    """返回 AstrBot logger；优先用 main.py 注入的实例。"""
    if injected is not None:
        return injected
    try:  # 生产/插件环境：唯一合规来源
        from astrbot.api import logger as astrbot_logger

        return astrbot_logger
    except Exception:  # noqa: BLE001 仅在脱离 AstrBot 的单测里走到
        return _NULL_LOGGER
