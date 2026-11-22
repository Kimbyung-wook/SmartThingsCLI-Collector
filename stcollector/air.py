"""공기질 측정 항목 (PLAN §15).

SmartThings 표준 capability 기준으로 범용으로 둔다. 실제 기기가 어떤 capability와 단위로
보고하는지는 연결 뒤 CLI 읽기 명령으로 확인해 맞춘다.

- 집계 테이블은 (기기, 속성)으로 묶으므로, 같은 PM2.5 가 dustSensor.fineDustLevel 과
  fineDustSensor.fineDustLevel 로 함께 와도 한 항목(fineDustLevel)이 된다.
  한 기기가 둘 다 보내면 집계는 capability 이름순 첫 번째(dustSensor)를 쓴다.
- 단위는 이벤트의 unit 을 그대로 저장하고 표시한다 (TVOC·HCHO 는 기기마다 다를 수 있다).
- *HealthConcern 은 기기가 주는 등급(문자)이다. 집계하지 않고 최신값만 보여 준다.
"""

from __future__ import annotations

# 측정값 capability -> 속성
AIR_CAPABILITIES: dict[str, list[str]] = {
    "dustSensor": ["dustLevel", "fineDustLevel"],
    "fineDustSensor": ["fineDustLevel"],
    "veryFineDustSensor": ["veryFineDustLevel"],
    "carbonDioxideMeasurement": ["carbonDioxide"],
    "carbonMonoxideMeasurement": ["carbonMonoxideLevel"],
    "tvocMeasurement": ["tvocLevel"],
    "formaldehydeMeasurement": ["formaldehydeLevel"],
    "airQualitySensor": ["airQuality"],
}

# 측정 항목(속성) -> (표시 이름, 기기가 단위를 주지 않을 때 쓸 단위). 화면에 이 순서로 놓는다.
AIR_METRICS: dict[str, tuple[str, str | None]] = {
    "veryFineDustLevel": ("PM1.0", "μg/m^3"),
    "fineDustLevel": ("PM2.5", "μg/m^3"),
    "dustLevel": ("PM10", "μg/m^3"),
    "carbonDioxide": ("CO2", "ppm"),
    "carbonMonoxideLevel": ("CO", "ppm"),
    "tvocLevel": ("TVOC", None),
    "formaldehydeLevel": ("HCHO", None),
    "airQuality": ("공기질 지수", None),
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

_FINE_UNITS = {"ppm", "mg/m^3", "mg/m3", "mg/m³"}


def air_digits(attribute: str, unit: str | None) -> int:
    """표시 자릿수. ppm·mg/m³ 로 오는 TVOC·HCHO 는 작은 값이라 소수 셋째 자리까지 쓴다."""
    if attribute in ("tvocLevel", "formaldehydeLevel"):
        return 3 if (unit or "").strip() in _FINE_UNITS else 0
    if attribute == "carbonMonoxideLevel":
        return 1
    return 0
