"""Receive-only H.265 support for aiortc 1.15 (RFC 7798, SRST without DONL).

aiortc has no public codec registration API. Keep the small compatibility hook
here and pin aiortc; it changes only this Python process, never Home Assistant.
"""

import av
from aiortc import codecs, rtcrtpreceiver
from aiortc.codecs.base import Decoder
from aiortc.mediastreams import VIDEO_TIME_BASE
from aiortc.rtcrtpparameters import RTCRtcpFeedback, RTCRtpCodecParameters

START_CODE = b"\x00\x00\x00\x01"
_installed = False


def depayload_hevc(payload: bytes) -> bytes:
    if len(payload) < 3 or payload[0] & 0x80 or not payload[1] & 7:
        raise ValueError("Invalid HEVC NAL header")
    kind = (payload[0] >> 1) & 0x3F
    if kind < 48:
        return START_CODE + payload
    if kind == 48:  # Aggregation packet: two-byte lengths followed by complete NALs.
        result = bytearray()
        offset = 2
        while offset < len(payload):
            if offset + 2 > len(payload):
                raise ValueError("Truncated HEVC aggregation length")
            length = int.from_bytes(payload[offset : offset + 2], "big")
            offset += 2
            if length < 2 or offset + length > len(payload):
                raise ValueError("Truncated HEVC aggregation NAL")
            result.extend(START_CODE + payload[offset : offset + length])
            offset += length
        return bytes(result)
    if kind == 49:  # Fragmentation unit: reconstruct NAL header on the start packet.
        if len(payload) < 4 or payload[2] & 0xC0 == 0xC0 or payload[2] & 0x3F >= 48:
            raise ValueError("Invalid HEVC fragment")
        prefix = b""
        if payload[2] & 0x80:
            header = bytes([(payload[0] & 0x81) | ((payload[2] & 0x3F) << 1), payload[1]])
            prefix = START_CODE + header
        return prefix + payload[3:]
    raise ValueError("HEVC PACI and reserved packet types are unsupported")


class HevcDecoder(Decoder):
    def __init__(self):
        self.codec = av.CodecContext.create("hevc", "r")
        self.codec.thread_count = 1

    def decode(self, encoded_frame):
        packet = av.Packet(encoded_frame.data)
        packet.pts = encoded_frame.timestamp
        packet.time_base = VIDEO_TIME_BASE
        try:
            return self.codec.decode(packet)
        except av.FFmpegError:
            # Joining mid-GOP can yield undecodable pictures until the next keyframe.
            return []


def enable_hevc():
    global _installed
    if _installed:
        return
    # Fail before opening the stream if this PyAV build has no HEVC decoder.
    av.Codec("hevc", "r")
    codecs.CODECS["video"].append(
        RTCRtpCodecParameters(
            mimeType="video/H265",
            clockRate=90000,
            payloadType=110,
            parameters={"profile-id": "1", "tier-flag": "0", "level-id": "180", "tx-mode": "SRST"},
            rtcpFeedback=[
                RTCRtcpFeedback(type="nack"),
                RTCRtcpFeedback(type="nack", parameter="pli"),
            ],
        )
    )
    original_decoder = rtcrtpreceiver.get_decoder
    original_depayload = rtcrtpreceiver.depayload

    def get_decoder(codec):
        return HevcDecoder() if codec.mimeType.lower() == "video/h265" else original_decoder(codec)

    def depayload(codec, payload):
        if codec.mimeType.lower() == "video/h265":
            return depayload_hevc(payload)
        return original_depayload(codec, payload)

    rtcrtpreceiver.get_decoder = get_decoder
    rtcrtpreceiver.depayload = depayload
    _installed = True
