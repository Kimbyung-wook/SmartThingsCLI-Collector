/* 대시보드 전용 소형 SVG 차트. 외부 라이브러리 없음. 라벨은 항상 textContent 로 넣는다. */
(function () {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const fmt = (v, d) => (v == null ? '—' : Number(v).toLocaleString('ko-KR', { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 }));

  function s(tag, attrs, parent) {
    const el = document.createElementNS(NS, tag);
    for (const k in attrs || {}) if (attrs[k] != null) el.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(el);
    return el;
  }
  function txt(parent, x, y, text, cls, anchor) {
    const el = s('text', { x, y, class: cls, 'text-anchor': anchor || 'start' }, parent);
    el.textContent = text;
    return el;
  }
  function color(c) { return c == null ? 'var(--other)' : c === 'ink' ? 'var(--ink2)' : 'var(--s' + (c + 1) + ')'; }

  // ---- 툴팁 ------------------------------------------------------------------
  const active = new Set();
  function tipFor(host) {
    let t = host.querySelector(':scope > .tip');
    if (!t) { t = document.createElement('div'); t.className = 'tip'; t.hidden = true; host.appendChild(t); }
    return t;
  }
  /* rows: [{key, value, sub, color}] */
  function fillTip(t, title, rows) {
    t.replaceChildren();
    const head = document.createElement('div'); head.className = 't'; head.textContent = title; t.appendChild(head);
    rows.forEach(r => {
      const row = document.createElement('div'); row.className = 'r';
      const name = document.createElement('span');
      if (r.color != null) { const k = document.createElement('i'); k.className = 'sw'; k.style.background = r.color; name.appendChild(k); }
      name.appendChild(document.createTextNode(r.key));
      const v = document.createElement('strong'); v.textContent = r.value;
      row.append(name, v); t.appendChild(row);
    });
  }
  function placeTip(host, t, x, y) {
    t.hidden = false;
    const W = host.clientWidth, w = t.offsetWidth, h = t.offsetHeight;
    let l = x + 12; if (l + w > W) l = x - w - 12;
    t.style.left = clamp(l, 0, Math.max(0, W - w)) + 'px';
    t.style.top = Math.max(0, y - h - 8) + 'px';
  }
  document.addEventListener('pointerdown', e => {
    active.forEach(c => { if (!c.host.contains(e.target)) c.hide(); });
  });

  function mount(host, draw) {
    host._draw = draw;
    draw();
  }
  function width(host) { return Math.max(240, Math.floor(host.clientWidth || host.parentElement.clientWidth || 320)); }

  function niceTicks(lo, hi, count) {
    if (hi <= lo) { hi = lo + 1; lo = lo - 1; }
    const raw = (hi - lo) / count, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].find(x => x * mag >= raw) * mag;
    const a = Math.floor(lo / step) * step, b = Math.ceil(hi / step) * step, out = [];
    for (let v = a; v <= b + step / 2; v += step) out.push(+v.toFixed(6));
    return out;
  }

  // ---- 선 + 범위 띠 -------------------------------------------------------------
  /* o: {n, x(i), tip(i), series:[{name, color, avg, min, max, coverage}], band, log, unit, digits,
         aria, directLabels, domain:[lo,hi]}
     band: 최소~최대 띠. 계열이 하나면 무채색(--band), 여럿이면 계열 색을 옅게(.rband)
     o.time 이 있으면 가로로 끄는 차트다 (pan 참고): x 는 시각에 비례하고 y축은 왼쪽에 고정 */
  function line(host, o) { mount(host, () => drawLine(host, o)); }
  // 빈 구간을 점선으로 잇는 최대 길이. x축 전체 칸 수(끄는 차트는 한 화면 길이)에 대한 비율. 이 이상이면 끊는다
  const GAP_LINK_MAX = 0.25;

  function drawLine(host, o) {
    host.replaceChildren();
    const T = o.time, W = width(host), Ht = o.height ? o.height(W) : (W < 520 ? 230 : 300), n = o.n;
    const vals = [];
    o.series.forEach(se => {
      for (let i = 0; i < n; i++) {
        if (se.avg[i] != null) vals.push(se.avg[i]);
        if (o.band && se.min && se.min[i] != null) { vals.push(se.min[i]); vals.push(se.max[i]); }
      }
    });
    let lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    if (o.domain) { lo = o.domain[0]; hi = o.domain[1]; }
    if (T && T.domain) {  // 끄는 차트: 불러온 전체 기준이고 넓어지기만 한다
      const d = T.domain;
      if (isFinite(lo) && isFinite(hi)) { d.lo = d.lo == null ? lo : Math.min(d.lo, lo); d.hi = d.hi == null ? hi : Math.max(d.hi, hi); }
      if (d.lo != null) { lo = d.lo; hi = d.hi; }
    }

    let ticks, map;
    if (o.log) {
      const top = Math.max(1, Math.ceil(Math.log10(hi + 1)));
      ticks = [0]; for (let k = 1; k <= top; k++) ticks.push(Math.pow(10, k));
      map = v => Math.log10(Math.max(v, 0) + 1) / Math.log10(ticks[ticks.length - 1] + 1);
    } else {
      ticks = niceTicks(lo, hi, 4);
      const a = ticks[0], b = ticks[ticks.length - 1];
      map = v => (v - a) / (b - a || 1);
    }
    const tickLabels = ticks.map(t => t.toLocaleString('ko-KR'));
    const longest = Math.max.apply(null, tickLabels.map(t => t.length));
    const M = { l: Math.max(26, longest * 7 + 8), r: 14, t: 12, b: 26 };
    const lastIdx = se => { for (let i = n - 1; i >= 0; i--) if (se.avg[i] != null) return i; return -1; };
    let labels = null;
    if (o.directLabels && !T) {
      const widest = Math.max.apply(null, o.series.map(se => se.name.length));
      M.r = clamp(widest * 12 + 18, 40, 84);
    }
    const iw = W - M.l - M.r, ih = Ht - M.t - M.b;
    // 끄는 차트의 그림 svg 는 0 부터 시작하고 폭은 불러온 기간 전체다. 한 화면(iw)이 T.view ms
    let X, cellStart, cellEnd, PW = W, k = 0;
    if (T) {
      k = iw / T.view;
      PW = Math.ceil((T.t1 - T.t0) * k) + M.r;
      const xs = T.xs, endOf = i => (i + 1 < n ? xs[i + 1] : T.xend);
      cellStart = i => (xs[i] - T.t0) * k;
      cellEnd = i => (endOf(i) - T.t0) * k;
      X = i => (cellStart(i) + cellEnd(i)) / 2;
    } else {
      X = i => M.l + (n > 1 ? (i + 0.5) / n * iw : iw / 2);
      cellStart = i => X(i) - iw / n / 2;
      cellEnd = i => X(i) + iw / n / 2;
    }
    const Y = v => M.t + ih - map(v) * ih;

    const svg = s('svg', { width: PW, height: Ht, tabindex: 0, role: 'img', 'aria-label': o.aria });
    let axis = null, pan = null;
    if (T) {
      axis = s('svg', { width: M.l, height: Ht, 'aria-hidden': 'true', class: 'yaxis' });
      svg.setAttribute('class', 'plot');
    }
    if (o.shade) {  // 경고 구간 표시. 짧은 구간도 보이도록 최소 2px
      for (let i = 0, from = -1; i <= n; i++) {
        const on = i < n && o.shade(i);
        if (on && from < 0) from = i;
        if (!on && from >= 0) {
          const x0 = cellStart(from), x1 = cellEnd(i - 1);
          const r = s('rect', { x: x0.toFixed(1), y: M.t, width: Math.max(2, x1 - x0).toFixed(1), height: ih }, svg);
          r.style.fill = 'var(--shade)';
          from = -1;
        }
      }
    }
    ticks.forEach((t, i) => {
      const y = Y(t).toFixed(1);
      const l = s('line', { x1: T ? 0 : M.l, x2: T ? PW : W - M.r, y1: y, y2: y }, svg);
      l.style.stroke = i ? 'var(--grid)' : 'var(--axis)';
      txt(axis || svg, M.l - 6, +y + 4, tickLabels[i], 'tick', 'end');
    });
    if (T) {
      // 라벨 사이를 58px 이상 띄워 오른쪽(최신)부터 놓는다. tickMs 가 있으면 그 간격의 정각에만 놓는다
      const every = o.tickMs ? (o.tickMs.find(v => v * k >= 58) || o.tickMs[o.tickMs.length - 1]) : 0;
      const off = o.tickOffset || 0;
      let last = Infinity;
      for (let i = n - 1; i >= 0; i--) {
        const x = X(i);
        if (x < 20) break;
        if (every && (T.xs[i] + off) % every !== 0) continue;
        if (last - x < 58 || x > PW - M.r - 10) continue;
        txt(svg, x, Ht - 8, o.x(i), 'tick', 'middle');
        last = x;
      }
    } else {
      const every = Math.max(1, Math.ceil(n / Math.max(3, Math.floor(iw / 58))));
      for (let i = n - 1; i >= 0; i -= every) {
        txt(svg, X(i), Ht - 8, o.x(i), 'tick', i === n - 1 ? 'end' : 'middle');
      }
    }

    // 띠를 모두 먼저 깔고 평균선을 그 위에 그린다. 여러 방이면 띠도 방 색(옅게)
    const multi = o.series.length > 1;
    const P = (i, v) => X(i).toFixed(1) + ' ' + Y(v).toFixed(1);
    const faintOf = se => i => se.coverage && se.coverage[i] != null && se.coverage[i] < 0.5;
    const hasBand = se => o.band && se.min && se.max;
    o.series.forEach(se => {
      if (!hasBand(se)) return;
      const faint = faintOf(se), ok = i => se.min[i] != null && se.max[i] != null;
      let solid = '', faded = '';
      for (let i = 0; i < n; i++) {
        if (!ok(i)) continue;
        // 이웃 구간이 비면 한 칸 폭의 띠로 그려서 외딴 점도 범위가 보이게 한다
        if (i < n - 1 && ok(i + 1)) {
          const q = 'M' + P(i, se.max[i]) + 'L' + P(i + 1, se.max[i + 1]) + 'L' + P(i + 1, se.min[i + 1]) + 'L' + P(i, se.min[i]) + 'Z';
          if (faint(i) || faint(i + 1)) faded += q; else solid += q;
        } else if (!(i > 0 && ok(i - 1))) {
          const w = Math.max(2, (cellEnd(i) - cellStart(i)) / 2), x = X(i);
          const q = 'M' + (x - w / 2).toFixed(1) + ' ' + Y(se.max[i]).toFixed(1) + 'H' + (x + w / 2).toFixed(1) + 'V' + Y(se.min[i]).toFixed(1) + 'H' + (x - w / 2).toFixed(1) + 'Z';
          if (faint(i)) faded += q; else solid += q;
        }
      }
      const drawBand = (d, op) => {
        if (!d) return;
        const p = s('path', { d, 'fill-rule': 'nonzero', opacity: op, class: multi ? 'rband' : null }, svg);
        p.style.fill = multi ? color(se.color) : 'var(--band)';
      };
      drawBand(solid, 1); drawBand(faded, 0.5);
    });

    // 점선으로 이을 만한 공백인가. 끄는 차트는 빈 시간의 길이로 판단한다
    const linkable = T ? (i, j) => T.xs[j] - T.xs[i + 1] < T.view * GAP_LINK_MAX : (i, j) => j - i - 1 < n * GAP_LINK_MAX;
    let bridged = false;
    o.series.forEach(se => {
      const c = color(se.color);
      const ok = i => se.avg[i] != null, faint = faintOf(se);
      let solid = '', faded = '', dashed = '';
      const linked = new Set();  // 점선으로 이어진 끝점 (외딴 점 표시 생략)
      for (let i = 0; i < n - 1; i++) {
        if (!ok(i)) continue;
        if (ok(i + 1)) {
          const seg = 'M' + P(i, se.avg[i]) + 'L' + P(i + 1, se.avg[i + 1]);
          if (faint(i) || faint(i + 1)) faded += seg; else solid += seg;
          continue;
        }
        // 빈 칸 뒤의 첫 값과 점선으로 잇는다. 공백이 길면 지금처럼 끊는다
        let j = i + 2;
        while (j < n && !ok(j)) j++;
        if (j < n && linkable(i, j)) {
          dashed += 'M' + P(i, se.avg[i]) + 'L' + P(j, se.avg[j]);
          linked.add(i); linked.add(j);
        }
      }
      const drawLinePath = (d, op) => {
        if (!d) return;
        const p = s('path', { d, fill: 'none', 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', opacity: op }, svg);
        p.style.stroke = c;
      };
      if (dashed) {
        const p = s('path', { d: dashed, fill: 'none', 'stroke-width': 1, 'stroke-dasharray': '3 3', opacity: 0.5, class: 'gapline' }, svg);
        p.style.stroke = c;
        bridged = true;
      }
      drawLinePath(faded, 0.35); drawLinePath(solid, 1);
      for (let i = 0; i < n; i++) {
        if (ok(i) && !linked.has(i) && !(i > 0 && ok(i - 1)) && !(i < n - 1 && ok(i + 1))) {
          const d = s('circle', { cx: X(i), cy: Y(se.avg[i]), r: 3, opacity: faint(i) ? 0.35 : 1 }, svg);
          d.style.fill = c;
        }
      }
    });

    // 끝 라벨: 겹치면 범례와 툴팁에 맡긴다
    if (o.directLabels && !T) {
      labels = o.series.map(se => { const i = lastIdx(se); return i < 0 ? null : { n: se.name, c: color(se.color), y: Y(se.avg[i]), x: X(i) }; }).filter(Boolean).sort((a, b) => a.y - b.y);
      const collide = labels.some((l, i) => i > 0 && l.y - labels[i - 1].y < 13);
      if (!collide) labels.forEach(l => txt(svg, W - M.r + 6, l.y + 4, l.n, 'endlabel'));
    }
    if (o.series.length === 1) {
      const se = o.series[0], i = lastIdx(se);
      if (i >= 0) {
        const d = s('circle', { cx: X(i), cy: Y(se.avg[i]), r: 4, 'stroke-width': 2 }, svg);
        d.style.fill = color(se.color); d.style.stroke = 'var(--surface)';
      }
    }

    const xl = s('line', { y1: M.t, y2: M.t + ih, 'stroke-width': 1, visibility: 'hidden' }, svg);
    xl.style.stroke = 'var(--ink2)';
    const dots = o.series.map(se => {
      const d = s('circle', { r: 4, 'stroke-width': 2, visibility: 'hidden' }, svg);
      d.style.fill = color(se.color); d.style.stroke = 'var(--surface)';
      return d;
    });
    if (T) {
      const wrap = document.createElement('div');
      wrap.className = 'panwrap';
      pan = document.createElement('div');
      pan.className = 'pan';
      pan.appendChild(svg);
      wrap.append(axis, pan);
      host.appendChild(wrap);
    } else {
      host.appendChild(svg);
    }
    if (bridged) {
      const key = document.createElement('div'), sw = document.createElement('i'), span = document.createElement('span');
      key.className = 'legend gapkey'; sw.className = 'dash-key';
      span.append(sw, '점선은 데이터가 없는 구간');
      key.appendChild(span); host.appendChild(key);
    }
    const t = tipFor(host);
    let cur = -1;
    const hide = () => { cur = -1; xl.setAttribute('visibility', 'hidden'); dots.forEach(d => d.setAttribute('visibility', 'hidden')); t.hidden = true; active.delete(ctl); };
    const ctl = { host, hide };
    const show = i => {
      cur = i; active.add(ctl);
      xl.setAttribute('visibility', 'visible'); xl.setAttribute('x1', X(i)); xl.setAttribute('x2', X(i));
      let top = Ht;
      o.series.forEach((se, k) => {
        if (se.avg[i] == null) { dots[k].setAttribute('visibility', 'hidden'); return; }
        const y = Y(se.avg[i]);
        dots[k].setAttribute('visibility', 'visible'); dots[k].setAttribute('cx', X(i)); dots[k].setAttribute('cy', y);
        top = Math.min(top, y);
      });
      const rows = [];
      const single = o.series.length === 1 && o.band && o.series[0].min;
      o.series.forEach(se => {
        if (single && se.avg[i] == null) {
          rows.push({ key: '평균', value: '데이터 없음', color: color(se.color) });
        } else if (single) {
          rows.push({ key: '평균', value: fmt(se.avg[i], o.digits) + ' ' + o.unit, color: color(se.color) });
          rows.push({ key: '최대', value: fmt(se.max[i], o.digits) });
          rows.push({ key: '최소', value: fmt(se.min[i], o.digits) });
        } else {
          const range = o.band && se.min && se.min[i] != null && se.max[i] != null ? ' (' + fmt(se.min[i], o.digits) + '~' + fmt(se.max[i], o.digits) + ')' : '';
          rows.push({ key: se.name, value: se.avg[i] == null ? '데이터 없음' : fmt(se.avg[i], o.digits) + range + ' ' + o.unit, color: color(se.color) });
        }
      });
      const cov = o.series.length === 1 && o.series[0].coverage ? o.series[0].coverage[i] : null;
      if (cov != null && cov < 0.9) rows.push({ key: '수집률', value: Math.round(cov * 100) + '%' });
      if (o.extra) rows.push(...o.extra(i));
      fillTip(t, o.tip(i), rows);
      const hx = T ? M.l + X(i) - pan.scrollLeft : X(i);  // 툴팁은 host 기준 좌표
      placeTip(host, t, hx, top === Ht ? M.t + 40 : top);
    };
    const idx = e => {
      const r = svg.getBoundingClientRect();
      if (!T) return clamp(Math.floor((e.clientX - r.left - M.l) / iw * n), 0, n - 1);
      const tm = T.t0 + (e.clientX - r.left) / k;
      let a = 0, b = n - 1;  // xs[i] <= tm 인 마지막 i
      while (a < b) { const m = (a + b + 1) >> 1; if (T.xs[m] <= tm) a = m; else b = m - 1; }
      return a;
    };
    const dragging = () => !!(T && T.dragging && T.dragging());
    svg.addEventListener('pointermove', e => { if (!dragging() && (e.pointerType === 'mouse' || e.buttons)) show(idx(e)); });
    svg.addEventListener('pointerdown', e => show(idx(e)));
    svg.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse') hide(); });
    const reveal = i => {  // 키보드로 옮긴 점이 화면 밖이면 그쪽으로 끈다
      if (!T) return;
      const x = X(i), vw = pan.clientWidth;
      if (x < pan.scrollLeft + 8 || x > pan.scrollLeft + vw - 8) pan.scrollLeft = x - vw / 2;
    };
    svg.addEventListener('keydown', e => {
      let i = null;
      if (e.key === 'ArrowLeft') i = Math.max(0, (cur < 0 ? n : cur) - 1);
      else if (e.key === 'ArrowRight') i = Math.min(n - 1, cur + 1);
      if (i == null) return;
      e.preventDefault(); reveal(i); show(i);
    });
    svg.addEventListener('blur', hide);
    return { pan, k, hide };
  }

  // ---- 가로로 끄는 차트 묶음 ------------------------------------------------------------
  /* 같은 탭의 차트가 함께 움직인다. 처음엔 최신 구간을 보여 주고, 왼쪽 끝에 가까워지면
     이전 묶음을 받아 앞에 붙인다. 불러온 묶음은 최대 max 개이고, 넘치면 보는 곳에서 먼 쪽을 버린다.
     o: {view(ms), first: 묶음, load(end) -> Promise<묶음>, merge([data...]) -> 합친 data, range(t0, t1) -> 문구, max}
     묶음: {start, end, data, hasOlder, oldest}. end 는 그 묶음이 끝나는 시각 (= 다음 묶음의 start) */
  const PAN_MAX = 10, PAN_NEAR = 0.5;
  function pan(o) {
    const g = { chunks: [o.first], newer: [], hasOlder: !!o.first.hasOlder, oldest: o.first.oldest, loading: false,
      left: null, charts: [], domains: {}, merged: null, max: o.max || PAN_MAX, view: o.view };
    let drag = null;
    const remerge = () => { g.merged = o.merge(g.chunks.map(c => c.data)); };
    remerge();
    g.bounds = () => {
      const t1 = g.chunks[g.chunks.length - 1].end;
      let t0 = g.chunks[0].start;
      if (!g.hasOlder && g.oldest != null && g.oldest > t0) t0 = g.oldest;  // 첫 기록 앞의 빈 기간은 그리지 않는다
      return [Math.min(t0, t1 - g.view), t1];
    };
    const clampLeft = v => { const [t0, t1] = g.bounds(); return clamp(v, t0, t1 - g.view); };

    // ◀ 이전 / 보고 있는 구간 / 다음 ▶ / 최신
    const btn = (text, label, fn) => {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'ghost'; b.textContent = text; b.setAttribute('aria-label', label);
      b.addEventListener('click', fn);
      return b;
    };
    const prev = btn('◀ 이전', '이전 구간', () => moveTo(g.left - g.view));
    const next = btn('다음 ▶', '다음 구간', () => moveTo(g.left + g.view));
    const latest = btn('최신', '최신 구간으로', () => toLatest());
    const label = document.createElement('span');
    label.className = 'range'; label.setAttribute('aria-live', 'polite');
    g.bar = document.createElement('div');
    g.bar.className = 'panbar';
    g.bar.append(prev, label, next, latest);
    const updateBar = () => {
      if (g.left == null) return;
      const [t0, t1] = g.bounds();
      label.textContent = o.range(g.left, g.left + g.view) + (g.loading ? ' · 불러오는 중' : '');
      prev.disabled = g.left <= t0 + 1 && !g.hasOlder;
      const atEnd = g.left >= t1 - g.view - 1 && !g.newer.length;
      next.disabled = atEnd; latest.disabled = atEnd;
    };

    const place = c => { if (c.r && c.r.pan) c.r.pan.scrollLeft = (g.left - g.bounds()[0]) * c.r.k; };
    function moveTo(v) {
      g.left = clampLeft(v);
      g.charts.forEach(place);
      updateBar();
      check();
    }
    async function fetchChunk(end) {
      g.loading = true; updateBar();
      try { return await o.load(end); } finally { g.loading = false; }
    }
    async function check() {
      if (g.loading || !g.charts.length || g.left == null) return;
      const [t0, t1] = g.bounds();
      try {
        if (g.hasOlder && g.left - t0 < g.view * PAN_NEAR) {
          const c = await fetchChunk(g.chunks[0].start);
          g.chunks.unshift(c);
          g.hasOlder = !!c.hasOlder;
          if (c.oldest != null) g.oldest = c.oldest;
          if (g.chunks.length > g.max) g.newer.push(g.chunks.pop().end);  // 가장 최신 묶음을 버리고 끝 시각만 기억한다
        } else if (g.newer.length && t1 - (g.left + g.view) < g.view * PAN_NEAR) {
          const c = await fetchChunk(g.newer.pop());
          g.chunks.push(c);
          if (g.chunks.length > g.max) { g.chunks.shift(); g.hasOlder = true; }
        } else { updateBar(); return; }
      } catch (e) { updateBar(); return; }  // 실패하면 다음 스크롤 때 다시 시도한다
      if (!g.charts.some(c => c.host.isConnected)) return;  // 그 사이 다른 화면으로 바뀜
      remerge();
      g.left = clampLeft(g.left);  // 보던 시각을 그대로 두므로 앞에 붙여도 화면이 튀지 않는다
      g.charts.forEach(c => c.host._draw());
      updateBar();
      check();
    }
    async function toLatest() {
      if (g.newer.length) {  // 버린 최신 묶음이 있으면 처음 상태로 다시 받는다
        let c;
        try { c = await fetchChunk(null); } catch (e) { updateBar(); return; }
        g.chunks = [c]; g.newer = []; g.hasOlder = !!c.hasOlder; g.oldest = c.oldest;
        remerge();
        g.left = g.bounds()[1] - g.view;
        g.charts.forEach(x => x.host._draw());
        updateBar();
        return;
      }
      moveTo(Infinity);
    }

    /* host 에 차트 하나를 붙인다. make(merged) 는 line 옵션에 xs(구간 시작 시각 배열), xend 를 넣어 돌려준다.
       key 가 같은 차트끼리 y축 범위를 공유한다 (다시 그려도 좁아지지 않게). */
    g.add = (host, make, key) => {
      const c = { host };
      const dom = g.domains[key] || (g.domains[key] = {});
      g.charts.push(c);
      mount(host, () => {
        const opt = make(g.merged);
        const [t0, t1] = g.bounds();
        if (g.left == null) g.left = t1 - g.view;
        g.left = clampLeft(g.left);
        opt.n = opt.xs.length;
        opt.time = { xs: opt.xs, xend: opt.xend, t0, t1, view: g.view, domain: dom, dragging: () => !!(drag && drag.moved) };
        c.r = drawLine(host, opt);
        const p = c.r.pan;
        place(c);
        p.addEventListener('scroll', () => {
          // 다시 그려 떼어 낸 옛 요소에도 밀려 있던 scroll 이 온다 (scrollLeft 0). 그대로 쓰면 왼쪽 끝으로 튄다
          if (!p.isConnected || c.r.pan !== p) return;
          c.r.hide();
          const v = clampLeft(g.bounds()[0] + p.scrollLeft / c.r.k);
          if (Math.abs(v - g.left) * c.r.k < 1.5) return;  // 다른 차트를 맞추며 생긴 반올림 차이
          g.left = v;
          g.charts.forEach(x => { if (x !== c) place(x); });
          updateBar();
          check();
        }, { passive: true });
        // 마우스 끌기. 터치는 브라우저 기본 가로 스크롤, 가로 휠도 기본 동작을 쓴다
        p.addEventListener('pointerdown', e => {
          if (e.pointerType !== 'mouse' || e.button !== 0) return;
          drag = { x: e.clientX, left: p.scrollLeft, moved: false, id: e.pointerId };
        });
        p.addEventListener('pointermove', e => {
          if (!drag || !(e.buttons & 1)) return;
          const dx = e.clientX - drag.x;
          if (!drag.moved && Math.abs(dx) > 4) { drag.moved = true; p.setPointerCapture(drag.id); p.classList.add('dragging'); c.r.hide(); }
          if (drag.moved) p.scrollLeft = drag.left - dx;
        });
        const end = () => { if (drag && drag.moved) c.r.hide(); drag = null; p.classList.remove('dragging'); };
        p.addEventListener('pointerup', end);
        p.addEventListener('pointercancel', end);
        updateBar();
      });
    };
    g.visible = () => [g.left, g.left + g.view];  // 지금 보이는 구간 [시작, 끝)
    g.start = () => { updateBar(); check(); };
    return g;
  }

  // ---- 세로 막대 ----------------------------------------------------------------
  /* o: {labels, title(i), values, unit, digits, aria}. 마지막 구간만 강조색 */
  function bars(host, o) { mount(host, () => drawBars(host, o)); }

  function drawBars(host, o) {
    host.replaceChildren();
    const W = width(host), Ht = W < 520 ? 200 : 240, n = o.values.length;
    const vals = o.values.filter(v => v != null);
    const ticks = niceTicks(0, Math.max(0.1, Math.max.apply(null, vals)), 4);
    const top = ticks[ticks.length - 1], labels = ticks.map(t => fmt(t, t < 10 && t % 1 ? 1 : 0));
    const M = { l: Math.max(26, Math.max.apply(null, labels.map(x => x.length)) * 7 + 8), r: 8, t: 10, b: 26 };
    const iw = W - M.l - M.r, ih = Ht - M.t - M.b, step = iw / n, bw = Math.min(24, Math.max(2, step - 2));
    const X = i => M.l + step * i + (step - bw) / 2, Y = v => M.t + ih - v / top * ih;
    const svg = s('svg', { width: W, height: Ht, tabindex: 0, role: 'img', 'aria-label': o.aria });
    ticks.forEach((t, i) => {
      const y = Y(t).toFixed(1);
      const l = s('line', { x1: M.l, x2: W - M.r, y1: y, y2: y }, svg);
      l.style.stroke = i ? 'var(--grid)' : 'var(--axis)';
      txt(svg, M.l - 6, +y + 4, labels[i], 'tick', 'end');
    });
    const every = Math.max(1, Math.ceil(n / Math.max(3, Math.floor(iw / 46))));
    for (let i = n - 1; i >= 0; i -= every) txt(svg, X(i) + bw / 2, Ht - 8, o.labels[i], 'tick', i === n - 1 ? 'end' : 'middle');
    const rects = o.values.map((v, i) => {
      if (!v) return null;
      const x = X(i), y = Y(v), b = M.t + ih, r = Math.min(4, bw / 2, b - y);
      const d = `M${x} ${b}V${y + r}Q${x} ${y} ${x + r} ${y}H${x + bw - r}Q${x + bw} ${y} ${x + bw} ${y + r}V${b}Z`;
      const p = s('path', { d }, svg); p.style.fill = i === n - 1 ? 'var(--accent)' : 'var(--hbar)';
      return p;
    });
    host.appendChild(svg);
    const t = tipFor(host);
    let cur = -1;
    const hide = () => { if (cur >= 0 && rects[cur]) rects[cur].setAttribute('opacity', 1); cur = -1; t.hidden = true; active.delete(ctl); };
    const ctl = { host, hide };
    const show = i => {
      if (cur >= 0 && rects[cur]) rects[cur].setAttribute('opacity', 1);
      cur = i; active.add(ctl);
      if (rects[i]) rects[i].setAttribute('opacity', 0.7);
      fillTip(t, o.title(i), [{ key: o.seriesName || '합계', value: fmt(o.values[i], o.digits) + ' ' + o.unit }]);
      placeTip(host, t, X(i) + bw / 2, Y(o.values[i] || 0));
    };
    const idx = e => clamp(Math.floor((e.clientX - svg.getBoundingClientRect().left - M.l) / step), 0, n - 1);
    svg.addEventListener('pointermove', e => { if (e.pointerType === 'mouse' || e.buttons) show(idx(e)); });
    svg.addEventListener('pointerdown', e => show(idx(e)));
    svg.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse') hide(); });
    svg.addEventListener('keydown', e => {
      if (e.key === 'ArrowLeft') { show(Math.max(0, (cur < 0 ? n : cur) - 1)); e.preventDefault(); }
      else if (e.key === 'ArrowRight') { show(Math.min(n - 1, cur + 1)); e.preventDefault(); }
    });
    svg.addEventListener('blur', hide);
  }

  // ---- 히트맵 -------------------------------------------------------------------
  let ramps = null;
  function hexRgb(h) { h = h.trim().replace('#', ''); return [0, 2, 4].map(i => parseInt(h.substr(i, 2), 16)); }
  function resetTheme() { ramps = null; }
  /* kind 'lux' 는 조도 전용 전구색, 생략하면 기본 색 */
  function rampColor(t, kind) {
    if (!ramps) {
      const st = getComputedStyle(document.documentElement), read = n => hexRgb(st.getPropertyValue(n));
      ramps = { base: [read('--lo'), read('--hi')], lux: [read('--lux0'), read('--lux1'), read('--lux2')] };
    }
    const stops = ramps[kind || 'base'], p = clamp(t, 0, 1) * (stops.length - 1);
    const i = Math.min(stops.length - 2, Math.floor(p)), f = p - i;
    return 'rgb(' + [0, 1, 2].map(k => Math.round(stops[i][k] + (stops[i + 1][k] - stops[i][k]) * f)).join(',') + ')';
  }

  /* o: {rows, cols, L, ch(W), rowEvery(r), colEvery, fill(r,c)->css|null, tip(r,c)->{title, rows}, aria} */
  function heat(host, o) { mount(host, () => drawHeat(host, o)); }

  function drawHeat(host, o) {
    host.replaceChildren();
    const W = width(host), nc = o.cols.length, nr = o.rows.length, L = o.L, B = 20, cw = (W - L - 2) / nc, ch = o.ch(W), Ht = nr * ch + B;
    const gap = cw >= 6 ? 1 : 0;
    const svg = s('svg', { width: W, height: Ht, tabindex: 0, role: 'img', 'aria-label': o.aria });
    for (let r = 0; r < nr; r++) if (o.rowEvery(r)) txt(svg, L - 6, r * ch + ch / 2 + 4, o.rows[r], 'tick', 'end');
    for (let r = 0; r < nr; r++) {
      for (let c = 0; c < nc; c++) {
        const f = o.fill(r, c);
        if (f == null) continue;
        const el = s('rect', { x: (L + c * cw + gap).toFixed(1), y: r * ch + 1, width: Math.max(1, cw - 2 * gap).toFixed(1), height: ch - 2, rx: gap ? 2 : 0 }, svg);
        el.style.fill = f;
      }
    }
    const every = Math.max(o.colEvery || 1, Math.ceil(nc / Math.max(3, Math.floor((W - L) / 42))));
    for (let c = 0; c < nc; c += every) txt(svg, L + c * cw + cw / 2, Ht - 5, o.cols[c], 'tick', 'middle');
    const mark = s('rect', { fill: 'none', 'stroke-width': 1.5, visibility: 'hidden', rx: 2 }, svg);
    mark.style.stroke = 'var(--ink)';
    host.appendChild(svg);
    const t = tipFor(host);
    let cr = -1, cc = -1;
    const hide = () => { cr = cc = -1; mark.setAttribute('visibility', 'hidden'); t.hidden = true; active.delete(ctl); };
    const ctl = { host, hide };
    const show = (r, c) => {
      cr = r; cc = c; active.add(ctl);
      mark.setAttribute('visibility', 'visible');
      mark.setAttribute('x', L + c * cw); mark.setAttribute('y', r * ch); mark.setAttribute('width', cw); mark.setAttribute('height', ch);
      const info = o.tip(r, c);
      fillTip(t, info.title, info.rows);
      placeTip(host, t, L + c * cw + cw / 2, r * ch);
    };
    const pos = e => {
      const b = svg.getBoundingClientRect();
      return [clamp(Math.floor((e.clientY - b.top) / ch), 0, nr - 1), clamp(Math.floor((e.clientX - b.left - L) / cw), 0, nc - 1)];
    };
    const inPlot = e => { const b = svg.getBoundingClientRect(); return e.clientY - b.top < nr * ch && e.clientX - b.left >= L; };
    svg.addEventListener('pointermove', e => { if ((e.pointerType === 'mouse' || e.buttons) && inPlot(e)) show.apply(null, pos(e)); });
    svg.addEventListener('pointerdown', e => { if (inPlot(e)) show.apply(null, pos(e)); });
    svg.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse') hide(); });
    svg.addEventListener('keydown', e => {
      const d = { ArrowLeft: [0, -1], ArrowRight: [0, 1], ArrowUp: [-1, 0], ArrowDown: [1, 0] }[e.key];
      if (!d) return;
      e.preventDefault();
      show(clamp((cr < 0 ? 0 : cr) + d[0], 0, nr - 1), clamp((cc < 0 ? 0 : cc) + d[1], 0, nc - 1));
    });
    svg.addEventListener('blur', hide);
  }

  window.Charts = { line, pan, bars, heat, rampColor, resetTheme, color, fmt };
})();
