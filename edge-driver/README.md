# SmartThings 앱에서 RPi 상태 보기 (Edge LAN 드라이버)

SmartThings Hub V3에 설치하는 Edge 드라이버다. 허브가 집 안 LAN에서 RPi 대시보드 서버의 `GET /hub/status`를 주기적으로(기본 60초) 읽어 SmartThings 앱의 기기 하나로 보여 준다.

- RPi의 SmartThings CLI 호출은 늘지 않는다. 허브가 RPi를 직접 읽는다.
- 드라이버는 읽기만 한다. RPi에 쓰는 요청이나 기기 제어 명령이 없다.
- `/hub/status`는 로그인이 없는 대신 설정에 적은 허브 IP에서만 열린다. 응답에는 기기·방 이름, 오류 문구, 비밀값이 없다.

## 앱에 보이는 항목

| 항목 | capability | 속성 | 값 |
|---|---|---|---|
| 수집 이상 알림 | `contactSensor` (표준) | `contact` | 닫힘 = 정상, 열림 = 이상 |
| CPU 온도 | `temperatureMeasurement` (표준) | `temperature` | °C |
| SmartThings 수집 | `<namespace>.piCollectStatus` | `collectStatus` | `정상`, `이력 지연 20분`, `수집 멈춤 45분`, `재인증 필요`, `수집 기록 없음`, `RPi 응답 없음` |
| 마지막 수집 | `<namespace>.piLastCollect` | `lastCollect` | `11-13 14:05` (KST, 월-일 시:분) |
| CPU 클럭 | `<namespace>.piClockSpeed` | `clockSpeed` | MHz |
| Load (1분) | `<namespace>.piLoadAverage` | `loadAverage` | 숫자 |
| 디스크 사용 | `<namespace>.piDiskUsage` | `diskUsage` | % (DB가 있는 파일 시스템) |
| 새로고침 | `refresh` (표준) | — | 아래로 당기면 바로 한 번 읽음 |

