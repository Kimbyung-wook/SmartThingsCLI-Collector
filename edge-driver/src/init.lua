-- stcollector RPi 상태 Edge 드라이버
--
-- 허브가 LAN 안의 RPi 대시보드 서버에서 GET /hub/status (읽기 전용 JSON)를 주기적으로 읽어
-- 기기 속성으로 표시한다. RPi 에 쓰는 요청이나 기기 제어 명령은 없다.
-- 기기는 앱의 "주변 기기 검색"을 누를 때 하나만 만든다 (LAN 검색을 하지 않음).

local Driver = require "st.driver"
local capabilities = require "st.capabilities"
local log = require "log"
local cosock = require "cosock"
local http = cosock.asyncify "socket.http"
local ltn12 = require "ltn12"

-- JSON 모듈 경로는 공식 문서에서 확인하지 못했다. st.json 을 먼저 쓰고, 없으면 dkjson 을 쓴다.
local json
do
  local ok, mod = pcall(require, "st.json")
  if ok and mod then
    json = mod
  else
    json = require "dkjson"
  end
end

local NS = "angelconcert61717"
local capCollect = capabilities[NS .. ".collectStatus"]
local capLast = capabilities[NS .. ".lastCollectTime"]
local capTemp = capabilities[NS .. ".cpuTemperature"]
local capClock = capabilities[NS .. ".cpuClock"]
local capCpu = capabilities[NS .. ".cpuUsage"]
local capDisk = capabilities[NS .. ".diskUsage"]

local DEVICE_NETWORK_ID = "stcollector-rpi-status"
local PROFILE = "stcollector-rpi.v1"
local STATUS_PATH = "/hub/status"
local HTTP_TIMEOUT_S = 5
local DEFAULT_POLL_S = 60
local FAILS_BEFORE_ALERT = 3   -- 연속 실패가 이 횟수에 이르면 contact 를 열림(이상)으로 바꾼다

-- 앱 기록 줄이기: "마지막으로 보낸 값"과 이만큼 이상 차이 날 때만 보낸다.
-- 비교 기준이 마지막으로 보낸 값이라, 값이 기준 근처에서 흔들려도 다시 보내지 않는다.
local TEMP_STEP_C = 1          -- CPU 온도 (/hub/status 의 1분 평균), 정수 °C 로 반올림해 보냄
local CPU_STEP_PCT = 5         -- CPU 사용률 (/hub/status 의 5분 평균), 정수 % 로 반올림해 보냄
local DISK_STEP_PCT = 1        -- 디스크 사용량 %
local CLOCK_STEP_MHZ = 1       -- CPU 클럭 MHz
-- 마지막 수집 시각: 수집이 정상인 동안은 보내지 않는다. 정상이 아니게 되면 바로 보내고,
-- 이상이 이어지는 동안 바뀔 때마다 보내고, 정상으로 돌아오면 한 번 보낸다.

-- 마지막으로 보낸 값은 persist 로 저장해 드라이버·허브 재시작 뒤에도 같은 값을 다시 올리지 않는다.
local PERSIST = { persist = true }

-- 앞뒤 공백, http:// 와 끝의 / 를 떼고 "호스트[:포트]" 형태만 받는다.
local function clean_address(raw)
  if type(raw) ~= "string" then return nil end
  local addr = raw:gsub("^%s+", ""):gsub("%s+$", ""):gsub("^[Hh][Tt][Tt][Pp]://", ""):gsub("/+$", "")
  if addr:match("^[%w%.%-]+$") or addr:match("^[%w%.%-]+:%d+$") then
    return addr
  end
  return nil
end

local function poll_seconds(device)
  local v = tonumber(device.preferences.pollSeconds)
  if not v or v < 30 then return DEFAULT_POLL_S end
  return math.floor(v)
end

local function fetch(addr)
  local chunks = {}
  local ok, code = http.request({
    url = "http://" .. addr .. STATUS_PATH,
    method = "GET",
    sink = ltn12.sink.table(chunks),
    create = function()
      local sock = cosock.socket.tcp()
      sock:settimeout(HTTP_TIMEOUT_S)
      return sock
    end,
  })
  if not ok then
    return nil, "연결 실패: " .. tostring(code)
  end
  if code ~= 200 then
    return nil, "HTTP " .. tostring(code)
  end
  local decoded = json.decode(table.concat(chunks))
  if type(decoded) ~= "table" then
    return nil, "응답 형식 오류"
  end
  return decoded
end

local function num(v)
  if type(v) == "number" and v == v then return v end
  return nil
end

local function round(v)
  return math.floor(v + 0.5)
end

-- 마지막으로 보낸 값과 다르거나(숫자는 threshold 이상 차이) force 일 때만 이벤트를 보낸다.
-- rounder 가 있으면 비교는 원래 값으로 하고, 보내는 값은 반올림한 값이다.
local function emit_changed(device, key, value, threshold, make_event, rounder, force)
  if value == nil then return end
  local field = "sent_" .. key
  local last = device:get_field(field)
  if last ~= nil and not force then
    if type(value) == "number" and type(last) == "number" then
      if math.abs(value - last) < (threshold or 0) or value == last then return end
    elseif value == last then
      return
    end
  end
  local out = rounder and rounder(value) or value
  device:set_field(field, out, PERSIST)
  device:emit_event(make_event(out))
end

local function emit_last_collect(device, text)
  return emit_changed(device, "last", text, nil, function(v) return capLast.lastCollectTime({ value = v }) end)
end

