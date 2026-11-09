# stcollector — SmartThings 센서 로컬 수집기

SmartThings API에서 센서 데이터를 받아 **Raspberry Pi 안의 SQLite에만** 저장합니다.
이 문서는 설치 안내입니다. 운영, 장애 대응, 보드 교체, 구조는 [문서 지도](docs/README.md)에서 찾으세요. 설계와 전체 계획은 [PLAN.md](PLAN.md)를 보세요.

지금 들어 있는 것은 **M0(준비), M1(수집), M2(집계), M3·M4(대시보드)** 입니다. 수집 코드의 CLI 전환과 배포 스크립트는 PC 테스트까지 끝났고 **RPi에는 아직 올리지 않았습니다.** 지금 RPi에는 예전 M1 코드가 있으므로, 올릴 때는 아래 M1의 기존 설치 안내를 따릅니다. 시험하지 못한 항목은 [RPi 미확인 목록](docs/OPERATIONS.md#rpi-미확인-항목)에 있습니다. 자동 백업(M5)은 아직 없습니다.

- Python 코드는 3.11+ 표준 라이브러리만 사용합니다. **Python 외의 유일한 실행 의존성은 SmartThings CLI**(3.0.0, linux-arm64 단일 실행 파일)입니다.
- 인터넷 쪽 연결은 SmartThings 서버로 나가는 HTTPS뿐입니다. 들어오는 연결은 집 안 네트워크에서 대시보드와 SSH로 오는 접속뿐입니다.
- **CLI 자격증명의 권한은 `controller:stCli`이며 읽기 전용이 아닙니다.** 수집기 코드의 명령 허용 목록으로 읽기 명령만 실행하도록 제한합니다. 이 제한은 수집기 안의 제한이므로, 자격증명 파일이 유출되면 막아 주지 못합니다. 그래서 파일은 `stcollector` 계정 전용(파일 600, 폴더 700)으로 둡니다.

---

## M0. 준비

### 1) Raspberry Pi 기본 설정

모니터와 키보드는 필요 없습니다. RPi를 공유기에 이더넷으로 연결하고, 같은 공유기에 연결된 PC에서 SSH로 설정합니다.

1. PC에서 SSH 키를 만듭니다. `C:\Users\<사용자>\.ssh\id_ed25519.pub` 파일의 내용을 다음 단계에서 씁니다.

   ```bash
   ssh-keygen -t ed25519
   ```

2. Raspberry Pi Imager에서 장치를 **Raspberry Pi 3**, OS를 **Raspberry Pi OS Lite (64-bit, Bookworm 이상)** 로 고릅니다.
   OS 설정 화면에서 호스트명(예: `homesensor`), 사용자와 비밀번호, 시간대를 넣고, SSH를 켠 뒤 **공개키 인증만 허용**을 선택해 위 공개키를 붙여 넣습니다.
3. uSD를 꽂고 LAN 케이블을 연결한 뒤 전원을 넣습니다. 첫 부팅은 파티션 확장과 재부팅 때문에 몇 분 걸립니다.
   PC에서 접속합니다. 이름으로 접속되지 않으면 공유기 관리 페이지의 접속 기기 목록에서 IP를 찾아 그 IP로 접속합니다.

   ```bash
   ssh <사용자>@homesensor.local
   ```

   접속되면 공유기에서 RPi에 고정 IP를 할당합니다.
4. SSH 접속 상태에서 다음을 실행합니다.

```bash
sudo apt update && sudo apt full-upgrade -y
```

```bash
sudo apt install -y ufw unattended-upgrades sqlite3
```

```bash
sudo timedatectl set-timezone Asia/Seoul
```

5. 방화벽: 집 안 네트워크에서 오는 SSH만 허용합니다. 먼저 `hostname -I` 로 RPi의 IP를 보고 대역을 맞추세요(예: IP가 `192.168.0.x` 이면 아래 명령의 `192.168.0.0/24` 그대로).
   **대역을 틀리면 SSH가 막히고, 모니터가 없으면 복구하기 어렵습니다.** 적용 후 지금 창을 닫지 말고 새 창에서 SSH 접속이 되는지 확인하세요.

```bash
sudo ufw default deny incoming && sudo ufw default allow outgoing && sudo ufw allow from 192.168.0.0/24 to any port 22 proto tcp && sudo ufw enable
```

6. microSD 쓰기를 줄이기 위해 로그 용량을 제한합니다. `/etc/systemd/journald.conf` 의 `[Journal]` 섹션에 `SystemMaxUse=64M` 을 넣고 `sudo systemctl restart systemd-journald` 를 실행합니다.
7. 운영 설정(BT·Wi-Fi 끔, 900MHz, 1코어)을 넣고 재부팅합니다. 설정 이유와 되돌리는 방법은 [전원 시험 결과](docs/POWER_TEST.md)에 있습니다. 이 설정은 **DZ5V3A와 QC5V(15cm 케이블)로 각각 약 12분 시험한 결과이므로, 다른 어댑터나 케이블에는 그대로 적용할 수 없습니다.** Wi-Fi를 끄므로 **유선 이더넷 연결이 필수**입니다.

   | 항목 | 파일 | 설정 |
   |---|---|---|
   | Bluetooth 끔 | `/boot/firmware/config.txt` | `dtoverlay=disable-bt` |
   | Wi-Fi 끔 | `/boot/firmware/config.txt` | `dtoverlay=disable-wifi` |
   | 클럭 제한 | `/boot/firmware/config.txt` | `arm_freq=900` |
   | 1코어 | `/boot/firmware/cmdline.txt` (한 줄 유지) | `maxcpus=1` |

   재부팅 후 `nproc` 이 `1` 이면 코어 설정이 적용된 것입니다. 전압 상태는 지금 쓰는 어댑터로 운영 중에 `vcgencmd get_throttled` 를 실행해 확인합니다. `throttled=0x0` 이면 부팅 후 전압 부족과 스로틀링 기록이 없다는 뜻입니다.

**(선택) 나중에 모니터나 KVM을 연결할 때**

- KVM이 다른 PC 쪽을 보고 있는 상태로 RPi가 부팅되면 HDMI 출력이 안 잡혀, 나중에 전환해도 화면이 안 나올 수 있습니다. 이럴 때는 `/boot/firmware/cmdline.txt` 의 **한 줄 끝에** 공백을 두고 아래 값을 덧붙인 뒤 재부팅합니다. 해상도는 모니터에 맞추세요.

  ```text
  video=HDMI-A-1:1920x1080@60D
  ```

- 콘솔에서는 PC와 복사·붙여넣기가 안 되고, CLI 로그인에 필요한 SSH 터널도 쓸 수 없습니다. 로그인은 SSH로 하세요.
- 콘솔 자동 로그인은 켜지 않는 것을 권장합니다. 기본 사용자가 비밀번호 없이 sudo를 쓰도록 설정돼 있으면, 화면만 켜도 자격증명까지 접근할 수 있게 됩니다.

### 2) SmartThings CLI 받기 (RPi에서 1회)

OAuth 앱을 만들려면 유료 Developer Plan(보도상 월 $4.99)이 필요해서, 기본 수집 방식은 무료인 CLI 로그인입니다. OAuth를 쓰려면 아래 [선택: OAuth 방식](#선택-oauth-방식)을 보세요.

[SmartThings CLI 릴리스](https://github.com/SmartThingsCommunity/smartthings-cli/releases)에서 linux-arm64용 파일을 받아 RPi의 `~/st-cli/smartthings` 에 실행 파일로 둡니다. 검증한 버전은 3.0.0입니다. 설치와 로그인은 다음 M1 단계에서 합니다.

---

## M1. 설치와 수집 시작 (RPi)

### 1) 코드 복사와 설치

PC의 이 폴더에서 RPi로 복사합니다. 받을 폴더를 먼저 만듭니다.

```bash
ssh <사용자>@<RPi IP> "mkdir -p ~/stcollector-src"
```

```bash
scp -r stcollector deploy config.example.toml <사용자>@<RPi IP>:~/stcollector-src/
```

RPi에서 설치합니다.

```bash
cd ~/stcollector-src && chmod +x deploy/install.sh && sudo ./deploy/install.sh
```

CLI 바이너리가 `~/st-cli/smartthings` 가 아닌 곳에 있으면 `sudo ./deploy/install.sh --cli-binary /경로/smartthings` 로 지정합니다.

설치 스크립트가 하는 일 (다시 실행해도 기존 설정, DB, 자격증명은 유지됩니다):
- 전용 계정 `stcollector`(로그인 불가)를 만듭니다.
- 코드를 `/opt/stcollector`(root 소유, 읽기만)에 둡니다.
- CLI를 `/opt/stcollector-cli/smartthings`(root 소유, 750, 그룹 `stcollector`)에 둡니다. 서비스 계정은 쓸 수 없고 실행만 합니다.
- 설정을 `/etc/stcollector/config.toml`(640)에 둡니다.
- 데이터를 `/var/lib/stcollector`(700)에, CLI의 `HOME`을 `/var/lib/stcollector/cli`(`stcollector` 소유, 700)에 둡니다.
- 보안 제한을 건 systemd 서비스를 등록합니다. 수집 서비스만 CLI 실행을 위해 `MemoryDenyWriteExecute` 를 끕니다.

### 2) 설정 입력

```bash
sudo nano /etc/stcollector/config.toml
```

처음 설치했다면 `backend = "cli"` 가 이미 들어 있어 기본값으로 시작하면 됩니다. 위치가 여러 개면 `location_id` 를 넣습니다.

이전에 설치했던 RPi는 기존 파일이 유지되므로, `[smartthings]` 섹션 안에 아래 네 줄을 직접 넣습니다. 파일 맨 끝에 넣으면 다른 섹션의 값이 되어 적용되지 않습니다.

```toml
backend = "cli"
cli_path = "/opt/stcollector-cli/smartthings"
cli_home = "/var/lib/stcollector/cli"
cli_timeout_s = 120
```

### 3) CLI 자격증명 이전

`homesensor` 계정으로 CLI에 로그인해 두었다면, 자격증명을 서비스 계정으로 옮깁니다. 스크립트는 내용을 출력하지 않고, 복사본을 검증한 뒤 원본을 지웁니다. 로그인한 계정 이름이 다르면 `--from-user 계정` 을 붙입니다.

```bash
cd ~/stcollector-src && sudo bash deploy/migrate-cli-credentials.sh
```

아직 로그인한 적이 없으면 이 단계 대신 [CLI 로그인과 재로그인](#cli-로그인과-재로그인)을 따릅니다.

### 4) 점검

```bash
stcollector check
```

다음을 확인합니다.
- 수집 대상 기기 목록 (센서 10기와 플러그가 보이는지)
- 상태 조회 결과와 센서가 보고한 시각
- **이벤트 이력 API 사용 가능 여부와 정렬 순서** (PLAN §12-5)

### 5) 수집 시작

```bash
sudo systemctl enable --now stcollector
```

```bash
journalctl -u stcollector -f
```

처음 실행하면 SmartThings에 남아 있는 최근 이력(최대 7일, `backfill_days`)을 먼저 가져옵니다.

### 6) 1주 후: 센서별 보고 간격

```bash
stcollector intervals --days 7
```

이 결과를 보고 수집 주기와 센서 설정(보고 주기·민감도) 조정 여부를 정합니다 (PLAN §6.1).

---

## CLI 로그인과 재로그인

RPi에는 브라우저가 없으므로 SSH 터널을 통해 PC 브라우저로 로그인합니다. CLI에는 별도의 로그인 명령이 없고, 자격증명이 필요할 때 로그인 페이지를 자동으로 띄웁니다. RPi에서는 `xdg-open` 대체 스크립트(`deploy/cli/xdg-open`, 설치 위치 `/opt/stcollector-cli/login-bin/xdg-open`)가 브라우저를 여는 대신 주소만 출력합니다.

1. PC에서 터널을 연 SSH로 접속합니다. 로그인이 끝날 때까지 이 창을 유지합니다.

   ```bash
   ssh -L 61973:localhost:61973 <사용자>@<RPi IP>
   ```

2. 그 SSH 창에서 수집을 멈춥니다.

   ```bash
   sudo systemctl stop stcollector
   ```

3. 재로그인이라면 기존 로그인 상태를 지웁니다. 처음 로그인할 때는 건너뜁니다. `logout` 명령은 RPi 미확인입니다.

   ```bash
   cd / && sudo -u stcollector env HOME=/var/lib/stcollector/cli PATH=/opt/stcollector-cli/login-bin:/usr/bin:/bin /opt/stcollector-cli/smartthings logout
   ```

4. 읽기 명령을 실행해 로그인을 시작합니다. 기기 목록은 화면에 남지 않도록 버립니다.

   ```bash
   cd / && sudo -u stcollector env HOME=/var/lib/stcollector/cli PATH=/opt/stcollector-cli/login-bin:/usr/bin:/bin /opt/stcollector-cli/smartthings devices --json > /dev/null
   ```

5. PC 브라우저에서 `http://localhost:61973/start` 를 열고 SmartThings에 로그인합니다. 로그인이 끝나면 4번 명령이 종료됩니다.
6. 소유자와 권한을 확인합니다. `stcollector 600` 이 나와야 합니다. 파일 내용은 열어 보지 마세요.

   ```bash
   sudo stat -c '%U %a' /var/lib/stcollector/cli/.local/share/@smartthings/cli/credentials.json
   ```

7. 수집을 다시 시작합니다.

   ```bash
   sudo systemctl start stcollector
   ```

### 확인되지 않은 사항: CLI 토큰 자동 갱신

CLI가 만료된 액세스 토큰을 스스로 갱신하는지는 **아직 확인하지 못했습니다.** 현재 액세스 토큰의 만료 시각은 2026-11-10T15:06Z이고, 자격증명에 리프레시 토큰은 들어 있습니다. 리프레시 토큰의 유효기간도 확인되지 않았습니다.

만료 시각이 지난 뒤 다음을 확인합니다.

1. 자격증명 파일의 수정 시각을 만료 전에 적어 두고, 만료 후 다시 비교합니다. 내용을 읽지 않는 명령입니다.

   ```bash
   sudo stat -c '%y' /var/lib/stcollector/cli/.local/share/@smartthings/cli/credentials.json
   ```

2. 만료 후 `stcollector check` 가 성공하고 `journalctl -u stcollector` 에 인증 오류가 없으면 갱신이 동작한 것입니다.

갱신이 되지 않으면 위 재로그인 절차를 만료 때마다 반복해야 하므로 무인 수집 방식을 다시 정해야 합니다.

---

## M2~M4. 집계와 대시보드 (RPi)

수집기와 같은 RPi에서 돌아갑니다. 외부 라이브러리나 외부 서비스를 쓰지 않고, 화면도 외부로 요청을 보내지 않습니다.

### 1) 코드 갱신

PC의 이 폴더에서 복사합니다.

```bash
scp -r stcollector deploy config.example.toml <사용자>@<RPi IP>:~/stcollector-src/
```

RPi에서 설치 스크립트를 다시 실행합니다. 설정, DB, CLI 자격증명은 그대로 유지되고 `[rollup]`, `[web]` 항목만 없을 때 덧붙입니다.

```bash
cd ~/stcollector-src && sudo ./deploy/install.sh
```

### 2) 집계 시작

매시 2분에 끝난 시간까지 집계합니다. 처음 한 번은 쌓인 원본 전체를 계산합니다.

```bash
sudo systemctl enable --now stcollector-rollup.timer
```

바로 한 번 돌려 보려면 다음을 실행합니다.

```bash
stcollector rollup
```

### 3) 대시보드 비밀번호

비밀번호를 정하지 않으면 웹서버는 시작을 거부합니다. 아래 명령은 비밀번호를 해시로만 바꿔서 출력합니다.

```bash
stcollector hash-password
```

출력된 `password_hash = "..."` 줄을 설정 파일의 `[web]` 섹션에 있는 같은 줄과 바꿉니다.

```bash
sudo nano /etc/stcollector/config.toml
```

### 4) 방화벽과 서비스

```bash
sudo ufw allow from 192.168.0.0/24 to any port 8080 proto tcp
```

```bash
sudo systemctl enable --now stcollector-web
```

```bash
systemctl status stcollector-web
```

### 5) 접속

같은 공유기에 연결된 스마트폰이나 PC 브라우저에서 `http://<RPi IP>:8080` 을 엽니다.

- 스마트폰 브라우저 메뉴의 **홈 화면에 추가**로 아이콘을 만들 수 있습니다. HTTP 접속이라 Android Chrome에서는 독립 앱 창이 아니라 브라우저 바로가기로 열릴 수 있습니다.
- 차트는 **끝난 시간까지의 집계**를 그립니다. 요약 화면의 현재값과 오늘 최고·최저는 최신 측정값까지 반영합니다.
- 비밀번호를 바꾸면 해시를 새로 넣고 `sudo systemctl restart stcollector-web` 을 실행합니다. 기존 로그인은 모두 풀립니다.
- 공유기 대역이 `192.168.0.x` 가 아니게 되면 ufw 규칙, `config.toml` 의 `allowed_networks`, `stcollector-web.service` 의 `IPAddressAllow` 를 함께 바꿉니다.

집계 규칙 조정과 수동 실행은 [운영 문서](docs/OPERATIONS.md#집계-수동-실행)를 보세요.

---

## 선택: OAuth 방식

OAuth-In 앱을 만들고 관리하려면 **유료 SmartThings Developer Plan**(보도상 월 $4.99)이 필요합니다. OAuth 방식은 권한을 읽기 전용(`r:devices:*`, `r:locations:*`)으로 줄일 수 있고 CLI를 수집에 쓰지 않습니다.

1. PC에서 앱을 만듭니다. CLI 3.0.0부터 `apps*` 명령은 `smartapps*` 로 바뀌었습니다. Client Secret을 다시 만들 때는 `smartthings smartapps:oauth:generate` 를 씁니다.

   ```bash
   smartthings smartapps:create
   ```

2. 질문에는 다음과 같이 답합니다.

   | 질문 | 답 |
   |---|---|
   | 앱 종류 | **OAuth-In App** |
   | Display Name / Description | 자유 (예: `home-sensor-collector`) |
   | Icon Image URL, Target URL | 비워 둠 |
   | Scopes | **`r:devices:*`, `r:locations:*`** 만 선택 |
   | Redirect URIs | `https://localhost/callback` |

3. 마지막에 출력되는 **OAuth Client Id**와 **OAuth Client Secret**을 안전한 곳에 적어 두세요. Secret은 이때 한 번만 보입니다.
4. `/etc/stcollector/config.toml` 의 `[smartthings]` 에서 `backend = "oauth"` 로 바꾸고, `client_id`, `client_secret`, `redirect_uri`, `scopes` 의 주석을 풀어 값을 넣습니다.
5. `stcollector auth` 를 실행해 출력된 URL을 브라우저에서 승인한 뒤, 주소창의 `https://localhost/callback?code=...&state=...` 전체를 터미널에 붙여 넣습니다. 브라우저에 "페이지를 열 수 없음"이 떠도 정상입니다.

---

## M1 완료 기준 확인

| 항목 | 확인 방법 |
|---|---|
| 24시간 연속 수집 | `systemctl status stcollector` 에서 계속 active, 로그에 오류 반복이 없음 |
| CLI 토큰 자동 갱신 | 액세스 토큰 만료 후에도 수집이 이어지고 인증 오류가 없음 ([확인 방법](#확인되지-않은-사항-cli-토큰-자동-갱신)) |
| 수집 방식 확정 | `sudo -u stcollector sqlite3 /var/lib/stcollector/sensors.db "select value from meta where key='mode'"` |
| 보고 간격 리포트 | `stcollector intervals` |

## 문제 해결

증상별 조치는 [운영 문서의 장애 대응](docs/OPERATIONS.md#장애-대응)으로 옮겼습니다. 보드나 microSD가 고장 나면 [보드 교체와 복구](docs/MIGRATION.md)를 보세요.

## 개발 (PC)

```bash
python -m unittest discover -s tests -t .
```

화면 개발용 가짜 데이터 (실제 DB에는 쓰지 마세요):

```bash
python tools/generate_fake_data.py --db data/fake.db --days 400
```

## 파일 구성

```
stcollector/
  config.py      설정 로드·검증
  transport.py   OAuth 방식의 HTTPS 전용, 리다이렉트 차단 HTTP 계층
  auth.py        OAuth 방식 승인·토큰 자동 갱신 (원자적 저장, 600)
  cli_client.py  SmartThings CLI 호출 (읽기 명령 허용 목록, 현재 기준)
  client.py      OAuth 방식 API (재시도, 429 대응, 다른 호스트로 토큰 전송 차단)
  collector.py   수집 루프 (이벤트 이력 우선, 상태 조회 대안)
  db.py, schema.sql  SQLite 저장 (WAL, 중복 제거)
  intervals.py   보고 간격 리포트
  rollup.py      시간·일·주 집계, 조도 구간, 재실, 원본 정리
  settings.py    [rollup], [web] 설정
  web/           대시보드 (표준 라이브러리 HTTP 서버, 자체 SVG 차트)
deploy/
  install.sh                     코드, CLI, 설정, systemd 설치
  migrate-cli-credentials.sh     CLI 자격증명을 서비스 계정으로 이전
  cli/xdg-open                   헤드리스 로그인용 (주소만 출력)
  stcollector                    명령행 래퍼
  systemd/                       수집, 웹, 집계 timer
docs/
  README.md        문서 지도
  ARCHITECTURE.md  코드 흐름, DB 테이블, 서비스, 보안 경계
  OPERATIONS.md    일상 점검, 장애 대응, RPi 미확인 항목
  MIGRATION.md     보드 교체와 복구
  POWER_TEST.md    RPi 전원 시험 결과와 운영 설정
tools/           가짜 데이터 생성기
tests/           단위 테스트
```
