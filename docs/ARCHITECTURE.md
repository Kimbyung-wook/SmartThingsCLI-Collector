# 구조

지금 코드가 **어떻게 동작하는지**를 설명합니다. **왜 이렇게 정했는지**와 앞으로의 단계는 [PLAN](../PLAN.md)이 기준입니다.
이 문서는 코드를 읽어 썼고, 아직 RPi에서 시험하지 못한 내용은 "RPi 미확인"으로 표시했습니다.

## 전체 흐름

```text
SmartThings 서버
   ▲ HTTPS (RPi에서 나가는 연결만)
   │
SmartThings CLI 실행 파일          backend = "cli" (현재 기준)
   ▲ 읽기 명령 + --json
   │
cli_client.py  ─┐
client.py      ─┴▶ collector.py ──▶ SQLite ◀── rollup.py (매시 2분)
(backend = "oauth")   stcollector      ▲        stcollector-rollup.timer
                      .service         │ 읽기 전용
                                  web/server.py, api.py ──▶ 집 안 브라우저
                                  stcollector-web.service
```

세 프로세스는 서로 호출하지 않고 **DB로만 연결**됩니다. 하나가 멈춰도 나머지는 계속 돕니다.

## 수집

`__main__.py`의 `_build()`가 설정의 `backend`로 클라이언트를 고릅니다. 설정 파일에서 이 줄을 지우면 코드 기본값 `"oauth"`로 동작합니다.

### CLI 클라이언트 (`cli_client.py`)

- `ALLOWED_COMMANDS`에 있는 읽기 명령만 실행합니다: `locations`, `locations:rooms`, `locations:history`, `devices`, `devices:status`, `devices:history`. 여기에 제어 명령을 넣으면 안 됩니다.
- 셸을 거치지 않고 인자 배열로 실행하며, 위치 인자와 옵션 값은 정규식으로 검사합니다.
- 환경 변수는 `HOME`(`cli_home`), `PATH`, `LANG`, `NO_COLOR`만 넘깁니다. `PATH`에 로그인용 `login-bin`이 없어 수집 중에는 `xdg-open` 대체 스크립트를 쓰지 않습니다.
- 로그인 정보 파일이 없으면 CLI를 실행하지 않고 `ReauthRequired`를 냅니다. 실행하면 브라우저 로그인을 기다리다 시간 초과되기 때문입니다.
- 출력에서 로그인 만료로 보이는 문구를 찾으면 `ReauthRequired`로 바꿉니다. 오류 문구는 `sanitize()`로 토큰처럼 보이는 값을 가린 뒤 로그에 남깁니다.
- HTTP 429나 5xx로 보이는 실패는 30초 뒤 1회 재시도합니다.
- 이력은 최신순으로 온다고 가정하고 `--before`로 페이지를 넘깁니다. 최신순이 아니면 `CliError`로 멈춥니다. 실제 출력과의 대조는 RPi 미확인입니다.

### OAuth 클라이언트 (`client.py`, `auth.py`, `transport.py`)

선택지로 남겨 둔 HTTP 방식입니다. `transport.py`가 HTTPS만 허용하고 리다이렉트를 막으며, `client.py`는 다른 호스트로 토큰을 보내지 않습니다. 토큰은 `token.json`(600)에 저장합니다.

### 수집 루프 (`collector.py`)

| 단계 | 동작 |
|---|---|
| 기기 목록 | 시작할 때와 `device_refresh_s`(기본 하루)마다 `devices` 테이블을 갱신합니다. 수집 대상 capability가 없는 기기는 제외합니다. |
| 방식 결정 | `mode = "auto"`면 이력 조회를 시험합니다. HTTP 400, 403, 404면 `status`, 되면 `history`로 정하고 `meta.mode`에 적습니다. |
| 이력 방식 (CLI) | `history_interval_s`(기본 300초)마다 `locations:history` 위치 단위로 읽습니다. |
| 이력 방식 (OAuth) | 기기마다 이력을 읽습니다. |
| 상태 방식 (CLI) | `devices --status` 1회로 전 기기를 읽고, `history_interval_s`보다 자주 읽지 않습니다. |
| 상태 방식 (OAuth) | 기기별로 `status_interval_s` 주기에 맞춰 읽습니다. |
| 저장 | `INSERT OR IGNORE`로 넣어 `readings` 기본 키가 같으면 버립니다. |

CLI 이력의 진행 위치와 빈틈 메우기:

- 시작점은 `meta`의 `history_cursor:<위치>` 하나입니다. 처음이면 `backfill_days`(기본 7일) 전부터 읽습니다.
- 매번 10분을 겹쳐 읽어 늦게 도착한 이벤트를 받습니다. 중복은 DB가 버립니다.
- 끝까지 읽은 뒤에만 커서를 옮기므로 중간에 실패하면 다음 주기에 다시 읽습니다.
- 평소에도 `history_backfill_max_pages`(기본 100페이지) 한도를 씁니다. 정전 뒤 밀린 이벤트를 한 번에 받기 위해서입니다.
- 새로 추가된 기기만 `devices:history`로 따로 채웁니다.

