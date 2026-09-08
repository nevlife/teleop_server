# teleop_server

> The clean WebRTC/TURN implementation is in `teleop_v2_server/`; deployment
> notes are in `V2.md`. The Zenoh server below remains a legacy reference.

NEV 텔레오프 시스템의 서버측 (relay/state hub) 통합 클라이언트 운전자(client) ↔ 차량(rover) 사이에서 명령·텔레메트리·영상을 한 Zenoh 라우터에 묶어 중계

## v2 개발 환경

```bash
cd ~/teleop_server
git submodule update --init --recursive

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest teleop_v2_server/tests
```

## 실행

```bash
source .venv/bin/activate
teleop-v2-server
```

완성 후 배포용 Docker Compose 구성은 [`V2.md`](V2.md)에 정리되어 있다.
기존 Zenoh 서버를 실행할 때만 `pip install -e '.[legacy]'`로 추가 의존성을
설치하고 `teleop-server`를 사용한다.

옵션:
```bash
teleop-server --teleop-config teleop_server/config.yaml \
              --stream-config stream_server/config.yaml \
              --zenoh-tcp-port 7447
```

## 디렉토리

```
teleop_server/                  # repo root (= 단일 pyproject, 단일 entry)
├── pyproject.toml
├── server_main.py              # 통합 entry — teleop-server console script
├── teleop_server/              # 텔레메트리·제어 relay (python 패키지)
│   ├── __init__.py
│   ├── main.py                 # solo 실행용 + setup_relays / run_send_loop
│   ├── state.py, robot_bridge.py, station_bridge.py, ...
│   ├── config/, telemetry/, zenoh_utils/, tests/
│   └── teleop_contracts/       # git submodule
└── stream_server/              # 영상 relay (python 패키지)
    ├── __init__.py
    ├── main.py                 # solo 실행용 + setup_relays / run_send_loop
    ├── state.py, robot_bridge.py, station_bridge.py, ...
    └── config/, zenoh_utils/, tests/
```

## 와이어 컨트랙트

[`teleop_contracts`](https://github.com/nevlife/teleop_contracts) 를
`teleop_server/teleop_contracts/` 에 git submodule 로 pin
