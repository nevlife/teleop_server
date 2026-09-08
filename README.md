# teleop_server

Native teleoperation signaling server. It pairs exactly one rover and one
operator for each `robot_id` and forwards WebRTC SDP/ICE signaling messages.

The server does not use Zenoh, does not decode video, and does not transcode
media. Video, telemetry, and control data travel end-to-end through WebRTC;
coturn only relays encrypted packets when a direct connection is unavailable.

## Development

```bash
git clone https://github.com/nevlife/teleop_server.git
cd teleop_server
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest teleop_v2_server/tests
teleop-server
```

The compatibility alias `teleop-v2-server` starts the same process.

Default endpoints:

- `ws://0.0.0.0:13437/ws`: rover/client signaling
- `http://0.0.0.0:13437/healthz`: health and connected-peer state

See [V2.md](V2.md) for TURN ports, router forwarding, protocol messages, and
Docker deployment.
