# SmartThings 앱에서 RPi 상태 보기 (Edge LAN 드라이버)

SmartThings Hub V3에 설치하는 Edge 드라이버다. 허브가 집 안 LAN에서 RPi 대시보드 서버의 `GET /hub/status`를 주기적으로(기본 60초) 읽어 SmartThings 앱의 기기 하나로 보여 준다.

- RPi의 SmartThings CLI 호출은 늘지 않는다. 허브가 RPi를 직접 읽는다.
- 드라이버는 읽기만 한다. RPi에 쓰는 요청이나 기기 제어 명령이 없다.
- `/hub/status`는 로그인이 없는 대신 설정에 적은 허브 IP에서만 열린다. 응답에는 기기·방 이름, 오류 문구, 비밀값이 없다.

## 앱에 보이는 항목

| 앱 이름 (한국어) | 영문 이름 | capability | 속성 | 값 |
|---|---|---|---|---|
| 열림/닫힘 (기기 대표 상태) | Contact | `contactSensor` (표준) | `contact` | 닫힘 = 수집 정상, 열림 = 이상 |
| CPU 온도 | CPU Temperature | `<namespace>.cpuTemperature` | `cpuTemperature` | 정수 °C, 1분 평균, 범위 -20~125 |
| CPU 클럭 | CPU Clock | `<namespace>.cpuClock` | `cpuClock` | MHz |
| CPU 사용률 | CPU Usage | `<namespace>.cpuUsage` | `cpuUsage` | 정수 %, 최근 5분 평균 |
| 수집 상태 | Collect Status | `<namespace>.collectStatus` | `collectStatus` | `정상`, `이력 지연 20분`, `수집 멈춤 45분`, `재인증 필요`, `수집 기록 없음`, `RPi 응답 없음` |
| 마지막 수집 시각 | Last Collect Time | `<namespace>.lastCollectTime` | `lastCollectTime` | `11-13 14:05` (KST, 월-일 시:분). 수집이 이상일 때만 갱신 |
| 디스크 사용량 | Disk Usage | `<namespace>.diskUsage` | `diskUsage` | % (DB가 있는 파일 시스템) |
| 지금 최신화 | Update Now | `<namespace>.updateNow` | — (명령 `update`만) | 기기 타일과 상세 화면의 버튼. 누르면 모든 값을 바로 다시 보냄 |
| 새로고침 | Refresh | `refresh` (표준) | — | 상세 화면을 아래로 당기면 `지금 최신화`와 같은 동작 |

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
- 앱 기록을 줄이려고 의미 있는 변화만 보낸다. 기준은 `src/init.lua` 맨 위 상수다.

| 항목 | RPi가 주는 값 | 보내는 조건 | 보내는 값 |
|---|---|---|---|
| CPU 온도 | 끝난 1분의 평균 | 마지막으로 보낸 값과 1°C 이상 차이 (`TEMP_STEP_C`) | 정수 °C로 반올림 |
| CPU 사용률 | 끝난 1분 요약 최근 5개의 평균 | 마지막으로 보낸 값과 5%p 이상 차이 (`CPU_STEP_PCT`) | 정수 %로 반올림 |
| 디스크 사용량 | 현재 | 1%p 이상 차이 (`DISK_STEP_PCT`) | 소수 첫째 자리 |
| CPU 클럭 | 5초 순간값 | 1MHz 이상 차이 (`CLOCK_STEP_MHZ`) | MHz |
| 마지막 수집 시각 | 마지막 수집 성공 시각 | 수집이 정상인 동안은 보내지 않음. 정상이 아니게 되면 바로, 이상이 이어지는 동안 바뀔 때마다, 정상으로 돌아오면 한 번 | 문구 |
| 수집 상태, 열림/닫힘 | 판정 결과 | 바뀔 때 바로 | 문구, 열림/닫힘 |

