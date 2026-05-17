"""struct pack/unpack round-trip for the two wire headers.

TCP_WIRE_SPEC §4 (16 B bot header) and §5 (28 B relay header). The bot's
prepend code must use the exact same struct format strings; any mismatch
here would silently corrupt every camera frame.
"""
import math
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from stream_server.wire_format import (  # noqa: E402
    BOT_HEADER_FMT,
    BOT_HEADER_SIZE,
    RELAY_HEADER_FMT,
    RELAY_HEADER_SIZE,
    encode_bot_header,
    encode_relay_header,
)


class TestBotHeader:
    def test_format_constants(self):
        # Spec §4: 16 B header.
        assert BOT_HEADER_FMT == "<dfB3x"
        assert BOT_HEADER_SIZE == 16
        assert struct.calcsize(BOT_HEADER_FMT) == 16

    def test_roundtrip_idr(self):
        ts = 1_700_000_000.123
        encode_ms = 4.5
        is_idr = True
        buf = encode_bot_header(ts, encode_ms, is_idr)
        assert len(buf) == 16
        ts_out, enc_out, flags_out = struct.unpack(BOT_HEADER_FMT, buf)
        assert math.isclose(ts_out, ts, rel_tol=0, abs_tol=1e-9)
        # f32 truncation tolerance.
        assert math.isclose(enc_out, encode_ms, rel_tol=0, abs_tol=1e-3)
        assert flags_out == 1

    def test_roundtrip_non_idr(self):
        buf = encode_bot_header(1_700_000_000.0, 0.0, False)
        _, _, flags = struct.unpack(BOT_HEADER_FMT, buf)
        assert flags == 0

    def test_padding_is_zero(self):
        # Spec §4: padding bytes must be 0x00 0x00 0x00 (3 trailing pad bytes).
        buf = encode_bot_header(1_700_000_000.0, 1.0, True)
        assert buf[13:16] == b"\x00\x00\x00"

    def test_appended_au_payload_passes_through(self):
        au = b"\x00\x00\x00\x01\x40\x01"  # H.265 VPS NAL start
        wire = encode_bot_header(1_700_000_000.0, 2.0, True) + au
        assert wire[BOT_HEADER_SIZE:] == au


class TestRelayHeader:
    def test_format_constants(self):
        # Spec §5: 28 B relay header (16 B bot header + 12 B server suffix).
        assert RELAY_HEADER_FMT == "<dfB3xdf"
        assert RELAY_HEADER_SIZE == 28
        assert struct.calcsize(RELAY_HEADER_FMT) == 28

    def test_roundtrip(self):
        ts = 1_700_000_000.123
        encode_ms = 3.0
        is_idr = True
        server_rx_ts = 1_700_000_000.150
        veh_to_srv_ms = 27.0
        buf = encode_relay_header(ts, encode_ms, is_idr, server_rx_ts, veh_to_srv_ms)
        assert len(buf) == 28
        ts_o, enc_o, flags_o, rx_o, v2s_o = struct.unpack(RELAY_HEADER_FMT, buf)
        assert math.isclose(ts_o, ts, abs_tol=1e-9)
        assert math.isclose(enc_o, encode_ms, abs_tol=1e-3)
        assert flags_o == 1
        assert math.isclose(rx_o, server_rx_ts, abs_tol=1e-9)
        assert math.isclose(v2s_o, veh_to_srv_ms, abs_tol=1e-2)

    def test_first_16_bytes_match_bot_header(self):
        # Spec §5: "offset 0..16 = bot header verbatim".
        ts, enc, is_idr = 1_700_000_000.5, 2.5, True
        bot = encode_bot_header(ts, enc, is_idr)
        relay = encode_relay_header(ts, enc, is_idr, 1_700_000_000.6, 10.0)
        assert relay[:16] == bot

    def test_appended_au_payload_passes_through(self):
        au = b"\xff\x00\xff\x00\xab\xcd"
        wire = encode_relay_header(
            1_700_000_000.0, 1.0, False, 1_700_000_000.1, 100.0
        ) + au
        assert wire[RELAY_HEADER_SIZE:] == au
