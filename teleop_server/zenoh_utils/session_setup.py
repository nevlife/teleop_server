"""Zenoh router config writer.

SECURITY: the listen endpoint generated here binds to 0.0.0.0 (all
interfaces) and the router has no authentication, encryption or TLS.
The trust model is "private network only" — the teleop_server MUST be
deployed on a trusted private/VPN interface. Exposing TCP 7447 to the
public internet would allow anyone to subscribe to telemetry, inject
control commands, or e-stop the robot.

If wider exposure is needed, terminate TLS at an external reverse proxy
(or use a VPN tunnel) and keep zenoh itself bound to 127.0.0.1 / the
private interface only.
"""
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def sync_zenohd_config(tcp_port: int, output_dir: Path = None) -> None:
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
    logger.info(f"zenohd.json5 → tcp/{tcp_port}")