- 비교 기준은 "마지막으로 보낸 값"이다. 예: 57°C를 보낸 뒤 57.4 ↔ 57.9로 흔들려도 보내지 않고, 58.0이 되면 58을 보낸다.
- 이상 판단: RPi의 `collect_ok`가 `false`이거나, 허브가 3회 연속 읽지 못함. RPi가 응답하지 않을 때는 새 시각이 없어 마지막으로 보낸 시각을 한 번 다시 보낸다.
- 마지막으로 보낸 값은 `device:set_field(..., { persist = true })`로 저장해 드라이버·허브 재시작 뒤에도 같은 값을 다시 올리지 않는다. 이 판으로 처음 바꿀 때는 저장된 값이 없어 항목마다 한 번씩 보낸다.
- 처음 기기를 만들었을 때는 마지막 수집 시각도 한 번 보낸다 (빈 칸으로 두지 않게).

### 지금 최신화 (타일 버튼, 새로고침)

- 기기 타일의 버튼(`updateNow`)을 누르거나 상세 화면을 아래로 당기면(`refresh`) `/hub/status`를 바로 읽고, 위 표의 기준과 상관없이 모든 항목(열림/닫힘, 수집 상태, 마지막 수집 시각, CPU 온도·클럭·사용률, 디스크)을 보낸다.
- 보낸 값은 `sent_*`에 저장돼 다음 주기 읽기의 비교 기준이 된다. 온도와 사용률은 이때도 정수로 반올림한다.
- 5초(`FORCE_MIN_INTERVAL_S`) 안에 다시 누르면 RPi를 다시 읽지 않는다.
- 이때 읽기에 실패하면 로그만 남기고 실패 횟수는 늘리지 않는다. 몇 번 연달아 눌렀다고 열림(이상) 알림이 앞당겨지지 않게, 알림은 주기 읽기(3회 연속 실패)로만 판단한다.
- 값이 앱에 이미 있는 값과 같으면 앱 화면은 그대로다. 같은 값의 이벤트를 앱 기록에 남기는지는 확인 못 했다 (`state_change` 옵션은 쓰지 않음).
- 이 버튼은 우리 가상 기기의 명령일 뿐이다. RPi에는 `GET /hub/status` 읽기만 하고, RPi나 다른 SmartThings 기기를 제어하지 않는다.

**타일 버튼 방식을 고른 근거**

