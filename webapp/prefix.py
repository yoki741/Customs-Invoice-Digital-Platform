# -*- coding: utf-8 -*-
"""子路径前缀支持（Nginx 反向代理 /customs-invoice/）。

Portal 以子路径方式挂载本平台：Nginx 先按
``rewrite ^/customs-invoice/(.*)$ /$1 break;`` 剥掉前缀再转发，同时注入
``X-Forwarded-Prefix: /customs-invoice``（见 generate_nginx_config.py 的
NGINX_EXTRA_DIRECTIVES）。

因此应用侧需要做两件事：

1. 页面里的绝对链接带上前缀 —— 模板通过 ``prefix`` 变量拼（见 templates_setup.py）；
2. ``RedirectResponse("/invoices")`` 产生的 ``Location`` 自动补前缀 —— 本模块的
   ``PrefixMiddleware`` 统一处理，不必改每个跳转点。

本地直连（无 X-Forwarded-Prefix）时前缀为空，行为与改造前完全一致。
"""

from __future__ import annotations

import os
import re

from starlette.types import ASGIApp, Message, Receive, Scope, Send

# 只接受单段、纯 ASCII 的前缀，避免注入非法 Location / 模板内容
_PREFIX_RE = re.compile(r"^/[A-Za-z0-9._~\-]{1,63}$")

# 前缀在 scope 中存放的键
SCOPE_KEY = "app_prefix"


def normalize_prefix(value: str) -> str:
    """校验并规范化前缀。

    Args:
        value (str): 原始前缀，例如 ``/customs-invoice/``。

    Returns:
        str: 规范化后的前缀（无尾斜杠）；非法或为空时返回空串。
    """
    prefix = (value or "").strip().rstrip("/")
    if not prefix or not _PREFIX_RE.match(prefix):
        return ""
    return prefix


def configured_prefix() -> str:
    """读取本地部署时写死的前缀（``APP_PREFIX``），默认无。

    Returns:
        str: 规范化后的前缀或空串。
    """
    return normalize_prefix(os.environ.get("APP_PREFIX", ""))


def current_prefix(scope: Scope) -> str:
    """读取当前请求的应用前缀。

    Args:
        scope (Scope): ASGI scope。

    Returns:
        str: 形如 ``/customs-invoice`` 的前缀；根路径部署时为空串。
    """
    return scope.get(SCOPE_KEY) or ""


# 属于 Portal 根路径、不能被补应用前缀的地址（oauth2-proxy 端点）
_EXTERNAL_PATH_PREFIXES = ("/oauth2/",)


def _prefix_location(location: str, prefix: str) -> str:
    """给 ``Location`` 补前缀。

    Args:
        location (str): 响应头原始值。
        prefix (str): 应用前缀。

    Returns:
        str: 补好前缀的地址；外链、协议相对地址、Portal 端点与已带前缀的
        地址原样返回。
    """
    if not location.startswith("/") or location.startswith("//"):
        return location
    if location.startswith(_EXTERNAL_PATH_PREFIXES):
        return location
    if location == prefix or location.startswith(prefix + "/"):
        return location
    return prefix + location


class PrefixMiddleware:
    """纯 ASGI 中间件：解析前缀并对响应 ``Location`` 补前缀。

    只改 ``Location``，不改响应体；页面内的链接由模板 ``prefix`` 变量负责。
    """

    def __init__(self, app: ASGIApp) -> None:
        """保存下游应用并读取兜底前缀。

        Args:
            app (ASGIApp): 下游 ASGI 应用。
        """
        self.app = app
        self.fallback = configured_prefix()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """解析前缀并包装 send，实现 Location 改写。

        Args:
            scope (Scope): ASGI scope。
            receive (Receive): ASGI receive。
            send (Send): ASGI send。
        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        prefix = self.fallback
        for key, value in scope.get("headers") or ():
            if key == b"x-forwarded-prefix":
                prefix = normalize_prefix(value.decode("latin-1")) or prefix
                break
        scope[SCOPE_KEY] = prefix

        if not prefix:
            await self.app(scope, receive, send)
            return

        async def send_with_prefix(message: Message) -> None:
            """转发响应消息，必要时改写 Location 头。

            Args:
                message (Message): 下游发出的 ASGI 响应消息。
            """
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or ())
                for index, (key, value) in enumerate(headers):
                    if key.lower() != b"location":
                        continue
                    location = value.decode("latin-1")
                    headers[index] = (
                        key,
                        _prefix_location(location, prefix).encode("latin-1"),
                    )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_prefix)
