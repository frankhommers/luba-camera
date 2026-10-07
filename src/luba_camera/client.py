"""Minimal HA WebSocket signalling client. No service-call API is exposed."""

import asyncio
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp


class CameraError(Exception):
    """A safe, user-facing failure (never includes raw signalling or credentials)."""


def websocket_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise CameraError("HA_URL must be an http(s) Home Assistant base URL.")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise CameraError("HA_URL must not contain credentials, a query, or a fragment.")
    return urlunsplit(
        (
            "wss" if parts.scheme == "https" else "ws",
            parts.netloc,
            parts.path.rstrip("/") + "/api/websocket",
            "",
            "",
        )
    )


class HAClient:
    def __init__(self, url: str, token: str):
        self.url = websocket_url(url)
        self.token = token
        self.session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.reader: asyncio.Task | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.events: dict[int, asyncio.Queue] = {}
        self.sequence = 0
        self.version = "unknown"

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        try:
            async with asyncio.timeout(15):
                self.ws = await self.session.ws_connect(self.url, heartbeat=20)
                hello = await self.ws.receive_json()
                if hello.get("type") != "auth_required":
                    raise CameraError("Unexpected HA authentication handshake.")
                await self.ws.send_json({"type": "auth", "access_token": self.token})
                result = await self.ws.receive_json()
                if result.get("type") != "auth_ok":
                    raise CameraError("Home Assistant rejected HA_TOKEN.")
                self.version = result.get("ha_version", "unknown")
            self.reader = asyncio.create_task(self._read())
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_):
        # Closing the socket also removes HA subscriptions and closes the camera session.
        if self.reader:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader
        if self.ws:
            await self.ws.close()
        if self.session:
            await self.session.close()

    async def _read(self):
        assert self.ws is not None
        try:
            async for message in self.ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = message.json()
                request_id = data.get("id")
                if data.get("type") == "result":
                    future = self.pending.get(request_id)
                    if future is not None and not future.done():
                        if data.get("success"):
                            future.set_result(data.get("result"))
                        else:
                            # Do not echo arbitrary server messages, SDP, URLs, or tokens.
                            future.set_exception(
                                CameraError("Home Assistant rejected the request.")
                            )
                elif data.get("type") == "event" and request_id in self.events:
                    self.events[request_id].put_nowait(data["event"])
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(CameraError("Home Assistant connection closed."))
            for queue in self.events.values():
                queue.put_nowait({"type": "error", "code": "connection_closed"})

    async def request(self, command: str, *, event_queue=None, **data: Any):
        assert self.ws is not None
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        if event_queue is not None:
            self.events[request_id] = event_queue
        try:
            await self.ws.send_json({"id": request_id, "type": command, **data})
            async with asyncio.timeout(15):
                result = await future
            return (request_id, result) if event_queue is not None else result
        finally:
            self.pending.pop(request_id, None)

    async def unsubscribe(self, subscription: int):
        try:
            async with asyncio.timeout(3):
                await self.request("unsubscribe_events", subscription=subscription)
        finally:
            self.events.pop(subscription, None)
