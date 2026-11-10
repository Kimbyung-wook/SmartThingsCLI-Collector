/* 대시보드 화면. API 응답의 이름은 신뢰하지 않고 항상 textContent 로 넣는다. */
(function () {
  'use strict';
  const H = 36e5, KST = 9 * H;
  const { fmt, color, rampColor } = Charts;
  const $ = id => document.getElementById(id);
  const view = $('view');

  function h(tag, props) {
    const el = document.createElement(tag);
    for (const k in props || {}) {
      const v = props[k];
      if (v == null) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v);
    }
    for (let i = 2; i < arguments.length; i++) {
      const c = arguments[i];
      if (c == null || c === false) continue;
      (Array.isArray(c) ? c : [c]).forEach(x => { if (x != null && x !== false) el.append(x); });
    }
    return el;
  }

  // ---- 시각 (항상 KST) ----------------------------------------------------------
  const kst = t => new Date(t + KST);
  const pad = n => String(n).padStart(2, '0');
  const md = t => { const d = kst(t); return (d.getUTCMonth() + 1) + '/' + d.getUTCDate(); };
  const hh = t => kst(t).getUTCHours();
  const hm = t => pad(kst(t).getUTCHours()) + ':' + pad(kst(t).getUTCMinutes());
  const stamp = t => (t == null ? '—' : pad(kst(t).getUTCMonth() + 1) + '/' + pad(kst(t).getUTCDate()) + ' ' + hm(t));
  function ago(t) {
    if (t == null) return '—';
    const m = Math.max(0, Math.round((Date.now() - t) / 6e4));
    if (m < 1) return '방금';
    if (m < 60) return m + '분 전';
    if (m < 1440) return Math.floor(m / 60) + '시간 전';
    return Math.floor(m / 1440) + '일 전';
  }
  function bucketX(t, grain) {
    if (grain === 'hour') return hh(t) + '시';
    if (grain === 'month') return (kst(t).getUTCMonth() + 1) + '월';
    return md(t);
  }
  function bucketTip(t, grain) {
    if (grain === 'hour') return md(t) + ' ' + hh(t) + '시';
    if (grain === 'week') return md(t) + ' 시작 주';
    if (grain === 'month') { const d = kst(t); return d.getUTCFullYear() + '년 ' + (d.getUTCMonth() + 1) + '월'; }
    return md(t);
  }

  // ---- API ----------------------------------------------------------------------
  async function api(path, params) {
    const q = new URLSearchParams(params || {}).toString();
    const res = await fetch(path + (q ? '?' + q : ''), { credentials: 'same-origin' });
    if (res.status === 401) { location.href = '/login'; throw new Error('login'); }
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  }

  // ---- 공용 조각 ------------------------------------------------------------------
  const S = { tab: 'summary', metric: 'temperature', period: '24h', room: 'all', luxRoom: null, luxScope: 'all', luxPeriod: 'month',
    occRoom: null, occPeriod: 'month', plugPeriod: 'month', sysPeriod: '24h' };
  const TABS = [['summary', '요약'], ['trend', '추세'], ['lux', '조도 분포'], ['occ', '재실'], ['plug', '플러그'], ['status', '상태']];
  const METRICS = [['temperature', '온도'], ['humidity', '습도'], ['illuminance', '조도'], ['dewpoint', '이슬점']];
  const PERIODS = [['24h', '24시간 · 시간별'], ['month', '1개월 · 일별'], ['year', '1년 · 주별']];
  let meta = null;

  function seg(key, options, label) {
    return h('div', { class: 'seg', role: 'group', 'aria-label': label },
      options.map(([v, t]) => h('button', { type: 'button', 'aria-pressed': String(S[key] === v), text: t,
        onclick: () => { S[key] = v; render(); } })));
  }
  function empty(title, detail) { return h('div', { class: 'empty' }, h('b', { text: title }), h('span', { text: detail })); }
  function stat(label, value, sub, cls) { return h('div', { class: 'stat' + (cls ? ' ' + cls : '') }, h('span', { text: label }), h('b', { text: value }), sub && h('small', { text: sub })); }
  function section(title, sub) { return h('section', { class: 'sec' }, h('div', null, h('h2', { text: title }), sub && h('p', { class: 'sub', text: sub }))); }
  function legendLine(c, text) { const k = h('i', { class: 'sw' }); k.style.background = c; return h('span', null, k, text); }
  function legendBox(c, text) { const k = h('i', { class: 'swb' }); k.style.background = c; return h('span', null, k, text); }
  function table(headers, rows, numeric) {
    return h('div', { class: 'scroll' }, h('table', null,
      h('thead', null, h('tr', null, headers.map((x, i) => h('th', { class: numeric && numeric(i) ? 'n' : null, text: x })))),
      h('tbody', null, rows.map(r => h('tr', null, r.map((x, i) => {
        const td = h('td', { class: numeric && numeric(i) ? 'n' : null });
        if (x instanceof Node) td.append(x); else td.textContent = x;
        return td;
      }))))));
  }
  /* 표는 열 때 만든다 (DOM을 작게 유지) */
  function tableView(build) {
    const d = h('details', null, h('summary', { text: '표로 보기' }));
    let done = false;
    d.addEventListener('toggle', () => { if (d.open && !done) { d.append(build()); done = true; } });
    return d;
  }
  function chartHost() { return h('div', { class: 'chart' }); }
  function ramp(lo, hi, kind) {
    const bar = h('i');
    bar.style.background = 'linear-gradient(90deg,' + [0, 0.5, 1].map(t => rampColor(t, kind)).join(',') + ')';
    return h('div', { class: 'ramp' }, h('span', { text: lo }), bar, h('span', { text: hi }));
  }
  function pendingNote() {
    return empty('아직 데이터 없음', '첫 집계(매시 2분)가 끝나면 표시');
  }
  /* DOM에 붙인 뒤에 차트를 그린다 (폭 측정) */
  const afterMount = [];
  function later(fn) { afterMount.push(fn); }

  // ---- 요약 -----------------------------------------------------------------------
  async function vSummary() {
    const summaryMetrics = METRICS.filter(m => m[0] !== 'dewpoint');
    const [d, ...charts] = await Promise.all([api('/api/summary'),
      ...summaryMetrics.map(([metric]) => api('/api/series', { metric, period: '24h' }))]);
    if (!d.has_data) return [pendingNote()];

    // 방별 현재값. 스와치 색이 아래 차트의 선 색과 같다.
    const now = (cur, digits) => (cur && cur.now != null ? fmt(cur.now, digits) : '—');
    const roomCard = r => {
      const swatch = h('i', { class: 'sw' });
      swatch.style.background = color(r.color);
      return h('div', { class: 'room' },
        h('div', { class: 'room-head' },
          h('span', { class: 'room-name' }, swatch, h('span', { text: r.name })),
          r.occupancy == null ? null : h('span', { class: 'pill' + (r.occupancy.occupied ? ' on' : '') }, h('i'), r.occupancy.occupied ? '재실' : '비어 있음')),
        h('div', { class: 'room-temp' }, now(r.temperature, 1), h('small', { text: '°C' })),
        h('div', { class: 'room-sub' },
          h('span', { text: '습도 ' + now(r.humidity, 0) + '%' }),
          h('span', { class: 'room-lux', text: now(r.illuminance, 0) + ' lx' })));
    };
    const measured = d.rooms.filter(r => r.temperature || r.humidity || r.illuminance || r.occupancy != null);
    const strip = measured.length ? h('div', { class: 'rooms' }, measured.map(roomCard)) : null;

    // 온도를 크게, 습도와 조도는 작게 그린다.
    const plot = ([metric, label], k, big) => {
      const c = charts[k], rs = c.series.filter(x => x.avg.some(v => v != null));
      const log = metric === 'illuminance';
      const sec = section(label, big ? '최근 24시간 · 시간별 평균' : log ? '최근 24시간 · 세로축 로그 눈금' : '최근 24시간');
      if (!rs.length) { sec.append(empty('아직 집계된 값 없음', '매시 2분 갱신')); return sec; }
      const host = chartHost();
      later(() => Charts.line(host, {
        n: c.buckets.length, x: i => bucketX(c.buckets[i], c.grain), tip: i => bucketTip(c.buckets[i], c.grain),
        series: rs, band: false, log, unit: c.unit, digits: c.digits, directLabels: rs.length > 1,
        height: big ? (W => (W < 520 ? 220 : 300)) : (W => (W < 520 ? 170 : 200)),
        aria: `최근 24시간 방별 ${label} 차트. 추세 화면에서 표로 볼 수 있습니다.`,
      }));
      sec.append(host);
      return sec;
    };
    const main = plot(summaryMetrics[0], 0, true);
    const small = h('div', { class: 'grid2' }, summaryMetrics.slice(1).map((m, i) => plot(m, i + 1, false)));

    const range = (cur, digits) => (cur && cur.today_min != null ? `${fmt(cur.today_min, digits)}~${fmt(cur.today_max, digits)}` : '—');
    const state = table(['방', '재실', '이슬점 °C', '오늘 온도 °C', '오늘 습도 %', '오늘 조도 lx'], d.rooms.map(r => [
      r.name,
      r.occupancy == null ? '센서 없음'
        : h('span', { class: 'pill' + (r.occupancy.occupied ? ' on' : '') }, h('i'), r.occupancy.occupied ? '재실' : '비어 있음'),
      r.dew_point != null ? fmt(r.dew_point, 1) : '—',
      range(r.temperature, 1), range(r.humidity, 0), range(r.illuminance, 0),
    ]), i => i >= 2);
    const stateSec = section('방별 상태');
    stateSec.append(state);
    return [strip, main, small, stateSec,
      d.closed_until ? h('p', { class: 'sub', text: `${stamp(d.closed_until)}까지 집계 · 매시 2분 갱신` }) : null];
  }

  // ---- 추세 -----------------------------------------------------------------------
  const unitDigits = d => d.digits;
  async function vTrend() {
    const d = await api('/api/series', { metric: S.metric, period: S.period });
    const available = d.series.filter(x => x.avg.some(v => v != null));
    const names = available.map(x => x.name);
    if (S.room !== 'all' && !names.includes(S.room)) S.room = 'all';
    const controls = h('div', { class: 'controls' },
      seg('metric', METRICS, '측정 항목'), seg('period', PERIODS, '기간'),
      names.length > 1 ? seg('room', [['all', '방별 비교'], ...names.map(n => [n, n])], '방') : null);
    if (!available.length) return [controls, pendingNote()];

    const cmp = S.room === 'all' && available.length > 1;
    const rs = cmp ? available : (S.room === 'all' ? available : available.filter(x => x.name === S.room));
    const digits = unitDigits(d), n = d.buckets.length;
    const log = S.metric === 'illuminance';
    const band = !cmp && rs[0].min != null;
    const aggr = r => {
      let sum = 0, w = 0, mx = -Infinity, mn = Infinity, mxI = -1, mnI = -1;
      for (let i = 0; i < n; i++) {
        if (r.avg[i] == null) continue;
        const c = r.coverage[i] || 0; sum += r.avg[i] * c; w += c;
        const hi = r.max ? r.max[i] : r.avg[i], lo = r.min ? r.min[i] : r.avg[i];
        if (hi > mx) { mx = hi; mxI = i; }
        if (lo < mn) { mn = lo; mnI = i; }
      }
      return { avg: w ? sum / w : null, mx, mn, mxI, mnI };
    };
    let stats;
    if (cmp) {
      stats = table(['방', '평균', '최대', '최소'], rs.map(r => {
        const g = aggr(r);
        return [r.name, fmt(g.avg, digits), fmt(g.mx, digits), fmt(g.mn, digits)];
      }), i => i > 0);
    } else {
      const g = aggr(rs[0]);
      stats = h('div', { class: 'stats' },
        stat('기간 평균', fmt(g.avg, digits) + ' ' + d.unit),
        stat('최대', fmt(g.mx, digits) + ' ' + d.unit, bucketTip(d.buckets[g.mxI], d.grain)),
        stat('최소', fmt(g.mn, digits) + ' ' + d.unit, bucketTip(d.buckets[g.mnI], d.grain)));
    }
    const legend = h('div', { class: 'legend' },
      cmp ? rs.map(r => legendLine(color(r.color), r.name))
        : [legendLine(color(rs[0].color), '평균'), band ? h('span', null, h('i', { class: 'band-key' }), '최대~최소 범위') : null],
      log ? h('span', { text: '세로축은 로그 눈금' }) : null,
      h('span', { text: '흐린 구간은 수집률 50% 미만' }));
    const host = chartHost();
    const metricLabel = METRICS.find(m => m[0] === S.metric)[1];
    later(() => Charts.line(host, {
      n, x: i => bucketX(d.buckets[i], d.grain), tip: i => bucketTip(d.buckets[i], d.grain),
      series: rs, band, log, unit: d.unit, digits, directLabels: cmp,
      aria: `${metricLabel} 추세 차트. 값은 아래 표로도 볼 수 있습니다.`,
    }));
    const tv = tableView(() => table(
      ['구간', ...(cmp ? rs.map(r => r.name + ' 평균') : band ? ['평균', '최대', '최소'] : ['평균'])],
      d.buckets.map((b, i) => [bucketTip(b, d.grain),
        ...(cmp ? rs.map(r => fmt(r.avg[i], digits)) : band ? [fmt(rs[0].avg[i], digits), fmt(rs[0].max[i], digits), fmt(rs[0].min[i], digits)] : [fmt(rs[0].avg[i], digits)])]),
      i => i > 0));
    return [controls, stats, legend, host, tv,
      S.period === '24h' && d.closed_until ? h('p', { class: 'sub', text: `${stamp(d.closed_until)}까지 집계 · 매시 2분 갱신` }) : null];
  }

  // ---- 조도 분포 -------------------------------------------------------------------
  function binRangeText(bins, range) {
    const first = bins[range.from].split('–')[0];
    const lastLabel = bins[range.to];
    const last = lastLabel.includes('–') ? lastLabel.split('–')[1] : lastLabel;
    return first.replace('+', '') + '–' + last + ' lx';
  }
  function histogram(d) {
    const max = Math.max.apply(null, d.totals_hours) || 1, sum = d.total_hours || 1;
    return h('div', { class: 'hist' }, d.bins.map((label, i) => {
      const inRange = d.range80 && i >= d.range80.from && i <= d.range80.to;
      const bar = h('div', { class: 'bar' });
      bar.style.width = (d.totals_hours[i] / max * 100) + '%';
      return h('div', { class: 'hrow' + (inRange ? ' in' : '') + (i === d.mode_bin ? ' mode' : ''), tabindex: 0,
        title: `${label} lx: ${fmt(d.totals_hours[i], 1)}시간` },
        h('span', { class: 'lab', text: label }), h('div', { class: 'tr' }, bar),
        h('span', { class: 'v', text: fmt(d.totals_hours[i] / sum * 100, 1) + '%' }));
    }));
  }
  function histLegend() {
    return h('div', { class: 'legend' }, legendBox('var(--hmode)', '가장 오래 머문 구간'),
      legendBox('var(--hrange)', '시간의 80%가 속한 범위'), legendBox('var(--hbar)', '그 밖의 구간'));
  }
  function histTable(d) {
    return tableView(() => table(['구간 (lx)', '시간', '비율'], d.bins.map((b, i) => [b, fmt(d.totals_hours[i], 1), fmt(d.totals_hours[i] / (d.total_hours || 1) * 100, 1) + '%']), i => i > 0));
  }

  async function vLux() {
    const sensors = meta.sensors.illuminance;
    const periodSeg = seg('luxPeriod', [['week', '7일'], ['month', '30일'], ['year', '1년']], '기간');
    if (!sensors.length) return [pendingNote()];
    if (!sensors.some(x => x.name === S.luxRoom)) S.luxRoom = sensors[0].name;
    const device = sensors.find(x => x.name === S.luxRoom);
    const d = await api('/api/lux-dist', { device: device.id, period: S.luxPeriod, scope: S.luxScope });
    const controls = h('div', { class: 'controls' },
      sensors.length > 1 ? seg('luxRoom', sensors.map(x => [x.name, x.name]), '방') : null,
      periodSeg,
      d.occupancy_available ? seg('luxScope', [['all', '전체 시간'], ['occupied', '재실 중만']], '시간 범위') : null);
    if (S.luxScope === 'occupied' && !d.occupancy_available) { S.luxScope = 'all'; return vLux(); }
    if (!d.has_data) return [controls, pendingNote()];

    const bins = d.bins;
    const stats = h('div', { class: 'stats' },
      stat('기간 평균', fmt(d.avg) + ' lx'),
      stat('최대 / 최소', fmt(d.max) + ' / ' + fmt(d.min, 1)),
      stat('가장 오래 머문 구간', bins[d.mode_bin] + ' lx', '전체의 ' + fmt(d.totals_hours[d.mode_bin] / d.total_hours * 100) + '%', 'lux'),
      stat('시간의 80%가 속한 범위', binRangeText(bins, d.range80)));
    const histSec = section('조도 구간별 머문 시간');
    histSec.append(histLegend(), histogram(d), histTable(d));

    const hours = Array.from({ length: 24 }, (_, i) => i + '시');
    const rowsDesc = bins.slice().reverse();
    let peak = 0; d.matrix.forEach(r => r.forEach(v => { peak = Math.max(peak, v); }));
    const binHost = chartHost();
    later(() => Charts.heat(binHost, {
      rows: rowsDesc, cols: hours, L: 62, colEvery: 3, ch: W => (W < 520 ? 17 : 21), rowEvery: () => true,
      fill: (r, c) => { const v = d.matrix[12 - r][c]; return v > 0 ? rampColor(Math.sqrt(v / peak), 'lux') : 'var(--cell0)'; },
      tip: (r, c) => ({ title: `${c}시 · ${rowsDesc[r]} lx`, rows: [{ key: '머문 시간', value: fmt(d.matrix[12 - r][c], 1) + '시간' }] }),
      aria: '시각과 조도 구간별 머문 시간 히트맵. 값은 아래 표로도 볼 수 있습니다.',
    }));
    const binSec = section('시각 × 조도 구간', '어느 시간대에 어떤 밝기가 많은지');
    binSec.append(ramp('짧음', '김', 'lux'), binHost, tableView(() => table(['조도 (lx)', ...hours], rowsDesc.map((b, r) => [b, ...hours.map((_, c) => fmt(d.matrix[12 - r][c], 1))]), i => i > 0)));

    const colLabels = d.date_columns.map(t => md(t));
    let top = 0; d.dates.forEach(col => col.forEach(v => { if (v != null) top = Math.max(top, v); }));
    const logTop = Math.log10(1 + top) || 1;
    const dateHost = chartHost();
    later(() => Charts.heat(dateHost, {
      rows: hours, cols: colLabels, L: 38, colEvery: S.luxPeriod === 'week' ? 1 : 5, ch: W => (W < 520 ? 12 : 14), rowEvery: r => r % 3 === 0,
      fill: (r, c) => { const v = d.dates[c][r]; return v == null ? null : rampColor(Math.log10(1 + v) / logTop, 'lux'); },
      tip: (r, c) => ({ title: `${colLabels[c]}${d.date_grain === 'week' ? ' 시작 주' : ''} ${r}시`, rows: [{ key: '평균', value: d.dates[c][r] == null ? '데이터 없음' : fmt(d.dates[c][r]) + ' lx' }] }),
      aria: '날짜와 시각별 평균 조도 히트맵. 값은 아래 표로도 볼 수 있습니다.',
    }));
    const dateSec = section('날짜 × 시각 평균 조도', d.date_grain === 'week' ? '주 단위 · 색은 로그 눈금' : '색은 로그 눈금');
    const key = h('div', { class: 'legend' }, ramp('0 lx', fmt(top) + ' lx', 'lux'), h('span', null, h('i', { class: 'nodata' }), '데이터 없음'));
    dateSec.append(key, dateHost, tableView(() => table(['시각', ...colLabels], hours.map((x, r) => [x, ...colLabels.map((_, c) => fmt(d.dates[c][r]))]), i => i > 0)));
    return [controls, stats, histSec, binSec, dateSec];
  }

  // ---- 재실 -----------------------------------------------------------------------
  const WD = ['월', '화', '수', '목', '금', '토', '일'];
  async function vOcc() {
    const d = await api('/api/occupancy', { period: S.occPeriod });
    const devices = d.devices.filter(x => x.known_hours > 0);
    const periodSeg = seg('occPeriod', [['month', '30일'], ['year', '1년']], '기간');
    if (!devices.length) return [h('div', { class: 'controls' }, periodSeg), pendingNote()];
    if (!devices.some(x => x.name === S.occRoom)) S.occRoom = devices[0].name;
    const dev = devices.find(x => x.name === S.occRoom);
    const controls = h('div', { class: 'controls' },
      devices.length > 1 ? seg('occRoom', devices.map(x => [x.name, x.name]), '방') : null, periodSeg);
    const stats = h('div', { class: 'stats' },
      stat('재실 비율이 가장 높은 시간', dev.peak ? `${WD[dev.peak.weekday]}요일 ${dev.peak.hour}시` : '—', dev.peak ? fmt(dev.peak.ratio * 100) + '%' : null),
      stat('전체 재실 비율', dev.ratio != null ? fmt(dev.ratio * 100) + '%' : '—', fmt(dev.known_hours) + '시간 측정'));
    const hours = Array.from({ length: 24 }, (_, i) => i + '시');
    const host = chartHost();
    later(() => Charts.heat(host, {
      rows: WD, cols: hours, L: 24, colEvery: 3, ch: W => (W < 520 ? 24 : 28), rowEvery: () => true,
      fill: (r, c) => { const v = dev.matrix[r][c]; return v == null ? null : rampColor(v); },
      tip: (r, c) => ({ title: `${WD[r]}요일 ${c}시`, rows: [{ key: '재실 비율', value: dev.matrix[r][c] == null ? '데이터 없음' : fmt(dev.matrix[r][c] * 100) + '%' }] }),
      aria: '요일과 시각별 재실 비율 히트맵. 값은 아래 표로도 볼 수 있습니다.',
    }));
    const heatSec = section('요일 × 시각 재실 비율');
    heatSec.append(ramp('0%', '100%'), host, tableView(() => table(['요일', ...hours], WD.map((x, r) => [x, ...hours.map((_, c) => dev.matrix[r][c] == null ? '—' : fmt(dev.matrix[r][c] * 100))]), i => i > 0)));
    const out = [controls, stats, heatSec];
    const luxSec = section('재실 중 조도 분포', '같은 방의 조도 센서 기준 · 보조등 기준값을 정할 때 참고');
    if (dev.lux_device) {
      const period = S.occPeriod === 'month' ? 'month' : 'year';
      const lux = await api('/api/lux-dist', { device: dev.lux_device, period, scope: 'occupied' });
      if (lux.has_data) {
        luxSec.append(h('div', { class: 'stats' },
          stat('재실 중 가장 많은 구간', lux.bins[lux.mode_bin] + ' lx', fmt(lux.totals_hours[lux.mode_bin] / lux.total_hours * 100) + '%'),
          stat('재실 시간의 80% 범위', binRangeText(lux.bins, lux.range80))), histLegend(), histogram(lux), histTable(lux));
      } else {
        luxSec.append(empty('재실 중 조도 데이터 없음', '이 기간에 재실과 조도가 함께 기록된 시간 없음'));
      }
    } else {
      luxSec.append(empty('같은 방에 조도 센서 없음', 'SmartThings에서 재실 센서와 조도 센서를 같은 방에 지정하면 계산'));
    }
    out.push(luxSec);
    return out;
  }

  // ---- 플러그 ---------------------------------------------------------------------
  async function vPlug() {
    const d = await api('/api/plugs', { period: S.plugPeriod });
    const controls = h('div', { class: 'controls' }, seg('plugPeriod', [['month', '30일 · 일별'], ['year', '1년 · 월별']], '기간'));
    if (!d.plugs.length) return [controls, pendingNote()];
    const on = d.plugs.filter(p => p.state === 'on').length;
    const metered = d.plugs.filter(p => p.kwh_total != null);
    const total = metered.reduce((a, p) => a + p.kwh_total, 0);
    const stats = h('div', { class: 'stats' },
      stat('지금 켜진 플러그', `${on} / ${d.plugs.length}`),
      metered.length ? stat('기간 전력량', fmt(total, 1) + ' kWh', `측정 지원 ${metered.length}기 합계`) : stat('기간 전력량', '—', '측정 지원 기기 없음'));
    const out = [controls, stats];
    if (d.total_kwh) {
      const host = chartHost();
      later(() => Charts.bars(host, {
        labels: d.buckets.map(b => bucketX(b, d.grain)), title: i => bucketTip(d.buckets[i], d.grain),
        values: d.total_kwh, unit: 'kWh', digits: 2, aria: '기간별 전력량 합계 막대 차트. 값은 아래 표로도 볼 수 있습니다.',
      }));
      const sec = section(d.grain === 'day' ? '일별 전력량 합계' : '월별 전력량 합계', '측정 지원 플러그만 합산');
      sec.append(host, tableView(() => table(['구간', 'kWh'], d.buckets.map((b, i) => [bucketTip(b, d.grain), fmt(d.total_kwh[i], 2)]), i => i > 0)));
      out.push(sec);
    }
    const maxKwh = Math.max.apply(null, metered.map(p => p.kwh_total).concat([0.001]));
    const listSec = section('플러그별', '전력량 순 · 켜진 시간은 기간 합계');
    listSec.append(table(['플러그', '상태', '지금', '오늘 켜진 시간', '기간 켜진 시간', 'kWh', ''], d.plugs.map(p => {
      const bar = h('div', { class: 'kbar' }); bar.style.width = ((p.kwh_total || 0) / maxKwh * 100) + '%';
      return [p.room ? `${p.name} · ${p.room}` : p.name,
        h('span', { class: 'pill' + (p.state === 'on' ? ' on' : '') }, h('i'), p.state === 'on' ? '켜짐' : p.state === 'off' ? '꺼짐' : '—'),
        p.power_w != null ? fmt(p.power_w, 1) + ' W' : '—',
        fmt(p.today_on_hours, 1) + '시간', fmt(p.on_hours_total, 1) + '시간',
        p.kwh_total == null ? '측정 안 됨' : fmt(p.kwh_total, 1) + (p.estimated ? ' (추정)' : ''),
        p.kwh_total == null ? '' : bar];
    }), i => i >= 2 && i <= 5));
    out.push(listSec);
    return out;
  }

  // ---- 상태 -----------------------------------------------------------------------
  const GB = 1024 ** 3;
  const SYS_PERIODS = [['1h', '1시간 · 1분별'], ['24h', '24시간 · 5분별']];
  function statusPill(text, kind) { return h('span', { class: 'pill' + (kind ? ' ' + kind : '') }, h('i'), text); }
  const EVENT_LABELS = { undervoltage: '전압 부족', throttled: '스로틀링', freq_capped: '클럭 제한', soft_temp: '온도 제한', overheat: '과열', boot: '부팅' };
  function duration(sec) {
    if (sec < 60) return sec + '초';
    if (sec < 3600) return Math.floor(sec / 60) + '분 ' + (sec % 60) + '초';
    return Math.floor(sec / 3600) + '시간 ' + Math.floor(sec % 3600 / 60) + '분';
  }
  function eventRow(e) {
    const label = EVENT_LABELS[e.kind] || e.kind;
    const kind = e.kind === 'boot' ? '' : e.kind === 'undervoltage' || e.kind === 'overheat' ? 'bad' : 'warn';
    const state = e.kind === 'boot' ? '' : e.end == null ? '진행 중' : e.interrupted ? '끝 시각 모름 (재시작)' : duration(e.seconds);
    const body = h('div', { class: 'body' });
    if (e.kind === 'boot') {
      body.append(h('p', { class: 'sub', text: 'Raspberry Pi 부팅 · 직전 프로세스 기록 없음' }));
    } else {
      const at = [e.temp_c != null ? fmt(e.temp_c, 1) + '°C' : null, e.freq_mhz != null ? fmt(e.freq_mhz) + ' MHz' : null, e.load1 != null ? 'load ' + fmt(e.load1, 2) : null].filter(Boolean).join(' · ');
      if (at) body.append(h('p', { class: 'sub', text: '시작할 때: ' + at }));
      body.append(e.procs.length ? table(['직전 60초 프로세스', 'CPU 초', '점유율'], e.procs.map(p => [p.name, fmt(p.cpu_s, 1), fmt(p.pct, 1) + '%']), i => i > 0)
        : h('p', { class: 'sub', text: '직전 프로세스 기록 없음' }));
    }
    return h('details', { class: 'event' },
      h('summary', null,  // 표시 / 시각 / 문제 / 기간. 값이 없어도 칸을 유지해 열을 맞춘다
        h('span', { class: 'pill' + (kind ? ' ' + kind : '') }, h('i')),
        h('span', { class: 'when', text: stamp(e.start) }),
        h('span', { text: label }),
        h('span', { class: 'when', text: state })),
      body);
  }
  function systemSections(sys) {
    const head = section('Raspberry Pi', '5초 간격');
    if (!sys || !sys.available) {
      head.append(empty('시스템 기록 없음', 'stcollector-sysmon 서비스가 실행되면 표시'));
      return [head];
    }
    const c = sys.current, out = [head];
    if (c) {
      const caps = c.capabilities || {};
      const uptime = c.uptime_s == null ? '—' : duration(c.uptime_s);
      head.append(h('div', { class: 'stats' },
        stat('CPU 온도', c.temp_c != null ? fmt(c.temp_c, 1) + ' °C' : '—'),
        stat('클럭', c.freq_mhz != null ? fmt(c.freq_mhz) + ' MHz' : '—'),
        stat('load (1분)', c.load1 != null ? fmt(c.load1, 2) : '—'),
        stat('여유 메모리', c.mem_avail_mb != null ? fmt(c.mem_avail_mb) + ' MB' : '—', c.mem_total_mb ? '전체 ' + fmt(c.mem_total_mb) + ' MB' : null),
        stat('디스크 여유', c.disk_free_gb != null ? fmt(c.disk_free_gb, 1) + ' GB' : '—', c.disk_total_gb ? '전체 ' + fmt(c.disk_total_gb, 1) + ' GB' : null),
        stat('가동 시간', uptime)));
      const pills = [c.undervoltage === true ? statusPill('전압 부족', 'bad') : c.undervoltage === false ? statusPill('전압 정상', 'on') : statusPill('전압 읽기 불가', 'warn')];
      (c.active || []).filter(k => k !== 'undervoltage').forEach(k => pills.push(statusPill(EVENT_LABELS[k] || k, k === 'overheat' ? 'bad' : 'warn')));
      head.append(h('div', { class: 'legend' }, pills));
      if (!caps.throttle_bits) head.append(h('p', { class: 'sub', text: '이 기기는 전압 부족만 읽을 수 있어 스로틀링은 표시 안 됨' }));
      if (c.stale) head.append(h('p', { class: 'sub', text: `현재값 갱신 멈춤 (마지막 ${stamp(c.ts_ms)}) · 서비스 상태 확인 필요` }));
      if (c.top && c.top.length) {
        const top = section('최근 60초 CPU 사용 프로세스');
        top.append(table(['프로세스', 'CPU 초', '점유율'], c.top.map(p => [p.name, fmt(p.cpu_s, 1), fmt(p.pct, 1) + '%']), i => i > 0));
        out.push(top);
      }
    } else {
      head.append(h('p', { class: 'sub', text: '현재값 파일 없음 · 모니터 중단 또는 재부팅 직후' }));
    }

    const q = sys.series, n = q.buckets.length;
    const oneHour = S.sysPeriod === '1h', periodLabel = oneHour ? '최근 1시간' : '최근 24시간';
    const charts = section(periodLabel, (oneHour ? '1분' : '5분') + ' 평균 · 온도의 띠는 최대~최소');
    charts.append(h('div', { class: 'controls' }, seg('sysPeriod', SYS_PERIODS, '기간')));
    out.push(charts);
    if (q.temp_avg.some(v => v != null)) {
      const uv = q.undervolt_s;
      const key = uv.some(v => v > 0) ? h('div', { class: 'legend' }, legendBox('var(--shade)', '전압 부족 구간')) : null;
      const base = { n, x: i => hm(q.buckets[i]), tip: i => stamp(q.buckets[i]), shade: i => uv[i] > 0,
        extra: i => (uv[i] > 0 ? [{ key: '전압 부족', value: uv[i] + '초' }] : []), height: W => (W < 520 ? 150 : 180) };
      const chart = (title, label, avg, opts) => {
        const sec = section(title), host = chartHost();
        const se = { name: label, color: 'ink', avg, coverage: q.coverage, min: opts.min || null, max: opts.max || null };
        later(() => Charts.line(host, { ...base, series: [se], band: !!opts.min, unit: opts.unit, digits: opts.digits, domain: opts.domain, aria: `${periodLabel} ${title} 차트. 값은 아래 표로도 볼 수 있습니다.` }));
        sec.append(host);
        return sec;
      };
      const loadTop = Math.max(1, ...q.load.filter(v => v != null));
      if (key) charts.append(key);
      out.push(
        chart('CPU 온도', '온도', q.temp_avg, { min: q.temp_min, max: q.temp_max, unit: '°C', digits: 1 }),
        chart('클럭', '클럭', q.freq, { unit: 'MHz', digits: 0 }),
        chart('load', 'load', q.load, { unit: '', digits: 2, domain: [0, loadTop] }),
        tableView(() => table(['시각', '온도 °C', '클럭 MHz', 'load', '전압 부족 초'],
          q.buckets.map((b, i) => [stamp(b), fmt(q.temp_avg[i], 1), fmt(q.freq[i]), fmt(q.load[i], 2), uv[i]]).filter((_, i) => q.coverage[i] != null), i => i > 0)));
    } else {
      charts.append(empty('이 기간 기록 없음', '모니터가 기록을 남기면 표시'));
    }
    const log = section('이벤트 로그', '행을 누르면 시작 직전 60초의 프로세스 표시');
    log.append(sys.events.length ? h('div', { class: 'events' }, sys.events.map(eventRow)) : h('p', { class: 'sub', text: '기록된 이벤트 없음' }));
    out.push(log);
    return out;
  }
  async function vStatus() {
    const [d, sys] = await Promise.all([api('/api/status'), api('/api/system', { period: S.sysPeriod }).catch(e => { if (e.message === 'login') throw e; return null; })]);
    const token = d.auth_state === 'reauth_required' ? statusPill('재인증 필요', 'bad')
      : d.auth_state === 'ok' ? statusPill('정상', 'on') : '수집 시작 전';
    const rows = [
      ['프로그램 버전', meta.version],
      ['수집 방식', d.mode === 'history' ? '이벤트 이력' : d.mode === 'status' ? '상태 조회' : '수집 시작 전'],
      ['마지막 수집', d.last_ok_ms ? `${stamp(d.last_ok_ms)} (${ago(d.last_ok_ms)})` : '—'],
      ['마지막 집계', d.rollup_finished_ms ? `${stamp(d.rollup_finished_ms)} (${ago(d.rollup_finished_ms)})` : '아직 없음'],
      ['SmartThings 인증', token],
      ['디스크', `사용 ${fmt(d.disk.used / GB, 1)}GB / 전체 ${fmt(d.disk.total / GB, 1)}GB`],
      ['DB 크기', `${fmt(d.disk.db_bytes / 1048576, 1)}MB`],
      ['최근 수집 오류', d.last_error_ms ? `${stamp(d.last_error_ms)}: ${d.last_error || ''}` : '없음'],
    ];
    const main = table(['항목', '값'], rows);
    const gaps = section('수집 공백', '최근 14일, 온습도·조도 센서가 모두 조용했던 시간');
    gaps.append(d.gaps.length ? table(['시작', '끝', '길이'], d.gaps.map(g => [stamp(g.start), stamp(g.end), g.hours + '시간']), i => i === 2)
      : h('p', { class: 'sub', text: d.closed_until ? '최근 14일 공백 없음' : '집계가 시작되면 표시' }));
    const sensors = section('센서별 마지막 보고');
    const low = b => b != null && b < 20;
    sensors.append(d.devices.length ? table(['기기', '방', '마지막 보고', '배터리'], d.devices.map(x => [x.name, x.room || '—', `${stamp(x.last_ms)} (${ago(x.last_ms)})`,
      x.battery == null ? '—' : low(x.battery) ? statusPill(`${fmt(x.battery)}% 교체 권장`, 'warn') : fmt(x.battery) + '%']), i => i === 3)
      : h('p', { class: 'sub', text: '아직 보고한 센서 없음' }));
    return [...systemSections(sys), section('수집'), main, gaps, sensors];
  }

  // ---- 틀 -------------------------------------------------------------------------
  const VIEWS = { summary: vSummary, trend: vTrend, lux: vLux, occ: vOcc, plug: vPlug, status: vStatus };
  let token = 0;
  async function render() {
    const me = ++token;
    view.classList.add('busy');
    afterMount.length = 0;
    let nodes;
    try {
      nodes = await VIEWS[S.tab]();
    } catch (e) {
      if (e.message === 'login') return;
      nodes = [empty('데이터를 불러오지 못했습니다', '잠시 후 다시 시도해 주세요')];
    }
    if (me !== token) return;
    view.replaceChildren(...nodes.filter(Boolean));
    view.classList.remove('busy');
    afterMount.splice(0).forEach(fn => fn());
    renderTabs();
  }
  function renderTabs() {
    $('tabs').replaceChildren(...TABS.map(([v, t]) => h('a', { href: '#' + v, text: t, 'aria-current': S.tab === v ? 'page' : null })));
  }
  function route() {
    const tab = location.hash.slice(1);
    S.tab = VIEWS[tab] ? tab : 'summary';
    renderTabs();
    window.scrollTo(0, 0);
    render();
  }
  function warnIcon() {
    const NS = 'http://www.w3.org/2000/svg', svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('width', 15); svg.setAttribute('height', 15); svg.setAttribute('viewBox', '0 0 16 16'); svg.setAttribute('aria-hidden', 'true');
    [['M8 1.6L15 14H1z', 1.5], ['M8 6.4v3.8M8 11.8v.6', 1.6]].forEach(([d, w]) => {
      const p = document.createElementNS(NS, 'path'); p.setAttribute('d', d); p.setAttribute('fill', 'none');
      p.setAttribute('stroke', 'currentColor'); p.setAttribute('stroke-width', w); p.setAttribute('stroke-linejoin', 'round'); p.setAttribute('stroke-linecap', 'round');
      svg.appendChild(p);
    });
    return svg;
  }
  async function loadMeta() {
    meta = await api('/api/meta');
    $('footer').textContent = `stcollector ${meta.version}`;
    $('alerts').replaceChildren(...meta.alerts.map(a => h('div', { class: 'alert' + (a.level === 'critical' ? ' critical' : '') }, warnIcon(), h('span', { text: (a.level === 'critical' ? '위험: ' : '') + a.text }))));
  }
  function tick() {
    const d = kst(Date.now());
    $('clock').textContent = `${d.getUTCMonth() + 1}월 ${d.getUTCDate()}일 ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} KST`;
  }

  // ---- 테마 -----------------------------------------------------------------------
  const THEMES = [['auto', '자동'], ['light', '라이트'], ['dark', '다크']];
  let theme = 'auto';
  try { theme = localStorage.getItem('theme') || 'auto'; } catch (e) { /* 저장 불가 환경 */ }
  function applyTheme() {
    if (theme === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', theme);
    $('theme').textContent = THEMES.find(t => t[0] === theme)[1];
    Charts.resetTheme();
  }
  $('theme').addEventListener('click', () => {
    theme = THEMES[(THEMES.findIndex(t => t[0] === theme) + 1) % 3][0];
    try { localStorage.setItem('theme', theme); } catch (e) { /* 무시 */ }
    applyTheme(); render();
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (theme === 'auto') { Charts.resetTheme(); render(); }
  });

  let lastWidth = 0, raf = 0;
  new ResizeObserver(entries => {
    const w = Math.round(entries[0].contentRect.width);
    if (w === lastWidth) return;
    lastWidth = w;
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => view.querySelectorAll('.chart').forEach(c => c._draw && c._draw()));
  }).observe(view);

  window.addEventListener('hashchange', route);
  applyTheme(); tick(); setInterval(tick, 20000);
  setInterval(() => loadMeta().catch(() => {}), 300000);
  loadMeta().catch(() => {}).finally(() => {
    if (!meta) meta = { sensors: { illuminance: [], occupancy: [], temperature: [], humidity: [] }, alerts: [] };
    route();
  });
})();
