# NEV Teleop Server (텔레메트리 전용)

NEV 원격 조종 시스템에서 **텔레메트리만** 다루는 server. 영상 스트림은 별도의 [`stream_server`](../stream_server/)가 처리한다. 두 server는 서로 다른 Zenoh 라우터(다른 포트)에서 동작하며 코드 의존성도 없다.

## 구조

```
main.py                      # 진입점 (asyncio 이벤트 루프 + run_send_loop)
config.yaml                  # 설정 파일 (텔레메트리 전용 키만)
state.py                     # SharedState (차량별 텔레메트리, 제어, 알림)
robot_bridge.py              # Bot ↔ teleop_server 텔레메트리 브릿지
station_bridge.py            # Client → teleop_server → Bot 명령 릴레이
config/                      # AppConfig 스키마 + 로더
telemetry/                   # 타임스탬프 파싱 + metrics / healthz
zenoh_utils/                 # Zenoh 세션 설정
```

## 와이어 컨트랙트

토픽 suffix, JSON envelope (`{"v": <SCHEMA_VERSION>, "ts": ..., ...payload}`),
`key_for(vid, suffix)` 빌더는 모두 형제 패키지 [`teleop_contracts`](../teleop_contracts/)
에서 import 한다. `main.py` / `conftest.py` 가 `sys.path` 에 추가해 주므로 별도
설치는 필요 없지만 같이 체크아웃되어 있어야 한다. 새 토픽을 추가하거나 페이로드
필드를 바꿀 때는 무조건 거기부터 수정한다 — 봇/클라이언트와 공유되는 단일 출처.

## 데이터 흐름

```
Bot    → nev/teleop/{vid}/{mux,twist,estop,cpu,mem,gpu,disk,net} → [teleop_server] → state aggregation
Bot    → nev/teleop/{vid}/bot_heartbeat → [teleop_server last_robot_recv]
Bot    → nev/teleop/{vid}/telemetry_pong → (router 통과) → Client
Client → nev/teleop/{vid}/teleop        → [teleop_server] → nev/teleop/{vid}/cmd          → Bot
Client → nev/teleop/{vid}/estop         → [teleop_server] → nev/teleop/{vid}/estop_cmd    → Bot
Client → nev/teleop/{vid}/cmd_mode      → [teleop_server] → nev/teleop/{vid}/cmd_mode_bot → Bot
Client → nev/teleop/{vid}/client_heartbeat     → [teleop_server station_connected]
Client → nev/teleop/{vid}/controller_heartbeat → [teleop_server joystick_connected]
Client → nev/teleop/{vid}/telemetry_ping → (router 통과) → Bot
[teleop_server] → nev/teleop/{vid}/telemetry → Client (집계 상태 push)
```

RTT (`telemetry_ping`/`telemetry_pong`) 는 server 가 측정하지 않고 zenoh 라우터로
client↔bot 사이를 통과만 시킨다. 단일 RTT = cli↔bot 전체.

> 클라이언트→서버는 `cmd_mode`, 서버→봇은 `cmd_mode_bot` 으로 **suffix 가 다르다**.
> 둘이 같으면 서버 자신의 publish 가 자기 subscriber 로 되돌아오는 피드백 루프가
> 생긴다. 두 zenoh subscriber (`RobotProtocol`, `StationBridge`) 도 `nev/teleop/**`
> 대신 처리할 suffix 패턴 (`nev/teleop/*/{suffix}`) 으로 좁혀 잡아서, 한 메시지가
> 두 핸들러에 동시에 dispatch 되거나 서버 publish 가 self-loop 로 돌아오는 일을
> 막는다.

## 실행

```bash
python3 main.py [--config config.yaml] [--zenoh-tcp-port 7447]
```

## 설정 (`config.yaml`)

| 파라미터 | 기본값 | 설명 |
|----------|--------|------|
| `zenoh_tcp_port` | `7447` | TCP 리슨 포트 (RELIABLE — 유일한 리스너) |
| `telemetry_rate` | `2.0` | 텔레메트리 push 주기 (Hz) |
| `station_timeout` | `3.0` | `client_heartbeat` 타임아웃 (초) |
| `disconnect_timeout` | `3.0` | 봇 데이터 미수신 → 끊김 (초) |
| `bw_calc_interval` | `1.0` | 텔레메트리 대역폭 계산 cycle (초) |
| `metrics.enabled` | `true` | embedded `/healthz` + `/metrics` aiohttp 서버 활성화 |
| `metrics.bind` | `127.0.0.1` | 위 endpoint 바인드 주소 (localhost-only by default) |
| `metrics.port` | `8080` | 위 endpoint TCP 포트 |

