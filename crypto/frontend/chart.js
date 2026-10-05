/* 图片显示确认模块 — K线 / KDJ(K,D) / MACD(柱) / B-S 信号
 *
 * 目的：把 bot 真正使用的那条行情序列画出来，并标出策略的 B/S 信号，
 *       用于人工核对「策略有没有按规则执行」。
 *
 * 原则：
 *   * 行情与指标全部来自后端 /api/chart（测试网 K 线 + 与运行器同一份指标函数），
 *     前端不做任何指标计算，避免出现第二套口径。
 *   * B = 金叉且 MACD 柱为正；S = 死叉且 MACD 柱为负；标记画在信号 K 线上，
 *     实际执行在其下一根 K 线开盘。
 *   * 只读展示，不含任何下单动作。
 */
(() => {
  "use strict";

  // 面板(8787)与本地预览(8799，自带 /api 代理)同源直连；其他静态端口回落面板 API。
  const API = ["8787", "8799"].includes(location.port) ? "" : "http://127.0.0.1:8787";
  const REFRESH_MS = 5000;
  const $ = (id) => document.getElementById(id);

  const COLORS = {
    up: "#0ecb81",
    down: "#f6465d",
    upFill: "#0ecb81",
    downFill: "#f6465d",
    grid: "#1f2630",
    gridStrong: "#2d3644",
    axis: "#5f6875",
    text: "#97a1b0",
    ink: "#eaecef",
    kLine: "#6aa9ff",
    dLine: "#d9a441",
    jLine: "#4a5361",
    dif: "#6aa9ff",
    dea: "#d9a441",
    long: "rgba(14,203,129,0.06)",
    short: "rgba(246,70,93,0.06)",
    crosshair: "#f0b90b",
  };

  const LAYOUT = { left: 10, right: 78, top: 26, bottom: 24, gap: 12 };
  const RATIO = { price: 0.54, kdj: 0.20, macd: 0.26 };
  const BARS_OPTIONS = [120, 200, 300, 500];
  const INTERVAL_MS = { "15m": 900000, "5m": 300000 };
  const MIN_BARS = 20;    // 缩放下限：至少保留 20 根
  const ZOOM_STEP = 1.18; // 每一格滚轮的缩放倍率

  const state = {
    symbol: "BTCUSDT",
    interval: "15m",
    bars: 200,
    showSignals: true,
    showTrades: true,
    auto: true,
    data: null,
    active: false,
    hover: -1,
    timer: null,
    clock: null,
    loading: false,
    error: "",
    lastAt: 0,
    reqSeq: 0,
    // 可视窗口：count = 可见根数（0 表示铺满全部），offset = 右侧隐藏的根数
    view: { count: 0, offset: 0 },
  };

  let drag = null;  // 拖动平移中的起点状态

  // ------------------------------------------------------------------ 工具
  const pad2 = (n) => String(n).padStart(2, "0");

  /** 毫秒 → 北京时间显示 */
  function bj(ms, withDate) {
    const d = new Date(Number(ms) + 8 * 3600 * 1000);
    const t = `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
    if (!withDate) return t;
    return `${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} ${t}`;
  }

  function bjFull(ms) {
    const d = new Date(Number(ms) + 8 * 3600 * 1000);
    return `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} ` +
           `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())}`;
  }

  const px = (v) => {
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    return n.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  };
  const num = (v, d = 2) => (Number.isFinite(Number(v)) ? Number(v).toFixed(d) : "—");

  function colorFor(v) {
    return Number(v) >= 0 ? COLORS.up : COLORS.down;
  }

  // ------------------------------------------------------------------ 画布
  const canvas = () => $("chartCanvas");

  function fitCanvas() {
    const cv = canvas();
    if (!cv) return null;
    const wrap = cv.parentElement;
    const cssW = Math.max(320, wrap.clientWidth);
    const cssH = Math.max(420, wrap.clientHeight || 0);
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(cssW * dpr);
    cv.height = Math.round(cssH * dpr);
    cv.style.width = cssW + "px";
    cv.style.height = cssH + "px";
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w: cssW, h: cssH };
  }

  function regions(w, h) {
    const innerW = w - LAYOUT.left - LAYOUT.right;
    const innerH = h - LAYOUT.top - LAYOUT.bottom - LAYOUT.gap * 2;
    const priceH = innerH * RATIO.price;
    const kdjH = innerH * RATIO.kdj;
    const macdH = innerH * RATIO.macd;
    const x0 = LAYOUT.left;
    let y = LAYOUT.top;
    const price = { x: x0, y, w: innerW, h: priceH };
    y += priceH + LAYOUT.gap;
    const kdj = { x: x0, y, w: innerW, h: kdjH };
    y += kdjH + LAYOUT.gap;
    const macd = { x: x0, y, w: innerW, h: macdH };
    return { price, kdj, macd, innerW };
  }

  function barGeom(rect, n) {
    const slot = rect.w / Math.max(1, n);
    const bodyW = Math.max(1, Math.min(14, slot * 0.68));
    return { slot, bodyW, cx: (i) => rect.x + slot * (i + 0.5) };
  }

  /** 把 state.view 收敛成合法的 [start, end) 可视区间（全局下标）。
   *  offset 从右端计数，所以新 K 线到来时视图会自然跟着最新一根走。 */
  function viewWindow(n) {
    const want = Math.round(state.view.count) || n;
    const count = n <= MIN_BARS ? n : Math.max(MIN_BARS, Math.min(n, want));
    const offset = Math.max(0, Math.min(n - count, Math.round(state.view.offset) || 0));
    return { start: n - offset - count, end: n - offset, count, offset };
  }

  function resetView() {
    state.view.count = 0;
    state.view.offset = 0;
  }

  function niceTicks(min, max, count) {
    if (!Number.isFinite(min) || !Number.isFinite(max) || min === max) {
      return [min];
    }
    const raw = (max - min) / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
    const start = Math.ceil(min / step) * step;
    const out = [];
    for (let v = start; v <= max + step * 0.5; v += step) out.push(v);
    return out;
  }

  // ------------------------------------------------------------------ 绘制
  function draw() {
    const fit = fitCanvas();
    if (!fit) return;
    const { ctx, w, h } = fit;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "rgba(0,0,0,0)";
    ctx.fillRect(0, 0, w, h);

    const data = state.data;
    const bars = data && data.bars ? data.bars : [];
    if (!bars.length) {
      ctx.fillStyle = COLORS.axis;
      ctx.font = "13px system-ui, sans-serif";
      ctx.fillText(state.error || "正在读取行情…", LAYOUT.left + 8, LAYOUT.top + 22);
      return;
    }

    const R = regions(w, h);
    const n = bars.length;
    const vw = viewWindow(n);
    const g = barGeom(R.price, vw.count);
    const idx = (i) => Math.round(R.price.x + g.slot * (i - vw.start + 0.5));

    // ---- 价格区间（按可见根数算，放大后纵轴自动跟着变细）----
    let lo = Infinity, hi = -Infinity;
    for (let i = vw.start; i < vw.end; i++) {
      const b = bars[i];
      if (b.l < lo) lo = b.l;
      if (b.h > hi) hi = b.h;
    }
    const padY = (hi - lo) * 0.08 || 1;
    lo -= padY; hi += padY;
    const py = (v) => R.price.y + R.price.h - ((v - lo) / (hi - lo)) * R.price.h;

    // ---- 推演方向底纹（验证用）----
    for (let i = vw.start; i < vw.end; i++) {
      const pos = bars[i].pos;
      if (pos !== "long" && pos !== "short") continue;
      ctx.fillStyle = pos === "long" ? COLORS.long : COLORS.short;
      ctx.fillRect(idx(i) - g.slot / 2, R.price.y, g.slot + 0.6, R.price.h);
    }

    // ---- 价格网格与刻度 ----
    ctx.font = "11px ui-monospace, Menlo, Consolas, monospace";
    ctx.textBaseline = "middle";
    const priceTicks = niceTicks(lo, hi, 5);
    for (const v of priceTicks) {
      const y = py(v);
      if (y < R.price.y - 1 || y > R.price.y + R.price.h + 1) continue;
      ctx.strokeStyle = COLORS.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(R.price.x, Math.round(y) + 0.5);
      ctx.lineTo(R.price.x + R.price.w, Math.round(y) + 0.5);
      ctx.stroke();
      ctx.fillStyle = COLORS.axis;
      ctx.fillText(px(v), R.price.x + R.price.w + 6, y);
    }

    // ---- K 线 ----
    for (let i = vw.start; i < vw.end; i++) {
      const b = bars[i];
      const up = b.c >= b.o;
      const col = up ? COLORS.up : COLORS.down;
      const cx = idx(i);
      ctx.strokeStyle = col;
      ctx.fillStyle = col;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(cx + 0.5, py(b.h));
      ctx.lineTo(cx + 0.5, py(b.l));
      ctx.stroke();
      const yo = py(b.o), yc = py(b.c);
      const top = Math.min(yo, yc);
      const hh = Math.max(1, Math.abs(yc - yo));
      ctx.fillRect(cx - g.bodyW / 2, top, g.bodyW, hh);
    }

    // ---- 最新价横线 ----
    const last = bars[n - 1];
    ctx.setLineDash([4, 4]);
    ctx.strokeStyle = COLORS.crosshair;
    ctx.beginPath();
    ctx.moveTo(R.price.x, py(last.c));
    ctx.lineTo(R.price.x + R.price.w, py(last.c));
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = COLORS.crosshair;
    ctx.fillRect(R.price.x + R.price.w + 2, py(last.c) - 8, 74, 16);
    ctx.fillStyle = "#17191d";
    ctx.fillText(px(last.c), R.price.x + R.price.w + 6, py(last.c));

    // ---- B / S 信号标记 ----
    if (state.showSignals && data.signals) {
      for (const s of data.signals) {
        const rel = s.i - (data.fetched_bars - n);
        if (rel < vw.start || rel >= vw.end) continue;
        const b = bars[rel];
        const cx = idx(rel);
        const isB = s.side === "B";
        // 重复信号（已持仓、同向、不动作）画成空心，避免与真正执行的开仓/反手混淆。
        const isRepeat = s.kind === "repeat";
        const col = isB ? COLORS.up : COLORS.down;
        const y = isB ? py(b.l) + 16 : py(b.h) - 16;
        ctx.beginPath();
        if (isB) {
          ctx.moveTo(cx, y - 7);
          ctx.lineTo(cx - 6, y + 4);
          ctx.lineTo(cx + 6, y + 4);
        } else {
          ctx.moveTo(cx, y + 7);
          ctx.lineTo(cx - 6, y - 4);
          ctx.lineTo(cx + 6, y - 4);
        }
        ctx.closePath();
        ctx.globalAlpha = isRepeat ? 0.45 : 1;
        if (isRepeat) {
          ctx.strokeStyle = col;
          ctx.lineWidth = 1.3;
          ctx.stroke();
        } else {
          ctx.fillStyle = col;
          ctx.fill();
        }
        ctx.font = isRepeat ? "600 9px system-ui, sans-serif" : "700 10px system-ui, sans-serif";
        ctx.fillStyle = col;
        ctx.textAlign = "center";
        ctx.fillText(isB ? "B" : "S", cx, isB ? y + 16 : y - 12);
        ctx.textAlign = "left";
        ctx.globalAlpha = 1;
      }
    }

    // ---- 本地成交标记 ----
    if (state.showTrades && data.trades) {
      for (const t of data.trades) {
        const rel = nearestIndex(bars, t.t, vw.start, vw.end);
        if (rel < 0) continue;
        const cx = idx(rel);
        const y = py(Number(t.price) || bars[rel].c);
        ctx.beginPath();
        ctx.arc(cx, y, 3.4, 0, Math.PI * 2);
        ctx.fillStyle = String(t.side).includes("多") ? COLORS.up : COLORS.down;
        ctx.fill();
        ctx.strokeStyle = "#12161d";
        ctx.lineWidth = 1.2;
        ctx.stroke();
      }
    }

    // ---- KDJ ----
    const kdjRect = R.kdj;
    const ky = (v) => kdjRect.y + kdjRect.h - (Math.max(0, Math.min(100, v)) / 100) * kdjRect.h;
    ctx.strokeStyle = COLORS.grid;
    for (const v of [20, 50, 80]) {
      const y = ky(v);
      ctx.setLineDash(v === 50 ? [3, 3] : []);
      ctx.beginPath();
      ctx.moveTo(kdjRect.x, Math.round(y) + 0.5);
      ctx.lineTo(kdjRect.x + kdjRect.w, Math.round(y) + 0.5);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = COLORS.axis;
      ctx.fillText(String(v), kdjRect.x + kdjRect.w + 6, y);
    }
    line(ctx, bars, (b) => ky(b.j), idx, COLORS.jLine, 1, [3, 3], vw);
    line(ctx, bars, (b) => ky(b.k), idx, COLORS.kLine, 1.6, null, vw);
    line(ctx, bars, (b) => ky(b.d), idx, COLORS.dLine, 1.6, null, vw);

    // ---- MACD ----
    const mRect = R.macd;
    let mLo = 0, mHi = 0;
    for (let i = vw.start; i < vw.end; i++) {
      const b = bars[i];
      mLo = Math.min(mLo, b.hist, b.dif, b.dea);
      mHi = Math.max(mHi, b.hist, b.dif, b.dea);
    }
    const mPad = (mHi - mLo) * 0.12 || 1;
    mLo -= mPad; mHi += mPad;
    const my = (v) => mRect.y + mRect.h - ((v - mLo) / (mHi - mLo)) * mRect.h;
    ctx.strokeStyle = COLORS.gridStrong;
    ctx.beginPath();
    ctx.moveTo(mRect.x, Math.round(my(0)) + 0.5);
    ctx.lineTo(mRect.x + mRect.w, Math.round(my(0)) + 0.5);
    ctx.stroke();
    ctx.fillStyle = COLORS.axis;
    ctx.fillText("0", mRect.x + mRect.w + 6, my(0));
    for (const v of [mHi - mPad, mLo + mPad]) {
      ctx.fillStyle = COLORS.axis;
      ctx.fillText(num(v, 2), mRect.x + mRect.w + 6, my(v));
    }
    const zero = my(0);
    for (let i = vw.start; i < vw.end; i++) {
      const b = bars[i];
      const cx = idx(i);
      const y = my(b.hist);
      ctx.fillStyle = b.hist >= 0 ? COLORS.up : COLORS.down;
      const top = Math.min(y, zero);
      const hh = Math.max(1, Math.abs(y - zero));
      ctx.fillRect(cx - g.bodyW / 2, top, g.bodyW, hh);
    }
    line(ctx, bars, (b) => my(b.dif), idx, COLORS.dif, 1.5, null, vw);
    line(ctx, bars, (b) => my(b.dea), idx, COLORS.dea, 1.5, null, vw);

    // ---- 时间轴 ----
    ctx.fillStyle = COLORS.axis;
    const withDate = g.slot >= 16;
    const labelW = withDate ? 92 : 46;
    const step = Math.max(1, Math.ceil(labelW / g.slot));
    ctx.textAlign = "center";
    for (let i = vw.end - 1; i >= vw.start; i -= step) {
      const cx = idx(i);
      ctx.fillText(bj(bars[i].t, withDate), cx, h - LAYOUT.bottom + 10);
    }
    ctx.textAlign = "left";

    // ---- 十字光标 ----
    if (state.hover >= 0 && state.hover < n) {
      const cx = idx(state.hover);
      ctx.strokeStyle = COLORS.crosshair;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(Math.round(cx) + 0.5, LAYOUT.top);
      ctx.lineTo(Math.round(cx) + 0.5, h - LAYOUT.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  function line(ctx, bars, getY, idx, color, width, dash, vw) {
    ctx.strokeStyle = color;
    ctx.lineWidth = width || 1;
    if (dash) ctx.setLineDash(dash);
    ctx.beginPath();
    let started = false;
    const from = vw ? vw.start : 0;
    const to = vw ? vw.end : bars.length;
    for (let i = from; i < to; i++) {
      const v = getY(bars[i]);
      if (!Number.isFinite(v)) { started = false; continue; }
      const x = idx(i);
      if (!started) { ctx.moveTo(x, v); started = true; }
      else ctx.lineTo(x, v);
    }
    ctx.stroke();
    if (dash) ctx.setLineDash([]);
  }

  function nearestIndex(bars, ms, from, to) {
    let best = -1, bestD = Infinity;
    const a = from == null ? 0 : from;
    const b = to == null ? bars.length : to;
    for (let i = a; i < b; i++) {
      const d = Math.abs(bars[i].t - ms);
      if (d < bestD) { bestD = d; best = i; }
    }
    return bestD <= 2 * INTERVAL_MS[state.interval] ? best : -1;
  }

  // ------------------------------------------------------------------ 头部与表格
  function renderHead() {
    const d = state.data;
    const box = $("chartMeta");
    if (!box) return;
    if (!d || !d.ok) {
      box.innerHTML = `<span class="chart-bad">${esc(state.error || "无数据")}</span>`;
      return;
    }
    const b = d.latest;
    const chg = d.prev_close ? ((b.c - d.prev_close) / d.prev_close) * 100 : 0;
    const cls = chg >= 0 ? "positive" : "negative";
    box.innerHTML =
      `<span class="chart-chip">${esc(d.market_label || d.market)}</span>` +
      `<span class="chart-chip">${esc(d.symbol)} · ${esc(d.interval)}</span>` +
      `<span class="chart-chip">策略键 ${esc(d.strategy_key || "—")}</span>` +
      `<span>最新收盘 <b>${px(b.c)}</b> <em class="${cls}">${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%</em></span>` +
      `<span>K <b>${num(b.k)}</b> · D <b>${num(b.d)}</b> · 柱 <b style="color:${colorFor(b.hist)}">${num(b.hist, 4)}</b></span>` +
      `<span>推演方向 <b>${b.pos === "long" ? "多" : b.pos === "short" ? "空" : "空仓"}</b></span>` +
      `<span>K线开盘 <b>${bjFull(b.t)}</b></span>` +
      `<span id="chartCountdown"></span>` +
      parityHtml(d);
  }

  /** 运行器读数比对：证明图上的数值就是 bot 真正使用的那根 K 线的数值。 */
  function parityHtml(d) {
    const p = d.parity;
    if (!p) return "";
    if (!p.found) {
      return `<span class="chart-bad">运行器比对：${esc(p.note || "无同根K线")}</span>`;
    }
    const f = p.fields;
    const one = (key, zh) => {
      const v = f[key];
      const bad = v && v.abs_diff != null && v.abs_diff > 1e-6;
      return `<span class="${bad ? "chart-bad" : ""}">${zh} <b>${num(v.chart, 4)}</b></span>`;
    };
    const run = d.runner || {};
    return `<span class="chart-chip">运行器同根 ${esc(bjFull(p.bar_ms))}</span>` +
      one("k", "K") + one("d", "D") + one("hist", "柱") +
      (p.match
        ? `<span class="chart-ok">✅ 与运行器逐项一致</span>`
        : `<span class="chart-bad">⚠️ 与运行器不一致，请核查</span>`) +
      `<span>运行器持仓 <b>${esc(run.position || "—")}</b> · 快照 ${run.updated_ms ? esc(bj(run.updated_ms, true)) : "—"}</span>`;
  }

  function renderSignals() {
    const box = $("chartSignalBox");
    if (!box) return;
    const d = state.data;
    if (!d || !d.ok || !(d.signals || []).length) {
      box.innerHTML = `<div class="empty-state"><span>○</span><div><b>窗口内无 B/S 信号</b>` +
                      `<small>扩大显示根数可看到更早的信号</small></div></div>`;
      return;
    }
    const rows = d.signals.slice(-14).reverse();
    box.innerHTML =
      `<div class="chart-table-head"><span>信号K线 · 北京</span><span>信号</span><span>动作</span>` +
      `<span>K</span><span>D</span><span>MACD柱</span><span>依据</span></div>` +
      rows.map((s) =>
        `<div class="chart-table-row">` +
        `<span>${bjFull(s.t)}</span>` +
        `<b class="${s.side === "B" ? "positive" : "negative"}">${s.side}</b>` +
        `<span>${esc(s.action)}</span>` +
        `<span>${num(s.k)}</span><span>${num(s.d)}</span>` +
        `<span style="color:${colorFor(s.hist)}">${num(s.hist, 4)}</span>` +
        `<span class="chart-why">${esc(s.reason)}</span>` +
        `</div>`
      ).join("");
  }

  function renderStatus() {
    const el = $("chartStatus");
    if (!el) return;
    if (state.error) { el.textContent = state.error; el.className = "chart-status is-bad"; return; }
    if (!state.data || !state.data.ok) { el.textContent = "正在取数…"; el.className = "chart-status"; return; }
    const age = state.lastAt ? Math.floor((Date.now() - state.lastAt) / 1000) : 0;
    el.textContent = `${age <= 1 ? "刚刚更新" : age + " 秒前更新"} · ${state.auto ? "自动 5 秒" : "已暂停"}`;
    el.className = "chart-status" + (age > 15 ? " is-bad" : "");
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function tickCountdown() {
    const el = $("chartCountdown");
    const d = state.data;
    if (!el || !d || !d.ok || !d.latest) return;
    const step = INTERVAL_MS[state.interval] || 900000;
    const next = d.latest.t + step;
    const left = Math.max(0, next - Date.now());
    const s = Math.floor(left / 1000);
    el.innerHTML = `距本根收盘 <b>${Math.floor(s / 60)}:${pad2(s % 60)}</b>`;
  }

  // ------------------------------------------------------------------ 取数
  async function load() {
    if (state.loading) return;
    state.loading = true;
    const seq = ++state.reqSeq;
    try {
      const url = `${API}/api/chart?symbol=${encodeURIComponent(state.symbol)}` +
                  `&interval=${encodeURIComponent(state.interval)}&bars=${state.bars}`;
      const res = await fetch(url, { cache: "no-store" });
      const data = await res.json();
      if (seq !== state.reqSeq) return;
      if (!data.ok) throw new Error(data.reason || "接口返回失败");
      state.data = data;
      state.error = "";
      state.lastAt = Date.now();
    } catch (e) {
      if (seq !== state.reqSeq) return;
      state.error = "取数失败：" + (e && e.message ? e.message : e);
    } finally {
      state.loading = false;
      draw();
      renderHead();
      renderSignals();
      renderStatus();
      tickCountdown();
      updateZoomBadge();
    }
  }

  // ------------------------------------------------------------------ 交互
  const canvasGeom = () => {
    const cv = canvas();
    const d = state.data;
    if (!cv || !d || !d.bars || !d.bars.length) return null;
    const rect = cv.getBoundingClientRect();
    const R = regions(rect.width, rect.height);
    const n = d.bars.length;
    const vw = viewWindow(n);
    const g = barGeom(R.price, vw.count);
    return { cv, d, n, vw, g, R, x0: rect.left, y0: rect.top, w: rect.width };
  };

  function onMove(ev) {
    const geom = canvasGeom();
    if (!geom) return;
    const { n, vw, g, R, x0 } = geom;
    const x = ev.clientX - x0;
    const i = (x < R.price.x || x > R.price.x + R.price.w)
      ? -1
      : Math.max(0, Math.min(n - 1, Math.floor((x - R.price.x) / g.slot) + vw.start));
    if (i === state.hover) return;
    state.hover = i;
    draw();
  }

  /** 滚轮缩放：以光标下的那根 K 线为锚点，和交易所一致。 */
  function onWheel(ev) {
    const geom = canvasGeom();
    if (!geom) return;
    const { n, vw, g, R, x0 } = geom;
    const x = ev.clientX - x0;
    if (x < R.price.x || x > R.price.x + R.price.w) return;
    ev.preventDefault();
    const anchor = vw.start + (x - R.price.x) / g.slot;       // 光标处的浮点下标
    const ratio = (anchor - vw.start) / vw.count;             // 在可视区里的相对位置
    const factor = ev.deltaY < 0 ? 1 / ZOOM_STEP : ZOOM_STEP;
    const count = n <= MIN_BARS ? n
      : Math.max(MIN_BARS, Math.min(n, Math.round(vw.count * factor)));
    if (count === vw.count) return;
    const start = anchor - ratio * count;                     // 锚点保持不动
    state.view.count = count;
    state.view.offset = Math.max(0, Math.min(n - count, Math.round(n - start - count)));
    draw();
    updateZoomBadge();
  }

  /** 拖动平移：向右拖＝回看更早的行情。 */
  function onDown(ev) {
    const geom = canvasGeom();
    if (!geom) return;
    const { cv, n, vw } = geom;
    if (vw.count >= n) return;                                // 全窗口时无需平移
    drag = { x: ev.clientX, offset: vw.offset };
    state.hover = -1;
    try { cv.setPointerCapture(ev.pointerId); } catch (_) { /* 合成事件无此指针 */ }
    cv.classList.add("is-panning");
    draw();
  }

  function onDrag(ev) {
    const geom = canvasGeom();
    if (!geom) return;
    const { n, vw, g } = geom;
    const dBars = (ev.clientX - drag.x) / g.slot;
    state.view.offset = Math.max(0, Math.min(n - vw.count, Math.round(drag.offset + dBars)));
    drag.moved = true;
    draw();
    updateZoomBadge();
  }

  function onUp(ev) {
    const cv = canvas();
    drag = null;
    if (cv) {
      cv.classList.remove("is-panning");
      if (cv.releasePointerCapture) {
        try { cv.releasePointerCapture(ev.pointerId); } catch (_) { /* 指针已释放 */ }
      }
    }
  }

  function updateZoomBadge() {
    const el = $("chartZoomInfo");
    const d = state.data;
    if (!el || !d || !d.bars || !d.bars.length) return;
    const n = d.bars.length;
    const vw = viewWindow(n);
    const cls = vw.count < n ? "chart-zoom is-zoomed" : "chart-zoom";
    el.className = cls;
    el.innerHTML = vw.count < n
      ? `显示 <b>${vw.count}</b> / ${n} 根 · 滚轮缩放 · 拖动平移`
      : `全窗口 <b>${n}</b> 根 · 滚轮缩放`;
  }

  function bindSegments() {
    document.querySelectorAll("[data-chart-symbol]").forEach((b) =>
      b.addEventListener("click", () => {
        state.symbol = b.dataset.chartSymbol;
        resetView();
        syncSegments();
        load();
      })
    );
    document.querySelectorAll("[data-chart-interval]").forEach((b) =>
      b.addEventListener("click", () => {
        state.interval = b.dataset.chartInterval;
        resetView();
        syncSegments();
        load();
      })
    );
    const barsSel = $("chartBars");
    if (barsSel) {
      barsSel.value = String(state.bars);
      barsSel.addEventListener("change", () => {
        state.bars = Number(barsSel.value) || 200;
        resetView();
        load();
      });
    }
    const sig = $("chartShowSignals");
    if (sig) sig.addEventListener("change", () => { state.showSignals = sig.checked; draw(); });
    const tr = $("chartShowTrades");
    if (tr) tr.addEventListener("change", () => { state.showTrades = tr.checked; draw(); });
    const auto = $("chartAuto");
    if (auto) {
      auto.checked = state.auto;
      auto.addEventListener("change", () => {
        state.auto = auto.checked;
        startTimer();
        renderStatus();
      });
    }
    const rf = $("chartRefreshBtn");
    if (rf) rf.addEventListener("click", load);
    const zr = $("chartZoomReset");
    if (zr) zr.addEventListener("click", () => { resetView(); draw(); updateZoomBadge(); });

    const cv = canvas();
    if (cv) {
      cv.addEventListener("pointermove", (ev) => { if (drag) onDrag(ev); else onMove(ev); });
      cv.addEventListener("pointerleave", () => {
        if (drag) return;
        state.hover = -1;
        draw();
      });
      cv.addEventListener("pointerdown", onDown);
      cv.addEventListener("pointerup", onUp);
      cv.addEventListener("pointercancel", onUp);
      cv.addEventListener("wheel", onWheel, { passive: false });
      cv.addEventListener("dblclick", () => { resetView(); draw(); updateZoomBadge(); });
    }
    window.addEventListener("resize", () => draw());
  }

  function syncSegments() {
    document.querySelectorAll("[data-chart-symbol]").forEach((b) =>
      b.classList.toggle("is-on", b.dataset.chartSymbol === state.symbol)
    );
    document.querySelectorAll("[data-chart-interval]").forEach((b) =>
      b.classList.toggle("is-on", b.dataset.chartInterval === state.interval)
    );
  }

  function startTimer() {
    if (state.timer) clearInterval(state.timer);
    if (!state.active) return;
    state.timer = setInterval(() => {
      if (state.active && state.auto && !document.hidden) load();
      renderStatus();
    }, REFRESH_MS);
    if (state.clock) clearInterval(state.clock);
    state.clock = setInterval(() => {
      if (state.active) { renderStatus(); tickCountdown(); }
    }, 1000);
  }

  // ------------------------------------------------------------------ 生命周期
  function enter() {
    state.active = true;
    syncSegments();
    startTimer();
    // 布局在视图显示后再量尺寸
    requestAnimationFrame(() => { draw(); load(); });
  }

  function leave() {
    state.active = false;
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
    if (state.clock) { clearInterval(state.clock); state.clock = null; }
  }

  let bound = false;
  function boot() {
    if (bound) return;
    bound = true;
    bindSegments();
    syncSegments();
  }

  window.ChartModule = { enter, leave, boot, refresh: load };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
