import time
from unittest.mock import patch

import pytest

from teleop_server.telemetry.parser import extract_timestamp_delay


class TestExtractTimestampDelay:
    def test_with_ts(self):
        now = time.time()
        data = {"ts": now - 0.1, "val": 42}
        ts, delay_ms = extract_timestamp_delay(data)
        assert ts == pytest.approx(now - 0.1, abs=0.01)
        assert delay_ms is not None
        assert delay_ms > 0

    def test_without_ts(self):
        data = {"val": 42}
        ts, delay_ms = extract_timestamp_delay(data)
        assert ts is None
        assert delay_ms is None
        assert data == {"val": 42}

    def test_delay_accuracy(self):
        fake_now = 1000.0
        data = {"ts": 999.9}
        with patch("telemetry.parser.time") as mock_time:
            mock_time.time.return_value = fake_now
            ts, delay_ms = extract_timestamp_delay(data)
        assert delay_ms == pytest.approx(100.0, abs=0.1)
