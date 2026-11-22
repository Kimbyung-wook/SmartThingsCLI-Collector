"""공기질 측정 항목 (PLAN §15). 항목 정의는 이 파일에만 둔다.

기준 기기는 PV28-CW (Edge 드라이버, 프로필 pv28cw-air-quality, component main)이고,
다른 공기질 기기도 받을 수 있게 SmartThings 표준 capability 를 범용으로 둔다.
사용자 정의 capability 가 오면 AIR_CAPABILITIES 와 AIR_METRICS 에 줄을 더한다.

- 집계 테이블은 (기기, 속성)으로 묶으므로 PM2.5 는 fineDustSensor.fineDustLevel 과
  dustSensor.fineDustLevel 어느 쪽으로 와도 한 항목(fineDustLevel)이 된다.
  PV28-CW 는 같은 값을 둘 다 보낸다. 수집 단계에서 fineDustSensor 가 있는 기기의
  dustSensor.fineDustLevel 을 버리고(keep_reading), 이미 저장된 기록이 있어도 집계·표시는
  fineDustSensor 를 먼저 고른다(capability_rank).
- 단위는 이벤트의 unit 을 그대로 저장한다. 화면에서만 display_unit 으로 다듬는다 (값은 그대로).
- *HealthConcern 은 기기가 주는 등급(문자)이다. 집계하지 않고 최신값만 보여 준다 (PV28-CW 는 보내지 않음).
- 공기질 기기의 온습도는 기본으로 방별 온습도에 섞지 않는다 (설정 [dashboard] air_quality_temp_in_rooms).
"""

from __future__ import annotations

# 측정값 capability -> 속성
AIR_CAPABILITIES: dict[str, list[str]] = {
    "carbonDioxideMeasurement": ["carbonDioxide"],
    "fineDustSensor": ["fineDustLevel"],
    "dustSensor": ["dustLevel", "fineDustLevel"],
    "veryFineDustSensor": ["veryFineDustLevel"],
    "formaldehydeMeasurement": ["formaldehydeLevel"],
    "tvocMeasurement": ["tvocLevel"],
    "carbonMonoxideMeasurement": ["carbonMonoxideLevel"],  # PV28-CW 에는 없음. 다른 기기용
    "airQualitySensor": ["airQuality"],  # PV28-CW 드라이버는 보내지 않음. 다른 기기용
}

# 측정 항목(속성) -> (표시 이름, 기기가 단위를 주지 않을 때 쓸 단위, 차트 부제의 값 품질 메모).
# 화면에 이 순서로 놓는다. 메모는 PV28-CW 센서 구성 기준 (반도체식 가스 센서, PM10 은 PM2.5 에서 환산).
AIR_METRICS: dict[str, tuple[str, str | None, str | None]] = {
    "fineDustLevel": ("PM2.5", "μg/m^3", None),
    "carbonDioxide": ("CO2", "ppm", None),
    "veryFineDustLevel": ("PM1.0", "μg/m^3", None),
    "dustLevel": ("PM10", "μg/m^3", "추정값(추세용)"),
    "tvocLevel": ("TVOC", None, "추정값(추세용)"),
    "formaldehydeLevel": ("HCHO", None, "추정값(추세용)"),
    "carbonMonoxideLevel": ("CO", "ppm", None),
    "airQuality": ("공기질 지수", None, None),
}

# 등급 속성 -> 그 등급이 가리키는 측정 항목. capability 이름과 속성 이름이 같다.
AIR_GRADES: dict[str, str] = {
    "veryFineDustHealthConcern": "veryFineDustLevel",
    "fineDustHealthConcern": "fineDustLevel",
    "dustHealthConcern": "dustLevel",
    "carbonDioxideHealthConcern": "carbonDioxide",
    "carbonMonoxideHealthConcern": "carbonMonoxideLevel",
    "tvocHealthConcern": "tvocLevel",
    "formaldehydeHealthConcern": "formaldehydeLevel",
    "airQualityHealthConcern": "airQuality",
}

GRADE_TEXT = {
    "good": "좋음",
    "moderate": "보통",
    "slightlyUnhealthy": "약간 나쁨",
    "unhealthy": "나쁨",
    "veryUnhealthy": "매우 나쁨",
    "hazardous": "위험",
}

# 수집 설정 기본값에 더할 것 (측정값 + 등급)
AIR_ATTRIBUTES: dict[str, list[str]] = {**AIR_CAPABILITIES, **{g: [g] for g in AIR_GRADES}}

# 같은 속성이 여러 capability 로 올 때 고르는 순서 (작을수록 먼저). 그 밖은 이름순.
_CAPABILITY_RANK = {"fineDustSensor": 0, "dustSensor": 1}

# 화면 표시 단위. 키는 기기가 보내는 unit 문자열 (μ 는 U+03BC, µ 는 U+00B5 둘 다 받는다)
_DISPLAY_UNITS = {
    "μg/m^3": "μg/m³", "µg/m^3": "μg/m³", "ug/m3": "μg/m³", "ug/m^3": "μg/m³", "μg/m3": "μg/m³", "µg/m3": "μg/m³",
    "mg/m^3": "mg/m³", "mg/m3": "mg/m³",
}

# 온습도를 방별 온습도와 따로 볼 기기 판정: 공기질 측정값 capability 가 하나라도 있으면 공기질 기기
CLIMATE_ATTRIBUTES = ("temperature", "humidity")


def is_air_device(capabilities) -> bool:
    return any(c in AIR_CAPABILITIES for c in capabilities or ())


def capability_rank(capability: str) -> int:
    return _CAPABILITY_RANK.get(capability, 9)


def keep_reading(capabilities, component: str, capability: str, attribute: str) -> bool:
    """기기의 capability 목록을 보고 저장할지 정한다 (수집 건수 줄이기).

    - fineDustSensor 가 있는 기기의 dustSensor.fineDustLevel 은 같은 PM2.5 의 중복이라 버린다.
    - 공기질 기기의 main 이 아닌 component 의 switch 는 기기 설정(PV28-CW 의 경보음)이라 버린다.
      플러그 탭에 섞이지 않게 한다. main 의 switch(예: 공기청정기 전원)는 그대로 받는다.
    """
    caps = capabilities or ()
    if capability == "dustSensor" and attribute == "fineDustLevel" and "fineDustSensor" in caps:
        return False
    if capability == "switch" and component != "main" and is_air_device(caps):
        return False
    return True


def display_unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    u = unit.strip()
    return _DISPLAY_UNITS.get(u, u) or None


def air_digits(attribute: str, unit: str | None) -> int:
    """표시 자릿수. ppm·mg/m³ 로 오는 TVOC·HCHO 는 작은 값이라 소수 셋째 자리까지 쓴다."""
    if attribute in ("tvocLevel", "formaldehydeLevel"):
        return 3 if display_unit(unit) in ("ppm", "mg/m³") else 0
    if attribute == "carbonMonoxideLevel":
        return 1
    return 0
