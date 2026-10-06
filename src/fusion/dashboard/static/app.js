/* Fusion Ledger: the dashboard's only script. No framework, no build step, no network beyond this
   origin. Every node is made with createElement/textContent (never innerHTML), so nothing read
   from the database can become markup. */
(function () {
  'use strict';

  // ---------------------------------------------------------------------------- DOM helpers
  var SVGNS = 'http://www.w3.org/2000/svg';

  function append(el, kids) {
    kids.forEach(function (k) {
      if (Array.isArray(k)) { append(el, k); return; }
      if (k === null || k === undefined || k === false) return;
      el.append(k.nodeType ? k : document.createTextNode(String(k)));
    });
  }
  function apply(el, props) {
    Object.keys(props || {}).forEach(function (key) {
      var v = props[key];
      if (v === null || v === undefined || v === false) return;
      if (key === 'class') el.setAttribute('class', v);
      else if (key === 'text') el.textContent = v;
      else if (key.slice(0, 2) === 'on') el.addEventListener(key.slice(2), v);
      else el.setAttribute(key, v === true ? '' : v);
    });
  }
  function h(tag, props) {
    var el = document.createElement(tag);
    apply(el, props);
    append(el, Array.prototype.slice.call(arguments, 2));
    return el;
  }
  function s(tag, props) {
    var el = document.createElementNS(SVGNS, tag);
    apply(el, props);
    append(el, Array.prototype.slice.call(arguments, 2));
    return el;
  }
  function $(sel, root) { return (root || document).querySelector(sel); }

  // ------------------------------------------------------------------------------ formatting
  function usd(n) {
    if (n === null || n === undefined) return '—';
    var a = Math.abs(n);
    if (a >= 100) return '$' + n.toFixed(0);
    if (a >= 1) return '$' + n.toFixed(2);
    return '$' + n.toFixed(4);
  }
  function usdShort(n) {
    if (n === 0) return '$0';
    return n >= 10 ? '$' + n.toFixed(0) : n >= 1 ? '$' + n.toFixed(1) : '$' + n.toFixed(2);
  }
  function pct(x, digits) { return x === null || x === undefined ? '—' : (x * 100).toFixed(digits || 0) + '%'; }
  function secs(ms) {
    if (ms === null || ms === undefined) return '—';
    return ms < 1000 ? Math.round(ms) + ' ms' : (ms / 1000).toFixed(ms < 10000 ? 2 : 1) + ' s';
  }
  function int(n) { return n === null || n === undefined ? '—' : Number(n).toLocaleString('en-US'); }
  function utc(ts) { return new Date(String(ts).replace(' ', 'T') + 'Z'); }
  function when(ts) {
    if (!ts) return '—';
    var d = utc(ts);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + ', ' +
      d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false });
  }
  function day(ts) { return utc(ts + ' 00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric' }); }
  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

  // ------------------------------------------------------------------------------- tooltips
  var tip = $('#tip');
  function showTip(text, x, y) {
    var lines = String(text).split('\n');
    tip.textContent = '';
    lines.forEach(function (line, i) { tip.append(h(i === 0 ? 'b' : 'span', { text: line })); });
    tip.style.display = 'block';
    var w = tip.offsetWidth, hgt = tip.offsetHeight;
    tip.style.left = Math.max(8, Math.min(x + 14, window.innerWidth - w - 8)) + 'px';
    tip.style.top = Math.max(8, Math.min(y + 14, window.innerHeight - hgt - 8)) + 'px';
  }
  function hideTip() { tip.style.display = 'none'; }
  document.addEventListener('pointermove', function (e) {
    var t = e.target.closest && e.target.closest('[data-tip]');
    if (t) showTip(t.getAttribute('data-tip'), e.clientX, e.clientY);
    else if (!e.target.closest || !e.target.closest('[data-live-tip]')) hideTip();
  });
  document.addEventListener('focusin', function (e) {
    var t = e.target.closest && e.target.closest('[data-tip]');
    if (!t) return;
    var r = t.getBoundingClientRect();
    showTip(t.getAttribute('data-tip'), r.left + r.width / 2, r.top + r.height / 2);
  });
  document.addEventListener('focusout', hideTip);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') hideTip(); });

  // --------------------------------------------------------------------- colours by identity
  // Colour follows the entity: names are sorted, then take slots 3..8 in order (a seventh and later
  // fold into the neutral slot). Slots 1 and 2 are reserved: Fusion is blue and the baseline model
  // orange wherever they are compared, so no model or stage ever borrows them.
  var STAGE_SLOT = { panel: 's3', solo: 's3', refine: 's7', judge: 's4', synthesis: 's5', eval: 's8',
    shadow_baseline: 'sx', shadow_judge: 'sx' };
  function slots(names) {
    var map = {};
    Array.from(new Set(names)).sort().forEach(function (n, i) { map[n] = i < 6 ? 's' + (i + 3) : 'sx'; });
    return map;
  }
  function chip(name, slot) {
    return h('span', { class: 'chip' }, h('span', { class: 'sw ' + (slot || 'sx') }), name);
  }
  function slotNum(cls) { return cls === 'sx' ? 'x' : cls.slice(1); }

  var STATUS = {
    completed: ['ok', '✓', 'Completed'], failed: ['bad', '✕', 'Failed'], running: ['idle', '●', 'Running'],
    stopped: ['warn', '■', 'Stopped'], interrupted: ['warn', '■', 'Interrupted'], halted: ['warn', '◐', 'Halted'],
  };
  function status(name) {
    var spec = STATUS[name] || ['idle', '○', name];
    return h('span', { class: 'st ' + spec[0] }, h('span', { 'aria-hidden': 'true', text: spec[1] }), spec[2]);
  }

  // --------------------------------------------------------------------------- data access
  var cache = { meta: null };
  function api(path) {
    return fetch(path, { headers: { Accept: 'application/json' } }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
        return body;
      });
    });
  }
  function query(params) {
    var q = new URLSearchParams();
    Object.keys(params).forEach(function (k) { if (params[k] !== '' && params[k] !== null && params[k] !== undefined) q.set(k, params[k]); });
    var text = q.toString();
    return text ? '?' + text : '';
  }

  // ------------------------------------------------------------------------- small widgets
  function section(num, title, eyebrow, body) {
    return h('section', { class: 'sec rise' },
      h('header', null, h('h2', { text: title }), eyebrow ? h('span', { class: 'eyebrow', text: eyebrow }) : null),
      body);
  }
  function panel() { return h.apply(null, ['div', { class: 'panel' }].concat(Array.prototype.slice.call(arguments))); }
  function legend(items) {
    return h('div', { class: 'legend' }, items.map(function (it) {
      return h('span', null, h('span', { class: 'sw ' + it[1] }), it[0]);
    }));
  }
  function table(head, rows, opts) {
    opts = opts || {};
    var t = h('table', null,
      opts.caption ? h('caption', { class: 'sr', text: opts.caption }) : null,
      h('thead', null, h('tr', null, head.map(function (c, i) { return h('th', { scope: 'col', class: i === 0 || (opts.left || []).indexOf(i) >= 0 ? 'l' : null, text: c }); }))),
      h('tbody', null, rows));
    return h('div', { class: 'scroll' }, t);
  }
  function tableView(summary, head, rows, left) {
    return h('details', { class: 'tbl' }, h('summary', { text: summary }), table(head, rows, { caption: summary, left: left }));
  }
  function copyCommand(text) {
    var btn = h('button', { class: 'btn', type: 'button', onclick: function () {
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(function () { btn.textContent = 'Copied'; setTimeout(function () { btn.textContent = 'Copy'; }, 1500); });
    } }, 'Copy');
    return h('div', { class: 'cmd' }, h('code', { text: text }), btn);
  }
  function empty(title, text, commands) {
    return h('div', { class: 'empty rise' }, h('h2', { text: title }), h('p', { class: 'lede', text: text }),
      commands.map(copyCommand));
  }
  function hbars(rows, opts) {
    // rows: [{label, value, text, slot}] - bars scale to the largest value
    var max = Math.max.apply(null, rows.map(function (r) { return r.value; }).concat([1e-12]));
    var box = h('div', { class: 'hb' });
    rows.forEach(function (r, i) {
      var fill = h('div', { class: 'fill ' + (r.slot || '') });
      fill.style.width = Math.max(0.5, r.value / max * 100) + '%';
      fill.style.setProperty('--d', (i * 50) + 'ms');
      if (r.slot) fill.style.background = 'var(--c' + slotNum(r.slot) + ')';
      box.append(h('div', { class: 'lab', title: r.label, text: r.label }), h('div', { class: 'track' }, fill),
        h('div', { class: 'val', text: r.text }));
    });
    return box;
  }
  function niceCeil(x) {
    if (x <= 0) return 1;
    var p = Math.pow(10, Math.floor(Math.log10(x))), f = x / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
  }
  function niceStep(span, target) {
    var raw = span / target, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
    return (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * p;
  }
  function topRounded(x, y, w, hgt, r) {
    r = Math.min(r, w / 2, hgt);
    return 'M' + x + ',' + (y + hgt) + 'L' + x + ',' + (y + r) + 'Q' + x + ',' + y + ' ' + (x + r) + ',' + y +
      'L' + (x + w - r) + ',' + y + 'Q' + (x + w) + ',' + y + ' ' + (x + w) + ',' + (y + r) + 'L' + (x + w) + ',' + (y + hgt) + 'Z';
  }

  // ------------------------------------------------------------------- cumulative spend chart
  function spendChart(daily) {
    var W = 760, H = 300, m = { l: 54, r: 112, t: 16, b: 30 }, pw = W - m.l - m.r, ph = H - m.t - m.b;
    var cf = 0, cb = 0, pts = daily.map(function (d) {
      cf += d.paired_cost; cb += d.baseline;
      return { t: utc(d.day + ' 00:00:00').getTime(), f: cf, b: cb, day: d.day, runs: d.runs, paired: d.paired_runs, df: d.paired_cost, db: d.baseline };
    });
    var t0 = pts[0].t, t1 = pts[pts.length - 1].t;
    if (t1 === t0) { t0 -= 432e5; t1 += 432e5; }
    var ymax = niceCeil(Math.max(cb, cf) * 1.05);
    var X = function (t) { return m.l + (t - t0) / (t1 - t0) * pw; };
    var Y = function (v) { return m.t + ph - v / ymax * ph; };
    var svg = s('svg', { class: 'chart', viewBox: '0 0 ' + W + ' ' + H, role: 'img',
      'aria-label': 'Cumulative spend: Fusion ' + usd(cf) + ' against an estimated ' + usd(cb) + ' for the baseline model' });
    for (var i = 0; i <= 4; i++) {
      var v = ymax * i / 4;
      svg.append(s('line', { class: i ? 'grid' : 'axis', x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }),
        s('text', { class: 'tick', x: m.l - 8, y: Y(v) + 4, 'text-anchor': 'end', text: usdShort(v) }));
    }
    var step = niceStep((t1 - t0) / 864e5, 5), seen = {};
    for (var d = 0; d <= (t1 - t0) / 864e5 + 0.01; d += step) {
      var tt = t0 + d * 864e5, label = new Date(tt).toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
      if (seen[label]) continue; seen[label] = 1;
      svg.append(s('text', { class: 'tick', x: X(tt), y: H - 8, 'text-anchor': 'middle', text: label }));
    }
    var line = function (key) { return pts.map(function (p, i) { return (i ? 'L' : 'M') + X(p.t).toFixed(1) + ',' + Y(p[key]).toFixed(1); }).join(''); };
    var area = line('b') + pts.slice().reverse().map(function (p) { return 'L' + X(p.t).toFixed(1) + ',' + Y(p.f).toFixed(1); }).join('') + 'Z';
    svg.append(s('path', { class: 'area', d: area }),
      s('path', { class: 'line c2', d: line('b') }), s('path', { class: 'line c1', d: line('f') }));
    var last = pts[pts.length - 1], yb = Y(last.b), yf = Y(last.f);
    if (Math.abs(yb - yf) < 28) { yb = Math.min(yb, yf) - 8; yf = Math.max(yb + 28, yf + 8); }
    svg.append(
      s('text', { class: 'strong', x: W - m.r + 8, y: yb, text: 'Baseline est.' }), s('text', { class: 'tick', x: W - m.r + 8, y: yb + 14, text: usd(last.b) }),
      s('text', { class: 'strong', x: W - m.r + 8, y: yf, text: 'Fusion' }), s('text', { class: 'tick', x: W - m.r + 8, y: yf + 14, text: usd(last.f) }));
    // hover / keyboard layer
    var cross = s('line', { class: 'cross', y1: m.t, y2: m.t + ph, visibility: 'hidden' });
    var dotB = s('circle', { class: 'dot c2', r: 5, visibility: 'hidden' }), dotF = s('circle', { class: 'dot c1', r: 5, visibility: 'hidden' });
    var hit = s('rect', { class: 'hit', x: m.l, y: m.t, width: pw, height: ph, tabindex: '0', 'data-live-tip': '1',
      role: 'group', 'aria-label': 'Spend by day; use the left and right arrow keys to read each day' });
    var cur = pts.length - 1;
    function at(i, cx, cy) {
      cur = Math.max(0, Math.min(pts.length - 1, i));
      var p = pts[cur], x = X(p.t);
      cross.setAttribute('x1', x); cross.setAttribute('x2', x); cross.setAttribute('visibility', 'visible');
      dotB.setAttribute('cx', x); dotB.setAttribute('cy', Y(p.b)); dotB.setAttribute('visibility', 'visible');
      dotF.setAttribute('cx', x); dotF.setAttribute('cy', Y(p.f)); dotF.setAttribute('visibility', 'visible');
      var box = svg.getBoundingClientRect(), px = cx === undefined ? box.left + x / W * box.width : cx,
        py = cy === undefined ? box.top + Y(p.f) / H * box.height : cy;
      showTip(day(p.day) + '\nFusion so far: ' + usd(p.f) + '\nBaseline estimate so far: ' + usd(p.b) +
        '\nThat day: ' + plural(p.runs, 'run') + ', ' + usd(p.df) + ' (baseline ' + usd(p.db) + ')', px, py);
    }
    hit.addEventListener('pointermove', function (e) {
      var box = svg.getBoundingClientRect(), tx = t0 + ((e.clientX - box.left) / box.width * W - m.l) / pw * (t1 - t0), best = 0;
      pts.forEach(function (p, i) { if (Math.abs(p.t - tx) < Math.abs(pts[best].t - tx)) best = i; });
      at(best, e.clientX, e.clientY);
    });
    hit.addEventListener('pointerleave', function () { [cross, dotB, dotF].forEach(function (n) { n.setAttribute('visibility', 'hidden'); }); hideTip(); });
    hit.addEventListener('focus', function () { at(cur); });
    hit.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowLeft') { at(cur - 1); e.preventDefault(); } else if (e.key === 'ArrowRight') { at(cur + 1); e.preventDefault(); }
    });
    svg.append(cross, dotB, dotF, hit);
    var rows = daily.map(function (d, i) {
      return h('tr', null, h('td', { text: d.day }), h('td', { text: String(d.runs) }), h('td', { text: usd(d.paired_cost) }),
        h('td', { text: usd(d.baseline) }), h('td', { text: usd(pts[i].f) }), h('td', { text: usd(pts[i].b) }));
    });
    return h('figure', null, h('figcaption', { text: 'Cumulative spend, Fusion against the baseline model' }), svg,
      legend([['Fusion (actual)', 's1'], ['Baseline model (estimate for the same tokens)', 's2'], ['What you kept', 's3']]),
      tableView('Table view', ['Day', 'Runs', 'Fusion', 'Baseline est.', 'Fusion cum.', 'Baseline cum.'], rows));
  }

  function runsPerDay(daily) {
    var W = 460, H = 170, m = { l: 34, r: 8, t: 10, b: 24 }, pw = W - m.l - m.r, ph = H - m.t - m.b;
    var ymax = Math.max(1, niceCeil(Math.max.apply(null, daily.map(function (d) { return d.runs; }))));
    var slot = pw / daily.length, bw = Math.max(3, Math.min(26, slot * 0.62));
    var svg = s('svg', { class: 'chart', viewBox: '0 0 ' + W + ' ' + H, role: 'img', 'aria-label': 'Runs per day' });
    [0, 0.5, 1].forEach(function (f) {
      var y = m.t + ph - f * ph;
      svg.append(s('line', { class: f ? 'grid' : 'axis', x1: m.l, x2: W - m.r, y1: y, y2: y }),
        s('text', { class: 'tick', x: m.l - 6, y: y + 4, 'text-anchor': 'end', text: String(Math.round(ymax * f)) }));
    });
    daily.forEach(function (d, i) {
      var x = m.l + slot * i + (slot - bw) / 2, hgt = Math.max(2, d.runs / ymax * ph), y = m.t + ph - hgt;
      svg.append(s('g', { class: 'pt', tabindex: '0', 'data-tip': day(d.day) + '\n' + plural(d.runs, 'run') + '\n' + usd(d.cost) + ' spent' },
        s('rect', { class: 'hit', x: m.l + slot * i, y: m.t, width: slot, height: ph }),
        s('path', { class: 'mk c1', d: topRounded(x, y, bw, hgt, 3) })));
    });
    [0, daily.length - 1].forEach(function (i, k) {
      if (k && i === 0) return;
      svg.append(s('text', { class: 'tick', x: m.l + slot * i + slot / 2, y: H - 6, 'text-anchor': k ? 'end' : 'start', text: day(daily[i].day) }));
    });
    return h('figure', null, h('figcaption', { text: 'Runs per day' }), svg,
      tableView('Table view', ['Day', 'Runs', 'Spend'], daily.map(function (d) {
        return h('tr', null, h('td', { text: d.day }), h('td', { text: String(d.runs) }), h('td', { text: usd(d.cost) }));
      })));
  }

  function winRate(sh) {
    if (!sh.total) {
      return h('div', null, h('p', { class: 'lede', text: 'No shadow comparisons yet.' }),
        h('p', { class: 'muted', text: 'A shadow run also asks the real baseline model and has a judge pick a winner blind. It costs real money, so it is off until you turn it on (FUSION_SHADOW_MODE, see the configuration guide).' }));
    }
    var W = 460, H = 74, m = { l: 12, r: 12 }, pw = W - m.l - m.r, X = function (v) { return m.l + v * pw; };
    var svg = s('svg', { class: 'chart', viewBox: '0 0 ' + W + ' ' + H, role: 'img',
      'aria-label': 'Fusion won or tied ' + pct(sh.win_rate) + ' of ' + sh.total + ' blind comparisons (95% interval ' + pct(sh.ci_low) + ' to ' + pct(sh.ci_high) + ')' });
    svg.append(s('line', { class: 'axis', x1: m.l, x2: W - m.r, y1: 38, y2: 38 }),
      s('rect', { x: X(sh.ci_low), y: 30, width: Math.max(2, X(sh.ci_high) - X(sh.ci_low)), height: 16, rx: 4, fill: 'var(--c1)', opacity: '0.28' }),
      s('line', { class: 'cross', x1: X(0.5), x2: X(0.5), y1: 20, y2: 56 }),
      s('circle', { class: 'mk c1', cx: X(sh.win_rate), cy: 38, r: 7 }),
      s('text', { class: 'tick', x: X(0.5), y: 70, 'text-anchor': 'middle', text: 'even' }),
      s('text', { class: 'tick', x: m.l, y: 70, text: '0%' }), s('text', { class: 'tick', x: W - m.r, y: 70, 'text-anchor': 'end', text: '100%' }),
      s('text', { class: 'strong', x: X(sh.win_rate), y: 14, 'text-anchor': 'middle', text: pct(sh.win_rate) }));
    var recent = sh.recent.map(function (c) {
      return h('tr', null, h('td', null, h('a', { href: '#/runs/' + c.run_id, text: c.run_id.slice(-8) })),
        h('td', { class: 'l', text: c.task_type || '—' }), h('td', { class: 'l', text: c.winner === 'fusion' ? 'Fusion' : c.winner === 'baseline' ? 'Baseline' : 'Tie' }),
        h('td', { text: c.fusion_score === null ? '—' : c.fusion_score.toFixed(2) }), h('td', { text: c.baseline_score === null ? '—' : c.baseline_score.toFixed(2) }));
    });
    return h('div', null, svg,
      h('p', { class: 'muted', text: 'Fusion won ' + sh.fusion_wins + ', lost ' + sh.baseline_wins + ', tied ' + sh.ties + ' of ' + sh.total +
        ' blind comparisons against ' + (sh.recent[0] ? sh.recent[0].baseline_model : 'the baseline') + '. The band is the 95% interval (Wilson, a tie counts as half a win).' }),
      table(['Run', 'Task', 'Winner', 'Fusion', 'Baseline'], recent, { left: [1, 2], caption: 'Recent shadow comparisons' }));
  }

  // ---------------------------------------------------------------------------- overview
  function overviewView() {
    return api('/api/overview').then(function (o) {
      if (o.empty) {
        return empty('Nothing recorded yet', 'Fusion keeps every run in a local SQLite database, and this page reads it. Run something and it appears here. The offline mock provider is free and needs no keys.',
          ['fusion ask "How should I retry a failed HTTP call?" --mock', 'fusion bench run -d v1 --arms solo-cheap,panel-duo --mock --limit 24']);
      }
      var p = o.paired, t = o.totals, root = h('div');
      var hero = h('div', { class: 'hero rise' }, h('span', { class: 'eyebrow', text: 'The thesis, in your numbers' }));
      if (p.runs && p.baseline_usd > 0) {
        hero.append(h('p', null, 'Fusion spent ', h('b', { class: 'fu', text: usd(p.fusion_usd) }), ' on ' + plural(p.runs, 'run') + '. One big model would have cost about ',
          h('b', { class: 'bl', text: usd(p.baseline_usd) }), ' for the same tokens: you kept ', h('b', { class: 'kept', text: pct(p.savings_percent / 100) }), '.'),
          h('p', { class: 'fine', text: 'The big-model figure is an estimate (the same input and output tokens at the baseline model\'s list price), not a measured run. ' +
            (p.runs < t.runs ? p.runs + ' of ' + t.runs + ' runs stored an estimate; the rest are left out of this comparison.' : '') }));
      } else {
        hero.append(h('p', null, 'Fusion has spent ', h('b', { class: 'fu', text: usd(t.cost_usd) }), ' on ' + plural(t.runs, 'run') + '.'),
          h('p', { class: 'fine', text: 'None of these runs stored a baseline estimate, so there is nothing to compare against yet.' }));
      }
      root.append(hero);
      var done = t.completed / Math.max(1, t.runs);
      root.append(h('div', { class: 'kpis rise' },
        h('div', { class: 'kpi' }, h('div', { class: 'eyebrow', text: 'Total spend' }), h('div', { class: 'v', text: usd(t.cost_usd) }), h('div', { class: 's', text: 'all ' + plural(t.runs, 'run') })),
        h('div', { class: 'kpi' }, h('div', { class: 'eyebrow', text: 'Saved' }), h('div', { class: 'v', text: p.runs ? usd(p.savings_usd) : '—' }), h('div', { class: 's', text: p.savings_percent === null ? 'no estimate stored' : pct(p.savings_percent / 100) + ' below the baseline' })),
        h('div', { class: 'kpi' }, h('div', { class: 'eyebrow', text: 'Completed' }), h('div', { class: 'v', text: pct(done) }), h('div', { class: 's', text: t.completed + ' of ' + t.runs })),
        h('div', { class: 'kpi' }, h('div', { class: 'eyebrow', text: 'Average latency' }), h('div', { class: 'v', text: secs(t.avg_latency_ms) }), h('div', { class: 's', text: 'wall time per run' })),
        h('div', { class: 'kpi' }, h('div', { class: 'eyebrow', text: 'Shadow win rate' }), h('div', { class: 'v', text: o.shadow.total ? pct(o.shadow.win_rate) : '—' }),
          h('div', { class: 's', text: o.shadow.total ? '95% CI ' + pct(o.shadow.ci_low) + ' to ' + pct(o.shadow.ci_high) : 'no comparisons yet' }))));
      if (p.runs) root.append(section('1', 'Spend against the baseline', 'cumulative', panel(spendChart(o.daily))));
      root.append(section('2', 'Activity and quality', '', h('div', { class: 'grid2' },
        panel(runsPerDay(o.daily)), panel(h('div', { class: 'cap', text: 'Shadow A/B against the real baseline' }), winRate(o.shadow)))));
      root.append(section('3', 'Where the runs go', '', h('div', { class: 'grid2' },
        panel(h('div', { class: 'cap', text: 'By task type' }), hbars(o.by_task.map(function (r) {
          return { label: r.task_type, value: r.runs, text: plural(r.runs, 'run') + ' · ' + usd(r.cost_usd) + ' · ' + secs(r.avg_latency_ms) };
        }))),
        panel(h('div', { class: 'cap', text: 'By strategy' }), hbars(o.by_strategy.map(function (r) {
          return { label: r.strategy, value: r.runs, text: plural(r.runs, 'run') + ' · ' + usd(r.cost) + ' · ' + secs(r.avg_latency_ms) };
        }))))));
      return root;
    });
  }

  // -------------------------------------------------------------------------------- runs
  function runsView(params) {
    var state = { task: params.get('task') || '', strategy: params.get('strategy') || '', status: params.get('status') || '',
      q: params.get('q') || '', offset: parseInt(params.get('offset') || '0', 10) || 0 };
    var limit = 25, body = h('div'), root = h('div');
    function setHash() {
      history.replaceState(null, '', '#/runs' + query({ task: state.task, strategy: state.strategy, status: state.status, q: state.q, offset: state.offset || '' }));
    }
    function load() {
      setHash();
      return api('/api/runs' + query({ task: state.task, strategy: state.strategy, status: state.status, q: state.q, limit: limit, offset: state.offset })).then(function (r) {
        body.replaceChildren(renderRuns(r));
        return r;
      });
    }
    function select(label, key, options) {
      var el = h('select', { 'aria-label': label }, h('option', { value: '', text: 'All ' + label.toLowerCase() + 's' }),
        options.map(function (o) { return h('option', { value: o, text: o, selected: state[key] === o }); }));
      el.addEventListener('change', function () { state[key] = el.value; state.offset = 0; load(); });
      return el;
    }
    function renderRuns(r) {
      var any = state.task || state.strategy || state.status || state.q;
      if (!r.total && !any) {
        return empty('No runs yet', 'Runs appear here as Fusion answers questions, from the command line or through an MCP client.', ['fusion ask "How should I retry a failed HTTP call?" --mock']);
      }
      var rows = r.rows.map(function (x) {
        var saved = x.baseline_usd ? 1 - x.cost_usd / x.baseline_usd : null;
        var tr = h('tr', { class: 'link', tabindex: '0', 'aria-label': 'Open run ' + x.run_id },
          h('td', { class: 'l', text: when(x.created_at) }), h('td', { class: 'l' }, h('a', { href: '#/runs/' + x.run_id, text: x.run_id.replace(/^run_/, '').slice(0, 10) })),
          h('td', { class: 'l', text: x.task_type }), h('td', { class: 'l', text: x.strategy }), h('td', { class: 'l' }, status(x.status)),
          h('td', { text: usd(x.cost_usd) }), h('td', { text: saved === null ? '—' : pct(saved) }), h('td', { text: secs(x.latency_ms) }),
          h('td', { text: x.confidence === null ? '—' : x.confidence.toFixed(2) }));
        var go = function () { location.hash = '#/runs/' + x.run_id; };
        tr.addEventListener('click', function (e) { if (e.target.tagName !== 'A') go(); });
        tr.addEventListener('keydown', function (e) { if (e.key === 'Enter') go(); });
        return tr;
      });
      var last = Math.min(r.total, state.offset + limit);
      var pager = h('div', { class: 'pager' }, r.total ? (state.offset + 1) + '–' + last + ' of ' + r.total : '0 runs',
        h('button', { class: 'btn', type: 'button', disabled: state.offset === 0, onclick: function () { state.offset = Math.max(0, state.offset - limit); load(); } }, 'Newer'),
        h('button', { class: 'btn', type: 'button', disabled: last >= r.total, onclick: function () { state.offset += limit; load(); } }, 'Older'));
      if (!r.total) return h('p', { class: 'loading', text: 'No run matches these filters.' });
      return h('div', null, table(['When', 'Run', 'Task', 'Strategy', 'Status', 'Cost', 'Under baseline', 'Latency', 'Confidence'], rows, { left: [1, 2, 3, 4], caption: 'Runs, newest first' }), pager);
    }
    return api('/api/runs' + query({ limit: 1 })).then(function (probe) {
      var f = probe.facets, search = h('input', { type: 'search', placeholder: 'Search the prompt or run id', 'aria-label': 'Search runs', value: state.q }), timer;
      search.addEventListener('input', function () { clearTimeout(timer); timer = setTimeout(function () { state.q = search.value.trim(); state.offset = 0; load(); }, 280); });
      root.append(h('div', { class: 'rise' }, h('h1', { text: 'Runs' }), h('p', { class: 'lede', text: 'Every answer Fusion has produced, newest first. Open one to see its answer, its claims and where the time went.' })),
        h('div', { class: 'filters rise' }, select('Task', 'task', f.task), select('Strategy', 'strategy', f.strategy), select('Status', 'status', f.status), search,
          h('button', { class: 'btn', type: 'button', onclick: function () { state = { task: '', strategy: '', status: '', q: '', offset: 0 }; route(); } }, 'Clear')),
        h('div', { class: 'panel rise' }, body));
      return load().then(function () { return root; });
    });
  }

  // ----------------------------------------------------------------------------- waterfall
  function waterfall(tl) {
    var calls = tl.calls, lane = 28, W = 900, gut = 176, right = 74, top = 26, H = top + lane * calls.length + 22, pw = W - gut - right;
    var total = Math.max(tl.total_ms, 1), step = niceStep(total / 1000, 6) * 1000, X = function (ms) { return gut + ms / (total * 1.0) * pw; };
    var svg = s('svg', { class: 'chart', viewBox: '0 0 ' + W + ' ' + H, role: 'img',
      'aria-label': 'Timeline of ' + plural(calls.length, 'model call') + ' over ' + secs(total) });
    for (var t = 0; t <= total + 1; t += step) {
      svg.append(s('line', { class: 'grid', x1: X(t), x2: X(t), y1: top - 6, y2: H - 18 }), s('text', { class: 'tick', x: X(t), y: 12, 'text-anchor': 'middle', text: (t / 1000) + ' s' }));
    }
    svg.append(s('line', { class: 'axis', x1: gut, x2: gut, y1: top - 6, y2: H - 18 }));
    calls.forEach(function (c, i) {
      var y = top + lane * i, slot = STAGE_SLOT[c.stage] || 'sx', w = Math.max(3, X(c.end_ms) - X(c.start_ms)), n = slotNum(slot);
      var tipText = c.model + ' · ' + c.stage + (c.shadow ? ' (measurement, not counted)' : '') + '\nStarted ' + secs(c.start_ms) + ', took ' + secs(c.latency_ms) +
        '\nTokens ' + int(c.input_tokens) + ' in / ' + int(c.output_tokens) + ' out' + (c.tokens_per_s ? ' · ' + c.tokens_per_s.toFixed(0) + ' tok/s' : '') +
        (c.ttft_ms ? '\nFirst token after ' + secs(c.ttft_ms) : '') + '\nCost ' + usd(c.cost_usd) + (c.cache_hit ? ' (replayed from cache)' : '') +
        (c.retries ? '\nRetries: ' + c.retries : '') + (c.ok ? '' : '\nFAILED: ' + (c.error || c.status));
      var g = s('g', { class: 'pt', tabindex: '0', 'data-tip': tipText, role: 'img', 'aria-label': tipText.replace(/\n/g, '. ') });
      g.append(s('rect', { class: 'hit', x: 0, y: y, width: W, height: lane }),
        s('text', { class: 'strong', x: gut - 10, y: y + 18, 'text-anchor': 'end', text: c.model + (c.ok ? '' : ' ✕') }),
        s('text', { class: 'tick', x: 4, y: y + 18, text: c.stage }));
      var bar = s('rect', { class: 'mk grow c' + n, x: X(c.start_ms), y: y + 7, width: w, height: 14, rx: 4 });
      bar.style.setProperty('--d', (i * 55) + 'ms');
      if (!c.ok) bar.setAttribute('stroke-dasharray', '4 3');
      if (c.shadow) bar.setAttribute('opacity', '0.55');
      g.append(bar, s('text', { class: c.ok ? 'tick' : 'fail', x: X(c.end_ms) + 6, y: y + 18, text: secs(c.latency_ms) + (c.cache_hit ? ' · cached' : '') }));
      svg.append(g);
    });
    svg.append(s('line', { class: 'cross', x1: X(total), x2: X(total), y1: top - 6, y2: H - 18 }),
      s('text', { class: 'strong', x: X(total), y: H - 4, 'text-anchor': 'end', text: 'done at ' + secs(total) }));
    var seen = {}, items = [];
    calls.forEach(function (c) { if (!seen[c.stage]) { seen[c.stage] = 1; items.push([c.stage, STAGE_SLOT[c.stage] || 'sx']); } });
    return h('figure', null, h('figcaption', { text: 'Latency timeline: each bar is one model call' }), h('div', { class: 'scroll' }, svg), legend(items),
      h('p', { class: 'muted', text: 'Bars that overlap ran at the same time; the run is as slow as its longest chain, not the sum of its calls.' }));
  }

  // --------------------------------------------------------------------------- markdown
  function inline(text) {
    var out = [], re = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*\s][^*]*\*)/g, last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) out.push(text.slice(last, m.index));
      var tok = m[0];
      out.push(tok[0] === '`' ? h('code', { text: tok.slice(1, -1) }) : tok.slice(0, 2) === '**' ? h('strong', { text: tok.slice(2, -2) }) : h('em', { text: tok.slice(1, -1) }));
      last = m.index + tok.length;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }
  function markdown(src) {
    var root = h('div', { class: 'md' }), lines = src.replace(/\r/g, '').split('\n'), i = 0;
    while (i < lines.length) {
      var line = lines[i], m;
      if (/^```/.test(line)) {
        var code = []; i++;
        while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
        i++; root.append(h('pre', null, h('code', { text: code.join('\n') })));
      } else if ((m = /^(#{1,4})\s+(.*)$/.exec(line))) {
        root.append(h('h' + (m[1].length + 1 > 4 ? 4 : m[1].length + 1), null, inline(m[2]))); i++;
      } else if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
        var ordered = /^\s*\d+\./.test(line), list = h(ordered ? 'ol' : 'ul');
        while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) { list.append(h('li', null, inline(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, '')))); i++; }
        root.append(list);
      } else if (!line.trim()) { i++; }
      else {
        var para = [];
        while (i < lines.length && lines[i].trim() && !/^(```|#{1,4}\s|\s*([-*]|\d+\.)\s)/.test(lines[i])) para.push(lines[i++]);
        root.append(h('p', null, inline(para.join(' '))));
      }
    }
    return root;
  }

  // --------------------------------------------------------------------------- run detail
  function runView(id) {
    return api('/api/runs/' + encodeURIComponent(id)).then(function (r) {
      var root = h('div'), cmp = r.cost_comparison, base = cmp ? cmp.baseline_estimated_cost_usd : null;
      root.append(h('div', { class: 'rise' }, h('a', { class: 'back', href: '#/runs', text: '← All runs' }),
        h('h1', null, h('span', { class: 'mono', text: r.run_id })),
        h('p', { class: 'lede' }, status(r.status), '  ', chip(r.strategy || 'unknown strategy', 's1'), chip(r.task_type, 'sx'), '  ', h('span', { class: 'muted', text: when(r.created_at) + ' UTC' }),
          r.partial ? h('span', { class: 'badge', text: 'partial: soft time limit' }) : null, r.halt_reason ? h('span', { class: 'badge', text: 'halted: ' + r.halt_reason }) : null)));
      root.append(h('div', { class: 'kpis rise' },
        h('div', { class: 'kpi' }, h('div', { class: 'eyebrow', text: 'Cost' }), h('div', { class: 'v', text: usd(r.cost_usd) }), h('div', { class: 's', text: base ? 'a big model: about ' + usd(base) + ' (' + pct(1 - r.cost_usd / base) + ' less)' : 'no baseline estimate' })),
        h('div', { class: 'kpi' }, h('div', { class: 'eyebrow', text: 'Wall time' }), h('div', { class: 'v', text: secs(r.latency_ms) }), h('div', { class: 's', text: r.task_metrics && r.task_metrics.calls ? plural(r.task_metrics.calls, 'model call') : '' })),
        h('div', { class: 'kpi' }, h('div', { class: 'eyebrow', text: 'Confidence' }), h('div', { class: 'v', text: r.confidence === null ? '—' : r.confidence.toFixed(2) }),
          h('div', { class: 's', text: r.agreement.n_models ? 'agreement across ' + r.agreement.n_models + ' of ' + r.agreement.n_requested + ' models' : '' })),
        h('div', { class: 'kpi' }, h('div', { class: 'eyebrow', text: 'Secrets redacted' }), h('div', { class: 'v', text: String(r.prompt.redaction_count) }), h('div', { class: 's', text: 'before any model saw the input' }))));
      if (r.warnings.length) root.append(h('div', { class: 'note rise' }, h('b', { text: 'Warnings. ' }), r.warnings.join(' · ')));
      root.append(section('1', 'The answer', '', panel(r.answer.text ? markdown(r.answer.text) : h('p', { class: 'muted', text: 'No answer was stored for this run.' }))));
      if (r.claims.length) {
        var models = slots([].concat.apply([], r.claims.map(function (c) { return c.models; })));
        var label = { consensus: 'Consensus', unique: 'One model', contradicted: '⚠ Contradicted', outliers: 'Outlier' };
        var counts = {}; r.claims.forEach(function (c) { counts[c.group] = (counts[c.group] || 0) + 1; });
        root.append(section('2', 'Claims and agreement', Object.keys(counts).map(function (k) { return counts[k] + ' ' + label[k].replace('⚠ ', '').toLowerCase(); }).join(' · '),
          panel(h('ul', { class: 'claims' }, r.claims.map(function (c) {
            return h('li', null, h('div', null, h('div', { class: 'tag ' + c.group, text: label[c.group] || c.group }), h('div', { class: 'meta', text: c.kind + (c.severity ? ' · ' + c.severity : '') })),
              h('div', null, h('div', { text: c.text }), c.file ? h('div', { class: 'meta mono', text: c.file + (c.line ? ':' + c.line : '') }) : null,
                c.evidence ? h('div', { class: 'meta', text: 'Evidence: ' + c.evidence }) : null,
                h('div', null, c.models.map(function (m) { return chip(m, models[m]); }))));
          })))));
      }
      if (r.timeline) {
        var spent = r.stages.filter(function (st) { return st.stage.indexOf('shadow') !== 0; }), most = spent.map(function (st) { return st.cost_usd; });
        root.append(section('3', 'Where the time and money went', secs(r.timeline.total_ms) + ' · ' + usd(r.cost_usd), h('div', { class: 'stack' },
          panel(waterfall(r.timeline)),
          panel(h('div', { class: 'cap', text: 'Cost by stage' }), hbars(spent.map(function (st, i) {
            return { label: st.stage, value: st.cost_usd, slot: STAGE_SLOT[st.stage] || 'sx', text: usd(st.cost_usd) + ' · ' + plural(st.calls, 'call') };
          })), most.length ? null : h('p', { class: 'muted', text: 'No priced calls.' })))));
        root.append(h('section', { class: 'sec rise' }, h('header', null, h('h3', { text: 'Every call' })),
          panel(table(['Stage', 'Model', 'Start', 'Took', 'Tokens in', 'Tokens out', 'Tok/s', 'First token', 'Cost', 'Status'], r.timeline.calls.map(function (c) {
            return h('tr', null, h('td', { class: 'l', text: c.stage }), h('td', { class: 'l', text: c.model }), h('td', { text: secs(c.start_ms) }), h('td', { text: secs(c.latency_ms) }),
              h('td', { text: int(c.input_tokens) }), h('td', { text: int(c.output_tokens) }), h('td', { text: c.tokens_per_s ? c.tokens_per_s.toFixed(0) : '—' }),
              h('td', { text: c.ttft_ms ? secs(c.ttft_ms) : '—' }), h('td', { text: usd(c.cost_usd) }),
              h('td', { class: 'l' }, c.ok ? status('completed') : h('span', { class: 'st bad', title: c.error || '' }, '✕ ' + (c.status || 'failed'))));
          }), { left: [1, 9], caption: 'Model calls of this run' }))));
      } else if (r.steps.length) {
        root.append(section('3', 'Steps', 'no call timeline stored for this run', panel(table(['Step', 'Model', 'Tokens in', 'Tokens out', 'Cost', 'Took'], r.steps.map(function (st) {
          return h('tr', null, h('td', { class: 'l', text: st.step }), h('td', { class: 'l', text: st.model || '—' }), h('td', { text: int(st.input_tokens) }), h('td', { text: int(st.output_tokens) }), h('td', { text: usd(st.cost_usd) }), h('td', { text: secs(st.latency_ms) }));
        }), { left: [1], caption: 'Steps' }))));
      }
      var pr = r.prompt, fields = [['Question', pr.primary], ['Background', pr.context]].concat(pr.snippets.map(function (sn, i) { return ['Snippet ' + (i + 1), sn]; }));
      var shown = fields.filter(function (f) { return f[1].chars; }).map(function (f) {
        return h('div', null, h('div', { class: 'cap', text: f[0] + ' · ' + int(f[1].chars) + ' characters' + (f[1].truncated ? ' (cut)' : '') }), h('pre', { class: 'prompt', text: f[1].text }));
      });
      root.append(section('4', 'What was asked', pr.raw ? 'unredacted' : 'redacted copy', panel(
        pr.raw ? h('div', { class: 'note bad' }, h('b', { text: 'Unredacted prompts are shown ' }), 'because FUSION_LOG_RAW_PROMPTS is true on this machine.')
          : h('p', { class: 'muted', text: 'This is the copy Fusion stored: secrets were replaced before any model saw the input. The original text is never shown unless FUSION_LOG_RAW_PROMPTS is set.' }),
        shown.length ? shown : h('p', { class: 'muted', text: 'No input text was stored.' }),
        pr.changed_files.length ? h('p', { class: 'muted', text: 'Files: ' + pr.changed_files.join(', ') }) : null)));
      var facts = [['Strategy', r.routing.strategy], ['Panel', (r.routing.selected_panel || []).join(', ')], ['Judge', r.routing.judge_model], ['Synthesizer', r.routing.synthesizer_model],
        ['Complexity / risk', [r.routing.complexity, r.routing.risk].filter(Boolean).join(' / ')], ['Why', (r.routing.reasons || []).join('; ')]].filter(function (f) { return f[1]; });
      if (facts.length) root.append(section('5', 'Routing', '', panel(h('dl', { class: 'facts' }, facts.map(function (f) { return [h('dt', { text: f[0] }), h('dd', { text: f[1] })]; })))));
      return root;
    });
  }

  // --------------------------------------------------------------------------- benchmarks
  function benchListView() {
    return api('/api/bench').then(function (b) {
      if (!b.runs.length) {
        return empty('No benchmark runs yet', 'A benchmark measures whether the panel beats one big model on tasks with known answers. A simulated run is free and needs no keys.',
          ['fusion bench run -d v1 --arms solo-cheap,panel-duo,panel-cheap --repeats 1 --mock --limit 24']);
      }
      var rows = b.runs.map(function (r) {
        return h('tr', null, h('td', { class: 'l' }, h('a', { href: '#/bench/' + r.run_id, text: r.run_id })), h('td', { class: 'l', text: r.dataset.split('/').pop() }),
          h('td', { class: 'l' }, h('span', { class: 'badge', text: r.mock ? 'simulated' : 'live' })), h('td', { class: 'l' }, status(r.status)),
          h('td', { text: r.done_jobs + ' / ' + r.total_jobs }), h('td', { class: 'l wrap' }, r.arms.map(function (a) { return chip(a, 'sx'); })),
          h('td', { text: usd(r.spent_usd) }), h('td', { class: 'l', text: when(r.created_at) }));
      });
      var opts = function (sel) { return b.runs.map(function (r, i) { return h('option', { value: r.run_id, text: r.run_id, selected: i === sel }); }); };
      var a = h('select', { 'aria-label': 'Earlier run' }, opts(Math.min(1, b.runs.length - 1))), z = h('select', { 'aria-label': 'Later run' }, opts(0));
      var go = h('button', { class: 'btn', type: 'button', onclick: function () { location.hash = '#/bench/compare' + query({ before: a.value, after: z.value }); } }, 'Compare');
      return h('div', null, h('div', { class: 'rise' }, h('h1', { text: 'Benchmarks' }), h('p', { class: 'lede', text: 'Studies run with `fusion bench`. Open one for its full report: quality with confidence intervals, cost and speed, and what the evidence supports. A simulated run uses made-up skill numbers and proves the pipeline, not the models.' })),
        h('div', { class: 'panel rise' }, table(['Run', 'Dataset', 'Mode', 'Status', 'Jobs', 'Arms', 'Spent', 'Started'], rows, { left: [1, 2, 3, 5, 7], caption: 'Benchmark runs' })),
        section('', 'Compare two runs', 'after a tuning change', panel(h('div', { class: 'filters' }, h('label', null, 'Earlier ', a), h('label', null, 'Later ', z), go))));
    });
  }
  function benchView(id) {
    return api('/api/bench').then(function (b) {
      var r = b.runs.filter(function (x) { return x.run_id === id; })[0];
      if (!r) throw new Error("No benchmark run '" + id + "'");
      var url = '/bench/' + encodeURIComponent(id) + '/report.html';
      return h('div', null, h('div', { class: 'rise' }, h('a', { class: 'back', href: '#/bench', text: '← All benchmarks' }), h('h1', null, h('span', { class: 'mono', text: r.run_id })),
        h('p', { class: 'lede' }, status(r.status), '  ', h('span', { class: 'badge', text: r.mock ? 'simulated models' : 'live models' }), '  ', r.done_jobs + ' of ' + r.total_jobs + ' jobs · ' + usd(r.spent_usd) + ' · ' + plural(r.arms.length, 'arm') + ' · ' + plural(r.repeats, 'repeat'), '  ',
          h('a', { href: url, target: '_blank', rel: 'noopener', text: 'Open the report in its own tab' }))),
        r.mock ? h('div', { class: 'note rise' }, h('b', { text: 'Simulated. ' }), 'These numbers come from simulated models with assumed skill, price and speed. They check the machinery; they say nothing about real models.') : null,
        // The report is first-party HTML (every value in it is escaped when it is built) served under a
        // CSP that allows no network access, so it is framed with scripts and its own origin: the
        // stricter opaque-origin sandbox is blocked outright by some embedded browsers.
        h('iframe', { class: 'report rise', src: url, title: 'Benchmark report for ' + r.run_id, sandbox: 'allow-scripts allow-same-origin' }));
    });
  }
  function benchCompareView(params) {
    var before = params.get('before'), after = params.get('after');
    if (!before || !after) { location.hash = '#/bench'; return Promise.resolve(h('div')); }
    return api('/api/bench/compare' + query({ before: before, after: after })).then(function (c) {
      // Most arm figures are intervals ({estimate, low, high}); a few are plain numbers.
      var est = function (v) { return v && typeof v === 'object' ? v.estimate : v; };
      var num = function (v, f) { v = est(v); return v === null || v === undefined ? '—' : f(v); };
      var delta = function (a, b, f) { a = est(a); b = est(b); return a === null || b === null || a === undefined || b === undefined ? '—' : (b - a >= 0 ? '+' : '−') + f(Math.abs(b - a)); };
      var rows = c.changes.map(function (ch) {
        var cmp = ch.comparison, v = cmp.verdicts || [], q = cmp.quality && cmp.quality.difference;
        var f3 = function (x) { return x.toFixed(3); };
        return h('tr', null, h('td', { class: 'l', text: ch.arm }), h('td', { text: String(ch.n_tasks) }),
          h('td', { text: num(ch.before.mean_quality, f3) + ' → ' + num(ch.after.mean_quality, f3) }),
          h('td', { text: q && q.estimate !== null ? (q.estimate >= 0 ? '+' : '−') + Math.abs(q.estimate).toFixed(3) + ' [' + q.low.toFixed(2) + ', ' + q.high.toFixed(2) + ']' : '—' }),
          h('td', { text: num(ch.before.cost_per_task, usd) + ' → ' + num(ch.after.cost_per_task, usd) }),
          h('td', { text: delta(ch.before.seconds_p50, ch.after.seconds_p50, function (x) { return x.toFixed(1) + ' s'; }) }),
          h('td', { class: 'l wrap' }, v.map(function (x) {
            var mark = { yes: ['ok', '✓'], no: ['bad', '✕'], inconclusive: ['idle', '?'], blocked: ['warn', '■'] }[x.outcome] || ['idle', '·'];
            return h('div', { title: x.reason }, h('span', { class: 'st ' + mark[0] }, h('span', { 'aria-hidden': 'true', text: mark[1] }), x.outcome), ' ' + x.claim.replace(/_/g, ' '));
          })));
      });
      return h('div', null, h('div', { class: 'rise' }, h('a', { class: 'back', href: '#/bench', text: '← All benchmarks' }), h('h1', null, h('span', { class: 'mono', text: c.before }), ' → ', h('span', { class: 'mono', text: c.after })),
        h('p', { class: 'lede', text: c.shared_tasks + ' tasks are in both runs. Differences are paired per task; the bracket is a 95% bootstrap interval. “Inconclusive” is the honest answer when the evidence is thin.' })),
        c.notes.length ? h('div', { class: 'note rise' }, c.notes.map(function (n) { return h('div', { text: n }); })) : null,
        c.changes.length ? h('div', { class: 'panel rise' }, table(['Arm', 'Tasks', 'Quality', 'Change (95% CI)', 'Cost per task', 'p50 time', 'Verdicts'], rows, { left: [6], caption: 'Per-arm change between the two runs' })) : h('p', { class: 'loading', text: 'The runs share no arm and no task, so there is nothing to compare.' }));
    });
  }

  // ----------------------------------------------------------------------------- config
  function configView() {
    return api('/api/config').then(function (c) {
      var mslot = slots(c.models.map(function (m) { return m.alias; }));
      var root = h('div');
      root.append(h('div', { class: 'rise' }, h('h1', { text: 'Configuration' }), h('p', { class: 'lede', text: 'What Fusion would use if you asked it something now: the strategies it can run, the models and prices behind them, and where each setting comes from. Read-only: change it with a config file or `--set`.' })));
      if (c.warnings.length) root.append(h('div', { class: 'note rise' }, h('b', { text: 'Catalog needs attention. ' }), 'Prices or model ids may be stale or retiring:', h('ul', null, c.warnings.map(function (w) { return h('li', { text: w }); }))));
      root.append(section('1', 'Strategies', 'default: ' + (c.default_strategy || '—'), panel(table(['Strategy', 'Kind', 'Models', 'Rounds', 'Aggregator', 'Cost cap', 'Budget'], c.strategies.map(function (st) {
        return h('tr', null, h('td', { class: 'l wrap' }, h('b', { text: st.name }), st.name === c.default_strategy ? h('span', { class: 'badge', text: 'default' }) : null, st.description ? h('div', { class: 'muted', text: st.description }) : null),
          h('td', { class: 'l', text: st.kind }), h('td', { class: 'l wrap' }, st.models.map(function (m) { return chip(m, mslot[m]); })), h('td', { text: String(st.rounds) }), h('td', { class: 'l', text: st.kind === 'solo' ? '—' : st.aggregator || '—' }),
          h('td', { text: st.max_cost_usd ? usd(st.max_cost_usd) : '—' }), h('td', { class: 'l', text: st.budgets.join(', ') || '—' }));
      }), { left: [1, 2, 4, 6], caption: 'Strategies' }))));
      root.append(section('2', 'Models and prices', 'USD per million tokens, in / out', panel(table(['Alias', 'Provider', 'Model id', 'Price in / out', 'Verified', 'Roles', 'State'], c.models.map(function (m) {
        return h('tr', null, h('td', { class: 'l' }, chip(m.alias, mslot[m.alias])), h('td', { class: 'l', text: m.provider }), h('td', { class: 'l mono', text: m.model_id }),
          h('td', { text: m.input_per_1m === null ? 'no price today' : '$' + m.input_per_1m + ' / $' + m.output_per_1m }), h('td', { text: m.verified_on || '—' }), h('td', { class: 'l wrap', text: m.roles.join(', ') }),
          h('td', { class: 'l' }, m.warnings.length ? h('span', { class: 'st warn', title: m.warnings.join('\n') }, '⚠ check') : m.enabled ? h('span', { class: 'st ok' }, '✓ current') : h('span', { class: 'st idle' }, '○ disabled')));
      }), { left: [1, 2, 5, 6], caption: 'Model catalog' }))));
      root.append(section('3', 'Where settings come from', '', h('div', { class: 'grid2' },
        panel(h('div', { class: 'cap', text: 'Layers, later wins' }), h('dl', { class: 'facts' }, c.layers.map(function (l) { return [h('dt', { text: l.name }), h('dd', { text: (l.path || 'built in') + (l.keys.length ? ' — sets ' + l.keys.join(', ') : '') })]; }))),
        panel(h('div', { class: 'cap', text: 'Files and keys' }), h('dl', { class: 'facts' },
          h('dt', { text: 'Baseline' }), h('dd', { text: c.baseline.name + (c.baseline.enabled ? '' : ' (disabled)') }), h('dt', { text: 'Database' }), h('dd', { text: c.paths.database }), h('dt', { text: 'Benchmarks' }), h('dd', { text: c.paths.bench }),
          h('dt', { text: 'User config' }), h('dd', { text: c.paths.user_config }), h('dt', { text: 'Project config' }), h('dd', { text: c.paths.project_config }),
          Object.keys(c.keys).map(function (k) { return [h('dt', { text: k + ' key' }), h('dd', null, c.keys[k] ? h('span', { class: 'st ok' }, '✓ set') : h('span', { class: 'st warn' }, '⚠ not set'))]; }),
          h('dt', { text: 'Raw prompts' }), h('dd', null, c.raw_prompts ? h('span', { class: 'st warn' }, '⚠ unredacted prompts are shown (FUSION_LOG_RAW_PROMPTS)') : h('span', { class: 'st ok' }, '✓ hidden: only redacted input is shown')))))));
      return root;
    });
  }

  // ------------------------------------------------------------------------------- router
  var routes = [
    [/^\/?$/, 'overview', function () { return overviewView(); }, 'Overview'],
    [/^\/runs\/([^/]+)$/, 'runs', function (m) { return runView(decodeURIComponent(m[1])); }, 'Run'],
    [/^\/runs$/, 'runs', function (m, p) { return runsView(p); }, 'Runs'],
    [/^\/bench\/compare$/, 'bench', function (m, p) { return benchCompareView(p); }, 'Compare benchmarks'],
    [/^\/bench\/([^/]+)$/, 'bench', function (m) { return benchView(decodeURIComponent(m[1])); }, 'Benchmark'],
    [/^\/bench$/, 'bench', function () { return benchListView(); }, 'Benchmarks'],
    [/^\/config$/, 'config', function () { return configView(); }, 'Configuration'],
  ];
  var view = $('#view'), token = 0;

  function mount(node, fresh) {
    view.replaceChildren(node);
    Array.prototype.forEach.call(view.querySelectorAll('.rise'), function (el, i) { el.style.setProperty('--d', Math.min(i, 8) * 70 + 'ms'); });
    if (fresh) { window.scrollTo(0, 0); view.focus({ preventScroll: true }); }
  }
  var navigated = false;
  function route() {
    var raw = location.hash.slice(1) || '/', cut = raw.indexOf('?'), path = cut < 0 ? raw : raw.slice(0, cut), params = new URLSearchParams(cut < 0 ? '' : raw.slice(cut + 1));
    var mine = ++token, hit = null, m;
    routes.some(function (r) { m = r[0].exec(path); if (m) { hit = r; return true; } return false; });
    Array.prototype.forEach.call(document.querySelectorAll('.tabs a'), function (a) {
      if (hit && a.getAttribute('data-tab') === hit[1]) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    if (!hit) { mount(h('div', { class: 'empty' }, h('h2', { text: 'Not found' }), h('p', null, h('a', { href: '#/', text: 'Back to the overview' }))), true); return; }
    document.title = hit[3] + ' · Fusion Ledger';
    var fresh = navigated; navigated = true;
    hideTip();
    hit[2](m, params).then(function (node) { if (mine === token) mount(node, fresh); }).catch(function (e) {
      if (mine !== token) return;
      mount(h('div', { class: 'empty' }, h('h2', { text: 'This could not be loaded' }), h('p', { class: 'lede', text: String(e.message || e) }),
        h('button', { class: 'btn', type: 'button', onclick: route }, 'Try again')), true);
    });
  }
  window.addEventListener('hashchange', route);

  // ------------------------------------------------------------------------------ chrome
  function setTheme(next, save) {
    if (next) document.documentElement.setAttribute('data-theme', next);
    var dark = document.documentElement.getAttribute('data-theme') === 'dark' ||
      (!document.documentElement.getAttribute('data-theme') && window.matchMedia('(prefers-color-scheme: dark)').matches);
    var btn = $('#theme'); btn.textContent = dark ? 'Light' : 'Dark'; btn.setAttribute('aria-pressed', dark ? 'true' : 'false');
    if (save) { try { localStorage.setItem('fusion-theme', next); } catch (e) { /* storage unavailable */ } }
  }
  $('#theme').addEventListener('click', function () {
    var dark = document.documentElement.getAttribute('data-theme') === 'dark' ||
      (!document.documentElement.getAttribute('data-theme') && window.matchMedia('(prefers-color-scheme: dark)').matches);
    setTheme(dark ? 'light' : 'dark', true);
  });
  $('#refresh').addEventListener('click', function () { navigated = false; route(); });
  setTheme(null, false);
  api('/api/meta').then(function (m) { cache.meta = m; var parts = m.db_path.split('/').filter(Boolean); $('#where').textContent = parts.length > 2 ? '…/' + parts.slice(-2).join('/') : m.db_path; $('#where').setAttribute('title', m.db_path); }).catch(function () { /* the views report their own errors */ });
  route();
})();
