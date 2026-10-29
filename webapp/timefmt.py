# -*- coding: utf-8 -*-
"""时间显示格式化：库内 UTC 转北京时间（UTC+8）。"""
from __future__ import annotations

from datetime import datetime, timedelta

CN_OFFSET = timedelta(hours=8)


def fmt_local(dt, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """UTC 时间（库内存储格式）转北京时间字符串；None 返回 '-'。"""
    if dt is None:
        return "-"
    if isinstance(dt, str):
        return dt
    return (dt + CN_OFFSET).strftime(fmt)
