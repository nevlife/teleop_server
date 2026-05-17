"""Static guard: stream_server must not depend on the UDP variant.

Checks every source file for:
  * forbidden identifiers naming the UDP sibling package
  * forbidden wire-format strings (rtx_request, PLI, etc.)
  * topic prefixes from the UDP variant (`nev/stream/...` minus our own
    `nev/stream_tcp/...`)
"""
import io
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY_FILES = [
    ROOT / "main.py",
    ROOT / "robot_bridge.py",
    ROOT / "bitrate_controller.py",
    ROOT / "station_bridge.py",
    ROOT / "state.py",
    ROOT / "stream_telemetry.py",
    ROOT / "wire_format.py",
    ROOT / "config" / "schema.py",
    ROOT / "config" / "__init__.py",
    ROOT / "zenoh_utils" / "__init__.py",
    ROOT / "zenoh_utils" / "session.py",
]

# Identifier names the UDP variant ships under. Importing these or naming a
# variable after them is a strong indicator of cross-package contamination.
_FORBIDDEN_IDENTIFIERS = {
    # Note: we cannot just ban "stream_server" because every reference in
    # comments/docstrings would fail. Tokenize filters to NAME tokens only,
    # so a code import like `from stream_server.foo import X` would still
    # surface as the identifier `stream_server` in the source.
    "stream_server",
}

# Wire-protocol strings that exist only in the UDP variant.
_FORBIDDEN_WIRE_STRINGS = (
    "rtx_request",
    "video_stats",  # bot->server stats topic in UDP variant; not in TCP spec
    "transport_mode",
    "low_freeze",
    "tcp_stale",
    "PLI",
)


def _identifiers_in(path: Path) -> set[str]:
    src = path.read_text()
    names = set()
    for tok in tokenize.tokenize(io.BytesIO(src.encode()).readline):
        if tok.type == tokenize.NAME:
            names.add(tok.string)
    return names


def _string_literals_in(path: Path) -> list[str]:
    src = path.read_text()
    out = []
    for tok in tokenize.tokenize(io.BytesIO(src.encode()).readline):
        if tok.type == tokenize.STRING:
            out.append(tok.string)
    return out


class TestNoUdpDep:
    def test_no_forbidden_identifiers(self):
        for p in PY_FILES:
            if not p.exists():
                continue
            names = _identifiers_in(p)
            for forbidden in _FORBIDDEN_IDENTIFIERS:
                assert forbidden not in names, (
                    f"{p} references forbidden identifier {forbidden!r} — "
                    f"stream_server must be independent from the UDP variant"
                )

    def test_no_udp_wire_strings_in_code(self):
        # Inspect raw source so we catch references in any context — comments,
        # strings, identifiers. The TCP variant has no RTX, no PLI, no
        # transport_mode toggle.
        for p in PY_FILES:
            if not p.exists():
                continue
            text = p.read_text()
            for forbidden in _FORBIDDEN_WIRE_STRINGS:
                assert forbidden not in text, (
                    f"{p} mentions forbidden UDP-variant token {forbidden!r}"
                )

    def test_topic_prefix_is_stream_tcp_only(self):
        # Any `nev/stream/` literal that is NOT `nev/stream_tcp/` is a
        # cross-prefix leak. Regex requires the `_tcp/` suffix.
        bad_prefix = re.compile(r"nev/stream/(?!tcp/)")
        for p in PY_FILES:
            if not p.exists():
                continue
            text = p.read_text()
            m = bad_prefix.search(text)
            assert m is None, (
                f"{p} contains forbidden prefix 'nev/stream/' at offset "
                f"{m.start() if m else -1} — TCP variant must use nev/stream_tcp/"
            )

    def test_no_udp_listen_port(self):
        # The TCP variant must not declare a UDP zenoh listen endpoint.
        for p in PY_FILES:
            if not p.exists():
                continue
            text = p.read_text()
            assert "udp/0.0.0.0" not in text, (
                f"{p} declares a UDP listen endpoint — TCP variant is TCP-only"
            )

    def test_no_stream_server_imports(self):
        # Catch `from stream_server...` and `import stream_server...` even if
        # the identifier surface check is bypassed by string concatenation.
        for p in PY_FILES:
            if not p.exists():
                continue
            text = p.read_text()
            assert "from stream_server" not in text, (
                f"{p} imports from the UDP stream_server package"
            )
            assert "import stream_server" not in text, (
                f"{p} imports the UDP stream_server package"
            )
