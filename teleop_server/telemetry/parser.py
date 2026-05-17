import time
from typing import Dict, Tuple, Optional


def extract_timestamp_delay(data) -> Tuple[Optional[float], Optional[float]]:
    if not isinstance(data, dict):
        return None, None
    ts = data.get("ts", None)
    # Use 'is not None' rather than truthiness so ts == 0 (a valid timestamp
    # in some clocks/tests) is not silently dropped.
    delay_ms = (time.time() - ts) * 1000.0 if ts is not None else None
    return ts, delay_ms
