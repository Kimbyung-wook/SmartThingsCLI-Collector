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
     band: 최소~최대 띠. 계열이 하나면 무채색(--band), 여럿이면 계열 색을 옅게(.rband) */
  function line(host, o) { mount(host, () => drawLine(host, o)); }
  // 빈 구간을 점선으로 잇는 최대 길이 (x축 전체 칸 수에 대한 비율). 이 이상이면 끊는다
  const GAP_LINK_MAX = 0.25;

  function drawLine(host, o) {
    host.replaceChildren();
    const W = width(host), Ht = o.height ? o.height(W) : (W < 520 ? 230 : 300), n = o.n;
    const vals = [];
    o.series.forEach(se => {
      for (let i = 0; i < n; i++) {
        if (se.avg[i] != null) vals.push(se.avg[i]);
        if (o.band && se.min && se.min[i] != null) { vals.push(se.min[i]); vals.push(se.max[i]); }
      }
    });
    let lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    if (o.domain) { lo = o.domain[0]; hi = o.domain[1]; }

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
    if (o.directLabels) {
      const widest = Math.max.apply(null, o.series.map(se => se.name.length));
      M.r = clamp(widest * 12 + 18, 40, 84);
    }
    const iw = W - M.l - M.r, ih = Ht - M.t - M.b;
    const X = i => M.l + (n > 1 ? (i + 0.5) / n * iw : iw / 2);
    const Y = v => M.t + ih - map(v) * ih;

    const svg = s('svg', { width: W, height: Ht, tabindex: 0, role: 'img', 'aria-label': o.aria });
    if (o.shade) {  // 경고 구간 표시. 짧은 구간도 보이도록 최소 2px
      const step = iw / n;
      for (let i = 0, from = -1; i <= n; i++) {
        const on = i < n && o.shade(i);
        if (on && from < 0) from = i;
        if (!on && from >= 0) {
          const r = s('rect', { x: (X(from) - step / 2).toFixed(1), y: M.t, width: Math.max(2, (i - from) * step).toFixed(1), height: ih }, svg);
          r.style.fill = 'var(--shade)';
          from = -1;
        }
      }
    }
    ticks.forEach((t, i) => {
      const y = Y(t).toFixed(1);
      const l = s('line', { x1: M.l, x2: W - M.r, y1: y, y2: y }, svg);
      l.style.stroke = i ? 'var(--grid)' : 'var(--axis)';
      txt(svg, M.l - 6, +y + 4, tickLabels[i], 'tick', 'end');
    });
    const every = Math.max(1, Math.ceil(n / Math.max(3, Math.floor(iw / 58))));
    for (let i = n - 1; i >= 0; i -= every) {
      txt(svg, X(i), Ht - 8, o.x(i), 'tick', i === n - 1 ? 'end' : 'middle');
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
          const w = Math.max(2, iw / n / 2), x = X(i);
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
        if (j < n && j - i - 1 < n * GAP_LINK_MAX) {
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
    if (o.directLabels) {
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
    host.appendChild(svg);
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
      placeTip(host, t, X(i), top === Ht ? M.t + 40 : top);
    };
    const idx = e => {
      const r = svg.getBoundingClientRect();
      return clamp(Math.floor((e.clientX - r.left - M.l) / iw * n), 0, n - 1);
    };
    svg.addEventListener('pointermove', e => { if (e.pointerType === 'mouse' || e.buttons) show(idx(e)); });
    svg.addEventListener('pointerdown', e => show(idx(e)));
    svg.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse') hide(); });
    svg.addEventListener('keydown', e => {
      if (e.key === 'ArrowLeft') { show(Math.max(0, (cur < 0 ? n : cur) - 1)); e.preventDefault(); }
      else if (e.key === 'ArrowRight') { show(Math.min(n - 1, cur + 1)); e.preventDefault(); }
    });
    svg.addEventListener('blur', hide);
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

  window.Charts = { line, bars, heat, rampColor, resetTheme, color, fmt };
})();
