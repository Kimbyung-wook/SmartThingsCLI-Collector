# SmartThings 앱에서 RPi 상태 보기 (Edge LAN 드라이버)

SmartThings Hub V3에 설치하는 Edge 드라이버다. 허브가 집 안 LAN에서 RPi 대시보드 서버의 `GET /hub/status`를 주기적으로(기본 60초) 읽어 SmartThings 앱의 기기 하나로 보여 준다.

- RPi의 SmartThings CLI 호출은 늘지 않는다. 허브가 RPi를 직접 읽는다.
- 드라이버는 읽기만 한다. RPi에 쓰는 요청이나 기기 제어 명령이 없다.
- `/hub/status`는 로그인이 없는 대신 설정에 적은 허브 IP에서만 열린다. 응답에는 기기·방 이름, 오류 문구, 비밀값이 없다.

## 앱에 보이는 항목

| 앱 이름 (한국어) | 영문 이름 | capability | 속성 | 값 |
|---|---|---|---|---|
| 열림/닫힘 (기기 대표 상태) | Contact | `contactSensor` (표준) | `contact` | 닫힘 = 수집 정상, 열림 = 이상 |
| CPU 온도 | CPU Temperature | `<namespace>.cpuTemperature` | `cpuTemperature` | °C, 범위 -20~125 |
| CPU 클럭 | CPU Clock | `<namespace>.cpuClock` | `cpuClock` | MHz |
| CPU 사용률 | CPU Usage | `<namespace>.cpuUsage` | `cpuUsage` | %, 최근 1분 평균 |
| 수집 상태 | Collect Status | `<namespace>.collectStatus` | `collectStatus` | `정상`, `이력 지연 20분`, `수집 멈춤 45분`, `재인증 필요`, `수집 기록 없음`, `RPi 응답 없음` |
| 마지막 수집 시각 | Last Collect Time | `<namespace>.lastCollectTime` | `lastCollectTime` | `11-13 14:05` (KST, 월-일 시:분) |
| 디스크 사용량 | Disk Usage | `<namespace>.diskUsage` | `diskUsage` | % (DB가 있는 파일 시스템) |
| 새로고침 | Refresh | `refresh` (표준) | — | 아래로 당기면 바로 한 번 읽음 |

