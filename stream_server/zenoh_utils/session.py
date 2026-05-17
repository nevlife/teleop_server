"""Zenoh session builder for stream_server.

Every publisher in this server runs with RELIABLE + BLOCK — that is the
defining property of the TCP variant (TCP_WIRE_SPEC §3). BLOCK means
`pub.put` will await backpressure rather than drop, which is what feeds
the BitrateController's `put_latency_ms` signal.
"""
import json
import logging
from pathlib import Path

import zenoh

logger = logging.getLogger(__name__)


# All publishers MUST be RELIABLE+BLOCK per TCP_WIRE_SPEC §3.
QOS_REL_BLOCK = dict(
    reliability=zenoh.Reliability.RELIABLE,
    congestion_control=zenoh.CongestionControl.BLOCK,
    priority=zenoh.Priority.INTERACTIVE_HIGH,
)

# Same delivery semantics, just deprioritised so heartbeat-class JSON traffic
# doesn't elbow camera AUs at the egress queue.
QOS_REL_BLOCK_LOW = dict(
    reliability=zenoh.Reliability.RELIABLE,
    congestion_control=zenoh.CongestionControl.BLOCK,
    priority=zenoh.Priority.DATA_LOW,
)


def sync_zenohd_config(tcp_port: int, output_dir: Path = None) -> None:
    """Re-write `zenohd.json5` so an external `zenohd` matches our listen port."""
    if output_dir is None:
        output_dir = Path(__file__).parent.parent

    config_content = (
        "{\n"
        "  listen: {\n"
        f'    endpoints: ["tcp/0.0.0.0:{tcp_port}"],\n'
        "  },\n"
        "  scouting: {\n"
        "    multicast: {\n"
        "      enabled: false,\n"
        "    },\n"
        "  },\n"
        "}\n"
    )

    (output_dir / "zenohd.json5").write_text(config_content)
    logger.info(f"zenohd.json5 -> tcp/{tcp_port}")


def open_router_session(tcp_port: int) -> zenoh.Session:
    """Open an in-process Zenoh router listening on tcp/0.0.0.0:{port}."""
    listen_eps = [f"tcp/0.0.0.0:{tcp_port}"]
    zconf = zenoh.Config()
    zconf.insert_json5("mode", '"router"')
    zconf.insert_json5("listen/endpoints", json.dumps(listen_eps))
    session = zenoh.open(zconf)
    logger.info(f"Zenoh STREAM_TCP router opened -> listening on {listen_eps}")
    return session
