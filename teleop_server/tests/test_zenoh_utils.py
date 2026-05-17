import tempfile
from pathlib import Path

from teleop_server.zenoh_utils.session_setup import sync_zenohd_config


class TestSyncZenohdConfig:
    def test_generates_config_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sync_zenohd_config(7447, Path(tmpdir))
            content = (Path(tmpdir) / "zenohd.json5").read_text()
            assert '"tcp/0.0.0.0:7447"' in content
            assert "udp/" not in content
            assert "enabled: false" in content

    def test_alternate_port(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sync_zenohd_config(9999, Path(tmpdir))
            content = (Path(tmpdir) / "zenohd.json5").read_text()
            assert '"tcp/0.0.0.0:9999"' in content