## 판단/계산 영역

- **RTT 측정**: 서버는 측정하지 않음. `telemetry_ping`/`telemetry_pong` 은 라우터로 통과만 시키며 단일 cli↔bot RTT 는 클라이언트가 측정.
- **텔레메트리 대역폭**: 텔레메트리 수신 바이트 → Mbps (1초 주기)
- **봇 연결 감지**: 봇 텔레메트리/`bot_heartbeat` 3초 미수신 → 끊김. `run_send_loop`
  안에서 edge-triggered state machine 으로 처리해서 transition 발생 시점에만
  연결/끊김 로그를 한 줄씩 찍는다 (매 tick 스팸 X).
- **클라이언트 연결 감지**: `client_heartbeat` 3초 미수신 → 끊김. `station_last_recv`
  쓰기는 zenoh 콜백 스레드가 아니라 event-loop 로 hop 한 뒤 수행한다 (race 방지).
- **조이스틱 연결**: `controller_heartbeat` 페이로드의 `connected` 필드.
- **Alerts 생성**: 상태 모순 감지 (E-Stop+이동, 제어 타임아웃, 스테이션 미연결 등)
- **상태 통합**: 전 차량 + 클라이언트 상태 → 단일 telemetry JSON
- **스케줄 miss 처리**: tick 이 push_interval 보다 오래 걸려서 `sleep_for <= 0`
  이 되면 1ms 로 spin 하지 않고 `last_push = now` 로 리셋한 뒤 한 interval 전체를
  sleep 한다. miss 횟수는 `teleop_send_loop_fell_behind_total` 로 카운트.

## 영상 관련 코드 부재

다음 기능은 모두 stream_server로 이전됨:

- 카메라 프레임 처리 / 헤더 확장 / dedupe LRU
- BitrateController (RTT/loss/SQP 기반 ABR)
- video_ctl / rtx_request / video_feedback / stream_heartbeat
- transport_mode (low_freeze / tcp_stale)
- video_stats / per-camera bitrate 분배

teleop_server에서 위 기능이 필요하면 stream_server를 같이 띄우고 클라이언트가 두 라우터(7447 + 7457)에 동시 접속한다.

## 테스트

```bash
make test           # 단위 테스트 (현재 103개)
make test-v         # 자세히
make compile        # py_compile 검사
```

`Makefile`이 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`을 자동으로 붙임. `pytest tests/` 직접 호출은 권장하지 않음.

`tests/test_no_stream_dep.py`는 teleop_server 코드 안에 stream_server import나 nev/stream/ prefix가 없는지 정적으로 검증한다. `tests/test_metrics.py` 는 `/healthz` / `/metrics` 엔드포인트와 카운터/히스토그램 라벨 cardinality 를 검증한다.

## 모니터링

teleop_server는 메인 asyncio 이벤트 루프 위에서 작은 aiohttp 서버를 띄워 두 개의 endpoint 를 노출한다. 기본 주소는 `http://127.0.0.1:8080`.

### `/healthz`

송신 루프(`run_send_loop`)가 최근에 한 번이라도 돌았는지 (`2 * push_interval` 이내) 를 체크.

```bash
curl -s http://localhost:8080/healthz | jq .
# 정상: 200 OK
# {"status":"ok","uptime_s":12.34,"last_send_loop_tick_age_s":0.43}
#
# 지연/스톨: 503 Service Unavailable
# {"status":"degraded","reason":"send_loop_stalled","last_tick_age_s":7.81}
```

### `/metrics`

Prometheus text exposition format. 스크레이핑 예시:

```bash
curl -s http://localhost:8080/metrics | head -20
```

노출되는 주요 지표:

