# 운영

설치는 [README](../README.md), 구조는 [ARCHITECTURE](ARCHITECTURE.md)를 봅니다. 명령은 모두 RPi의 SSH 창에서 실행합니다.

## 일상 점검

먼저 대시보드의 **상태** 탭과 상단 경고를 봅니다. 수집 중단, 재인증 필요, 집계 지연, 디스크 부족, 배터리 부족을 표시합니다. 조건은 [ARCHITECTURE](ARCHITECTURE.md#웹-web)에 있습니다.

### 서비스

```bash
systemctl status stcollector stcollector-web --no-pager
```

```bash
systemctl list-timers stcollector-rollup.timer --no-pager
```

**수집 서비스가 active여도 수집이 멈췄을 수 있습니다.** 로그인이 만료되면 프로세스는 끝나지 않고 5분마다 다시 시도합니다. 아래 수집 상태까지 확인하세요.

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

### 수집 상태

```bash
sudo -u stcollector sqlite3 /var/lib/stcollector/sensors.db "select key, value from meta where key in ('mode','auth_state','last_ok_ms','last_error','rollup_finished_ms')"
```

`auth_state`가 `ok`여야 합니다. 시각 값은 UTC epoch 밀리초입니다.

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

0이 아니면 [POWER_TEST](POWER_TEST.md)를 봅니다. 최종 운영 설정(BT·Wi-Fi 끔, 900MHz, 1코어)은 DZ5V3A와 QC5V(15cm 케이블)로 각각 약 12분만 시험했으므로, 장시간 안정성은 이 값으로 계속 확인합니다. `nproc`이 `1`이면 1코어 설정이 적용된 것입니다.

### 디스크

```bash
df -h /
```

```bash
sudo du -sh /var/lib/stcollector /var/lib/stcollector/cli/.local/state
```

뒤쪽 경로는 CLI 자체 로그가 있는 곳입니다. 회전 여부와 쓰기량은 RPi 미확인이므로 처음 며칠은 크기가 늘어나는 속도를 봅니다.

### 집계 수동 실행

| 목적 | 명령 |
|---|---|
| 끝난 시간까지 집계 | `stcollector rollup` |
| 타이머와 같은 샌드박스, 같은 메모리 제한으로 실행 | `sudo systemctl start stcollector-rollup.service` |
| 원본 전체를 다시 계산 | `stcollector rollup --full` |

`--full`은 처음 한 번이나 집계 규칙을 바꾼 뒤에만 씁니다. 원본이 쌓일수록 오래 걸리고 microSD에 많이 씁니다.

### 설정 바꾸기

```bash
sudo nano /etc/stcollector/config.toml
```

수집과 웹은 시작할 때 설정을 읽으므로 재시작합니다. 집계는 실행할 때마다 읽습니다.

```bash
sudo systemctl restart stcollector stcollector-web
```

`stcollector intervals --days 7`로 센서별 보고 간격을 측정한 뒤 `[rollup.max_hold_s]`를 조정할 수 있습니다. 과거 구간에도 적용하려면 `stcollector rollup --full`을 실행합니다.

코드 갱신은 [README: 코드 갱신](../README.md#1-코드-갱신)을 따릅니다. 설정, DB, 자격증명은 유지됩니다.

## 장애 대응

CLI 재로그인 절차는 [README: CLI 로그인과 재로그인](../README.md#cli-로그인과-재로그인)에 있습니다.

### 수집

| 증상 | 조치 |
|---|---|
| 경고나 로그에 `재인증 필요`, `auth_state = reauth_required` | CLI 재로그인 절차를 진행합니다. 경고 문구와 로그는 `stcollector auth`를 안내하지만, CLI 방식에서 이 명령은 안내문만 출력합니다. OAuth 방식이면 `stcollector auth`를 실행합니다. |
| `SmartThings CLI 로그인 정보가 없습니다` | 자격증명 파일이 없습니다. CLI 로그인 절차나 `deploy/migrate-cli-credentials.sh`를 실행합니다. |
| CLI를 실행하지 못함 | `sudo ./deploy/install.sh`를 다시 실행하고, 설정의 `cli_path`가 `/opt/stcollector-cli/smartthings`인지 봅니다. 이 파일은 `stcollector` 계정으로만 실행됩니다. |
| `stcollector check`는 되는데 서비스에서만 CLI가 실패 | 샌드박스 문제입니다. check는 systemd 밖에서 돕니다. 먼저 `RestrictAddressFamilies`에 `AF_NETLINK`가 없는 점을 의심합니다. 아래 절을 봅니다. RPi 미확인. |
| CLI 호출 시간 초과 | 네트워크를 확인하고 `cli_timeout_s`(기본 120초)를 늘립니다. 900MHz 1코어에서 1회 호출은 약 6초 걸렸습니다. |
| `이력이 최신순이 아니어서 페이지를 이어 갈 수 없습니다` | CLI 출력 순서가 코드의 가정과 다릅니다. `stcollector check`의 `정렬:` 줄을 확인하고 코드를 고쳐야 합니다. |
| `위치 이력 N페이지 한도에 도달` | 오래 멈춘 뒤 밀린 이벤트가 많은 경우입니다. `history_backfill_max_pages`를 늘릴 수 있습니다. 그 사이 이벤트는 받지 못했을 수 있습니다. |
| 이벤트 이력 API 403/404 | `mode = "auto"`면 상태 조회 방식으로 자동 전환됩니다. CLI 방식의 상태 조회는 `history_interval_s`보다 자주 읽지 않습니다. |
| `위치가 N개입니다` | `stcollector check` 출력에 나온 ID를 설정의 `location_id`에 넣습니다. 이 오류가 나면 수집 서비스는 30초마다 재시작합니다. |
| `다른 수집기가 이미 실행 중입니다` | 수집기를 직접 실행했는데 서비스가 돌고 있거나, 수동 집계가 타이머와 겹친 경우입니다. 집계에서도 같은 문구가 나옵니다. 잠시 뒤 다시 실행합니다. |
| `stcollector check`의 종료 코드 | 2는 설정 오류, 3은 인증 필요, 4는 API 오류입니다. |

### 샌드박스 안에서 CLI가 실패할 때 (RPi 미확인)

`stcollector.service`의 `RestrictAddressFamilies`에는 `AF_NETLINK`가 없습니다. CLI가 네트워크 인터페이스를 조회한다면 실패할 수 있습니다. 시험용으로 재정의 파일을 만듭니다.

```bash
sudo systemctl edit stcollector
```

다음 두 줄을 넣고 저장한 뒤 `sudo systemctl restart stcollector`를 실행합니다.

```ini
[Service]
RestrictAddressFamilies=AF_NETLINK
```

해결되면 같은 내용을 `deploy/systemd/stcollector.service`에 반영합니다. 그래도 실패하면 재정의를 지우고 로그를 살펴봅니다. 다른 서비스의 보안 설정은 낮추지 않습니다.

### 집계

| 증상 | 조치 |
|---|---|
| 차트가 비어 있음 | `systemctl list-timers stcollector-rollup.timer`로 타이머가 켜져 있는지 봅니다. 수집 시작 후 첫 정시 2분 뒤부터 표시됩니다. |
| `집계 작업이 2시간 넘게 실행되지 않았습니다` | `journalctl -u stcollector-rollup -n 20 --no-pager`를 봅니다. 메모리 제한(`MemoryMax=256M`)에 걸리면 수동으로 `stcollector rollup`을 한 번 실행해 밀린 구간을 처리합니다. |

### 대시보드

| 증상 | 조치 |
|---|---|
| 웹서버가 시작되지 않음 | `journalctl -u stcollector-web -n 20 --no-pager`를 봅니다. `비밀번호가 설정되지 않아`가 보이면 [대시보드 비밀번호](../README.md#3-대시보드-비밀번호) 단계를 진행합니다. 설정 오류는 자동 재시작되지 않으므로 고친 뒤 `sudo systemctl restart stcollector-web`을 실행합니다. |
| 접속이 안 됨 | `sudo ufw status`로 8080 허용을 확인하고, 접속 기기가 같은 공유기에 연결돼 있는지 봅니다. |
| `허용되지 않은 네트워크입니다` | 접속 기기의 대역이 `allowed_networks`에 있어야 합니다. |
| 공유기 대역이 `192.168.0.x`에서 바뀜 | `ufw` 규칙, `config.toml`의 `allowed_networks`, `stcollector-web.service`의 `IPAddressAllow`를 함께 바꿉니다. |
| `실패가 많아 5분간 로그인이 막혔습니다` | 5분 뒤 다시 시도합니다. 비밀번호를 잊었으면 `stcollector hash-password`로 해시를 새로 넣고 웹을 재시작합니다. |

### 하드웨어와 저장장치

| 증상 | 조치 |
|---|---|
| 전압 부족 경고 | `vcgencmd get_throttled`를 확인하고 [POWER_TEST](POWER_TEST.md)를 봅니다. |
| 디스크 부족 | 위 [디스크](#디스크) 명령으로 원인을 찾습니다. `sudo journalctl --vacuum-size=64M`로 로그를 줄이거나 `retention_days`를 줄입니다. |
| DB 손상, microSD 고장, 보드 고장 | [MIGRATION](MIGRATION.md)을 봅니다. |

### 자격증명 유출이 의심될 때

| 방식 | 조치 |
|---|---|
| CLI | `controller:stCli` 권한이라 **기기 제어까지 가능합니다.** `sudo systemctl stop stcollector`로 수집을 멈추고 SmartThings 계정 비밀번호를 바꿉니다. `smartthings logout`이나 비밀번호 변경이 서버 쪽 토큰을 폐기하는지는 확인하지 못했으므로, 이후 SmartThings 앱에서 기기 동작 이력을 살펴봅니다. |
| OAuth | `smartthings smartapps:delete`로 앱을 지웁니다. 읽기 권한만 있었으므로 기기 제어는 불가능했습니다. |

## RPi 미확인 항목

PC 단위 테스트는 통과했지만 RPi에서 시험하지 못했습니다. 첫 배포 때 위에서부터 확인합니다.

| 항목 | 확인 방법 |
|---|---|
| `install.sh`, `migrate-cli-credentials.sh` 실제 실행 (문법 검사만 함) | [README M1](../README.md#m1-설치와-수집-시작-rpi) 순서대로 실행 |
| CLI 실제 출력과 `cli_client.py` 파싱, 페이징 대조 | `stcollector check`, 수집 후 `meta.mode`와 건수 |
| systemd 샌드박스 아래 CLI 동작 (`MemoryDenyWriteExecute=false`만으로 충분한지, `AF_NETLINK`) | `sudo systemctl start stcollector` 후 로그 |
| CLI 액세스 토큰 자동 갱신, 리프레시 토큰 유효기간 | [README 확인 절차](../README.md#확인되지-않은-사항-cli-토큰-자동-갱신) |
| `smartthings logout` 동작 | 재로그인 때 확인 |
| CLI 자체 로그의 크기와 microSD 쓰기량 | 위 [디스크](#디스크) 명령 |
| 실제 데이터로 집계와 대시보드, RPi 응답 속도 | M2~M4 완료 기준 ([PLAN §11](../PLAN.md#11-단계별-진행)) |
| 수집 부하까지 포함한 장시간 전압 상태 | `vcgencmd get_throttled` |
| [MIGRATION](MIGRATION.md)의 복구 절차 | 복구 또는 M5 복원 시험 때 확인 |