- 열림(이상)이 되는 경우
  - RPi 판정: 아래 [수집 정상 판정](#수집-정상-판정)에서 이상
  - 허브 판정: `/hub/status`를 3회 연속 못 읽음 (기본 주기로 약 3분). RPi 꺼짐, 웹 서비스 중지, 주소 설정 오류, 허브 IP 미허용
- 온도·클럭·load는 RPi 모니터(`stcollector-sysmon`)의 현재값이 30초 안에 갱신됐을 때만 바뀐다. 모니터가 멈추면 마지막 값이 그대로 남는다.
- 같은 값을 매번 보내지 않는다. 온도 0.5°C, load 0.1, 디스크 0.5%, 클럭 1MHz 이상 바뀔 때만 이벤트를 보낸다.

## 수집 정상 판정

RPi의 `stcollector/web/api.py`의 `collect_health()`가 수집 DB의 `meta` 값으로 판정한다. 위에서부터 먼저 맞는 것을 쓴다.

| 상태 | 조건 | contact |
|---|---|---|
| 재인증 필요 | `auth_state = reauth_required` | 열림 |
| 수집 기록 없음 | `last_ok_ms` 없음 (수집이 한 번도 성공하지 않음) | 열림 |
| 수집 멈춤 N분 | 마지막 수집 성공(`last_ok_ms`) 후 30분 초과 | 열림 |
| 이력 지연 N분 | 이력 조회 연속 실패(`history_fail_since_ms`)가 10분 초과 | 60분 이하면 닫힘, 넘으면 열림 |
| 정상 | 그 밖 | 닫힘 |

- 30분, 10분은 대시보드 상단 경고와 같은 기준이다.
- 이력 지연은 대개 SmartThings 서버 쪽 일시 오류다. 그동안 현재값을 임시로 받고 회복되면 밀린 이력을 다시 받으므로 1시간까지는 알림을 보내지 않는다.

## 준비물

- PC에 SmartThings CLI 설치와 로그인 (`smartthings` 명령). 로그인은 사용자가 직접 한다.
- 허브의 고정 IP (공유기 DHCP 예약)
- RPi 설정 변경은 아래 [RPi 쪽 설정](#rpi-쪽-설정) 참고. 배포·재시작은 사용자 동의 후 진행한다.

## PC에서 할 일 (사용자가 직접 실행)

PowerShell에서 저장소 루트(`iot`)로 이동한 뒤 진행한다. `<namespace>`는 `capabilities:create` 결과에 나오는 내 네임스페이스로 바꾼다. 이 저장소의 프로파일 파일(`profiles/stcollector-rpi.yml`)은 이미 그 값으로 적혀 있다. 네임스페이스가 다르면 프로파일과 `capabilities/*.presentation.json`의 `id`도 함께 고친다.

```powershell
$NS = "<namespace>"
```

### 1. 커스텀 capability 5개 만들기

```powershell
smartthings capabilities:create -i edge-driver/capabilities/pi-collect-status.json
smartthings capabilities:create -i edge-driver/capabilities/pi-last-collect.json
smartthings capabilities:create -i edge-driver/capabilities/pi-clock-speed.json
smartthings capabilities:create -i edge-driver/capabilities/pi-load-average.json
smartthings capabilities:create -i edge-driver/capabilities/pi-disk-usage.json
```

- 결과의 id가 `<namespace>.piCollectStatus`, `piLastCollect`, `piClockSpeed`, `piLoadAverage`, `piDiskUsage`인지 확인한다. 다르면 프로파일·Lua·presentation의 id를 결과에 맞춘다.
- 앞서 시험으로 만든 `cpuClockTest`는 쓰지 않는다.

### 2. capability presentation 5개 만들기

```powershell
smartthings capabilities:presentation:create "$NS.piCollectStatus" 1 -i edge-driver/capabilities/pi-collect-status.presentation.json
smartthings capabilities:presentation:create "$NS.piLastCollect" 1 -i edge-driver/capabilities/pi-last-collect.presentation.json
smartthings capabilities:presentation:create "$NS.piClockSpeed" 1 -i edge-driver/capabilities/pi-clock-speed.presentation.json
smartthings capabilities:presentation:create "$NS.piLoadAverage" 1 -i edge-driver/capabilities/pi-load-average.presentation.json
smartthings capabilities:presentation:create "$NS.piDiskUsage" 1 -i edge-driver/capabilities/pi-disk-usage.presentation.json
```

### 3. 채널 만들기 (처음 한 번)

```powershell
smartthings edge:channels:create
```

- 이름, 설명, 약관 URL을 물으면 입력한다. 개인용이므로 약관 URL은 아무 주소나 넣어도 되는 것으로 알려져 있다 (미확인).
- 결과의 채널 id를 적어 둔다.

### 4. 패키징과 업로드

```powershell
smartthings edge:drivers:package edge-driver
```

- 성공하면 드라이버 id와 버전이 나온다.
- 프로파일에 적은 커스텀 capability가 아직 없으면 여기서 실패한다. 1단계부터 확인한다.

### 5. 채널에 드라이버 올리기, 허브 등록, 설치

```powershell
smartthings edge:channels:assign
smartthings edge:channels:enroll
smartthings edge:drivers:install
```

- 각 명령은 드라이버, 채널, 허브를 목록에서 고르게 한다.
- 순서: 채널에 드라이버 배정 → 허브를 채널에 등록 → 허브에 드라이버 설치.
- 드라이버를 고친 뒤에는 4단계와 `edge:channels:assign`만 다시 하면 허브가 새 버전을 받는다 (반영까지 몇 분에서 몇 시간 걸릴 수 있음, 미확인). 기다리지 않으려면 `edge:drivers:install`을 다시 실행한다.

### 6. 앱에서 기기 추가와 RPi 주소 입력

1. SmartThings 앱 → 기기 추가(+) → **주변 기기 검색** (Scan nearby)
2. 검색이 도는 동안 드라이버가 `RPi 상태` 기기를 하나 만든다. 이미 있으면 새로 만들지 않는다.
3. 기기 → 오른쪽 위 메뉴 → 설정
   - `RPi 주소 (IP:포트)`: 기본값 `192.168.0.17:8080`. RPi 주소가 바뀌었으면 고친다. `http://`는 붙여도 되고 안 붙여도 된다.
   - `읽기 주기 (초)`: 30~600, 기본 60
4. 1분쯤 뒤 값이 채워지는지 본다. 비어 있으면 7단계 로그를 본다.

### 7. 로그 보기

```powershell
smartthings edge:drivers:logcat
```

- 허브 IP와 드라이버를 물으면 고른다. PC와 허브가 같은 LAN에 있어야 한다.
- 잘 되면 `RPi 상태 읽기 시작: ...` 이 보이고, 실패하면 `RPi 상태 읽기 실패 N회: HTTP 404` 같은 줄이 보인다.

| 로그 | 원인 |
|---|---|
| `HTTP 404` | RPi 설정 `hub_status_allow`에 허브 IP가 없음, 또는 RPi 웹 서비스가 새 코드가 아님 |
| `연결 실패: timeout`, `connection refused` | RPi 꺼짐, 웹 서비스 중지, 주소·포트 오타, 허브 IP가 방화벽(`ufw`)이나 `IPAddressAllow` 대역 밖 |
| `RPi 주소 설정이 올바르지 않습니다` | 설정 값에 경로나 공백이 섞임. `IP:포트`만 넣는다 |

### 8. 휴대폰 알림 (루틴)

앱 → 자동화 → 루틴 추가

- 조건: `RPi 상태` → 열림/닫힘 → **열림**
- 실행: 내게 알림 보내기 (문구 예: "RPi 수집 이상. 대시보드 확인")
- 회복 알림이 필요하면 **닫힘** 조건으로 루틴을 하나 더 만든다.

## RPi 쪽 설정

허브가 읽을 수 있게 하려면 `/etc/stcollector/config.toml`의 `[web]`에 허브 IP를 넣고 웹 서비스를 재시작한다. 코드 배포와 재시작은 사용자 동의 후 한다.

```toml
[web]
hub_status_allow = ["<허브 IP>"]
```

```bash
sudo systemctl restart stcollector-web
```

- `/hub/status`는 `allowed_networks`가 아니라 `hub_status_allow`만 본다. 연결 자체는 `ufw`와 `stcollector-web.service`의 `IPAddressAllow`(둘 다 `192.168.0.0/24`)를 거치므로, 허브가 이 대역 안에 있으면 둘 다 바꿀 필요가 없다.
- RPi에서 직접 확인하려면 `127.0.0.1`도 목록에 잠깐 넣고 `curl -s http://127.0.0.1:8080/hub/status`를 실행한다. 확인 뒤 빼 둔다.

## 파일

| 파일 | 내용 |
|---|---|
| `config.yml` | 드라이버 이름, packageKey, 권한(`lan`, `discovery`) |
| `profiles/stcollector-rpi.yml` | 기기 프로파일과 설정 항목(RPi 주소, 읽기 주기) |
| `src/init.lua` | 기기 생성, 주기 읽기, 이벤트 보내기 |
| `capabilities/*.json` | 커스텀 capability 정의 |
| `capabilities/*.presentation.json` | 앱 표시 방식 (대시보드, 상세 화면, 루틴 조건) |

`tests/test_edge_driver.py`가 capability 이름에서 만든 id, presentation id, 프로파일, Lua의 이름이 서로 맞는지와 Lua가 읽는 응답 필드가 `/hub/status`에 있는지 확인한다. Lua 자체는 PC에서 실행해 보지 않았다.

## 근거와 확인 못 한 점

근거로 삼은 자료

- 기기 생성 `driver:try_create_device({type = "LAN", device_network_id, label, profile, manufacturer, model, vendor_provided_label})`, discovery 함수 모양: [Driver 문서](https://developer.smartthings.com/docs/edge-device-drivers/driver.html), 공식 예제 [wemo 드라이버](https://github.com/SmartThingsCommunity/SmartThingsEdgeDrivers/tree/main/drivers/SmartThings/wemo)
- HTTP 요청 `cosock.asyncify "socket.http"`, `ltn12.sink.table`, 반환값 `success, code, headers, status`: [LAN 기기 문서](https://developer.smartthings.com/docs/devices/hub-connected/lan)
- 타이머 `device.thread:call_on_schedule`, `call_with_delay`, `cancel_timer`: [Thread 문서](https://developer.smartthings.com/docs/edge-device-drivers/thread.html), wemo 예제
- 설정(preferences) 문법, `infoChanged`의 `args.old_st_store.preferences`: [Preferences 문서](https://developer.smartthings.com/docs/devices/preferences)
- capability JSON, presentation 생성 명령: [Custom Capabilities](https://developer.smartthings.com/docs/devices/capabilities/custom-capabilities), [Capability Presentations](https://developer.smartthings.com/docs/devices/capabilities/capability-presentations)
- CLI 명령 이름: [smartthings-cli](https://github.com/SmartThingsCommunity/smartthings-cli)

추정이거나 확인 못 한 점

- JSON 모듈 경로: 문서에 경로가 없다. `st.json`을 먼저 쓰고 없으면 `dkjson`을 쓰게 했다.
- 커스텀 capability의 숫자 속성에 `unit`을 두는 스키마: 표준 capability와 같은 모양(`unit`에 `enum`)으로 썼다. 공식 예제는 문자열 속성만 있었다.
- presentation의 `detailView` `displayType: "state"`와 `state.label`, 루틴 조건의 `numberField`·`textField` 세부 키: 문서의 표시 형식 목록에는 있으나 예제로는 확인 못 했다. 생성 명령이 거부하면 오류 문구에 맞춰 고친다.
- presentation 라벨의 한글: 그대로 표시될 것으로 본다. 깨지면 영문으로 바꾼다.
- 프로파일의 `metadata`(`vid`, `mnmn`): 넣지 않았다. 앱 상세 화면에 커스텀 항목이 안 보이면 device configuration을 만들어 `vid`를 넣어야 할 수 있다.
- 설정 이름 길이 제한, `edge:channels:create`의 옵션: 확인 못 했다. 위 명령은 대화형으로 쓰는 것을 전제로 했다.
- `try_create_device`를 discovery 안에서 바로 부르는 방식은 문서의 함수 정의와 예제에 맞지만, LAN 검색 없이 쓰는 공식 예제는 찾지 못했다.
- Lua 코드는 허브나 PC에서 실행해 보지 않았다. 첫 설치 때 logcat으로 확인한다.
