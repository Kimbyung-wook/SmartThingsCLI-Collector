# 운영

- 설치: [README](../README.md)
- 구조: [ARCHITECTURE](ARCHITECTURE.md)
- 명령 실행 위치: 모두 RPi의 SSH 창

## 일상 점검

- 먼저 볼 곳: 대시보드의 상태 탭과 상단 경고
- 경고 항목: 수집 중단, 재인증 필요, 집계 지연, 디스크 부족, 배터리 부족, 전압 부족, 스로틀링, 과열
- 경고 조건: [ARCHITECTURE](ARCHITECTURE.md#웹-web)

### 서비스

```bash
systemctl status stcollector stcollector-web stcollector-sysmon --no-pager
```

```bash
systemctl list-timers stcollector-rollup.timer --no-pager
```

- **수집 서비스가 active여도 수집이 멈췄을 수 있음**
- 로그인이 만료돼도 프로세스는 끝나지 않고 5분마다 재시도하므로 아래 수집 상태까지 확인

### 로그

```bash
journalctl -u stcollector -n 50 --no-pager
```

```bash
journalctl -u stcollector-web -n 20 --no-pager
```

```bash
journalctl -u stcollector-rollup -n 20 --no-pager
```

```bash
journalctl -u stcollector-sysmon -n 20 --no-pager
```

### 수집 상태

```bash
sudo -u stcollector sqlite3 /var/lib/stcollector/sensors.db "select key, value from meta where key in ('mode','auth_state','last_ok_ms','last_error','rollup_finished_ms')"
```

- `auth_state` 정상값: `ok`
- 시각 값: UTC epoch 밀리초

### 전압

```bash
vcgencmd get_throttled
```

| 값 | 뜻 |
|---|---|
| `throttled=0x0` | 부팅 후 전압 부족과 스로틀링 기록 없음 |
| 비트 0 (`0x1`) | 지금 전압 부족 |
| 비트 2 (`0x4`) | 지금 스로틀링 |
| 비트 16 (`0x10000`) | 부팅 후 전압 부족이 있었음 |
| 비트 18 (`0x40000`) | 부팅 후 스로틀링이 있었음 |

- 0이 아닐 때: [POWER_TEST](POWER_TEST.md) 참고
- 운영 설정: QC5V + 15cm 케이블, BT·Wi-Fi 끔, 900MHz, 1코어
- 시험 범위: DZ5V3A, QC5V(25cm 케이블), QC5V(15cm 케이블)로 각각 약 12분. 장시간 안정성은 이 값으로 계속 확인
- 지난 기록: 상태 탭의 이벤트 로그에 전압 부족 시작 시각과 직전 60초의 프로세스 표시
- `nproc`이 `1`: 1코어 설정 적용 상태

### 디스크

```bash
df -h /
```

```bash
sudo du -sh /var/lib/stcollector /var/lib/stcollector/cli/.local/state
```

- 뒤쪽 경로: CLI 자체 로그 위치
- 회전 여부와 쓰기량: RPi 미확인. 처음 며칠은 크기 증가 속도 확인

### 집계 수동 실행

| 목적 | 명령 |
|---|---|
| 끝난 시간까지 집계 | `stcollector rollup` |
| 타이머와 같은 샌드박스, 같은 메모리 제한으로 실행 | `sudo systemctl start stcollector-rollup.service` |
| 원본 전체를 다시 계산 | `stcollector rollup --full` |

- `--full` 사용 시점: 처음 한 번이나 집계 규칙을 바꾼 뒤에만
- `--full` 부담: 원본이 쌓일수록 오래 걸리고 microSD 쓰기 증가

### 명령 목록

| 명령 | 용도 |
|---|---|
| `stcollector check` | 기기 목록과 이벤트 이력 점검 |
| `stcollector run` | 수집 루프. systemd 서비스용 |
| `stcollector rollup` | 집계 |
| `stcollector web` | 대시보드 서버. systemd 서비스용 |
| `stcollector sysmon` | RPi 상태 모니터. systemd 서비스용 |
| `stcollector intervals` | 센서별 보고 간격 리포트 |
| `stcollector hash-password` | 대시보드 비밀번호 해시 생성 |
| `stcollector auth` | OAuth 방식 승인. CLI 방식에서는 안내문만 출력 |

### 설정 바꾸기

```bash
sudo nano /etc/stcollector/config.toml
```

- 수집, 웹, 모니터: 시작할 때 설정을 읽으므로 재시작 필요
- 집계: 실행할 때마다 설정을 읽음

```bash
sudo systemctl restart stcollector stcollector-web stcollector-sysmon
```

- SmartThings 앱에서 RPi 상태 보기: `[web] hub_status_allow`에 허브 IP를 넣고 웹 재시작. 드라이버 설치는 [edge-driver/README.md](../edge-driver/README.md)
- `max_hold_s` 조정: `stcollector intervals --days 7`로 센서별 보고 간격을 측정한 뒤 `[rollup.max_hold_s]` 변경
- 과거 구간에도 적용: `stcollector rollup --full`
- 코드 갱신: [README: 코드 갱신](../README.md#1-코드-갱신) 절차. 설정, DB, 자격증명은 유지

## 장애 대응

- CLI 재로그인 절차: [README: CLI 로그인과 재로그인](../README.md#cli-로그인과-재로그인)

### 수집

| 증상 | 조치 |
|---|---|
| 경고나 로그에 `재인증 필요`, `auth_state = reauth_required` | CLI 재로그인 절차 진행. 경고 문구와 로그는 `stcollector auth`를 안내하지만 CLI 방식에서 이 명령은 안내문만 출력. OAuth 방식이면 `stcollector auth` 실행 |
| `SmartThings CLI 로그인 정보가 없습니다` | 자격증명 파일 없음. CLI 로그인 절차나 `deploy/migrate-cli-credentials.sh` 실행 |
| CLI를 실행하지 못함 | `sudo ./deploy/install.sh` 재실행, 설정의 `cli_path`가 `/opt/stcollector-cli/smartthings`인지 확인. 이 파일은 `stcollector` 계정으로만 실행 가능 |
| `stcollector check`는 되는데 서비스에서만 CLI가 실패 | 샌드박스 문제. check는 systemd 밖에서 동작. 현재 배포에서는 발생하지 않음. 먼저 `RestrictAddressFamilies`에 `AF_NETLINK`가 없는 점을 의심. 아래 절 참고 |
| CLI 호출 시간 초과 | 네트워크 확인, `cli_timeout_s`(기본 120초) 증가. 900MHz 1코어에서 1회 호출은 약 6초 |
| `위치 이력 N페이지 한도에 도달` | 오래 멈춘 뒤 밀린 이벤트가 많은 경우. 나머지는 다음 주기에 이어서 수신. 더 빨리 받으려면 `history_max_pages` 증가 |
| 이벤트 이력 API 403/404 | `mode = "auto"`면 상태 조회 방식으로 자동 전환. CLI 방식의 상태 조회는 `history_interval_s`보다 자주 읽지 않음 |
| `위치가 N개입니다` | `stcollector check` 출력에 나온 ID를 설정의 `location_id`에 입력. 이 오류가 나면 수집 서비스는 30초마다 재시작 |
| `다른 수집기가 이미 실행 중입니다` | 수집기를 직접 실행했는데 서비스가 돌고 있거나, 수동 집계가 타이머와 겹친 경우. 집계에서도 같은 문구 출력. 잠시 뒤 재실행 |
| `stcollector check`의 종료 코드 | 2는 설정 오류, 3은 인증 필요, 4는 API 오류 |

### 샌드박스 안에서 CLI가 실패할 때

- 현재 배포: 샌드박스 안 CLI 정상 동작 확인. 아래는 CLI 교체 뒤 다시 실패할 때의 절차
- 아래 재정의 절차: RPi 미확인
- 원인 후보: `stcollector.service`의 `RestrictAddressFamilies`에 `AF_NETLINK` 없음. CLI가 네트워크 인터페이스를 조회하면 실패 가능

1. 시험용 재정의 파일 생성

   ```bash
   sudo systemctl edit stcollector
   ```

2. 다음 두 줄을 넣고 저장한 뒤 `sudo systemctl restart stcollector` 실행

   ```ini
   [Service]
   RestrictAddressFamilies=AF_NETLINK
   ```

3. 해결되면 같은 내용을 `deploy/systemd/stcollector.service`에 반영
4. 그래도 실패하면 재정의를 지우고 로그 확인

- 다른 서비스의 보안 설정 완화 금지

### 집계

| 증상 | 조치 |
|---|---|
| 차트가 비어 있음 | `systemctl list-timers stcollector-rollup.timer`로 타이머 활성 여부 확인. 수집 시작 후 첫 정시 2분 뒤부터 표시 |
| `집계 작업이 2시간 넘게 실행되지 않았습니다` | `journalctl -u stcollector-rollup -n 20 --no-pager` 확인. 메모리 제한(`MemoryMax=256M`)에 걸리면 수동으로 `stcollector rollup`을 1회 실행해 밀린 구간 처리 |

### 대시보드

| 증상 | 조치 |
|---|---|
| 웹서버가 시작되지 않음 | `journalctl -u stcollector-web -n 20 --no-pager` 확인. `비밀번호가 설정되지 않아`가 보이면 [대시보드 비밀번호](../README.md#3-대시보드-비밀번호) 단계 진행. 설정 오류는 자동 재시작되지 않으므로 수정 후 `sudo systemctl restart stcollector-web` 실행 |
| 접속이 안 됨 | `sudo ufw status`로 8080 허용 확인, 접속 기기가 같은 공유기에 연결돼 있는지 확인 |
| `허용되지 않은 네트워크입니다` | 접속 기기의 대역이 `allowed_networks`에 있어야 함 |
| 공유기 대역이 `192.168.0.x`에서 바뀜 | `ufw` 규칙, `config.toml`의 `allowed_networks`, `stcollector-web.service`의 `IPAddressAllow`를 함께 변경 |
| SmartThings 앱의 `RPi 상태`가 열림 | 앱의 `SmartThings 수집` 값 확인. `RPi 응답 없음`이면 RPi·웹 서비스·허브 IP 허용 확인, 그 밖이면 대시보드 상태 탭의 경고대로 조치. 판정 기준: [ARCHITECTURE: 허브 상태 경로](ARCHITECTURE.md#허브-상태-경로-hubstatus) |
| 허브 드라이버 로그에 `HTTP 404` | `config.toml`의 `[web] hub_status_allow`에 허브 IP가 있는지 확인 후 `sudo systemctl restart stcollector-web`. 허브 IP가 바뀌었으면 공유기 DHCP 예약부터 확인 |
| `실패가 많아 5분간 로그인이 막혔습니다` | 5분 뒤 재시도. 비밀번호를 잊었으면 `stcollector hash-password`로 해시를 새로 넣고 웹 재시작 |

### 하드웨어와 저장장치

| 증상 | 조치 |
|---|---|
| 전압 부족 경고 | `vcgencmd get_throttled` 확인, [POWER_TEST](POWER_TEST.md) 참고 |
| 디스크 부족 | 위 [디스크](#디스크) 명령으로 원인 확인. `sudo journalctl --vacuum-size=64M`로 로그 축소 또는 `retention_days` 축소 |
| DB 손상, microSD 고장, 보드 고장 | [MIGRATION](MIGRATION.md) 참고 |

### 자격증명 유출이 의심될 때

| 방식 | 조치 |
|---|---|
| CLI | `controller:stCli` 권한이라 **기기 제어까지 가능.** `sudo systemctl stop stcollector`로 수집 중지 후 SmartThings 계정 비밀번호 변경. `smartthings logout`이나 비밀번호 변경이 서버 쪽 토큰을 폐기하는지는 미확인이므로 이후 SmartThings 앱에서 기기 동작 이력 점검 |
| OAuth | `smartthings smartapps:delete`로 앱 삭제. 읽기 권한만 있었으므로 기기 제어는 불가능 |

## RPi 미확인 항목

- 기준: 2026-11-10 01:15 KST 배포 후
- 배포로 확인된 항목
  - `install.sh`, `migrate-cli-credentials.sh` 실제 실행
  - 서비스 샌드박스 안 CLI 동작 (기기 25개, 수집 방식 `history`)
  - PC에서 `http://<RPi IP>:8080` 응답

| 항목 | 확인 방법 |
|---|---|
| CLI 액세스 토큰 자동 갱신(만료 2026-11-11 00:06 KST), 리프레시 토큰 유효기간 | [README 확인 절차](../README.md#확인되지-않은-사항-cli-토큰-자동-갱신) |
| 만료 시 재인증 출력 | 만료 뒤 `auth_state`와 `journalctl -u stcollector` |
| 스마트폰 로그인 | 같은 공유기에 연결된 스마트폰에서 `http://<RPi IP>:8080` 접속 |
| `smartthings logout` 동작 | 재로그인 때 확인 |
| CLI 자체 로그의 크기와 microSD 쓰기량 | 위 [디스크](#디스크) 명령 |
| 실제 데이터로 본 대시보드와 첫 집계, RPi 응답 속도 | M2~M4 완료 기준 ([PLAN §11](../PLAN.md#11-단계별-진행)) |
| 수집 부하까지 포함한 장시간 전압 상태 | `vcgencmd get_throttled` |
| [MIGRATION](MIGRATION.md)의 복구 절차 | 복구 또는 M5 복원 시험 때 확인 |