- 커스텀 capability의 id는 `capabilities:create`가 영문 이름(name)에서 만든다. 첫 단어는 모두 소문자, 다음 단어는 첫 글자만 대문자다 (사용자 시험: `CPU Clock Test` → `cpuClockTest`). 속성 이름도 id와 같게 했다.
- 앱의 한국어 이름은 capability 번역 파일(`capabilities/i18n/*.ko.json`)에서 온다. presentation에는 영문 문구와 `{{i18n.label}}` 참조만 두었다. 아래 [항목 이름과 한국어](#항목-이름과-한국어) 참고.
- 표준 `contactSensor`의 이름(열림/닫힘)은 앱이 정한다.

- 열림(이상)이 되는 경우
  - RPi 판정: 아래 [수집 정상 판정](#수집-정상-판정)에서 이상
  - 허브 판정: `/hub/status`를 3회 연속 못 읽음 (기본 주기로 약 3분). RPi 꺼짐, 웹 서비스 중지, 주소 설정 오류, 허브 IP 미허용
- CPU 온도를 표준 `temperatureMeasurement`로 두지 않은 이유
  - 사용자 결정: RPi CPU 온도는 80~100°C를 넘을 수 있음
  - 표준 capability를 쓴 다른 드라이버(Klipper 3D 프린터) 작성자가 루틴 조건 온도가 -20~50°C로 제한된다고 보고함 (공식 문서로는 확인 못 함)
  - 앱이 표준 온도를 방 온도처럼 다룰 수 있어 실내 온도와 섞일 우려
- CPU 사용률은 RPi 모니터가 `/proc/stat`의 표본 차이로 계산한다. load average는 단위 없는 대기 작업 수라 %로 보이기 어려워 바꿨다.
- 온도·클럭·CPU 사용률은 RPi 모니터(`stcollector-sysmon`)의 현재값이 30초 안에 갱신됐을 때만 바뀐다. 모니터가 멈추면 마지막 값이 그대로 남는다.
- 같은 값을 매번 보내지 않는다. 온도 0.5°C, CPU 사용률 5%p, 디스크 0.5%, 클럭 1MHz 이상 바뀔 때만 이벤트를 보낸다.

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

## 항목 이름과 한국어

- 공식 문서의 방법: presentation에는 영문 문구나 `{{i18n.label}}` 같은 참조를 두고, 언어별 이름은 capability 번역 파일(`tag: "ko"`)로 올린다. 앱은 휴대폰 언어 설정에 맞는 번역을 보여 준다 ([Capability Translations](https://developer.smartthings.com/docs/devices/capabilities/capability-translations), [Capability Presentations](https://developer.smartthings.com/docs/devices/capabilities/capability-presentations)).
- presentation JSON에 한국어를 직접 적는 것은 문서에 허용·금지 어느 쪽도 없다. 확인된 방법이 번역 파일이라 그쪽을 택했다.
- 번역 파일 모양 (`capabilities/i18n/cpu-clock.ko.json`)

```json
{"tag": "ko", "label": "CPU 클럭",
 "attributes": {"cpuClock": {"label": "CPU 클럭", "displayTemplate": "{{device.label}} CPU 클럭: {{value}} {{unit}}"}}}
```

- 미확인: 번역 태그가 `ko`인지 `ko-KR`인지, 한국어 번역이 없을 때 영어로 돌아가는지, `en` 번역이 꼭 있어야 하는지, `displayTemplate`이 어디에 쓰이는지(기록 화면으로 추정).

## 준비물

- PC에 SmartThings CLI 설치와 로그인 (`smartthings` 명령). 로그인은 사용자가 직접 한다.
- 허브의 고정 IP (공유기 DHCP 예약)
- RPi 설정 변경은 아래 [RPi 쪽 설정](#rpi-쪽-설정) 참고. 배포·재시작은 사용자 동의 후 진행한다.

## PC에서 할 일 (사용자가 직접 실행)

PowerShell에서 저장소 루트(`iot`)로 이동한 뒤 진행한다. `<namespace>`는 `capabilities:create` 결과에 나오는 내 네임스페이스로 바꾼다. 이 저장소의 프로파일 파일(`profiles/stcollector-rpi.yml`)은 이미 그 값으로 적혀 있다. 네임스페이스가 다르면 프로파일과 `capabilities/*.presentation.json`의 `id`도 함께 고친다.

```powershell
$NS = "<namespace>"
```

### 1. 커스텀 capability 6개 만들기

```powershell
smartthings capabilities:create -i edge-driver/capabilities/cpu-temperature.json
smartthings capabilities:create -i edge-driver/capabilities/cpu-clock.json
smartthings capabilities:create -i edge-driver/capabilities/cpu-usage.json
smartthings capabilities:create -i edge-driver/capabilities/collect-status.json
smartthings capabilities:create -i edge-driver/capabilities/last-collect-time.json
smartthings capabilities:create -i edge-driver/capabilities/disk-usage.json
```

- 결과의 id가 `<namespace>.cpuTemperature`, `cpuClock`, `cpuUsage`, `collectStatus`, `lastCollectTime`, `diskUsage`인지 확인한다. 다르면 프로파일·Lua·presentation·번역 파일의 id를 결과에 맞춘다.
- 앞서 시험으로 만든 `cpuClockTest`는 쓰지 않는다. 이번 `cpuClock`과 이름이 다르므로 겹치지 않는다.

### 2. capability presentation 6개 만들기

```powershell
smartthings capabilities:presentation:create "$NS.cpuTemperature" 1 -i edge-driver/capabilities/cpu-temperature.presentation.json
smartthings capabilities:presentation:create "$NS.cpuClock" 1 -i edge-driver/capabilities/cpu-clock.presentation.json
smartthings capabilities:presentation:create "$NS.cpuUsage" 1 -i edge-driver/capabilities/cpu-usage.presentation.json
smartthings capabilities:presentation:create "$NS.collectStatus" 1 -i edge-driver/capabilities/collect-status.presentation.json
smartthings capabilities:presentation:create "$NS.lastCollectTime" 1 -i edge-driver/capabilities/last-collect-time.presentation.json
smartthings capabilities:presentation:create "$NS.diskUsage" 1 -i edge-driver/capabilities/disk-usage.presentation.json
```

### 2-1. 번역 올리기 (영어, 한국어)

capability마다 영어와 한국어 번역을 하나씩 올린다. 휴대폰 언어가 한국어면 한국어 이름이 보인다.

```powershell
$files = @{ cpuTemperature = "cpu-temperature"; cpuClock = "cpu-clock"; cpuUsage = "cpu-usage";
            collectStatus = "collect-status"; lastCollectTime = "last-collect-time"; diskUsage = "disk-usage" }
foreach ($id in $files.Keys) {
  foreach ($tag in "en", "ko") {
    smartthings capabilities:translation:upsert "$NS.$id" 1 -i "edge-driver/capabilities/i18n/$($files[$id]).$tag.json"
  }
}
```

- 명령 이름은 공식 문서 표기(`capabilities:translation:upsert`, 단수)를 따랐다. 버전 인자 `1`이 필요한지는 확인 못 했다. 오류가 나면 `smartthings capabilities:translation:upsert --help`로 인자를 확인한다.
- 번역을 올리지 않으면 상세 화면과 루틴의 항목 이름이 비거나 `{{i18n.label}}` 그대로 보일 수 있다 (미확인).

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
| `capabilities/i18n/*.{en,ko}.json` | 항목 이름 번역 (영어, 한국어) |

`tests/test_edge_driver.py`가 capability 이름에서 만든 id, presentation id, 프로파일, Lua의 이름이 서로 맞는지와 Lua가 읽는 응답 필드가 `/hub/status`에 있는지 확인한다. Lua 자체는 PC에서 실행해 보지 않았다.

## 근거와 확인 못 한 점

근거로 삼은 자료

- 기기 생성 `driver:try_create_device({type = "LAN", device_network_id, label, profile, manufacturer, model, vendor_provided_label})`, discovery 함수 모양: [Driver 문서](https://developer.smartthings.com/docs/edge-device-drivers/driver.html), 공식 예제 [wemo 드라이버](https://github.com/SmartThingsCommunity/SmartThingsEdgeDrivers/tree/main/drivers/SmartThings/wemo)
- HTTP 요청 `cosock.asyncify "socket.http"`, `ltn12.sink.table`, 반환값 `success, code, headers, status`: [LAN 기기 문서](https://developer.smartthings.com/docs/devices/hub-connected/lan)
- 타이머 `device.thread:call_on_schedule`, `call_with_delay`, `cancel_timer`: [Thread 문서](https://developer.smartthings.com/docs/edge-device-drivers/thread.html), wemo 예제
- 설정(preferences) 문법, `infoChanged`의 `args.old_st_store.preferences`: [Preferences 문서](https://developer.smartthings.com/docs/devices/preferences)
- capability JSON, presentation 생성 명령: [Custom Capabilities](https://developer.smartthings.com/docs/devices/capabilities/custom-capabilities), [Capability Presentations](https://developer.smartthings.com/docs/devices/capabilities/capability-presentations)
- CLI 명령 이름: [smartthings-cli](https://github.com/SmartThingsCommunity/smartthings-cli)
- 구조 참고 (코드 복사 없음): toddaustin07의 [PCControl](https://github.com/toddaustin07/PCControl), [HTTPDevices](https://github.com/toddaustin07/HTTPDevices) (Apache-2.0). discovery에서 LAN 검색 없이 `try_create_device`로 기기 하나를 만드는 방식, 설정의 `IP:포트` 문자열, `capabilities["<namespace>.<id>"]` 참조, 프로파일에 `vid`·`mnmn` 없이 커스텀 capability를 쓰는 방식이 같다
- 폴링 방식: 허브가 LAN 기기의 로컬 JSON을 60초마다 한 번씩 읽는 커뮤니티 드라이버(Davis AirLink)와 같은 구조. RPi에서 허브로 밀어 넣는 방식은 만들지 않는다

추정이거나 확인 못 한 점

- JSON 모듈 경로: 문서에 경로가 없다. `st.json`을 먼저 쓰고 없으면 `dkjson`을 쓰게 했다.
- 커스텀 capability의 숫자 속성에 `unit`을 두는 스키마: 표준 capability와 같은 모양(`unit`에 `enum`)으로 썼다. 공식 예제는 문자열 속성만 있었다. CPU 온도 단위는 `C`로 두고 표시 형식에서 `°`를 붙였다(`52.6°C`로 보일 것으로 추정).
- presentation의 `detailView` `displayType: "state"`와 `state.label`, 루틴 조건의 `numberField`·`textField` 세부 키: 문서의 표시 형식 목록에는 있으나 예제로는 확인 못 했다. 생성 명령이 거부하면 오류 문구에 맞춰 고친다.
- 한국어 이름: 번역 파일로 올리는 방식은 문서에 있으나 실제 앱에서 보지 못했다. 위 [항목 이름과 한국어](#항목-이름과-한국어)의 미확인 항목 참고.
- 프로파일의 `metadata`(`vid`, `mnmn`): 넣지 않았다. 위 커뮤니티 드라이버들도 넣지 않았다. 그래도 앱 상세 화면에 커스텀 항목이 안 보이면 device configuration을 만들어 `vid`를 넣어야 할 수 있다.
- 설정 이름 길이 제한, `edge:channels:create`의 옵션: 확인 못 했다. 위 명령은 대화형으로 쓰는 것을 전제로 했다.
- `try_create_device`를 discovery 안에서 바로 부르는 방식은 문서의 함수 정의와 예제에 맞지만, LAN 검색 없이 쓰는 공식 예제는 찾지 못했다.
- Lua 코드는 허브나 PC에서 실행해 보지 않았다. 첫 설치 때 logcat으로 확인한다.
