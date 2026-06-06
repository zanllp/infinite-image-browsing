"""Small ASGI-to-aiohttp bridge used by the ComfyUI custom node entrypoint.

ComfyUI exposes an aiohttp server, while Infinite Image Browsing already exposes a
FastAPI/Starlette ASGI app.  This bridge lets us mount that app without adding a
new PyPI dependency, which is important for packaged/offline ComfyUI builds.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, MutableMapping, Optional

from aiohttp import web

ASGIApp = Callable[[MutableMapping[str, Any], Callable[[], Awaitable[Dict[str, Any]]], Callable[[Dict[str, Any]], Awaitable[None]]], Awaitable[None]]


def _split_host_port(host_header: str, scheme: str) -> tuple[str, int]:
    """Return an ASGI server tuple from an HTTP Host header."""
    default_port = 443 if scheme == "https" else 80
    host_header = host_header or "127.0.0.1"

    # IPv6 literals may be formatted as [::1]:8188. Keep the parser small but
    # robust enough for normal ComfyUI localhost usage.
    if host_header.startswith("[") and "]" in host_header:
        host, _, rest = host_header[1:].partition("]")
        if rest.startswith(":"):
            try:
                return host, int(rest[1:])
            except ValueError:
                return host, default_port
        return host, default_port

    if ":" in host_header:
        host, port_text = host_header.rsplit(":", 1)
        try:
            return host, int(port_text)
        except ValueError:
            return host_header, default_port

    return host_header, default_port


class ComfyUIASGIMount:
    """Mount an ASGI application under an aiohttp route prefix."""

    def __init__(self, asgi_app: ASGIApp, prefix: str) -> None:
        self.asgi_app = asgi_app
        self.prefix = prefix.rstrip("/") or "/"

    def register(self, aiohttp_app: web.Application) -> None:
        """Register this ASGI app on the provided ComfyUI aiohttp app."""
        pattern = self.prefix if self.prefix == "/" else f"{self.prefix}{{tail:.*}}"
        aiohttp_app.router.add_route("*", pattern, self.handle)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        body_sent = False
        response: Optional[web.StreamResponse] = None
        response_started = asyncio.Event()

        async def receive() -> Dict[str, Any]:
            nonlocal body_sent
            if body_sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: Dict[str, Any]) -> None:
            nonlocal response
            message_type = message.get("type")

            if message_type == "http.response.start":
                headers = {}
                for raw_name, raw_value in message.get("headers", []):
                    name = raw_name.decode("latin-1") if isinstance(raw_name, bytes) else str(raw_name)
                    value = raw_value.decode("latin-1") if isinstance(raw_value, bytes) else str(raw_value)
                    # aiohttp manages Transfer-Encoding itself. Passing it through can
                    # create invalid combinations for streamed responses.
                    if name.lower() == "transfer-encoding":
                        continue
                    headers[name] = value

                response = web.StreamResponse(
                    status=int(message.get("status", 200)),
                    headers=headers,
                )
                await response.prepare(request)
                response_started.set()
                return

            if message_type == "http.response.body":
                if response is None:
                    response = web.StreamResponse(status=200)
                    await response.prepare(request)
                    response_started.set()

                chunk = message.get("body", b"") or b""
                if chunk:
                    await response.write(chunk)

                if not message.get("more_body", False):
                    await response.write_eof()
                return

            raise RuntimeError(f"Unsupported ASGI message type: {message_type}")

        raw_path_qs = request.raw_path
        raw_path_only, _, raw_query_string = raw_path_qs.partition("?")

        scope: MutableMapping[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": f"{request.version.major}.{request.version.minor}",
            "method": request.method,
            "scheme": request.scheme,
            "path": request.path,
            # Preserve the original percent-encoded UTF-8 bytes. This is required
            # for ComfyUI filenames containing Chinese text, e.g. "通用放大.png".
            "raw_path": raw_path_only.encode("ascii", "ignore"),
            "query_string": raw_query_string.encode("ascii", "ignore"),
            "root_path": "",
            "headers": [(name.lower(), value) for name, value in request.raw_headers],
            "client": (request.remote or "", 0),
            # Avoid request.url here: some aiohttp/yarl versions reject a Host
            # value containing a port (for example "127.0.0.1:8188"), which can
            # abort the response before FastAPI sees the request.
            "server": _split_host_port(request.host, request.scheme),
        }

        await self.asgi_app(scope, receive, send)

        if response is None:
            return web.Response(status=500, text="ASGI application did not return a response")

        if not response.prepared:
            await response.prepare(request)
            await response.write_eof()

        return response
