"""teleop_server must not depend on stream_server.

Statically scans the source of the target modules to verify that
'stream_server' and the video topic prefix (nev/stream/) do not appear.
"""
import io
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PY_FILES = [
    ROOT / "main.py",
    ROOT / "robot_bridge.py",
    ROOT / "station_bridge.py",
    ROOT / "state.py",
    ROOT / "config" / "schema.py",
    ROOT / "config" / "loader.py",
    ROOT / "config" / "__init__.py",
    ROOT / "telemetry" / "parser.py",
    ROOT / "telemetry" / "__init__.py",
    ROOT / "zenoh_utils" / "session_setup.py",
    ROOT / "zenoh_utils" / "__init__.py",
]

_FORBIDDEN_IDENTIFIERS = {
    "stream_server",
    "BitrateController",
    "StreamSharedState",
    "StreamNetworkStatus",
    "StreamTelemetryDriver",
}


def _identifiers_in(path: Path) -> list[str]:
    src = path.read_text()
    names = []
    for tok in tokenize.tokenize(io.BytesIO(src.encode()).readline):
        if tok.type == tokenize.NAME:
            names.append(tok.string)
    return names


class TestNoStreamDep:
    def test_no_stream_imports(self):
        for p in PY_FILES:
            if not p.exists():
                continue
            names = set(_identifiers_in(p))
            for forbidden in _FORBIDDEN_IDENTIFIERS:
                assert forbidden not in names, (
                    f"{p} references forbidden identifier {forbidden!r} — "
                    f"teleop_server must be independent from stream_server"
                )

    def test_no_stream_topic_prefix_in_code(self):
        for p in PY_FILES:
            if not p.exists():
                continue
            text = p.read_text()
            assert "nev/stream/" not in text, (
                f"{p} contains forbidden 'nev/stream/' prefix — "
                f"teleop_server must use nev/teleop/ exclusively"
            )

    def test_no_robot_topic_prefix_in_code(self):
        # The legacy nev/robot/ prefix must also be gone (migrated to teleop).
        for p in PY_FILES:
            if not p.exists():
                continue
            text = p.read_text()
            assert "nev/robot/" not in text, (
                f"{p} contains forbidden legacy 'nev/robot/' prefix"
            )

    def test_no_gcs_topic_prefix_in_code(self):
        for p in PY_FILES:
            if not p.exists():
                continue
            text = p.read_text()
            assert "nev/gcs/" not in text, (
                f"{p} contains forbidden legacy 'nev/gcs/' prefix"
            )

    def test_no_station_topic_prefix_in_code(self):
        for p in PY_FILES:
            if not p.exists():
                continue
            text = p.read_text()
            assert "nev/station/" not in text, (
                f"{p} contains forbidden legacy 'nev/station/' prefix"
            )

    def test_no_video_topic_handlers(self):
        forbidden_handlers = (
            "_handle_camera", "_handle_video_stats", "_handle_video_feedback",
            "_relay_video_ctl", "_relay_rtx_request",
            "send_video_bitrate", "send_video_ping", "BitrateController",
        )
        for p in PY_FILES:
            if not p.exists():
                continue
            text = p.read_text()
            for name in forbidden_handlers:
                assert name not in text, (
                    f"{p} contains video-side handler {name!r} — "
                    f"video belongs in stream_server"
                )