- 타일 버튼은 capability presentation의 `dashboard.actions`에 `displayType: "pushButton"`을 두어 만든다. 대시보드 동작으로 쓸 수 있는 표시 형식에 `pushButton`이 있다 ([Capability Presentations](https://developer.smartthings.com/docs/devices/capabilities/capability-presentations), [Display Types](https://developer.smartthings.com/docs/devices/capabilities/display-types)).
- 표준 `momentary`를 쓰지 않은 이유: 문서의 capability 목록 페이지가 브라우저에서만 그려져 `momentary`의 presentation에 타일 버튼이 있는지 확인하지 못했다. 커스텀 capability는 presentation을 직접 쓰므로 버튼 모양과 한국어 이름(`지금 최신화`)을 정할 수 있다.
- 표준 `refresh`는 그대로 둔다 (상세 화면 당겨서 새로고침).

**타일 표시에 미치는 영향 (추정)**

- 프로파일 순서: `contactSensor` → `updateNow` → 나머지. `updateNow`는 표시할 상태가 없는 명령 전용이라, 타일 상태는 지금처럼 열림/닫힘이 남는 것을 노렸다.
- 기기 설정(vid)이 없을 때 앱이 타일의 상태와 버튼을 어느 capability에서 가져오는지는 문서에 없다. 문서에는 "프로파일 순서대로 표시가 자동 생성된다"는 말만 있다 ([Device Configurations](https://developer.smartthings.com/docs/devices/configurations-and-presentations/device-configurations)).
- 그래서 설치 뒤 확인할 것: ① 타일에 버튼이 보이는지 ② 타일 상태가 열림/닫힘 그대로인지 ③ 상세 화면에 `지금 최신화` 버튼이 보이는지. 어긋나면 아래 [타일 버튼이 안 보일 때](#타일-버튼이-안-보일-때)로 기기 설정을 만든다.

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

### 1. 커스텀 capability 7개 만들기

```powershell
smartthings capabilities:create -i edge-driver/capabilities/cpu-temperature.json
smartthings capabilities:create -i edge-driver/capabilities/cpu-clock.json
smartthings capabilities:create -i edge-driver/capabilities/cpu-usage.json
smartthings capabilities:create -i edge-driver/capabilities/collect-status.json
smartthings capabilities:create -i edge-driver/capabilities/last-collect-time.json
smartthings capabilities:create -i edge-driver/capabilities/disk-usage.json
smartthings capabilities:create -i edge-driver/capabilities/update-now.json
```

- 결과의 id가 `<namespace>.cpuTemperature`, `cpuClock`, `cpuUsage`, `collectStatus`, `lastCollectTime`, `diskUsage`, `updateNow`인지 확인한다. 다르면 프로파일·Lua·presentation·번역 파일의 id를 결과에 맞춘다.
- 앞서 시험으로 만든 `cpuClockTest`는 쓰지 않는다. 이번 `cpuClock`과 이름이 다르므로 겹치지 않는다.

### 2. capability presentation 7개 만들기

```powershell
smartthings capabilities:presentation:create "$NS.cpuTemperature" --capability-version 1 -i edge-driver/capabilities/cpu-temperature.presentation.json
smartthings capabilities:presentation:create "$NS.cpuClock" --capability-version 1 -i edge-driver/capabilities/cpu-clock.presentation.json
smartthings capabilities:presentation:create "$NS.cpuUsage" --capability-version 1 -i edge-driver/capabilities/cpu-usage.presentation.json
smartthings capabilities:presentation:create "$NS.collectStatus" --capability-version 1 -i edge-driver/capabilities/collect-status.presentation.json
smartthings capabilities:presentation:create "$NS.lastCollectTime" --capability-version 1 -i edge-driver/capabilities/last-collect-time.presentation.json
smartthings capabilities:presentation:create "$NS.diskUsage" --capability-version 1 -i edge-driver/capabilities/disk-usage.presentation.json
smartthings capabilities:presentation:create "$NS.updateNow" --capability-version 1 -i edge-driver/capabilities/update-now.presentation.json
```

### 2-1. 번역 올리기 (영어, 한국어)

capability마다 영어와 한국어 번역을 하나씩 올린다. 휴대폰 언어가 한국어면 한국어 이름이 보인다.

```powershell
$files = @{ cpuTemperature = "cpu-temperature"; cpuClock = "cpu-clock"; cpuUsage = "cpu-usage";
            collectStatus = "collect-status"; lastCollectTime = "last-collect-time"; diskUsage = "disk-usage";
            updateNow = "update-now" }
foreach ($id in $files.Keys) {
  foreach ($tag in "en", "ko") {
    smartthings capabilities:translations:upsert "$NS.$id" --capability-version 1 -i "edge-driver/capabilities/i18n/$($files[$id]).$tag.json"
  }
}
```

- CLI의 명령 이름은 `capabilities:translations:upsert`(복수)이고, 버전은 위치 인자가 아니라 `--capability-version` 옵션으로 준다 (2026-11-13 CLI `--help`로 확인). `presentation:create`도 같다.
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

- 각 명령은 드라이버, 채널, 허브를 목록에서 고르게 한다. 고르지 않고 넘어가면 아무것도 바뀌지 않는다.
- 순서: 채널에 드라이버 배정 → 허브를 채널에 등록 → 허브에 드라이버 설치.
- 목록에서 고르는 대신 id를 직접 넣을 수도 있다. 드라이버 id는 `smartthings edge:drivers`, 채널 id는 `smartthings edge:channels`, 허브 id는 `smartthings devices`에서 종류가 허브인 기기의 id다.

```powershell
smartthings edge:channels:assign <driver-id> --channel <channel-id>
smartthings edge:channels:enroll <hub-id> --channel <channel-id>
smartthings edge:drivers:install <driver-id> --hub <hub-id> --channel <channel-id>
```

- 반영 확인: `smartthings edge:channels:drivers <channel-id>`에 드라이버가, `smartthings edge:channels:enrollments <hub-id>`에 채널이, `smartthings edge:drivers:installed --hub <hub-id>`에 드라이버가 보여야 한다.
- 드라이버를 고친 뒤에는 4단계와 `edge:channels:assign`만 다시 하면 허브가 새 버전을 받는다 (반영까지 몇 분에서 몇 시간 걸릴 수 있음, 미확인). 기다리지 않으려면 `edge:drivers:install`을 다시 실행한다.

### 이미 설치한 뒤 `지금 최신화` 버튼만 더할 때

capability 6개와 채널·허브 등록이 이미 끝났다면 아래만 실행한다. `<driver-id>`, `<channel-id>`, `<hub-id>`는 5단계와 같다.

```powershell
smartthings capabilities:create -i edge-driver/capabilities/update-now.json
smartthings capabilities:presentation:create "$NS.updateNow" --capability-version 1 -i edge-driver/capabilities/update-now.presentation.json
smartthings capabilities:translations:upsert "$NS.updateNow" --capability-version 1 -i edge-driver/capabilities/i18n/update-now.en.json
smartthings capabilities:translations:upsert "$NS.updateNow" --capability-version 1 -i edge-driver/capabilities/i18n/update-now.ko.json
smartthings edge:drivers:package edge-driver
smartthings edge:channels:assign <driver-id> --channel <channel-id>
smartthings edge:drivers:install <driver-id> --hub <hub-id> --channel <channel-id>
```

- 첫 줄 결과의 id가 `<namespace>.updateNow`인지 확인한다.
- 프로파일 이름(`stcollector-rpi.v1`)은 그대로라 기기를 지우고 다시 만들 필요는 없을 것으로 본다 (미확인). 새 버전 설치 뒤에도 앱에 버튼이 안 보이면 앱을 다시 열고, 그래도 없으면 기기를 지우고 6단계로 다시 만든다.
- 새 capability를 만든 직후에는 presentation 반영에 시간이 걸릴 수 있다는 보고가 있다 (미확인).

### 타일 버튼이 안 보일 때

기기 설정(device configuration)을 만들어 타일의 상태와 버튼을 직접 정한다. 문서의 밝기 조절기 예처럼 `dashboard.states`와 `dashboard.actions`에 서로 다른 capability를 넣을 수 있다 ([Device Configurations](https://developer.smartthings.com/docs/devices/configurations-and-presentations/device-configurations)).

1. 지금 프로파일에서 기기 설정 틀을 만든다. 프로파일 id는 `smartthings deviceprofiles`에서 찾는다.

   ```powershell
   smartthings presentation:device-config:generate <profile-id> -o edge-driver/device-config.json
   ```

2. 만든 파일의 `dashboard`를 아래처럼 고친다 (상태 = 열림/닫힘, 버튼 = 지금 최신화). `detailView`, `automation`은 생성된 그대로 둔다.

   ```json
   "dashboard": {
     "states": [{ "component": "main", "capability": "contactSensor", "version": 1 }],
     "actions": [{ "component": "main", "capability": "<namespace>.updateNow", "version": 1 }]
   }
   ```

3. 기기 설정을 올리고, 결과의 `vid`를 프로파일에 넣는다. Edge 드라이버는 `mnmn` 없이 `vid`만 넣으면 된다고 문서에 있다 ([Device Presentations](https://developer.smartthings.com/docs/devices/configurations-and-presentations/device-presentations)).

   ```powershell
   smartthings presentation:device-config:create -i edge-driver/device-config.json
   ```

   ```yaml
   # profiles/stcollector-rpi.yml 맨 아래
   metadata:
     vid: <vid>
   ```

4. 4~5단계(package, assign, install)를 다시 한다.

- `presentation:device-config:generate`, `create`의 정확한 인자는 확인 못 했다. 오류가 나면 `--help`로 확인한다.

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
| `capabilities/*.json` | 커스텀 capability 정의 (`update-now.json`은 타일 버튼용 명령만 있음) |
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
- `updateNow`: 속성 없이 명령만 있는 커스텀 capability(`attributes: {}`)가 `capabilities:create`에서 받아들여지는지, `pushButton`의 `command`에 인자 없는 명령 이름만 써도 되는지(문서 예는 `argument: "push"`를 함께 씀), 상세 화면(`detailView`)에서 `pushButton`을 쓸 수 있는지, 번역 파일의 `commands` 항목 모양은 확인 못 했다. 생성 명령이 거부하면 오류 문구에 맞춰 고친다.
- 기기 설정 없이 타일의 상태·버튼이 어느 capability에서 오는지: 문서에 없다. 위 [지금 최신화](#지금-최신화-타일-버튼-새로고침)의 확인 항목 참고.
