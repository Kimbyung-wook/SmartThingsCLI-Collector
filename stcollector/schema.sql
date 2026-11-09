-- stcollector 스키마 v1
-- 시각은 모두 UTC epoch 밀리초(ts_ms)로 저장하고, 화면에서만 KST로 변환한다.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS devices (
    device_id         TEXT PRIMARY KEY,
    label             TEXT,
    name              TEXT,
    room_id           TEXT,
    room_name         TEXT,
    location_id       TEXT,
    capabilities_json TEXT NOT NULL,   -- 수집 대상 capability 목록 (JSON 배열)
    updated_at_ms     INTEGER NOT NULL
);

-- 원본 측정값. (기기, 컴포넌트, capability, 속성, 보고 시각)이 같으면 한 번만 저장한다.
CREATE TABLE IF NOT EXISTS readings (
    device_id   TEXT    NOT NULL,
    component   TEXT    NOT NULL,
    capability  TEXT    NOT NULL,
    attribute   TEXT    NOT NULL,
    ts_ms       INTEGER NOT NULL,
    value_num   REAL,                   -- 숫자 값 (온도, 습도, 조도, 전력 ...)
    value_text  TEXT,                   -- 문자 값 (motion: active/inactive, switch: on/off ...)
    unit        TEXT,
    source      TEXT    NOT NULL,       -- 'history' | 'status'
    event_hash  INTEGER,                -- 이력 이벤트의 hash (status 방식은 NULL)
    PRIMARY KEY (device_id, component, capability, attribute, ts_ms)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings (ts_ms);

-- 이벤트 이력 방식: 기기별로 어디까지 받았는지 기록
CREATE TABLE IF NOT EXISTS cursors (
    device_id     TEXT PRIMARY KEY,
    last_epoch_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

-- ---- 스키마 v2: 집계 (M2) ---------------------------------------------------
-- bucket_start 는 구간 시작의 UTC epoch 밀리초. 일·주 구간은 KST 자정에서 시작한다.
-- last_val 은 구간 끝에 유지되던 값 (누적 에너지 계산용).

-- 새 측정값이 들어온 시간대. 트리거가 채우고 집계 작업이 비운다.
CREATE TABLE IF NOT EXISTS rollup_dirty (
    device_id  TEXT    NOT NULL,
    component  TEXT    NOT NULL,
    capability TEXT    NOT NULL,
    attribute  TEXT    NOT NULL,
    hour_ms    INTEGER NOT NULL,
    PRIMARY KEY (device_id, component, capability, attribute, hour_ms)
) WITHOUT ROWID;

-- 집계 작업이 처리 중인 시간대. 처리 도중 들어온 측정값은 rollup_dirty 에 새로 쌓인다.
CREATE TABLE IF NOT EXISTS rollup_claimed (
    device_id  TEXT    NOT NULL,
    component  TEXT    NOT NULL,
    capability TEXT    NOT NULL,
    attribute  TEXT    NOT NULL,
    hour_ms    INTEGER NOT NULL,
    PRIMARY KEY (device_id, component, capability, attribute, hour_ms)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS trg_readings_dirty AFTER INSERT ON readings
BEGIN
    INSERT OR IGNORE INTO rollup_dirty(device_id, component, capability, attribute, hour_ms)
    VALUES (NEW.device_id, NEW.component, NEW.capability, NEW.attribute,
            NEW.ts_ms - (NEW.ts_ms % 3600000));
END;

-- 집계가 아는 측정 항목 목록. readings 를 훑지 않기 위해 따로 둔다.
CREATE TABLE IF NOT EXISTS rollup_series (
    device_id  TEXT NOT NULL,
    component  TEXT NOT NULL,
    capability TEXT NOT NULL,
    attribute  TEXT NOT NULL,
    unit       TEXT,
    PRIMARY KEY (device_id, attribute, component, capability)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rollup_hour (
    device_id    TEXT    NOT NULL,
    attribute    TEXT    NOT NULL,
    bucket_start INTEGER NOT NULL,
    avg          REAL    NOT NULL,   -- 시간 가중 평균
    min          REAL    NOT NULL,
    max          REAL    NOT NULL,
    last_val     REAL    NOT NULL,
    n            INTEGER NOT NULL,   -- 구간 안의 보고 건수
    coverage     REAL    NOT NULL,   -- 0~1, 값이 알려져 있던 시간의 비율
    PRIMARY KEY (device_id, attribute, bucket_start)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rollup_day (
    device_id    TEXT    NOT NULL,
    attribute    TEXT    NOT NULL,
    bucket_start INTEGER NOT NULL,
    avg          REAL    NOT NULL,
    min          REAL    NOT NULL,
    max          REAL    NOT NULL,
    last_val     REAL    NOT NULL,
    n            INTEGER NOT NULL,
    coverage     REAL    NOT NULL,
    PRIMARY KEY (device_id, attribute, bucket_start)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS rollup_week (
    device_id    TEXT    NOT NULL,
    attribute    TEXT    NOT NULL,
    bucket_start INTEGER NOT NULL,
    avg          REAL    NOT NULL,
    min          REAL    NOT NULL,
    max          REAL    NOT NULL,
    last_val     REAL    NOT NULL,
    n            INTEGER NOT NULL,
    coverage     REAL    NOT NULL,
    PRIMARY KEY (device_id, attribute, bucket_start)
) WITHOUT ROWID;

-- 조도 구간별 체류 시간(초). 구간 번호는 rollup.LUX_LABELS 의 순서.
CREATE TABLE IF NOT EXISTS lux_hist_hour (
    device_id    TEXT    NOT NULL,
    bucket_start INTEGER NOT NULL,
    lux_bin      INTEGER NOT NULL,
    seconds      REAL    NOT NULL,
    PRIMARY KEY (device_id, bucket_start, lux_bin)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS occupancy_hour (
    device_id        TEXT    NOT NULL,
    bucket_start     INTEGER NOT NULL,
    occupied_seconds REAL    NOT NULL,
    known_seconds    REAL    NOT NULL,   -- 재실 여부를 알고 있던 시간
    PRIMARY KEY (device_id, bucket_start)
) WITHOUT ROWID;

-- 재실 중 조도 분포. 재실 센서와 같은 방(또는 같은 기기)의 조도 센서를 짝지어 계산한다.
CREATE TABLE IF NOT EXISTS lux_occ_hour (
    occ_device_id TEXT    NOT NULL,
    lux_device_id TEXT    NOT NULL,
    bucket_start  INTEGER NOT NULL,
    lux_bin       INTEGER NOT NULL,
    seconds       REAL    NOT NULL,
    PRIMARY KEY (occ_device_id, lux_device_id, bucket_start, lux_bin)
) WITHOUT ROWID;
