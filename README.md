# Luba Camera

Browser-free Mammotion camera snapshots through the existing Home Assistant
WebRTC API. This is an experimental, standalone Python receiver: it opens a
camera session, decodes a few frames, saves JPEGs, and disconnects.

**No changes to Home Assistant or Mammotion-HA are required for the tested setup.**
There is no mower movement, mowing, resume, or nudge functionality.

## Verified setup

Live-tested on 7 October 2026 with:

- LUBA 2 AWD 3000X
- Mammotion-HA v0.6.16 and Home Assistant 2026.9.4
- Python 3.13 on macOS
- Both left and right cameras: three 640×480 JPEGs per session
- A subsequent new session on the left camera

The first successful three-image capture took about 15 seconds. Images were
visually inspected. An initial VP8-only attempt established a connection and
received packets, but produced no frames. Offering and decoding **H.265/HEVC**
resolved this for the tested mower.

HA OS/Linux, other mower models, 4G, concurrent viewers, and long-running use
have not been validated by this project. This is a proof of concept, not a
production-ready Home Assistant integration.

## Architecture

```text
Python receiver ── HA WebSocket camera API ──> Home Assistant
                                                  │
                                           Mammotion-HA
                                                  │
                                           Agora signalling

Luba ── Mammotion/Agora media transport ──> Python aiortc + HEVC decoder
                                                        │
                                                 JPEGs + result.json
```

HA manages the existing Mammotion account, stream credentials, and camera
session lifecycle. Python negotiates through HA, then receives and decodes the
WebRTC media. No browser, additional Mammotion login, always-on service, Docker,
or AI service is needed. The existing Mammotion/Agora cloud connection is still
required.

## Quick start

Install [uv](https://docs.astral.sh/uv/), then:

```sh
git clone https://github.com/frankhommers/luba-camera.git
cd luba-camera
uv sync --locked --python 3.13
cp .env.example .env
chmod 600 .env
```

If `.env` already exists, skip the copy. Set:

- `HA_URL`: your Home Assistant base URL
- `HA_TOKEN`: a long-lived access token from your HA profile
- `HA_CAMERA`: your actual Mammotion camera entity ID

Environment variables take precedence over `.env`. Credentials, captures and the
virtual environment are ignored by Git. The tested PyAV wheel includes the
required FFmpeg decoder; no separate FFmpeg executable was needed.

```sh
# Check authentication and camera configuration without opening a video stream
uv run luba-camera doctor

# Save three images, two seconds apart, then disconnect
uv run luba-camera snapshot

# Select another camera using its actual HA entity ID
uv run luba-camera snapshot --camera camera.your_mower_right

# Save one image
uv run luba-camera snapshot --count 1

# Use another credentials file
uv run luba-camera --env-file /path/to/credentials.env snapshot
```

Each capture creates `captures/<UTC-timestamp>/` containing `frame-01.jpg` etc.
and `result.json`. The report includes receipt times, frame timestamps, hashes,
negotiated codecs, and transport statistics. Exit code 0 means success; 1 means
failure. Failed captures also write a report once the output directory exists.
Partial images do not mean the complete capture succeeded: check report `status`.

The default capture deadline is 30 seconds, including configuration, negotiation,
and receiving frames. Authentication has a separate 15-second limit; cleanup may
add a few seconds. Use `--timeout 60` to extend capture time, up to 120 seconds.
The receiver continuously consumes video between saved frames to avoid building
up a stale buffer. `--warmup 1` skips the first second of decoded video.

Close other viewers of the same camera when testing: a new Agora session can
displace an existing viewer. HA may report a camera as `streaming` even without
an active viewer. A static scene can yield identical photos; move an object in
view if you want a visual freshness check.

## HEVC implementation and limitations

aiortc 1.15.0 supports VP8 and H.264 video by default. `hevc.py` adds receive-only
H.265 support using PyAV and RFC 7798 RTP depacketization. Because aiortc has no
public codec registration API, this module uses **process-wide internal hooks**.
The aiortc version is pinned and the dependency lockfile is included.

Run this as a standalone process. The hooks have not been evaluated for use
inside HA's shared Python process. No installed library files, Home Assistant
files, or Mammotion-HA files are patched.

Supported HEVC subset: Main profile, single-stream transport (SRST), single NAL
units, aggregation packets, and fragmentation units without DONL. No HEVC sender,
PACI, multilayer video, or Agora channel-encryption support. Normal WebRTC
DTLS/SRTP transport encryption is active. VP8 and H.264 remain available.

Reports omit tokens, full SDP, and ICE credentials. Raw third-party error messages
and logs are suppressed. A `waiting_for_video` timeout with a connected transport
means negotiation worked but no image was decoded; possible causes include a
missing keyframe, codec mismatch, or a camera that is not publishing.

## Development

```sh
uv run pytest -q
uv run ruff check .
```

The 16 tests cover a local fake HA WebSocket server with real WebRTC video,
JPEG output, authentication failures, stream rejection, timeouts, cleanup,
HEVC RTP parsing, malformed packets, and decoding an image generated locally
with libx265. Tests do not connect to a real HA instance or mower. The HEVC
round-trip test requires a PyAV build with the libx265 encoder.

Developed with OpenAI Codex in an agentic workflow, including implementation,
automated tests, and live capture trials against the owner's actual mower.
The reported live results are separate from the synthetic tests. Maintainer
review and wider platform/lifecycle testing are still needed before integration.

This prototype is shared in the discussion about native snapshots in
[Mammotion-HA issue #827](https://github.com/mikey0000/Mammotion-HA/issues/827).

## References

- [HA WebRTC signalling](https://github.com/home-assistant/core/blob/2026.9.4/homeassistant/components/camera/webrtc.py)
- [Mammotion camera v0.6.16](https://github.com/mikey0000/Mammotion-HA/blob/v0.6.16/custom_components/mammotion/camera.py)
- [aiortc](https://github.com/aiortc/aiortc)
- [RFC 7798: HEVC over RTP](https://www.rfc-editor.org/rfc/rfc7798)

## License

MIT. Dependencies retain their own licenses.
