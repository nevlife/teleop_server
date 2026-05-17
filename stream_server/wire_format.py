"""On-the-wire header definitions for stream_server.

This module is deliberately import-light (just `struct`) so tests can pull
in the format constants and helpers without dragging in `zenoh`. Source of
truth: /home/nev/teleop/TCP_WIRE_SPEC.md sections 4 and 5.
"""
import struct

# TCP_WIRE_SPEC §4: bot->server header.
#   vehicle_ts  f64  bot system_clock seconds
#   encode_ms   f32  encoder turnaround for this AU (ms)
#   flags       u8   bit0 = is_idr
#   padding     3 B  must be zero
# Little-endian. Total = 16 bytes.
BOT_HEADER_FMT = "<dfB3x"
BOT_HEADER_SIZE = struct.calcsize(BOT_HEADER_FMT)  # 16

# TCP_WIRE_SPEC §5: server->client relay header. First 16 B are the bot
# header verbatim; we append server_rx_ts (f64) and veh_to_srv_ms (f32).
RELAY_HEADER_FMT = "<dfB3xdf"
RELAY_HEADER_SIZE = struct.calcsize(RELAY_HEADER_FMT)  # 28


def encode_bot_header(vehicle_ts: float, encode_ms: float, is_idr: bool) -> bytes:
    return struct.pack(BOT_HEADER_FMT, vehicle_ts, encode_ms, 1 if is_idr else 0)


def encode_relay_header(
    vehicle_ts: float,
    encode_ms: float,
    is_idr: bool,
    server_rx_ts: float,
    veh_to_srv_ms: float,
) -> bytes:
    return struct.pack(
        RELAY_HEADER_FMT,
        vehicle_ts,
        encode_ms,
        1 if is_idr else 0,
        server_rx_ts,
        veh_to_srv_ms,
    )
