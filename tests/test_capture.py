"""Exercise signalling and real video decoding against a local fake HA server."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from PIL import Image

from luba_camera.capture import capture
from luba_camera.client import CameraError, HAClient, websocket_url


class ChangingVideo(VideoStreamTrack):
    def __init__(self):
        super().__init__()
        self.index = 0

    async def recv(self):
        frame = await super().recv()
        self.index += 1
        frame.planes[0].update(bytes([32 + self.index % 160]) * frame.planes[0].buffer_size)
        return frame


@asynccontextmanager
async def fake_ha(mode="video"):
    calls = []
    peers = []
    socket_closed = asyncio.Event()

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        auth = await ws.receive_json()
        assert auth == {"type": "auth", "access_token": "test-token"}
        if mode == "auth_failure":
            await ws.send_json({"type": "auth_invalid", "message": "secret must not appear"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "test"})
        try:
            async for message in ws:
                msg = message.json()
                calls.append(msg["type"])
                result = None
                if msg["type"] == "camera/webrtc/get_client_config":
                    result = {"configuration": {"iceServers": []}}
                await ws.send_json(
                    {
                        "id": msg["id"],
                        "type": "result",
                        "success": True,
                        "result": result,
                    }
                )
                if msg["type"] != "camera/webrtc/offer":
                    continue
                if mode == "rejected":
                    await ws.send_json(
                        {
                            "id": msg["id"],
                            "type": "event",
                            "event": {
                                "type": "error",
                                "code": "500",
                                "message": "secret must not appear",
                            },
                        }
                    )
                    continue
                await ws.send_json(
                    {
                        "id": msg["id"],
                        "type": "event",
                        "event": {
                            "type": "session",
                            "session_id": "fake-session",
                        },
                    }
                )
                if mode == "silent":
                    continue
                pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
                peers.append(pc)
                pc.addTrack(ChangingVideo())
                await pc.setRemoteDescription(RTCSessionDescription(sdp=msg["offer"], type="offer"))
                await pc.setLocalDescription(await pc.createAnswer())
                await ws.send_json(
                    {
                        "id": msg["id"],
                        "type": "event",
                        "event": {
                            "type": "answer",
                            "answer": pc.localDescription.sdp,
                        },
                    }
                )
        finally:
            socket_closed.set()
        return ws

    app = web.Application()
    app.router.add_get("/api/websocket", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", calls, socket_closed
    finally:
        await asyncio.gather(*(pc.close() for pc in peers))
        await runner.cleanup()


async def test_actual_webrtc_video_to_jpegs_and_cleanup(tmp_path):
    async with fake_ha() as (url, calls, closed):
        async with HAClient(url, "test-token") as client:
            report = await capture(
                client,
                "camera.test",
                tmp_path / "run",
                count=3,
                interval=0.1,
                warmup=0,
                timeout=10,
            )
        await asyncio.wait_for(closed.wait(), 2)
        assert calls == [
            "camera/webrtc/get_client_config",
            "camera/webrtc/offer",
            "unsubscribe_events",
        ]
    assert report["status"] == "ok"
    assert len(report["frames"]) == 3
    assert len({f["pts"] for f in report["frames"]}) == 3
    assert len({f["sha256"] for f in report["frames"]}) == 3
    for frame in report["frames"]:
        with Image.open(tmp_path / "run" / frame["file"]) as image:
            assert image.size == (640, 480)
            assert image.format == "JPEG"
    manifest = (tmp_path / "run" / "result.json").read_text()
    assert "test-token" not in manifest
    assert "candidate:" not in manifest


@pytest.mark.parametrize("mode,expected", [("rejected", "failed"), ("silent", "timeout")])
async def test_failure_closes_subscription_and_records_stage(tmp_path, mode, expected):
    async with fake_ha(mode) as (url, calls, closed):
        async with HAClient(url, "test-token") as client:
            with pytest.raises(CameraError) as exc:
                await capture(client, "camera.test", tmp_path / "run", timeout=0.5)
            assert "secret" not in str(exc.value)
        await asyncio.wait_for(closed.wait(), 2)
        assert "unsubscribe_events" in calls
    report = json.loads((tmp_path / "run" / "result.json").read_text())
    assert report["status"] == expected
    assert report["frames"] == []


async def test_authentication_failure_is_sanitized():
    async with fake_ha("auth_failure") as (url, _, _):
        with pytest.raises(CameraError, match="rejected HA_TOKEN"):
            async with HAClient(url, "test-token"):
                pytest.fail("Authentication should have failed")


def test_websocket_base_path_and_credential_rejection():
    assert websocket_url("https://example.com/ha/") == "wss://example.com/ha/api/websocket"
    for url in (
        "ftp://example.com",
        "https://user:secret@example.com",
        "https://example.com?token=x",
    ):
        with pytest.raises(CameraError):
            websocket_url(url)