-- 마지막 수집 시각. collect_ok 가 false 이거나 RPi 가 응답하지 않으면 이상으로 본다.
local function update_last_collect(device, ok, text)
  local was_bad = device:get_field("collect_bad") == true
  if ok then
    if was_bad or device:get_field("sent_last") == nil then  -- 정상으로 돌아옴(같은 문구여도 한 번), 또는 처음 한 번
      emit_changed(device, "last", text or "-", nil, function(v) return capLast.lastCollectTime({ value = v }) end, nil, true)
    end
  elseif not was_bad then  -- 정상이 아니게 됨: 같은 문구여도 바로 보냄 (RPi 무응답이면 마지막으로 알던 시각)
    emit_changed(device, "last", text or device:get_field("sent_last") or "-", nil, function(v) return capLast.lastCollectTime({ value = v }) end, nil, true)
  elseif text then  -- 이상이 이어지는 동안은 바뀔 때마다
    emit_last_collect(device, text)
  end
  if was_bad ~= not ok then
    device:set_field("collect_bad", not ok, PERSIST)
  end
end

local function set_alert(device, alert)
  emit_changed(device, "contact", alert and "open" or "closed", nil, function(v)
    if v == "open" then return capabilities.contactSensor.contact.open() end
    return capabilities.contactSensor.contact.closed()
  end)
end

local function poll(device)
  local addr = clean_address(device.preferences.rpiAddress)
  if not addr then
    log.warn("RPi 주소 설정이 올바르지 않습니다: " .. tostring(device.preferences.rpiAddress))
    emit_changed(device, "collect", "RPi 주소 설정 확인", nil, function(v) return capCollect.collectStatus({ value = v }) end)
    return
  end
  local data, err = fetch(addr)
  if not data then
    local fails = (device:get_field("fails") or 0) + 1
    device:set_field("fails", fails)
    log.warn(string.format("RPi 상태 읽기 실패 %d회: %s", fails, err))
    if fails >= FAILS_BEFORE_ALERT then
      set_alert(device, true)
      emit_changed(device, "collect", "RPi 응답 없음", nil, function(v) return capCollect.collectStatus({ value = v }) end)
      update_last_collect(device, false, nil)  -- 새 시각은 없으므로 이상이 된 순간에만 마지막 값을 보냄
    end
    return
  end
  device:set_field("fails", 0)

  set_alert(device, data.collect_ok ~= true)
  if type(data.collect_text) == "string" then
    emit_changed(device, "collect", data.collect_text, nil, function(v) return capCollect.collectStatus({ value = v }) end)
  end
  update_last_collect(device, data.collect_ok == true,
    type(data.last_ok_text) == "string" and data.last_ok_text or "-")
  emit_changed(device, "temp", num(data.cpu_temp_c), TEMP_STEP_C,
    function(v) return capTemp.cpuTemperature({ value = v, unit = "C" }) end, round)
  emit_changed(device, "clock", num(data.cpu_mhz), CLOCK_STEP_MHZ,
    function(v) return capClock.cpuClock({ value = v, unit = "MHz" }) end)
  emit_changed(device, "cpu", num(data.cpu_pct), CPU_STEP_PCT,
    function(v) return capCpu.cpuUsage({ value = v, unit = "%" }) end, round)
  emit_changed(device, "disk", num(data.disk_used_pct), DISK_STEP_PCT,
    function(v) return capDisk.diskUsage({ value = v, unit = "%" }) end)
end

local function start_polling(device)
  local old = device:get_field("timer")
  if old then
    device.thread:cancel_timer(old)
  end
  local interval = poll_seconds(device)
  local timer = device.thread:call_on_schedule(interval, function() poll(device) end, "rpi-status-poll")
  device:set_field("timer", timer)
  device.thread:call_with_delay(2, function() poll(device) end, "rpi-status-first")
  log.info(string.format("RPi 상태 읽기 시작: %s, %d초 주기", tostring(device.preferences.rpiAddress), interval))
end

local function device_init(driver, device)
  start_polling(device)
end

local function device_added(driver, device)
  -- 첫 읽기 전에는 이상(열림)으로 두지 않는다. 연속 실패가 쌓이면 poll 이 바꾼다.
  set_alert(device, false)
end

local function info_changed(driver, device, event, args)
  local old = (args and args.old_st_store and args.old_st_store.preferences) or {}
  if old.rpiAddress ~= device.preferences.rpiAddress or old.pollSeconds ~= device.preferences.pollSeconds then
    device:set_field("fails", 0)
    start_polling(device)
  end
end

local function device_removed(driver, device)
  local timer = device:get_field("timer")
  if timer then
    device.thread:cancel_timer(timer)
  end
end

local function handle_refresh(driver, device)
  poll(device)
end

-- 앱의 "주변 기기 검색"에서 호출된다. 이미 있으면 아무것도 하지 않는다.
local function discovery(driver, _, should_continue)
  for _, d in ipairs(driver:get_devices()) do
    if d.device_network_id == DEVICE_NETWORK_ID then
      return
    end
  end
  log.info("RPi 상태 기기를 만듭니다")
  local ok = driver:try_create_device({
    type = "LAN",
    device_network_id = DEVICE_NETWORK_ID,
    label = "RPi 상태",
    profile = PROFILE,
    manufacturer = "stcollector",
    model = "rpi-status",
    vendor_provided_label = "RPi 상태",
  })
  if not ok then
    log.error("기기 생성 요청 실패")
  end
end

local driver = Driver("stcollector-rpi-status", {
  discovery = discovery,
  lifecycle_handlers = {
    init = device_init,
    added = device_added,
    infoChanged = info_changed,
    removed = device_removed,
  },
  capability_handlers = {
    [capabilities.refresh.ID] = {
      [capabilities.refresh.commands.refresh.NAME] = handle_refresh,
    },
  },
})

driver:run()
