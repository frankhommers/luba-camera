"""On-demand WebRTC receiver using HA for Mammotion/Agora signalling."""

import asyncio
import hashlib
import json
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp

from .client import CameraError, HAClient
from .hevc import enable_hevc


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def rtc_configuration(config: dict) -> RTCConfiguration:
    return RTCConfiguration(
        iceServers=[
            RTCIceServer(
                urls=server["urls"],
                username=server.get("username"),
                credential=server.get("credential"),
            )
            for server in config.get("configuration", {}).get("iceServers", [])
        ]
    )


def save_frame(frame, path: Path) -> dict:
    frame.to_image().save(path, format="JPEG", quality=90)
    return {
        "file": path.name,
        "width": frame.width,
        "height": frame.height,
        "pts": frame.pts,
        "time_base": str(frame.time_base),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


async def capture(
    client: HAClient,
    entity: str,
    output: Path,
    *,
    count: int = 3,
    interval: float = 2,
    warmup: float = 1,
    timeout: float = 30,
) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "entity_id": entity,
        "ha_version": client.version,
        "started_at": utc_now(),
        "status": "running",
        "stage": "client_configuration",
        "frames": [],
    }
    pc = None
    tasks: list[asyncio.Task] = []
    subscription = None
    start = time.monotonic()
    try:
        async with asyncio.timeout(timeout):
            config = await client.request("camera/webrtc/get_client_config", entity_id=entity)
            enable_hevc()
            pc = RTCPeerConnection(rtc_configuration(config))
            pc.addTransceiver("video", direction="recvonly")
            if config.get("dataChannel"):
                pc.createDataChannel(config["dataChannel"])

            done = asyncio.get_running_loop().create_future()

            async def receive(track):
                first = None
                next_save = None
                try:
                    while len(report["frames"]) < count:
                        frame = await track.recv()
                        now = time.monotonic()
                        if first is None:
                            first = now
                            next_save = now + warmup
                        report["stage"] = "receiving_video"
                        if now < next_save:
                            continue
                        received_at = utc_now()
                        path = output / f"frame-{len(report['frames']) + 1:02d}.jpg"
                        metadata = await asyncio.to_thread(save_frame, frame, path)
                        metadata["received_at"] = received_at
                        report["frames"].append(metadata)
                        print(f"Saved {path.name}: {frame.width}x{frame.height}", flush=True)
                        next_save = now + interval
                    if not done.done():
                        done.set_result(None)
                except Exception:
                    if not done.done():
                        done.set_exception(
                            CameraError("Video ended or a frame could not be saved.")
                        )

            @pc.on("track")
            def on_track(track):
                if track.kind == "video":
                    tasks.append(asyncio.create_task(receive(track)))

            @pc.on("connectionstatechange")
            def on_connectionstatechange():
                if pc.connectionState == "failed" and not done.done():
                    done.set_exception(CameraError("WebRTC media connection failed (ICE/DTLS)."))

            report["stage"] = "gathering_ice"
            await pc.setLocalDescription(await pc.createOffer())
            queue = asyncio.Queue()
            report["stage"] = "opening_stream"
            subscription, _ = await client.request(
                "camera/webrtc/offer",
                entity_id=entity,
                offer=pc.localDescription.sdp,
                event_queue=queue,
            )

            async def signalling():
                buffered = []

                async def add_candidate(candidate):
                    text = candidate.get("candidate", "")
                    if not text:
                        await pc.addIceCandidate(None)
                        return
                    parsed = candidate_from_sdp(text.removeprefix("candidate:"))
                    parsed.sdpMid = candidate.get("sdpMid")
                    parsed.sdpMLineIndex = candidate.get("sdpMLineIndex")
                    await pc.addIceCandidate(parsed)

                try:
                    while True:
                        event = await queue.get()
                        match event.get("type"):
                            case "answer":
                                report["negotiated_codecs"] = [
                                    line.removeprefix("a=rtpmap:")
                                    for line in event["answer"].splitlines()
                                    if line.startswith("a=rtpmap:")
                                ]
                                await pc.setRemoteDescription(
                                    RTCSessionDescription(sdp=event["answer"], type="answer")
                                )
                                report["stage"] = "waiting_for_video"
                                for candidate in buffered:
                                    await add_candidate(candidate)
                                buffered.clear()
                            case "candidate":
                                if pc.remoteDescription is None:
                                    buffered.append(event["candidate"])
                                else:
                                    await add_candidate(event["candidate"])
                            case "error":
                                raise CameraError(
                                    "Camera stream negotiation was rejected or closed."
                                )
                except Exception as exc:
                    if not done.done():
                        done.set_exception(
                            exc
                            if isinstance(exc, CameraError)
                            else CameraError("Could not process the WebRTC answer or candidate.")
                        )

            tasks.append(asyncio.create_task(signalling()))
            await done
            report["status"] = "ok"
            report["stage"] = "complete"
    except TimeoutError:
        report["status"] = "timeout"
        raise CameraError(f"No complete capture within {timeout:g}s; stage: {report['stage']}.")
    except BaseException:
        report["status"] = "failed"
        raise
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if subscription is not None:
            with suppress(Exception):
                await client.unsubscribe(subscription)
        if pc is not None:
            report["connection"] = {
                "peer": pc.connectionState,
                "ice": pc.iceConnectionState,
                "ice_gathering": pc.iceGatheringState,
            }
            stats = await pc.getStats()
            report["media_stats"] = [
                {
                    key: value
                    for key, value in vars(stat).items()
                    if key
                    in {
                        "type",
                        "kind",
                        "packetsReceived",
                        "packetsLost",
                        "bytesReceived",
                        "bytesSent",
                        "dtlsState",
                        "iceRole",
                        "packetsSent",
                    }
                }
                for stat in stats.values()
                if stat.type in {"transport", "inbound-rtp"}
            ]
            with suppress(Exception):
                async with asyncio.timeout(5):
                    await pc.close()
        report["finished_at"] = utc_now()
        report["duration_seconds"] = round(time.monotonic() - start, 2)
        (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    return report
