import argparse
import asyncio
import json
import logging
import math
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

from .capture import capture
from .client import CameraError, HAClient


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Browser-free Luba camera test through Home Assistant"
    )
    result.add_argument("--env-file", default=".env", help="Local credentials file (default: .env)")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("doctor", "snapshot"):
        command = commands.add_parser(name)
        command.add_argument("--camera", help="Camera entity ID; defaults to HA_CAMERA")
        if name == "snapshot":
            command.add_argument("--count", type=int, default=3)
            command.add_argument("--interval", type=float, default=2)
            command.add_argument("--warmup", type=float, default=1)
            command.add_argument("--timeout", type=float, default=30)
            command.add_argument("--output", type=Path, default=Path("captures"))
    return result


async def run(args):
    load_dotenv(args.env_file, override=False)
    url = os.environ.get("HA_URL", "")
    token = os.environ.get("HA_TOKEN", "")
    entity = args.camera or os.environ.get("HA_CAMERA", "")
    if not url or not token or not entity.startswith("camera."):
        raise CameraError("Set HA_URL, HA_TOKEN and HA_CAMERA in .env (see .env.example).")
    if args.command == "snapshot":
        if not 1 <= args.count <= 20:
            raise CameraError("--count must be between 1 and 20.")
        if not all(math.isfinite(v) for v in (args.interval, args.warmup, args.timeout)):
            raise CameraError("Timing values must be finite.")
        if args.interval < 0 or args.warmup < 0 or not 1 <= args.timeout <= 120:
            raise CameraError("Use nonnegative intervals and a timeout of 1–120 seconds.")
        if args.warmup + (args.count - 1) * args.interval >= args.timeout:
            raise CameraError("Timeout must leave time for stream setup and all requested frames.")
    async with HAClient(url, token) as client:
        if args.command == "doctor":
            states = await client.request("get_states")
            state = next((s for s in states if s["entity_id"] == entity), None)
            if state is None:
                raise CameraError("Camera entity was not found in Home Assistant.")
            updates = [
                {
                    "entity_id": s["entity_id"],
                    "installed_version": s.get("attributes", {}).get("installed_version"),
                }
                for s in states
                if s["entity_id"].startswith("update.") and "mammotion" in s["entity_id"]
            ]
            config = await client.request("camera/webrtc/get_client_config", entity_id=entity)
            print(
                json.dumps(
                    {
                        "ha_version": client.version,
                        "camera": entity,
                        "state": state["state"],
                        "integrations": updates,
                        "webrtc_configuration_available": True,
                        "ice_server_count": len(
                            config.get("configuration", {}).get("iceServers", [])
                        ),
                        "note": "Configuration only; no stream or mower movement was requested.",
                    },
                    indent=2,
                )
            )
        else:
            output = args.output / datetime.now(UTC).strftime("%Y%m%dT%H%M%S-%fZ")
            print(f"Capture directory: {output.resolve()}", flush=True)
            await capture(
                client,
                entity,
                output,
                count=args.count,
                interval=args.interval,
                warmup=args.warmup,
                timeout=args.timeout,
            )
            print("Capture complete; camera subscription closed.")


def main():
    args = parser().parse_args()
    # Third-party logs can include SDP, ICE credentials, or server URLs.
    logging.getLogger().addHandler(logging.NullHandler())
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(run(args))
    except CameraError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (aiohttp.ClientError, TimeoutError, OSError):
        print(
            "Error: connection or local file operation failed; check HA_URL and output access.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        print("Capture interrupted; connections closed.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(
            f"Error: {type(exc).__name__}; raw details omitted to protect credentials.",
            file=sys.stderr,
        )
        return 1
    return 0