오류 처리:

| 오류 | 동작 |
|---|---|
| `ReauthRequired` | `meta.auth_state = reauth_required`를 쓰고 5분 뒤 다시 시도합니다. **프로세스는 끝나지 않아 서비스는 active로 보입니다.** |
| `ConfigError` | 프로세스가 종료 코드 2로 끝나고, systemd가 30초마다 재시작합니다. |
| 그 밖의 오류 | `meta.last_error`에 기록하고 30초부터 최대 900초까지 대기를 늘려 재시도합니다. |

성공할 때마다 `meta.last_ok_ms`와 `auth_state = ok`를 씁니다.

## 집계 (`rollup.py`)

규칙의 이유는 [PLAN §7](../PLAN.md#7-저장--집계)에 있습니다.

1. `readings` 삽입 트리거가 바뀐 시간대를 `rollup_dirty`에 표시합니다.
2. 집계가 **끝난 시간**의 표시를 `rollup_claimed`로 옮기고 처리합니다. 처리 중 도착한 값은 `rollup_dirty`에 새로 쌓입니다.
3. 항목별로 영향받는 시간 범위를 계산합니다. 이전 값이 다음 시간까지 유지될 수 있어 `max_hold_s`만큼 뒤쪽 범위까지 넓힙니다.
4. `rollup_hour`, `lux_hist_hour`, `occupancy_hour`, `lux_occ_hour`를 그 범위만 지우고 다시 씁니다.
5. `rollup_day`, `rollup_week`는 시간 집계에서 파생합니다. 경계는 KST이고 주는 월요일에 시작합니다. 값이 그대로면 쓰지 않아 microSD 쓰기를 줄입니다.
6. 하루 한 번 보관 기간이 지난 `readings`를 지웁니다(`retention_days`, 기본 365일). 항목별 최신 보고 1건은 남기고, 집계 테이블은 지우지 않습니다.

처음 실행이거나 `--full`이면 남아 있는 원본 전체를 다시 계산합니다. 같은 구간을 다시 계산해도 결과는 같습니다.

## 웹 (`web/`)

- `server.py`: `ThreadingHTTPServer` 기반입니다. 정적 파일은 시작할 때 메모리에 올리며, 요청 경로는 그 표에서만 찾습니다. 시작 때 스키마를 적용한 뒤로는 요청마다 읽기 전용(`mode=ro`) DB 연결을 씁니다.
- `api.py`: JSON API입니다. 차트는 집계 테이블에서만 읽고, `readings`는 최신값과 아직 집계되지 않은 오늘 구간의 최대·최소에만 씁니다.
- `session.py`: scrypt 비밀번호 검증과 서명된 세션 쿠키입니다. 세션 키는 비밀번호 해시에서 유도해 재시작해도 유지되고, 비밀번호를 바꾸면 기존 세션이 모두 무효가 됩니다.
- `static/`: 화면 코드와 자체 SVG 차트(`charts.js`)입니다. 외부 자원을 불러오지 않습니다.

| API | 캐시 기준 |
|---|---|
| `/api/series`, `/api/lux-dist`, `/api/occupancy`, `/api/plugs` | 집계 완료 시각과 현재 시 |
| `/api/summary`, `/api/status`, `/api/meta` | 15초 |

화면 상단 경고는 `api.alerts()`가 만듭니다.

| 경고 | 조건 |
|---|---|
| 재인증 필요 | `auth_state = reauth_required` |
| 수집 중단 | 마지막 성공 후 30분 초과 |
| 집계 지연 | 마지막 집계 후 2시간 10분 초과 |
| 디스크 부족 | 여유 공간 1GB 미만 |
| 배터리 부족 | 20% 미만 |

## DB 테이블

정의의 원본은 `stcollector/schema.sql`입니다. 시각은 모두 UTC epoch 밀리초로 저장하고 화면에서만 KST로 바꿉니다. 저널 방식은 WAL입니다.

| 테이블 | 역할 | 쓰는 쪽 |
|---|---|---|
| `meta` | 키-값 상태. `mode`, `auth_state`, `last_ok_ms`, `last_error`, `last_error_ms`, `location_id`, `history_cursor:<위치>`, `rollup_closed_until_ms`, `rollup_finished_ms`, `rollup_retention_ms`, `schema_version` | 수집, 집계 |
| `devices` | 기기, 방, 위치, capability 목록 | 수집 |
| `readings` | 원본 측정값. 기본 키는 (기기, 컴포넌트, capability, 속성, 보고 시각) | 수집 (삭제는 집계) |
| `cursors` | 기기별 마지막 수신 시각 | 수집 |
| `rollup_dirty` | 새 측정값이 들어온 시간대. 트리거가 채움 | 트리거, 집계 |
| `rollup_claimed` | 집계가 처리 중인 시간대 | 집계 |
| `rollup_series` | 집계가 아는 측정 항목 목록. `readings`를 훑지 않기 위해 둠 | 집계 |
| `rollup_hour`, `rollup_day`, `rollup_week` | 시간 가중 평균, 최소, 최대, 마지막 값, 건수, coverage | 집계 |
| `lux_hist_hour` | 조도 구간별 체류 시간(초) | 집계 |
| `occupancy_hour` | 재실 시간과 재실 여부를 알고 있던 시간 | 집계 |
| `lux_occ_hour` | 재실 중 조도 분포 | 집계 |

웹은 시작 때 스키마 적용을 제외하면 읽기만 합니다.

## 서비스와 타이머

| 유닛 | 형태 | 실행 | 주요 제한 |
|---|---|---|---|
| `stcollector.service` | 상시, 실패 시 30초 뒤 재시작 | `run` | 나가는 네트워크 허용. CLI의 V8 JIT 때문에 `MemoryDenyWriteExecute=false` |
| `stcollector-web.service` | 상시, 실패 시 재시작. 종료 코드 2는 재시작 안 함 | `web` | `MemoryMax=128M`, 집 안 대역과 루프백만 통신 |
| `stcollector-rollup.service` | 1회 실행 (`oneshot`) | `rollup` | `MemoryMax=256M`, `Nice=10`, IO idle, `PrivateNetwork=true` |
| `stcollector-rollup.timer` | 매시 2분, 꺼져 있던 동안 놓치면 켜진 뒤 1회 | 위 서비스를 시작 | |

- `install.sh`는 유닛을 복사하고 `daemon-reload`만 합니다. **활성화(`enable`)는 사람이 합니다.** 이미 실행 중인 수집과 웹만 재시작합니다.
- 유닛 사이에 `After=`나 `Requires=` 의존 관계는 없습니다.
- 수집기와 집계는 각각 잠금 파일(`sensors.db.lock`, `sensors.db.rollup.lock`)로 중복 실행을 막습니다.
- 수집, 웹, 집계 모두 `stcollector` 계정으로 돌고 `ProtectSystem=strict`, `ProtectHome=true`, `NoNewPrivileges=true` 등을 겁니다. 쓸 수 있는 곳은 `StateDirectory`인 `/var/lib/stcollector`뿐입니다.

## 파일 위치

| 경로 | 내용 | 소유, 권한 |
|---|---|---|
| `/opt/stcollector` | 코드 | root, 읽기만 |
| `/opt/stcollector-cli/smartthings` | CLI 실행 파일 | root:stcollector, 750 |
| `/opt/stcollector-cli/login-bin/xdg-open` | 헤드리스 로그인용 스크립트 | root:stcollector, 750 |
| `/usr/local/bin/stcollector` | 명령행 래퍼. `stcollector` 계정으로 전환해 실행 | root, 755 |
| `/etc/stcollector/config.toml` | 설정, 대시보드 비밀번호 해시 | root:stcollector, 640 |
| `/var/lib/stcollector/sensors.db` (`-wal`, `-shm`) | 데이터 | stcollector, 폴더 700 |
| `/var/lib/stcollector/cli/.local/share/@smartthings/cli/credentials.json` | CLI 자격증명 | stcollector, 파일 600, 폴더 700 |
| `/var/lib/stcollector/cli/.local/state/@smartthings/cli/smartthings.log` | CLI 자체 로그 | stcollector |
| `/var/lib/stcollector/token.json` | OAuth 토큰 (OAuth 방식일 때만) | stcollector, 600 |

## 보안 경계

| 경계 | 막는 수단 | 한계 |
|---|---|---|
| 인터넷 → RPi | 포트포워딩 없음, 웹훅 없음, `ufw` 기본 거부 | 운영 설정에 의존합니다. 코드가 강제하지 않습니다. |
| 집 안 네트워크 → 대시보드 | `ufw`, 앱의 `allowed_networks`, 웹 서비스의 `IPAddressAllow`, 비밀번호 | HTTP라서 비밀번호와 쿠키가 집 안 네트워크에서 암호화되지 않습니다. 쿠키에 `Secure`를 붙일 수 없습니다. |
| 대시보드 로그인 | scrypt, IP별 5회 실패 시 5분 차단, `HttpOnly`·`SameSite=Lax` 쿠키, POST `Origin` 검사, 자체 출처만 허용하는 CSP | 로그인 전에는 로그인 화면, CSS, manifest, 아이콘만 받을 수 있습니다. |
| 수집기 → 기기 제어 | `ALLOWED_COMMANDS` 허용 목록 | 수집기 안의 제한입니다. 자격증명 파일이 유출되면 막지 못합니다. |
| CLI 자격증명 파일 | `stcollector` 계정 전용 (파일 600, 폴더 700) | 권한은 `controller:stCli`로 제어까지 가능합니다. 웹과 집계도 같은 계정으로 돌고, sudo 사용자는 읽을 수 있습니다. |
| 프로세스 격리 | systemd 보안 옵션 | 수집 서비스는 나가는 IP를 제한하지 않습니다. 샌드박스 아래 CLI 동작은 RPi 미확인입니다. |
| 로그 | CLI 오류 문구 가림, 웹 접속 로그 없음 | CLI 자체 로그의 내용은 확인하지 못했습니다. |

DB에는 기기 이름, 방 이름, 위치 ID가 들어 있으므로 DB 사본도 외부에 올리지 않습니다.
