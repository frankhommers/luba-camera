import re
from fractions import Fraction

import av
import pytest
from aiortc.jitterbuffer import JitterFrame

from luba_camera.hevc import START_CODE, HevcDecoder, depayload_hevc


def test_single_aggregation_and_fragmented_nals():
    nal = b"\x26\x01abcdef"
    assert depayload_hevc(nal) == START_CODE + nal
    other = b"\x40\x01xyz"
    aggregation = b"\x60\x01" + len(nal).to_bytes(2, "big") + nal
    aggregation += len(other).to_bytes(2, "big") + other
    assert depayload_hevc(aggregation) == START_CODE + nal + START_CODE + other
    start = b"\x62\x01\x93abc"
    end = b"\x62\x01\x53def"
    assert depayload_hevc(start) + depayload_hevc(end) == START_CODE + nal


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x26",
        b"\x26\x00a",
        b"\xa6\x01a",
        b"\x60\x01\x00",
        b"\x60\x01\x00\x05a",
        b"\x62\x01\xd3a",
        b"\x62\x01\x93",
        b"\x64\x01a",
    ],
)
def test_malformed_or_unsupported_rtp_is_rejected(payload):
    with pytest.raises(ValueError):
        depayload_hevc(payload)


def test_real_hevc_encoded_picture_depacketizes_and_decodes():
    encoder = av.CodecContext.create("libx265", "w")
    encoder.width, encoder.height = 64, 48
    encoder.pix_fmt = "yuv420p"
    encoder.time_base = Fraction(1, 30)
    encoder.thread_count = 1
    encoder.options = {
        "preset": "ultrafast",
        "tune": "zerolatency",
        "x265-params": "log-level=none:pools=none:frame-threads=1:repeat-headers=1",
    }
    source = av.VideoFrame(64, 48, "yuv420p")
    source.pts = 0
    for index, plane in enumerate(source.planes):
        plane.update(bytes([96 if index == 0 else 128]) * plane.buffer_size)
    encoded = b"".join(bytes(packet) for packet in encoder.encode(source) + encoder.encode(None))
    nals = [nal for nal in re.split(b"\x00\x00\x00?\x01", encoded) if nal]
    assert nals
    reassembled = bytearray()
    for nal in nals:
        # Force multi-packet fragments, including parameter sets, to exercise reconstruction.
        if len(nal) <= 4:
            reassembled.extend(depayload_hevc(nal))
            continue
        kind = (nal[0] >> 1) & 0x3F
        fu = bytes([(nal[0] & 0x81) | (49 << 1), nal[1]])
        data = nal[2:]
        middle = len(data) // 2
        reassembled.extend(depayload_hevc(fu + bytes([kind | 0x80]) + data[:middle]))
        reassembled.extend(depayload_hevc(fu + bytes([kind | 0x40]) + data[middle:]))
    frames = HevcDecoder().decode(JitterFrame(data=bytes(reassembled), timestamp=90000))
    assert len(frames) == 1
    assert (frames[0].width, frames[0].height) == (64, 48)
    assert frames[0].pts == 90000