| 지표 | 타입 | 설명 |
|------|------|------|
| `teleop_vehicles_connected` | Gauge | `age < disconnect_timeout` 인 차량 수 |
| `teleop_station_connected` | Gauge | 0 / 1 (스테이션 연결 여부) |
| `teleop_send_loop_tick_seconds` | Histogram | `run_send_loop` 한 iteration 당 wall time |
| `teleop_send_loop_fell_behind_total` | Counter | 송신 루프가 스케줄을 놓친 횟수 (`sleep_for <= 0`) |
| `teleop_bytes_in_total{topic=...}` | Counter | 토픽별 수신 바이트 |
| `teleop_bytes_out_total{topic=...}` | Counter | 토픽별 송신 바이트 |
| `teleop_parse_errors_total{topic=...}` | Counter | JSON / envelope decode 실패 횟수 |
| `teleop_telemetry_age_seconds{vid=...}` | Gauge | 차량별 마지막 봇 패킷 age (초) |
| `teleop_publish_errors_total{topic=...}` | Counter | `pub.put()` 예외 발생 횟수 |

`topic` label 은 고정된 enum (`mux/teleop/estop/cmd_mode/cpu/mem/gpu/disk/net/bot_heartbeat/...`) 으로만 들어가며 그 외 값은 모두 `"other"` 로 collapse 된다 (cardinality discipline). `vid` label 은 현재 배포(차량 2대)에서는 안전하지만, 향후 차량이 수십~수백 대로 늘어나면 메트릭 cardinality 제한이 필요할 수 있다.

### 비활성화

```yaml
# config.yaml
metrics:
  enabled: false
```

또는 `metrics.port` 충돌 시 다른 포트로 옮기는 것이 보통 더 낫다.

### 외부 노출 (주의)

기본은 `127.0.0.1` 바인드 — 동일 호스트의 Prometheus / 디버깅 도구에서만 접근 가능. `0.0.0.0` 으로 바꾸면 **인증 없이** 노출되므로 사설망/방화벽 뒤에서만 사용할 것. 메트릭에는 robot ID, 대역폭, 연결 상태 등이 그대로 들어 있다.

```yaml
metrics:
  enabled: true
  bind: "0.0.0.0"   # at your own risk: no auth, no TLS
  port: 8080
```

> **Note**: `prometheus_client` 가 누락된 환경에서도 `/healthz` 는 정상 동작하며 시작 시 경고 로그가 찍힌다. 이 경우 `/metrics` 는 503 을 반환한다. `aiohttp` 가 없으면 `metrics.enabled=true` 여도 endpoint 전체가 시작되지 않고 경고만 로그된다.

## 의존성

- [eclipse-zenoh](https://zenoh.io/)
- [PyYAML](https://pyyaml.org/)
- [aiohttp](https://docs.aiohttp.org/) — `/healthz` + `/metrics` endpoint
- [prometheus_client](https://github.com/prometheus/client_python) — optional, `/metrics` 비활성화 시 미설치 가능
- **stream_server에 의존하지 않음** — 별도 라우터, 별도 토픽 prefix(`nev/teleop/...`), 별도 코드.

## Security

> **WARNING — no auth, no TLS, no encryption.**
>
> The teleop_server opens a zenoh router on `tcp/0.0.0.0:{zenoh_tcp_port}`
> (default 7447) with **no authentication, no TLS, and no payload encryption**.
> The trust model is "private network only".
>
> Anyone who can reach the listen ports can:
> - Subscribe to all telemetry (`nev/teleop/{vid}/telemetry`) — full robot
>   state including positions, modes, system resources.
> - Publish control commands (`nev/teleop/{vid}/teleop`,
>   `nev/teleop/{vid}/cmd_mode`) and drive the robot.
> - Publish or clear e-stop (`nev/teleop/{vid}/estop`).
>
> Operators **must** bind these ports only to a trusted private interface
> (VLAN, VPN, WireGuard, Tailscale, etc.) and **never** expose them to the
> public internet. If wider exposure is required, terminate TLS / auth at an
> external reverse proxy and keep the zenoh listener bound to localhost or
> the private interface only.
>
> The `/healthz` and `/metrics` endpoints default to `metrics.bind=127.0.0.1`
> (localhost-only). Setting `metrics.bind=0.0.0.0` exposes robot IDs,
> bandwidth, and connection state with **no auth / no TLS** — same trust
> model as the zenoh ports. See the "외부 노출" subsection under 모니터링.
>
> The hardcoded `0.0.0.0` binds live in `zenoh_utils/session_setup.py` and
> `main.py`; change them together if you need a narrower bind.
