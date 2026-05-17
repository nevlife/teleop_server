# NEV Stream Server (TCP variant)

TCP-only video relay for the NEV teleoperation stack. Implements
[`TCP_WIRE_SPEC.md`](../TCP_WIRE_SPEC.md) (v0.1) — the on-the-wire contract
shared by `stream_bot`, this server, and `stream_client`. No code is
shared between those three packages; they only share this spec.

> **Difference vs. [`stream_server`](../stream_server/) (UDP/RTP)**:
> AU framed, RTX/PLI 없음, loss path 없음.

## Structure

```
main.py                  # asyncio entry point
config.yaml              # TCP-only configuration keys
state.py                 # StreamSharedState (network + per-camera state)
robot_bridge.py          # Bot <-> server (camera AU receive + dedupe + forward)
station_bridge.py        # Client -> server (video_feedback / stream_heartbeat)
stream_telemetry.py      # video_ping/pong RTT + liveness checks
bitrate_controller.py    # ABR: put_latency + RTT (no loss path)
config/                  # StreamAppConfig schema + YAML loader
zenoh_utils/             # Zenoh session helpers (RELIABLE+BLOCK enforced)
zenohd.json5             # external zenohd config (synced on startup)
```

## Data flow

```
Bot    -> nev/stream_tcp/{vid}/camera[/{cam}]   -> [server]  -> nev/stream_tcp/{vid}/camera[/{cam}]  -> Client
Bot    -> nev/stream_tcp/{vid}/video_pong       -> [server]  (RTT)
Client -> nev/stream_tcp/{vid}/video_feedback   -> [server]  (latency_p95, freeze_ms)
Client -> nev/stream_tcp/{vid}/stream_heartbeat -> [server]  (5 Hz; rising edge -> dedupe reset)
[server] -> nev/stream_tcp/{vid}/video_ping     -> Bot       (1 Hz)
[server] -> nev/stream_tcp/{vid}/video_ctl      -> Bot       (BitrateController output)
```

The server is a transparent relay for AU payload bytes — no NAL parsing.
On every camera AU it prepends a 12 B server suffix to form the §5 28 B
relay header, then publishes RELIABLE+BLOCK so client subscribers get the
exact same delivery semantics as the bot publishers.

## Run

```bash
python3 main.py [--config config.yaml] [--zenoh-tcp-port 7457] [--vehicle-id 0]
```

The process opens an in-process Zenoh router on TCP only (no UDP locator,
per spec §1).

## Configuration (`config.yaml`)

| Key | Default | Notes |
|-----|---------|-------|
| `vehicle_id` | `"0"` | seeds video_ping target before any camera arrives |
| `zenoh_tcp_port` | `7457` | TCP listen port |
| `video_ping_rate` | `1.0` | server->bot ping cadence (Hz) |
| `client_heartbeat_timeout` | `3.0` | stream_heartbeat silence -> disconnected (s) |
| `bot_disconnect_timeout` | `3.0` | bot AU/pong silence -> disconnected (s) |
| `video.initial_kbps` | `2500` | start bitrate (spec §10) |
| `video.put_latency_high_ms` | `80.0` | ABR DOWN trigger (spec §9) |
| `video.rtt_high_ms` | `200.0` | ABR DOWN trigger (spec §9) |
| `video.dwell_s` | `5.0` | dwell between ABR changes (spec §9) |
| `video.recovery_dwell_s` | `30.0` | sustained-calm gate for ABR UP |

The full schema lives in `config/schema.py`.

## Topic table (TCP_WIRE_SPEC §2)

| Suffix | Pub | Sub | Payload |
|---|---|---|---|
| `camera` or `camera/{cam_id}` | bot | server | 16 B header + AU (§4) |
| `camera` or `camera/{cam_id}` | server | client | 28 B relay header + AU (§5) |
| `video_ctl` | server | bot | JSON `{type:"bitrate", kbps, camera_id?}` |
| `video_feedback` | client | server | JSON `{latency_p95_ms, freeze_ms_last_1s, ...}` |
| `stream_heartbeat` | client | server | JSON `{ts}` (5 Hz) |
| `video_ping` | server | bot | JSON `{ts}` (1 Hz) |
| `video_pong` | bot | server | JSON `{ts, rx_ts}` |

## BitrateController (TCP_WIRE_SPEC §9)

Signals:
* `put_latency_ms_p95` — 1 s window p95 of `pub.put` duration on the
  server -> client camera channel. Under RELIABLE+BLOCK this rises when the
  downstream queue stalls.
* `rtt_ms_p95` — 1 s window p95 of server <-> bot ping RTT.

Policy:
* Either signal over its high threshold => `bitrate *= 0.8`, dwell 5 s.
* Both signals at ≤ 50% of high for `recovery_dwell_s` (30 s) =>
  `bitrate *= 1.1`, dwell 5 s.
* Clamped to `[min_kbps, max_kbps]` (default `[200, 3000]` kbps).

No loss-based decision path exists in code — RELIABLE+BLOCK means loss=0
by construction, so any such path would be dead code.

## Sanity checks (TCP_WIRE_SPEC §13)

Camera AUs are dropped (and counted) if:
* `vehicle_ts` is negative, below `1e6`, or more than 10 s in the future
* AU payload size > 1 MiB or < 16 B (header size)

## Tests

```bash
make test          # quick run
make test-v        # verbose
make compile       # py_compile only
```

`Makefile` sets `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` so ROS Jazzy's
PYTHONPATH (which mixes py3.12 site-packages into a py3.10 venv) does not
break pytest plugin validation.

`tests/test_au_framing.py` round-trips the 16 B and 28 B headers — these
formats must match the bot's prepend code exactly. `tests/test_no_udp_dep.py`
statically rejects any reference to the UDP `stream_server`, to legacy wire
tokens (`rtx_request`, `transport_mode`, etc.), or to topic prefixes other
than `nev/stream_tcp/`.

## Dependencies

* [eclipse-zenoh](https://zenoh.io/)
* [PyYAML](https://pyyaml.org/)
* No dependency on `stream_server` (UDP/RTP variant) — verified statically.
