from __future__ import annotations

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class ServerConfig:
    bind: str = "0.0.0.0"
    port: int = 13437
    hello_timeout_s: float = 5.0
    max_message_bytes: int = 1024 * 1024
    turn_url: str = "turn:127.0.0.1:13438?transport=udp"
    turn_username: str = "teleop"
    turn_password: str = "teleop-dev"

    @classmethod
    def from_env(cls) -> "ServerConfig":
        cfg = cls(
            bind=os.getenv("TELEOP_BIND", cls.bind),
            port=int(os.getenv("TELEOP_PORT", str(cls.port))),
            hello_timeout_s=float(
                os.getenv("TELEOP_HELLO_TIMEOUT_S", str(cls.hello_timeout_s))
            ),
            max_message_bytes=int(
                os.getenv("TELEOP_MAX_MESSAGE_BYTES", str(cls.max_message_bytes))
            ),
            turn_url=os.getenv("TELEOP_TURN_URL", cls.turn_url),
            turn_username=os.getenv("TELEOP_TURN_USERNAME", cls.turn_username),
            turn_password=os.getenv("TELEOP_TURN_PASSWORD", cls.turn_password),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.bind:
            raise ValueError("bind must not be empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("port must be in 1..65535")
        if self.hello_timeout_s <= 0:
            raise ValueError("hello_timeout_s must be positive")
        if not 1024 <= self.max_message_bytes <= 4 * 1024 * 1024:
            raise ValueError("max_message_bytes must be in 1KiB..4MiB")
        if not self.turn_url.startswith(("turn:", "turns:")):
            raise ValueError("turn_url must start with turn: or turns:")
