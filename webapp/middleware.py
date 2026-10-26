# -*- coding: utf-8 -*-
"""来源 IP 校验中间件（ASGI / FastAPI 版）。

纵深防御第一层：只信任来自 Docker 内部网络或本机回环的请求。

认证链在 Nginx / oauth2-proxy / Portal Authorizer 层完成，本平台通过
``Remote-*`` 请求头接收身份。若不校验来源，任何能直连容器端口的进程都能
伪造 ``Remote-User`` 冒充任意用户，因此必须拒绝非内网直连。

注册方式见 ``webapp/main.py``：作为最后一个 ``add_middleware``，使其位于
中间件栈最外层，最先执行。
"""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

# 与 DTD_nginx 其他子应用（EST_Cutting / iwork）保持一致的内网网段
_TRUSTED_IP_PREFIXES = (
    "172.",
    "10.",
    "192.168.",
    "127.",
    # Starlette TestClient 的占位地址，仅出现在进程内测试，不来自网络
    "testclient",
)


class TrustedProxyMiddleware:
    """纯 ASGI 中间件：校验客户端 IP 是否属于内网，否则返回 403。"""

    def __init__(self, app: ASGIApp) -> None:
        """保存下游应用。

        Args:
            app (ASGIApp): 下游 ASGI 应用。
        """
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """校验来源后放行，非白名单来源直接 403。

        Args:
            scope (Scope): ASGI scope。
            receive (Receive): ASGI receive。
            send (Send): ASGI send。
        """
        # 非 HTTP 请求（如 lifespan 生命周期事件）直接放行
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        remote_addr = client[0] if client else ""
        if not remote_addr or not remote_addr.startswith(_TRUSTED_IP_PREFIXES):
            await send({
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            })
            await send({"type": "http.response.body", "body": b"Forbidden"})
            return

        await self.app(scope, receive, send)
