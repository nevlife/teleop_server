from dataclasses import dataclass, field
from typing import List


@dataclass
class ZenohConfig:
    # teleop_server runs its own router. TCP-only — UDP listener removed
    # since no client/bot ever connected over UDP (all locators are tcp/…).
    tcp_port: int = 7447


@dataclass
class ServerConfig:
    telemetry_rate: float = 2.0
    station_timeout: float = 3.0


@dataclass
class TelemetryConfig:
    control_keys: List[str] = field(default_factory=lambda: ["mux", "twist", "network", "estop"])
    disconnect_timeout: float = 3.0
    bw_calc_interval: float = 1.0
    seq_max: int = 65536

    @property
    def control_keys_set(self) -> frozenset:
        return frozenset(self.control_keys)


@dataclass
class MetricsConfig:
    """Configuration for the embedded /healthz + /metrics aiohttp server.

    Binds to localhost by default — exposing without auth (set
    ``bind: "0.0.0.0"``) is a deliberate operator decision documented in
    the README's Monitoring section.
    """
    enabled: bool = True
    bind: str = "127.0.0.1"
    port: int = 8080


@dataclass
class AppConfig:
    zenoh: ZenohConfig = field(default_factory=ZenohConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)

    def validate(self) -> None:
        if not (0 < self.zenoh.tcp_port < 65536):
            raise ValueError(f"zenoh_tcp_port must be in (0, 65535], got {self.zenoh.tcp_port}")

        if self.server.telemetry_rate <= 0:
            raise ValueError(f"telemetry_rate must be positive, got {self.server.telemetry_rate}")

        if self.server.station_timeout <= 0:
            raise ValueError(
                f"station_timeout must be positive, got {self.server.station_timeout}"
            )

        if self.telemetry.disconnect_timeout <= 0:
            raise ValueError(
                f"disconnect_timeout must be positive, got {self.telemetry.disconnect_timeout}"
            )

        if self.telemetry.bw_calc_interval <= 0:
            raise ValueError(
                f"bw_calc_interval must be positive, got {self.telemetry.bw_calc_interval}"
            )

        if self.telemetry.seq_max <= 0:
            raise ValueError(f"seq_max must be positive, got {self.telemetry.seq_max}")

        if not (0 < self.metrics.port < 65536):
            raise ValueError(
                f"metrics.port must be in (0, 65535], got {self.metrics.port}"
            )
        if not isinstance(self.metrics.bind, str) or not self.metrics.bind:
            raise ValueError(
                f"metrics.bind must be a non-empty string, got {self.metrics.bind!r}"
            )
