/* BTC 量化终端 · 交易所风格控制台
 * ---------------------------------------------------------------------------
 * 数据一律来自本机面板 API（同源 8787 或跨端口回落），金额/仓位/委托/成交
 * 取自币安接口，不由本地账本推算；离线时显示明确的「不可达」状态，绝不伪造。
 *
 * 视图：交易台（重点信息）/ 监控（100% 覆盖）/ 图表 / 设置
 * 监控纪律：正常态安静；异常由横幅、徽标与监控清单喊出来；风控状态常驻可见。
 */
(() => {
  "use strict";

  // 面板(8787)与本地预览(8799，自带 /api 代理)同源直连；其他静态端口回落面板 API。
  const API = ["8787", "8799"].includes(location.port) ? "" : "http://127.0.0.1:8787";
  const REFRESH_MS = 20000;
  const $ = (id) => document.getElementById(id);

  const VIEWS = ["desk", "monitor", "chart", "settings"];
  let currentView = "desk";
  let timer = null;
  let clockTimer = null;
  let lastSyncAt = 0;
  let apiFailStreak = 0;

  // 数据缓存（同一轮刷新内绝不重复请求同一接口）
  let statusCache = null;
  let summaryCache = null;
  let readingCache = null;
  let equityCache = null;
  let monitorCache = null;
  let alertsCache = null;
  let marketCache = null;
  let ordersCache = [];
  let tradesCache = [];
  let sparks = { BTCUSDT: null, ETHUSDT: null };
  let sparkFetchedAt = 0;
  let wsAliveAt = 0;

  // 历史记录显示设置（沿用旧键，用户原设置不丢）
  const HISTORY_LIMITS = [10, 20];
  const historyLimitKey = "crypto.histLimit";
  let historyLimit = 10;
  try {
    const saved = Number(localStorage.getItem(historyLimitKey) || 10);
    if (HISTORY_LIMITS.includes(saved)) historyLimit = saved;
  } catch (e) { historyLimit = 10; }
  let histMode = "orders";
  let evtFilter = "all";

  // ------------------------------------------------------------------ utils
  const num = (v, d = 2) =>
    v === null || v === undefined || Number.isNaN(Number(v))
      ? "—"
      : Number(v).toLocaleString("en-US", {
          minimumFractionDigits: d,
          maximumFractionDigits: d,
        });

  const numOrNull = (v) =>
    v === undefined || v === null || v === "" || Number.isNaN(Number(v))
      ? null
      : Number(v);

  const signed = (v, d = 2) => {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    const n = Number(v);
    return (n >= 0 ? "+" : "") + num(n, d);
  };

  const pct = (v, d = 2) =>
    v === null || v === undefined || Number.isNaN(Number(v))
      ? "—"
      : (Number(v) * 100).toFixed(d) + "%";

  const signedPct = (v, d = 2) => {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    const n = Number(v);
    return (n >= 0 ? "+" : "") + (n * 100).toFixed(d) + "%";
  };

  const esc = (s) =>
    String(s === null || s === undefined ? "" : s).replace(
      /[&<>"']/g,
      (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );

  const coin = (symbol) => {
    const text = String(symbol || "").toUpperCase();
    if (text.startsWith("ETH")) return "ETH";
    if (text.startsWith("BTC")) return "BTC";
    return text.replace("USDT", "") || "—";
  };

  const coinChip = (symbol) => {
    const c = coin(symbol);
    const cls = c === "BTC" ? "btc" : c === "ETH" ? "eth" : "";
    return `<span class="chip ${cls}">${esc(c)}</span>`;
  };

  const sideZh = (side) => {
    const s = String(side || "FLAT").toUpperCase();
    return s === "LONG" ? "多" : s === "SHORT" ? "空" : "空仓";
  };

  const sideChip = (side) => {
    const s = String(side || "FLAT").toUpperCase();
    const cls = s === "LONG" ? "long" : s === "SHORT" ? "short" : "flat";
    return `<span class="chip ${cls}">${sideZh(s)}</span>`;
  };

  const pnlCls = (v) => {
    const n = Number(v);
    if (!Number.isFinite(n) || n === 0) return "";
    return n > 0 ? "up" : "down";
  };

  const pnlHtml = (v, d = 2) =>
    `<span class="${pnlCls(v)}">${signed(v, d)}</span>`;

  const fmtTime = (ms) => {
    if (!ms) return "—";
    const d = new Date(Number(ms));
    if (Number.isNaN(d.getTime())) return "—";
    return new Intl.DateTimeFormat("sv-SE", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit",
      day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
      hourCycle: "h23",
    }).format(d);
  };
  const fmtMDHM = (ms) => {
    const s = fmtTime(ms);
    return s === "—" ? "—" : s.slice(5, 16); // 10-05 09:54
  };
  const fmtHMS = (ms) => {
    const s = fmtTime(ms);
    return s === "—" ? "—" : s.slice(11); // 09:54:02
  };

  // 信号 K 线：bar_ms 是该根开盘时刻，信号须等收盘、下一根开盘执行
  const fmtKlineBeijing = (ms, interval) => {
    if (!ms) return "—";
    const start = fmtTime(ms);
    const mins = String(interval || "15m").toLowerCase() === "5m" ? 5 : 15;
    const end = fmtTime(Number(ms) + mins * 60_000);
    if (start === "—" || end === "—") return "—";
    return `${start.slice(5, 16)}–${end.slice(11, 16)}`;
  };

  const ageText = (ms) => {
    if (!ms) return "—";
    const s = Math.max(0, Math.round((Date.now() - Number(ms)) / 1000));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.round(s / 60)}m`;
    return `${(s / 3600).toFixed(1)}h`;
  };

  const ageTitle = (ms) => {
    if (!ms) return "无时间戳";
    return `更新于 ${fmtTime(ms)}`;
  };

  // 数值变化闪烁（一次性高亮，不做滚动动画）
  const prevNums = new Map();
  function flash(el, val, forceDir) {
    if (!el) return;
    const n = Number(val);
    if (!Number.isFinite(n)) return;
    const prev = prevNums.get(el.id);
    if (prev !== undefined && n !== prev) {
      const dir = forceDir || (n > prev ? "flash-up" : "flash-down");
      el.classList.remove("flash-up", "flash-down");
      void el.offsetWidth;
      el.classList.add(dir);
      setTimeout(() => el.classList.remove("flash-up", "flash-down"), 800);
    }
    prevNums.set(el.id, n);
  }

  const setText = (id, text, cls) => {
    const el = $(id);
    if (!el) return;
    el.textContent = text;
    if (cls !== undefined) el.className = cls;
  };

  let toastTimer = null;
  function toast(msg, kind) {
    const t = $("toast");
    if (!t) return;
    t.textContent = msg;
    t.className = "toast " + (kind || "");
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 7000);
  }

  async function api(path, opts) {
    const res = await fetch(API + path, Object.assign({ cache: "no-store" }, opts || {}));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  // ------------------------------------------------------------------ 状态推导
  // 运行器口径风控量：mtm = 钱包余额 + 未实现；日亏/回撤都以运行记录为准
  function runnerRisk() {
    const s = summaryCache || {};
    const q = s.quality || {};
    const ctx = q.context || {};
    const wallet = Number(s.wallet_balance || 0);
    const upnl = Number(s.unrealized_pnl || 0);
    const mtm = wallet + upnl;
    const dayStart = Number(ctx.day_start_eq || 0);
    const peakEq = Number(ctx.peak || 0);
    return {
      ctx, q, mtm, dayStart, peakEq,
      dayPnl: dayStart > 0 ? (mtm - dayStart) / dayStart : null,
      dayLoss: dayStart > 0 ? Math.max(0, (dayStart - mtm) / dayStart) : null,
      dd: peakEq > 0 ? Math.max(0, (peakEq - mtm) / peakEq) : null,
    };
  }

  function runnerInfo() {
    return (readingCache && readingCache.runner) || null;
  }

  function heartbeatState() {
    const r = runnerInfo();
    if (!r || !r.updated_ms) return { level: "bad", text: "无心跳", age: null };
    const ageSec = (Date.now() - Number(r.updated_ms)) / 1000;
    if (r.status !== "running" && r.status !== "starting")
      return { level: "bad", text: r.status || "未知", age: ageSec };
    if (ageSec > 90) return { level: "bad", text: "心跳过期", age: ageSec };
    if (ageSec > 60) return { level: "warn", text: "心跳偏慢", age: ageSec };
    return { level: "ok", text: r.status === "starting" ? "启动中" : "运行中", age: ageSec };
  }

  function marketState() {
    const m = marketCache || {};
    const ageSec = m.event_time_ms ? (Date.now() - Number(m.event_time_ms)) / 1000 : null;
    const sh = (monitorCache && monitorCache.stream_health) || null;
    const streamOk = !sh || sh.state === "connected"; // 无流数据时不误报
    if (ageSec === null) return { level: "warn", text: "无行情", ageSec: null, streamOk };
    if (!streamOk || ageSec > 120) return { level: "bad", text: "行情中断", ageSec, streamOk };
    if (ageSec > 30) return { level: "warn", text: "行情偏旧", ageSec, streamOk };
    return { level: "ok", text: "实时", ageSec, streamOk };
  }

  function protectionState() {
    const st = statusCache || {};
    const g = st.guardian || {};
    if (g.protection_ok === false) return { level: "bad", text: "保护单异常" };
    if (g.last_error) return { level: "warn", text: "有保护错误" };
    return { level: "ok", text: "正常" };
  }

  function reconState() {
    const st = statusCache || {};
    const nets = exchangeNets();
    const want = st.desired_nets || {};
    const gaps = ["BTCUSDT", "ETHUSDT"].map((sym) => {
      const w = Number(want[sym] || 0);
      const h = nets[sym] ? Number(nets[sym].signed || 0) : 0;
      return { sym, want: w, have: h, gap: Math.abs(w - h) };
    }).filter((r) => r.gap >= 5e-4);
    return { gaps, ok: gaps.length === 0 };
  }

  function exchangeNets() {
    const st = statusCache || {};
    const out = {};
    const put = (p) => {
      if (!p) return;
      const sym = p.symbol || "";
      if (!sym) return;
      const side = String(p.side || "FLAT").toUpperCase();
      const qty = Number(p.quantity || 0);
      out[sym] = Object.assign({}, p, {
        signed: side === "LONG" ? qty : side === "SHORT" ? -qty : 0,
      });
    };
    put(st.exchange_position);
    Object.values(st.exchange_positions || {}).forEach(put);
    return out;
  }

  // 告警合成：返回 { level, items, paused, holds, reconGaps }
  function computeAlerts() {
    const r = runnerRisk();
    const hb = heartbeatState();
    const mk = marketState();
    const pr = protectionState();
    const rc = reconState();
    const st = statusCache || {};
    const ext = st.external_interventions || {};
    const extEntries = Object.entries(ext).filter(([, v]) => v);
    const paused = extEntries.filter(([, v]) => v && v.paused);
    const holds = extEntries.filter(([, v]) => v && v.hold && !v.paused);

    const items = []; // {level, text}
    const push = (level, text) => items.push({ level, text });

    if (hb.level === "bad") push(2, `运行器：${hb.text}${hb.age != null ? `（${ageText(runnerInfo()?.updated_ms)} 前）` : ""}`);
    if (hb.level === "warn") push(2, `运行器：${hb.text}`);
    if (mk.level === "bad") push(2, mk.text === "行情中断" ? `行情数据中断（${Math.round(mk.ageSec || 0)}s 未更新）` : mk.text);
    if (mk.level === "warn") push(1, "行情数据偏旧");

    if (r.ctx.halted) push(3, "熔断已触发 · 停止开新仓，等待人工解除");
    if (r.dayLoss != null && r.dayLoss >= 0.03) push(3, `日亏 ${pct(r.dayLoss)} ≥ 3% 额度`);
    else if (r.dayLoss != null && r.dayLoss >= 0.02) push(2, `日亏 ${pct(r.dayLoss)} 接近 3% 额度`);
    if (r.dd != null && r.dd >= 0.10) push(3, `回撤 ${pct(r.dd)} ≥ 10% 熔断线`);
    else if (r.dd != null && r.dd >= 0.05) push(2, `回撤 ${pct(r.dd)} 已过 5%`);

    if (paused.length) push(3, `人工干预未确认 · 已暂停自动开仓：${paused.map(([s]) => coin(s)).join(" · ")}`);
    else if (holds.length) push(1, `已跟随人工操作：${holds.map(([s]) => coin(s)).join(" · ")}`);

    if (pr.level === "bad") push(3, "保护单异常 · 该标的已禁止开新仓");
    else if (pr.level === "warn") push(2, "保护单存在错误记录");

    if (!rc.ok) {
      const gapText = rc.gaps.map((g) => `${coin(g.sym)} 差 ${num(g.gap, 4)}`).join(" · ");
      const mode = ((readingCache || {}).runner || {}).mode;
      if (mode === "observation_only" || mode === "observe") {
        push(1, `观察模式：策略账本与交易所净仓不联动（${gapText}）`);
      } else {
        push(2, `净仓不一致：${gapText}`);
      }
    }

    if (r.ctx.missed_bars) push(1, `停机期间错过 ${r.ctx.missed_bars} 根 K 线（只记账不补单）`);

    // 近 2 小时关键事件提示（详情在事件流）
    const crit2h = (alertsCache || []).filter((a) => {
      if (String(a.level) !== "CRITICAL") return false;
      const t = Number(a.epoch || 0) * 1000;
      return t && Date.now() - t < 2 * 3600 * 1000;
    });
    if (crit2h.length) push(1, `近 2 小时关键事件 ${crit2h.length} 条（见事件流）`);

    const level = items.reduce((m, it) => Math.max(m, it.level), 0);
    return { level, items, paused, holds, reconGaps: rc.gaps };
  }

  // ------------------------------------------------------------------ 模式 / 市场文案
  // 单一来源：模式徽标、页脚、设置页都用这两个函数，避免各处硬编码「测试网」。
  // market 取自心跳的 market 字段（mainnet / testnet）；取不到时按「未知市场」显示，
  // 绝不猜成测试网 —— 猜错会让人以为在跑测试网，实际在下真钱单。
  const marketZh = (market) =>
    market === "mainnet" ? "主网"
    : market === "testnet" ? "测试网"
    : "未知市场";

  function modeZh(mode, market) {
    const raw = String(mode == null ? "" : mode);
    if (!raw) return "模式未上报";
    if (raw === "testnet_orders" || raw === "execute") {
      return `${marketZh(market)} · ${market === "mainnet" ? "真实下单" : "实盘下单"}`;
    }
    if (raw === "observe" || raw === "observation_only") return `${marketZh(market)} · 只观察`;
    if (raw === "paper") return "纸面 · Paper";
    return raw;
  }

  const isObserveMode = (mode) => mode === "observe" || mode === "observation_only";

  const isExecMode = (mode) => mode === "testnet_orders" || mode === "execute";

  // ------------------------------------------------------------------ 顶栏 / 行情条
  function renderTopbar() {
    const r = runnerInfo();
    const hb = heartbeatState();
    const mk = marketState();
    const rk = runnerRisk();

    // 模式（市场取自运行器心跳：主网/测试网由运行器上报，前端不猜）
    const mode = r && r.mode;
    const market = r && r.market;
    const modeText = modeZh(mode, market);
    const modeEl = $("modePill");
    if (modeEl) {
      const isObserve = isObserveMode(mode);
      const dotCls = hb.level === "ok" ? (isObserve ? "amber" : "green") : hb.level === "warn" ? "amber" : "red";
      modeEl.className = "pill " + (hb.level === "ok" ? (isObserve ? "is-warn" : "is-live") : hb.level === "warn" ? "is-warn" : "is-danger");
      modeEl.innerHTML = `<i class="dot ${dotCls}"></i><b>${esc(modeText)}</b>`;
      modeEl.title = r ? `心跳 ${fmtTime(r.updated_ms)} · pid ${r.pid || "—"} · ${modeText}` : "未收到运行器心跳";
    }

    const tsE = $("tsEngine");
    if (tsE) {
      tsE.className = "tstat " + (hb.level === "ok" ? "good" : hb.level === "warn" ? "warn" : "bad");
      $("tsEngineV").textContent = hb.age == null ? "—" : ageText(r && r.updated_ms);
      tsE.title = r ? `${hb.text} · ${ageTitle(r.updated_ms)}` : "无运行器心跳";
    }
    const tsF = $("tsFeed");
    if (tsF) {
      tsF.className = "tstat " + (mk.level === "ok" ? "good" : mk.level === "warn" ? "warn" : "bad");
      $("tsFeedV").textContent = mk.ageSec == null ? "—" : `${Math.round(mk.ageSec)}s`;
      tsF.title = `${mk.text} · 行情事件时间 ${fmtTime(marketCache && marketCache.event_time_ms)}`;
    }
    const tsR = $("tsRisk");
    if (tsR) {
      let cls = "good", text = "正常";
      if (rk.ctx.halted || (rk.dayLoss != null && rk.dayLoss >= 0.03) || (rk.dd != null && rk.dd >= 0.10)) {
        cls = "bad"; text = "危险";
      } else if ((rk.dayLoss != null && rk.dayLoss >= 0.02) || (rk.dd != null && rk.dd >= 0.05)) {
        cls = "warn"; text = "注意";
      }
      tsR.className = "tstat " + cls;
      $("tsRiskV").textContent = text;
      tsR.title = `日亏 ${pct(rk.dayLoss)} / 回撤 ${pct(rk.dd)} / halted ${rk.ctx.halted ? "true" : "false"}`;
    }

    const al = computeAlerts();
    const badge = $("alertBadge");
    if (badge) {
      badge.textContent = "L" + al.level;
      badge.className = "alert-badge l" + al.level;
      badge.title = al.items.length ? al.items.map((i) => `L${i.level} ${i.text}`).join("\n") : "正常运行";
    }

    const clk = $("clock");
    if (clk) clk.textContent = fmtTime(Date.now()).slice(11, 19);

    // 页脚同步状态
    const foot = $("footSync");
    if (foot) {
      if (!lastSyncAt) foot.textContent = "正在取数…";
      else {
        const age = Math.floor((Date.now() - lastSyncAt) / 1000);
        foot.textContent = `数据更新 ${fmtHMS(lastSyncAt)}${age <= 1 ? "（刚刚）" : `（${age}s 前）`}${apiFailStreak >= 2 ? " · 接口异常" : ""}`;
      }
    }

    // 页脚市场（由 /api/market 下发，取不到时保留 HTML 里的原文案）
    const fm = $("footMarket");
    if (fm) {
      const mc = marketCache || {};
      const label = mc.market_label
        || (mc.market === "mainnet" ? "主网 (mainnet)"
          : mc.market === "testnet" ? "测试网 (testnet)" : "");
      if (label) fm.textContent = "币安" + label;
    }
  }

  function renderTicker() {
    const m = marketCache || {};
    ["BTCUSDT", "ETHUSDT"].forEach((sym) => {
      const id = sym === "BTCUSDT" ? "BTC" : "ETH";
      const pxEl = $("tick" + id + "px");
      const chgEl = $("tick" + id + "chg");
      const closes = sparks[sym];
      let px = null;
      if (sym === "BTCUSDT") px = numOrNull(m.mark_price) ?? numOrNull(m.price);
      if (px == null) px = numOrNull(readingClose(sym));
      if (px != null && pxEl) {
        pxEl.textContent = num(px, 2);
        flash(pxEl, px);
      } else if (pxEl) {
        pxEl.textContent = "—";
      }
      if (closes && closes.length > 1 && chgEl) {
        const first = closes[0], last = closes[closes.length - 1];
        const chg = (last - first) / first;
        chgEl.textContent = `${chg >= 0 ? "▲" : "▼"} ${signedPct(chg)}`;
        chgEl.className = "chg mono " + (chg >= 0 ? "up" : "down");
        chgEl.title = "近 24 小时（96 根 15m）首尾对比";
      } else if (chgEl) {
        chgEl.textContent = "—";
      }
      drawSpark($("spark" + id), closes);
    });

    setText("tickFunding", m.funding_rate_annualized == null ? "—" : signedPct(m.funding_rate_annualized), "mono");
    setText("tickSpread", m.best_bid && m.best_ask ? `${num(m.best_bid, 1)} / ${num(m.best_ask, 1)}` : "—", "mono");
    const src = $("tickSource");
    if (src) {
      const sh = (monitorCache && monitorCache.stream_health) || null;
      const stream = sh ? (sh.state === "connected" ? "WS 已连接" : `WS ${sh.state || "未知"}`) : "WS —";
      src.textContent = `${m.market_label || m.market || "币安"} · ${stream}`;
    }
    setText("tickAge", m.event_time_ms ? ageText(m.event_time_ms) : "—", "mono");
  }

  function readingClose(sym) {
    const rows = (readingCache && readingCache.readings) || [];
    const hit = rows.find((x) => x && x.symbol === sym && x.interval === "15m");
    return hit ? hit.close : null;
  }

  function drawSpark(svg, closes) {
    if (!svg) return;
    if (!closes || closes.length < 2) { svg.innerHTML = ""; return; }
    const w = 86, h = 24, pad = 1;
    const min = Math.min.apply(null, closes), max = Math.max.apply(null, closes);
    const span = max - min || 1;
    const pts = closes.map((v, i) => {
      const x = pad + (i / (closes.length - 1)) * (w - pad * 2);
      const y = h - pad - ((v - min) / span) * (h - pad * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    const up = closes[closes.length - 1] >= closes[0];
    const color = up ? "#0ecb81" : "#f6465d";
    svg.innerHTML = `<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.2" opacity="0.85"></polyline>`;
  }

  // ------------------------------------------------------------------ 告警横幅
  function renderBanner() {
    const bar = $("banner");
    if (!bar) return;
    const al = computeAlerts();
    if (!al.items.length) { bar.hidden = true; return; }
    bar.hidden = false;
    const lv = al.level;
    bar.className = "banner " + (lv >= 3 ? "l3" : lv === 2 ? "l2" : "info-b");
    $("bannerIcon").textContent = lv >= 3 ? "!" : lv === 2 ? "!" : "i";
    $("bannerTitle").textContent =
      lv >= 3 ? `危险 · ${al.items.filter((i) => i.level >= 3).map((i) => i.text)[0] || "需要处理"}`
      : lv === 2 ? `注意 · ${al.items.filter((i) => i.level >= 2).map((i) => i.text).join("；")}`
      : "提示";
    $("bannerDetail").textContent = al.items.map((i) => `L${i.level} ${i.text}`).join("　·　");

    const box = $("bannerActions");
    const actions = [];
    al.paused.forEach(([sym, v]) => {
      actions.push(`<button class="btn danger sm" data-resume="${esc(sym)}" title="${esc(v && v.detail ? v.detail : "")}">确认恢复 ${esc(coin(sym))} 自动开仓</button>`);
    });
    if (al.reconGaps.length) {
      actions.push(`<button class="btn sm" data-act="reconcile">立即对账</button>`);
    }
    box.innerHTML = actions.join("");
    box.querySelectorAll("[data-resume]").forEach((b) =>
      b.addEventListener("click", () => resumeSymbol(b.dataset.resume)));
    box.querySelectorAll('[data-act="reconcile"]').forEach((b) =>
      b.addEventListener("click", () => doReconcile()));
  }

  // ------------------------------------------------------------------ 交易台
  function renderKpis() {
    const s = summaryCache || {};
    const r = runnerRisk();
    const st = statusCache || {};
    const nets = exchangeNets();
    const bySym = s.by_symbol || {};
    const posBySym = s.positions_by_symbol || {};

    // 钱包权益
    const wallet = numOrNull(s.wallet_balance);
    setText("kpiEquity", wallet == null ? "—" : num(wallet, 2), "v mono");
    flash($("kpiEquity"), wallet);
    setText("kpiEquitySub", s.available_balance == null ? "可用 —" : `可用 ${num(s.available_balance, 2)} · USDT`);

    // 未实现
    const upnl = numOrNull(s.unrealized_pnl);
    const upEl = $("kpiUpnl");
    if (upEl) {
      upEl.textContent = upnl == null ? "—" : signed(upnl, 2);
      upEl.className = "v mono " + pnlCls(upnl);
      flash(upEl, upnl);
    }
    const uParts = ["BTCUSDT", "ETHUSDT"].map((sym) => {
      const v = (posBySym[sym] || {}).unrealized_pnl;
      return v == null ? `${coin(sym)} —` : `${coin(sym)} ${signed(v, 2)}`;
    });
    setText("kpiUpnlSub", uParts.join(" · ") + " · USDT");

    // 今日盈亏
    const dayPnl = r.dayPnl;
    const dayEl = $("kpiDayPnl");
    if (dayEl) {
      dayEl.textContent = dayPnl == null ? "—" : signedPct(dayPnl);
      dayEl.className = "v mono " + pnlCls(dayPnl);
      flash(dayEl, dayPnl);
    }
    const dayUsd = r.dayStart > 0 ? r.mtm - r.dayStart : null;
    setText("kpiDayPnlSub", `日初 ${num(r.dayStart, 2)}${dayUsd == null ? "" : ` · ${signed(dayUsd, 2)} USDT`}`);

    // 回撤
    const dd = r.dd;
    const ddEl = $("kpiDd");
    if (ddEl) {
      ddEl.textContent = dd == null ? "—" : pct(dd);
      ddEl.className = "v mono " + (dd == null ? "" : dd >= 0.10 ? "down" : dd >= 0.05 ? "warn" : "");
      flash(ddEl, dd);
    }
    setText("kpiDdSub", `峰值 ${num(r.peakEq, 2)} · 运行器口径`);

    // 已实现净额（窗口）
    const real = numOrNull(s.net_pnl);
    const realEl = $("kpiReal");
    if (realEl) {
      realEl.textContent = real == null ? "—" : signed(real, 2);
      realEl.className = "v mono " + pnlCls(real);
      flash(realEl, real);
    }
    const fee = numOrNull(s.commission);
    setText("kpiRealSub", `自 ${esc(s.record_start || "—")} · 手续费 ${num(fee, 2)}`);

    // 保证金占用
    const totalMargin = ["BTCUSDT", "ETHUSDT"].reduce(
      (acc, sym) => acc + (Number((nets[sym] || {}).margin) || 0), 0);
    const marginPct = r.mtm > 0 ? totalMargin / r.mtm : null;
    const mEl = $("kpiMargin");
    if (mEl) {
      mEl.textContent = totalMargin > 0 ? num(totalMargin, 2) : "—";
      mEl.className = "v mono " + (marginPct == null ? "" : marginPct >= 0.8 ? "down" : marginPct >= 0.6 ? "warn" : "");
      flash(mEl, totalMargin);
    }
    const mSub = ["BTCUSDT", "ETHUSDT"].map((sym) => {
      const v = Number((nets[sym] || {}).margin) || 0;
      return `${coin(sym)} ${num(v, 0)}`;
    });
    setText("kpiMarginSub", `占用 ${marginPct == null ? "—" : pct(marginPct)} · ${mSub.join(" · ")}`);

    setText("kpiFoot",
      `记录窗口自 ${s.record_start || "—"} 起（委托/成交/成本/回撤）；持仓、余额、挂单为交易所实时状态。` +
      (s.record_start_note ? "" : ""));

    // 模式提示（若闸门关闭，明确展示）
    const rp = (runnerInfo() && runnerInfo().params) || {};
    const gates = [];
    if (rp.block_on_daily_loss === false) gates.push("日亏闸门关");
    if (rp.halt_on_max_drawdown === false) gates.push("回撤熔断关");
    if (gates.length) setText("kpiFoot", (($("kpiFoot") || {}).textContent || "") + ` 注意：${gates.join(" / ")}（以运行器参数为准，监控页可见）。`);
  }

  function renderPositions() {
    const st = statusCache || {};
    const nets = exchangeNets();
    const sources = st.position_sources || [];
    const m = marketCache || {};
    const box = $("posBox");
    if (!box) return;

    let html = "";
    let any = false;
    ["BTCUSDT", "ETHUSDT"].forEach((sym) => {
      const lots = sources.filter((src) => (src.symbol || "BTCUSDT") === sym);
      const pos = nets[sym] || {};
      const side = String(pos.side || "FLAT").toUpperCase();
      const qty = Number(pos.quantity || 0);
      if (!lots.length && (side === "FLAT" || !qty)) return;
      any = true;
      const mark = Number(pos.mark_price ?? (sym === "BTCUSDT" ? m.mark_price : null) ?? 0);

      lots.forEach((src) => {
        const lotSide = String(src.side || "LONG").toUpperCase();
        const sign = lotSide === "LONG" ? 1 : -1;
        const lotQty = Number(src.qty || 0);
        const lotPx = Number(src.px || 0);
        const est = mark && lotPx ? (mark - lotPx) * lotQty * sign : null;
        const notional = mark ? lotQty * mark * sign : null;
        const margin = notional == null ? null : Math.abs(notional) / 10;
        html += `<tr>
          <td>${coinChip(sym)}</td>
          <td><span class="src src-strategy">${esc(src.name || src.strategy_id || "策略")}</span></td>
          <td>${sideChip(lotSide)}</td>
          <td class="num">${notional == null ? "—" : num(notional, 2)}<span class="sub">${num(lotQty, 4)} ${coin(sym)}</span></td>
          <td class="num">${num(lotPx, 2)}<span class="sub">参考价</span></td>
          <td class="num">${mark ? num(mark, 2) : "—"}</td>
          <td class="num">${margin == null ? "—" : num(margin, 2)}<span class="sub">估算</span></td>
          <td class="num">${est == null ? "—" : pnlHtml(est)}<span class="sub">估算</span></td>
          <td class="c">${src.layer_count != null ? `${src.layer_count}/${src.max_layers || 3}` : "—"}</td>
          <td><span class="mono">${esc(fmtKlineBeijing(src.bar_ms, src.interval))}</span><span class="sub why">${esc(src.reason || "—")}</span></td>
        </tr>`;
      });

      if (side !== "FLAT" && qty) {
        const netNotional = Number(pos.notional || 0) || (mark ? qty * mark * (side === "LONG" ? 1 : -1) : 0);
        const netMargin = Number(pos.margin || 0) || Math.abs(netNotional) / 10;
        const upnl = Number(pos.unrealized_pnl || 0);
        html += `<tr class="is-net">
          <td>${coinChip(sym)}</td>
          <td><span class="tag dim">币安净仓</span></td>
          <td>${sideChip(side)}</td>
          <td class="num">${num(netNotional, 2)}<span class="sub">${num(qty, 4)} ${coin(sym)}</span></td>
          <td class="num">${pos.entry_price ? num(pos.entry_price, 2) : "—"}<span class="sub">成交均价</span></td>
          <td class="num">${pos.mark_price ? num(pos.mark_price, 2) : (mark ? num(mark, 2) : "—")}</td>
          <td class="num">${num(netMargin, 2)}<span class="sub">交易所</span></td>
          <td class="num">${pnlHtml(upnl)}<span class="sub">交易所</span></td>
          <td class="c">—</td>
          <td><span class="sub why">${lots.length > 1 ? "两条策略合成" : lots[0] ? `${esc(lots[0].name)} 对应净仓` : "交易所账户"}</span></td>
        </tr>`;
      }
    });

    box.innerHTML = html || `<tr><td colspan="10"><div class="empty"><b>空仓</b>当前无持仓</div></td></tr>`;

    const liveNets = ["BTCUSDT", "ETHUSDT"].filter((sym) => {
      const p = nets[sym] || {};
      return String(p.side || "FLAT").toUpperCase() !== "FLAT" && Number(p.quantity || 0);
    });
    const sideTag = $("posSideTag");
    if (sideTag) {
      sideTag.textContent = liveNets.length
        ? liveNets.map((sym) => `${coin(sym)} ${sideZh(nets[sym].side)}`).join(" · ")
        : "空仓";
      sideTag.className = "tag " + (liveNets.length ? "up" : "dim");
    }
    const totalMargin = ["BTCUSDT", "ETHUSDT"].reduce(
      (acc, sym) => acc + (Number((nets[sym] || {}).margin) || 0), 0);
    setText("posMarginTag", totalMargin > 0 ? `保证金 ${num(totalMargin, 2)} USDT` : "保证金 —", "mono");
    setText("posFoot", "策略腿与币安净仓逐位核对：数量应相等；不等即为外部干预、漏单或部分成交，需查对账。两行开仓价口径不同（参考价 vs 成交均价）。");
  }

  function renderOpenOrders() {
    const st = statusCache || {};
    const list = st.open_orders || [];
    const box = $("openOrdersBox");
    if (!box) return;
    const nProtect = list.filter((o) => o.reduceOnly).length;
    setText("openOrdersCount", String(list.length), "tag " + (list.length ? "warn" : "dim"));
    setText("openOrdersMeta", list.length ? `保护单 ${nProtect} / 开仓单 ${list.length - nProtect}` : "追价完成后不留挂单");
    box.innerHTML = list.length ? list.map((o) => {
      const isProt = !!o.reduceOnly;
      const sideTxt = o.side === "SELL" ? "卖出" : "买入";
      const sideCls = o.side === "SELL" ? "down" : "up";
      const kind = isProt
        ? `<span class="tag warn" title="${esc(o.clientOrderId || "")}">保护单</span>`
        : `<span class="tag info">开仓/加仓</span>`;
      return `<tr>
        <td><span class="mono">${esc(fmtHMS(o.time))}</span></td>
        <td>${coinChip(o.symbol)}</td>
        <td class="${sideCls}">${sideTxt}</td>
        <td>${kind}</td>
        <td class="num">${num(o.price, 2)}</td>
        <td class="num">${num(o.origQty, 4)}</td>
        <td><span class="mono muted" title="${esc(o.clientOrderId || "")}">${esc(String(o.clientOrderId || "").slice(0, 14))}</span></td>
      </tr>`;
    }).join("") : `<tr><td colspan="7"><div class="empty"><b>无挂单</b>追价完成后不留挂单</div></td></tr>`;
  }

  // ------------------------------------------------------------------ 历史记录
  const isHistoryRow = (row) => {
    const start = Number((summaryCache || {}).record_start_ms || 0);
    return String(row.source || "") === "strategy" &&
      (!start || Number(row.time ?? row.updateTime ?? 0) >= start);
  };

  function orderPnlMap() {
    const map = {};
    (tradesCache || []).forEach((t) => {
      const id = String(t.orderId || "");
      if (!id) return;
      if (!map[id]) map[id] = { realized: 0, commission: 0 };
      map[id].realized += Number(t.realizedPnl || 0);
      map[id].commission += Number(t.commission || 0);
    });
    return map;
  }

  function collapseTradesByOrder() {
    const by = {};
    (tradesCache || []).filter(isHistoryRow).forEach((t) => {
      const id = String(t.orderId || "");
      if (!id) return;
      if (!by[id]) {
        by[id] = { orderId: id, time: t.time, source: t.source, symbol: t.symbol || "",
          side: t.side, maker: true, qty: 0, notional: 0, realized: 0, commission: 0, fills: 0 };
      }
      const row = by[id];
      row.symbol = row.symbol || t.symbol || "";
      row.time = Math.max(Number(row.time || 0), Number(t.time || 0));
      row.qty += Number(t.qty || 0);
      row.notional += Number(t.price || 0) * Number(t.qty || 0);
      row.realized += Number(t.realizedPnl || 0);
      row.commission += Number(t.commission || 0);
      row.fills += 1;
      row.maker = row.maker && !!t.maker;
    });
    return Object.values(by).sort((a, b) => Number(b.time || 0) - Number(a.time || 0));
  }

  function renderHistory() {
    const box = $("histBox");
    if (!box) return;
    setText("histTitle", histMode === "orders" ? "历史委托" : "历史成交");
    const start = (summaryCache || {}).record_start || "—";
    const pnlMap = orderPnlMap();

    if (histMode === "orders") {
      const rows = (ordersCache || [])
        .filter((o) => isHistoryRow(o) && Number(o.executedQty || 0) > 0)
        .sort((a, b) => Number(b.time || 0) - Number(a.time || 0))
        .slice(0, historyLimit);
      setText("histMeta", `${rows.length} / 最多 ${historyLimit} 条 · 自 ${start}`, "mono");
      box.innerHTML = `<table class="grid">
        <thead><tr><th>时间</th><th>合约</th><th>方向 / 类型</th><th class="num">委托 / 均价</th><th class="num">委托量</th><th class="num">成交量</th><th class="num">盈亏</th><th>来源</th><th>状态</th></tr></thead>
        <tbody>${rows.length ? rows.map((o) => {
          const pnl = pnlMap[String(o.orderId)];
          const net = pnl ? pnl.realized - pnl.commission : null;
          return `<tr>
            <td><span class="mono">${esc(fmtMDHM(o.time))}</span></td>
            <td>${coinChip(o.symbol)}</td>
            <td>${o.side === "BUY" ? "买" : "卖"} · ${esc(o.type || "")}</td>
            <td class="num">${num(o.price, 2)} / ${Number(o.avgPrice) ? num(o.avgPrice, 2) : "—"}</td>
            <td class="num">${num(o.origQty, 4)}</td>
            <td class="num">${num(o.executedQty, 4)}</td>
            <td class="num">${net == null ? "—" : pnlHtml(net)}</td>
            <td><span class="src src-strategy">策略自动</span></td>
            <td>${esc(statusZh(o.status))}</td>
          </tr>`;
        }).join("") : `<tr><td colspan="9"><div class="empty"><b>无记录</b>记录起点之后暂无已成交的策略委托</div></td></tr>`}</tbody>
      </table>`;
    } else {
      const rows = collapseTradesByOrder().slice(0, historyLimit);
      setText("histMeta", `${rows.length} / 最多 ${historyLimit} 笔 · 自 ${start}`, "mono");
      box.innerHTML = `<table class="grid">
        <thead><tr><th>时间</th><th>合约</th><th>订单号</th><th>方向 / 角色</th><th class="num">价格 × 数量</th><th class="num">已实现</th><th class="num">手续费</th><th class="num">净盈亏</th><th>来源</th></tr></thead>
        <tbody>${rows.length ? rows.map((t) => {
          const px = t.qty ? t.notional / t.qty : 0;
          const net = t.realized - t.commission;
          return `<tr>
            <td><span class="mono">${esc(fmtMDHM(t.time))}</span></td>
            <td>${coinChip(t.symbol)}</td>
            <td><span class="mono muted" title="${esc(t.orderId)}">…${esc(String(t.orderId).slice(-8))}</span></td>
            <td>${t.side === "BUY" ? "买" : "卖"} · ${t.maker ? "Maker" : "Taker"}${t.fills > 1 ? ` ×${t.fills}` : ""}</td>
            <td class="num">${num(px, 2)} × ${num(t.qty, 4)}</td>
            <td class="num">${signed(t.realized, 2)}</td>
            <td class="num">${num(t.commission, 4)}</td>
            <td class="num">${pnlHtml(net)}</td>
            <td><span class="src src-strategy">策略自动</span></td>
          </tr>`;
        }).join("") : `<tr><td colspan="9"><div class="empty"><b>无记录</b>记录起点之后暂无策略成交</div></td></tr>`}</tbody>
      </table>`;
    }
  }

  function statusZh(st) {
    const s = String(st || "").toUpperCase();
    if (s === "FILLED") return "已成交";
    if (s === "CANCELED") return "已撤销";
    if (s === "NEW") return "挂单中";
    if (s === "PARTIALLY_FILLED") return "部分成交";
    if (s === "EXPIRED") return "已过期";
    return s || "—";
  }

  // ------------------------------------------------------------------ 右栏：系统监控清单
  function renderHealth() {
    const box = $("healthList");
    if (!box) return;
    const hb = heartbeatState();
    const mk = marketState();
    const pr = protectionState();
    const rc = reconState();
    const r = runnerRisk();
    const rp = (runnerInfo() && runnerInfo().params) || {};
    const st = statusCache || {};
    const g = st.guardian || {};
    const sh = (monitorCache && monitorCache.stream_health) || null;
    const s = summaryCache || {};

    const rows = [];
    rows.push({
      cls: hb.level === "bad" ? "is-bad" : hb.level === "warn" ? "is-warn" : "",
      dot: hb.level === "ok" ? "green pulse" : hb.level === "warn" ? "amber" : "red",
      k: "运行器", v: hb.age == null ? "无心跳" : `${hb.text} · ${ageText(runnerInfo()?.updated_ms)}`,
      x: runnerInfo() ? `pid ${runnerInfo().pid || "—"}` : "",
    });
    rows.push({
      cls: mk.level === "bad" ? "is-bad" : mk.level === "warn" ? "is-warn" : "",
      dot: mk.level === "ok" ? "green pulse" : mk.level === "warn" ? "amber" : "red",
      k: "行情链路", v: sh && sh.state !== "connected" ? `WS ${sh.state}` : mk.text,
      x: mk.ageSec == null ? "" : `${Math.round(mk.ageSec)}s 前`,
    });
    const riskBad = r.ctx.halted || (r.dayLoss != null && r.dayLoss >= 0.03) || (r.dd != null && r.dd >= 0.10);
    const riskWarn = !riskBad && ((r.dayLoss != null && r.dayLoss >= 0.02) || (r.dd != null && r.dd >= 0.05));
    rows.push({
      cls: riskBad ? "is-bad" : riskWarn ? "is-warn" : "",
      dot: riskBad ? "red" : riskWarn ? "amber" : "green",
      k: "风控", v: riskBad ? "危险" : riskWarn ? "注意" : "正常",
      x: `日亏 ${pct(r.dayLoss)} / 回撤 ${pct(r.dd)}`,
    });
    const protectOrders = (st.open_orders || []).filter((o) => o.reduceOnly).length;
    rows.push({
      cls: pr.level === "bad" ? "is-bad" : pr.level === "warn" ? "is-warn" : "",
      dot: pr.level === "ok" ? "green" : pr.level === "warn" ? "amber" : "red",
      k: "保护单", v: pr.text,
      x: `${protectOrders} 张挂单 · ${g.allow_new_entries ? "允许开新仓" : "禁止开新仓"}`,
    });
    rows.push({
      cls: rc.ok ? "" : "is-bad",
      dot: rc.ok ? "green" : "red",
      k: "账本对账", v: rc.ok ? "一致" : "不一致",
      x: rc.ok ? "" : rc.gaps.map((g2) => `${coin(g2.sym)} 差 ${num(g2.gap, 4)}`).join(" · "),
    });
    rows.push({
      cls: r.ctx.missed_bars ? "is-warn" : "",
      dot: r.ctx.missed_bars ? "amber" : "green",
      k: "数据质量", v: `错过 ${r.ctx.missed_bars ?? 0} 根 / ${r.ctx.missed_signals ?? 0} 信号`,
      x: s.coverage ? `成交覆盖 ${s.coverage.fills ?? "—"} 笔` : "",
    });
    rows.push({
      cls: "",
      dot: "blue",
      k: "记录窗口", v: `自 ${s.record_start || "—"}`,
      x: `净额 ${signed(s.net_pnl, 2)}`,
    });
    if (rp.block_on_daily_loss === false || rp.halt_on_max_drawdown === false) {
      const off = [];
      if (rp.block_on_daily_loss === false) off.push("日亏闸门关");
      if (rp.halt_on_max_drawdown === false) off.push("回撤熔断关");
      rows.push({ cls: "is-warn", dot: "amber", k: "风控闸门", v: off.join(" / "), x: "运行器参数" });
    }

    box.innerHTML = rows.map((r2) =>
      `<div class="hl ${r2.cls}"><span class="dot ${r2.dot}"></span><span class="hl-k">${esc(r2.k)}</span><span class="hl-v">${r2.v}</span>${r2.x ? `<span class="hl-x">${r2.x}</span>` : ""}</div>`
    ).join("");
  }

  // ------------------------------------------------------------------ 右栏：信号卡
  function renderSignals() {
    const box = $("sigCards");
    if (!box) return;
    const reading = readingCache || {};
    const rows = (reading.readings || []).filter((r) => r && r.symbol && r.interval === "15m");
    setText("sigMeta", reading.updated_ms ? `更新 ${fmtHMS(reading.updated_ms)}` : "—", "ph-meta mono");
    if (!rows.length) {
      box.innerHTML = `<div class="empty"><b>等待读数</b>运行器尚未落盘 latest_reading</div>`;
      return;
    }
    const st = statusCache || {};
    const srcs = st.position_sources || [];
    box.innerHTML = rows.map((r) => {
      const sym = r.symbol;
      const k = numOrNull(r.K), d = numOrNull(r.D), hist = numOrNull(r.MACD_HIST);
      const src = srcs.find((x) => (x.symbol || "") === sym);
      const posTag = src
        ? `<span class="tag ${src.side === "LONG" ? "up" : "down"}">${sideZh(src.side)} ${src.layer_count ?? 1}/${src.max_layers ?? 3} 层</span>`
        : `<span class="tag dim">空仓</span>`;
      const cross = r.gold ? "金叉" : r.dead ? "死叉" : "无交叉";
      const crossCls = r.gold ? "up" : r.dead ? "down" : "muted";
      const histCls = hist == null ? "" : hist > 0 ? "up" : hist < 0 ? "down" : "";
      const kdRel = k != null && d != null ? (k > d ? "K 在 D 上" : k < d ? "K 在 D 下" : "K=D") : "—";
      const missed = (r.missed_bars || r.missed_signals)
        ? ` · <span class="warn">错过 ${r.missed_bars || 0}/${r.missed_signals || 0}</span>` : "";
      return `<div class="sig-card">
        <div class="sig-head">
          <span class="name">${esc(coin(sym))} 15m</span>
          <span class="id">${esc(src ? src.strategy_id : sym === "BTCUSDT" ? "kdj15" : "eth15")}</span>
          ${posTag}
        </div>
        <div class="sig-nums">
          <span class="sig-num"><span class="k">K</span><span class="v mono">${k == null ? "—" : num(k, 1)}</span></span>
          <span class="sig-num"><span class="k">D</span><span class="v mono">${d == null ? "—" : num(d, 1)}</span></span>
          <span class="sig-num"><span class="k">MACD柱</span><span class="v mono ${histCls}">${hist == null ? "—" : signed(hist, 2)}</span></span>
        </div>
        ${sigSpark(r.series)}
        <div class="sig-foot">
          <span class="grow">${esc(fmtKlineBeijing(r.bar_ms, r.interval))} · <span class="${crossCls}">${cross}</span> · ${kdRel}${missed}</span>
        </div>
      </div>`;
    }).join("");
  }

  function sigSpark(series) {
    const k = (series && series.k) || [];
    const d = (series && series.d) || [];
    const h = (series && series.hist) || [];
    if (k.length < 2) return "";
    const W = 240, H = 40, topH = 26, pad = 2;
    const n = k.length;
    const xAt = (i) => pad + (i / (n - 1)) * (W - pad * 2);
    const yAt = (v) => pad + (1 - Math.min(100, Math.max(0, v)) / 100) * (topH - pad * 2);
    const line = (arr) => arr.map((v, i) => `${xAt(i).toFixed(1)},${yAt(Number(v)).toFixed(1)}`).join(" ");
    const maxH = Math.max.apply(null, h.map((v) => Math.abs(Number(v) || 0)).concat([1e-9]));
    const bw = (W - pad * 2) / n * 0.6;
    const bars = h.map((v, i) => {
      const val = Number(v) || 0;
      const hh = Math.max(1, Math.abs(val) / maxH * (H - topH - pad));
      const x = xAt(i) - bw / 2;
      const y = val >= 0 ? H - hh - pad : H - pad - 0.5;
      const color = val >= 0 ? "#0ecb81" : "#f6465d";
      return `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${hh.toFixed(1)}" fill="${color}" opacity="0.55"></rect>`;
    }).join("");
    return `<svg class="sig-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">
      <polyline points="${line(k)}" fill="none" stroke="#6aa9ff" stroke-width="1.4"></polyline>
      <polyline points="${line(d)}" fill="none" stroke="#d9a441" stroke-width="1.4"></polyline>
      ${bars}
    </svg>`;
  }

  // ------------------------------------------------------------------ 事件流
  function feedItem(a, compact) {
    const lv = String(a.level || "INFO").toUpperCase();
    const cls = lv === "CRITICAL" ? "is-critical" : "";
    const t = Number(a.epoch || 0) * 1000;
    return `<div class="feed-item ${cls}">
      <span class="lv ${lv}">${lv === "CRITICAL" ? "CRIT" : lv}</span>
      <span class="tx"><b>${esc(a.title || a.key || "事件")}</b>${a.detail ? `<span>${esc(String(a.detail).slice(0, compact ? 80 : 200))}</span>` : ""}</span>
      <span class="ts">${esc(t ? fmtMDHM(t) : "—")}</span>
    </div>`;
  }

  function renderFeed() {
    const box = $("feedBox");
    if (!box) return;
    const list = alertsCache;
    if (!list) { box.innerHTML = `<div class="empty"><b>读取中</b>—</div>`; return; }
    if (!list.length) { box.innerHTML = `<div class="empty"><b>暂无事件</b>alerts.jsonl 为空</div>`; return; }
    setText("feedMeta", `近 ${list.length} 条 · 最新 ${fmtHMS(Number(list[0].epoch || 0) * 1000)}`, "ph-meta mono");
    box.innerHTML = list.slice(0, 9).map((a) => feedItem(a, true)).join("");
  }

  // ------------------------------------------------------------------ 监控视图
  function kvRows(box, rows) {
    if (!box) return;
    box.innerHTML = rows.map((r) =>
      `<div class="kv-row ${r.cls || ""}"><span class="k">${r.k}</span><span class="v">${r.v}</span>${r.x ? `<span class="x">${r.x}</span>` : ""}</div>`
    ).join("");
  }

  // 监控页顶部 · 系统自检横条：一行回答用户的 6 个问题。
  // 只读现有状态缓存，不额外发请求；任何字段缺失都降级显示，不抛错。
  function renderSelfCheck() {
    const box = $("selfCheckList");
    if (!box) return;
    const st = statusCache || {};
    const g = st.guardian || {};
    const s = summaryCache || {};
    const cov = s.coverage || {};

    const hb = heartbeatState();
    const mkState = marketState();
    const risk = runnerRisk();
    const pr = protectionState();
    const rc = reconState();
    const rInfo = runnerInfo();

    // ① 运行器
    const rows = [{
      lv: hb.level, k: "运行器",
      v: hb.age == null ? "无心跳" : hb.text,
      title: hb.age == null ? "未收到运行器心跳" : `心跳 ${ageText(rInfo && rInfo.updated_ms)} 前`,
    }];

    // ② 行情数据（含市场一致性哨兵：面板采集器 vs 运行器）
    const panelMarket = (marketCache || {}).market || null;
    const runnerMarket = (monitorCache && monitorCache.heartbeat && monitorCache.heartbeat.market)
      || (rInfo && rInfo.market) || null;
    const diverge = Boolean(panelMarket && runnerMarket && panelMarket !== runnerMarket);
    rows.push({
      lv: diverge ? "bad" : mkState.level, k: "行情数据",
      v: diverge ? "市场不一致" : (mkState.ageSec == null ? "无行情" : mkState.text),
      title: diverge
        ? `面板采集器 ${panelMarket} ≠ 运行器 ${runnerMarket}：页面价格不是下单用的价格`
        : "行情流与运行器同市场",
    });

    // ③ 风控
    const riskBad = risk.ctx.halted
      || (risk.dayLoss != null && risk.dayLoss >= 0.03)
      || (risk.dd != null && risk.dd >= 0.10);
    const riskWarn = !riskBad && ((risk.dayLoss != null && risk.dayLoss >= 0.02)
      || (risk.dd != null && risk.dd >= 0.05));
    rows.push({
      lv: riskBad ? "bad" : riskWarn ? "warn" : "ok", k: "风控",
      v: riskBad ? (risk.ctx.halted ? "已熔断" : "接近上限") : riskWarn ? "注意" : "正常",
      title: `日亏 ${pct(risk.dayLoss)} / 回撤 ${pct(risk.dd)} / halted ${risk.ctx.halted ? "true" : "false"}`,
    });

    // ④ 执行与保护
    const nProt = (st.open_orders || []).filter((o) => o.reduceOnly).length;
    let lv4 = pr.level, v4 = pr.text;
    if (lv4 !== "bad" && g.allow_new_entries === false) { lv4 = "warn"; v4 = "禁止开新仓"; }
    rows.push({
      lv: lv4, k: "执行与保护", v: v4,
      title: `保护单 ${nProt} 张 · ${g.allow_new_entries === false ? "禁止开新仓" : "允许开新仓"}`,
    });

    // ⑤ 账本
    rows.push({
      lv: rc.ok ? "ok" : "bad", k: "账本", v: rc.ok ? "一致" : "不一致",
      title: rc.ok ? "策略账本与交易所净仓一致"
        : rc.gaps.map((x) => `${coin(x.sym)} 差 ${num(x.gap, 4)}`).join(" · "),
    });

    // ⑥ 数据核对（接口覆盖 / 是否截断）
    const fills = cov.fills;
    rows.push({
      lv: cov.truncated || fills == null ? "warn" : "ok", k: "数据核对",
      v: fills == null ? "无成交数据" : `覆盖 ${fills} 笔`,
      title: cov.truncated ? "接口返回被截断，统计窗口可能不完整" : "接口覆盖完整（未截断）",
    });

    box.innerHTML = rows.map((r2) => {
      const cls = r2.lv === "bad" ? "is-bad" : r2.lv === "warn" ? "is-warn" : "";
      const dot = r2.lv === "ok" ? "green" : r2.lv === "warn" ? "amber" : "red";
      return `<div class="hl ${cls}" title="${esc(r2.title || "")}">` +
        `<span class="dot ${dot}"></span><span class="hl-k">${esc(r2.k)}</span>` +
        `<span class="hl-v">${esc(r2.v)}</span></div>`;
    }).join("");
  }

  function renderMonitorView() {
    renderSelfCheck();
    const st = statusCache || {};
    const s = summaryCache || {};
    const reading = readingCache || {};
    const r = runnerInfo();
    const rp = (r && r.params) || {};
    const g = st.guardian || {};
    const mk = marketCache || {};
    const mon = monitorCache || {};
    const sh = mon.stream_health || null;
    const lock = mon.lock || null;
    const risk = runnerRisk();
    const nets = exchangeNets();

    // ---- 运行器
    const hb = heartbeatState();
    setText("mrMeta", r && r.updated_ms ? `心跳 ${fmtTime(r.updated_ms)}` : "无心跳", "ph-meta mono");
    const uptime = r && r.started_ms ? ((Date.now() - Number(r.started_ms)) / 3600000).toFixed(1) + " h" : "—";
    kvRows($("mrKv"), [
      { k: "状态", v: `<span class="${hb.level === "ok" ? "up" : hb.level === "warn" ? "warn" : "down"}">${esc(hb.text)}</span>`, x: r ? esc(r.status || "") : "" },
      { k: "模式", v: `<span class="${hb.level === "ok" ? (isObserveMode(r && r.mode) ? "warn" : "up") : "down"}">${esc(modeZh(r && r.mode, r && r.market))}</span>`, x: isExecMode(r && r.mode) ? "execute：按信号真实下单" : isObserveMode(r && r.mode) ? "只记账不下单" : "" },
      { k: "进程", v: r && r.pid ? `pid ${r.pid}` : "—", x: lock && lock.pid ? `锁文件 pid ${lock.pid}` : "无锁文件" },
      { k: "启动时间", v: r && r.started_ms ? fmtTime(r.started_ms) : "—", x: `运行 ${uptime}` },
      { k: "心跳时间", v: r && r.updated_ms ? fmtTime(r.updated_ms) : "—", x: hb.age == null ? "" : `${Math.round(hb.age)}s 前` },
      { k: "轮询间隔", v: r && r.interval_sec ? `${r.interval_sec}s` : "—", x: "每轮检查信号与净仓" },
      { k: "活跃策略", v: esc((r && r.strategies || []).join(" · ") || "—"), x: "5m 已停用" },
      { k: "品种 / 市场", v: esc(((r && r.symbols) || []).join(" + ") || "—"), x: esc(r && r.market || "—") },
      { k: "参数指纹", v: esc(r && r.params_fingerprint || "—"), x: "变更需重启生效" },
    ]);

    // ---- 行情链路
    const kline = sh && sh.kline_age_sec || {};
    const markAge = sh && sh.mark_age_sec || {};
    setText("mfMeta", mk.event_time_ms ? `行情 ${fmtTime(mk.event_time_ms)}` : "—", "ph-meta mono");
    const rd = (reading.readings || []).find((x) => x && x.symbol === "BTCUSDT" && x.interval === "15m") || reading;

    // 行情通道来源：按 K 线地址的 host 判断，不再硬编码「测试网」。
    // 这正是 2026-10-02 事故的观测点 —— 行情腿跟错市场时这里必须看得见。
    const klineUrl = String((rd && rd.kline_url) || "");
    const klineSrcText = /testnet\.binancefuture\.com|demo-fapi/.test(klineUrl)
      ? "测试网 K 线"
      : /fapi\.binance\.com/.test(klineUrl) ? "主网 K 线" : "来源未知";

    // 行情一致性：面板采集器市场 vs 运行器市场。
    // 两者分叉 = 页面价格不是运行器下单用的价格（本次故障的核心症状）。
    const panelMarket = mk.market || null;
    const runnerMarket = (mon.heartbeat && mon.heartbeat.market) || (r && r.market) || null;
    const mkConsistent = panelMarket && runnerMarket
      ? { ok: panelMarket === runnerMarket }
      : null;

    kvRows($("mfKv"), [
      { k: "市场", v: esc(mk.market_label || mk.market || "—"), x: esc(mk.source || "") },
      {
        k: "行情一致性",
        v: !mkConsistent ? "—"
          : mkConsistent.ok ? `<span class="up">一致</span>` : `<span class="down">不一致</span>`,
        x: !mkConsistent ? ""
          : mkConsistent.ok ? "面板采集器 = 运行器"
            : `面板 ${esc(panelMarket)} ≠ 运行器 ${esc(runnerMarket)}`,
        cls: mkConsistent && !mkConsistent.ok ? "is-bad" : "",
      },
      { k: "标记价 BTC", v: mk.mark_price ? num(mk.mark_price, 2) : "—", x: mk.event_time_ms ? `${Math.round((Date.now() - mk.event_time_ms) / 1000)}s 前` : "" },
      { k: "指数价", v: mk.index_price ? num(mk.index_price, 2) : "—" },
      { k: "资金费年化", v: mk.funding_rate_annualized == null ? "—" : signedPct(mk.funding_rate_annualized) },
      { k: "买一 / 卖一", v: mk.best_bid && mk.best_ask ? `${num(mk.best_bid, 1)} / ${num(mk.best_ask, 1)}` : "—" },
      { k: "WS 流", v: sh ? (sh.state === "connected" ? `<span class="up">已连接</span>` : `<span class="warn">${esc(sh.state)}</span>`) : "无数据", x: sh ? `事件 ${sh.events ?? "—"} · ${sh.updated_ms ? ageText(sh.updated_ms) + " 前" : ""}` : "" },
      { k: "K线年龄", v: `BTC 15m ${kline["BTCUSDT|15m"] ?? "—"}s · ETH 15m ${kline["ETHUSDT|15m"] ?? "—"}s`, x: `1h 线 BTC ${kline["BTCUSDT|1h"] ?? "—"}s` },
      { k: "标记价年龄", v: `BTC ${markAge["BTCUSDT"] ?? "—"}s · ETH ${markAge["ETHUSDT"] ?? "—"}s` },
      { k: "运行器K线", v: rd && rd.bar_utc ? `${rd.bar_utc} UTC` : "—", x: rd && rd.close ? `收 ${num(rd.close, 2)}` : "" },
      { k: "错过 K 线", v: `${risk.ctx.missed_bars ?? 0} 根 / ${risk.ctx.missed_signals ?? 0} 信号`, x: "停机只记账不补单" },
      { k: "行情通道", v: esc(String(klineUrl || "—").replace("https://", "")), x: klineSrcText },
    ]);

    // ---- 风控闸门
    setText("mrkMeta", risk.ctx.halted ? "已熔断" : "未熔断", "ph-meta mono");
    const dayGate = rp.block_on_daily_loss === false ? `<span class="warn">关闭</span>` : `<span class="up">启用</span>`;
    const ddGate = rp.halt_on_max_drawdown === false ? `<span class="warn">关闭</span>` : `<span class="up">启用</span>`;
    kvRows($("mrkKv"), [
      { k: "日亏额度", v: `${pct(risk.dayLoss)} <span class="sub">/ 3%</span>`, x: risk.dayLoss != null && risk.dayLoss >= 0.02 ? "接近上限" : "未到上限", cls: risk.dayLoss != null && risk.dayLoss >= 0.03 ? "is-bad" : risk.dayLoss != null && risk.dayLoss >= 0.02 ? "is-warn" : "" },
      { k: "当前回撤", v: `${pct(risk.dd)} <span class="sub">/ 10%</span>`, x: risk.dd != null && risk.dd >= 0.05 ? "已过 5%" : "未到 5%", cls: risk.dd != null && risk.dd >= 0.1 ? "is-bad" : risk.dd != null && risk.dd >= 0.05 ? "is-warn" : "" },
      { k: "熔断状态", v: risk.ctx.halted ? `<span class="down">halted = true</span>` : "halted = false", x: risk.ctx.halted ? "等待人工解除" : "正常" },
      { k: "日亏闸门", v: dayGate, x: "block_on_daily_loss" },
      { k: "回撤熔断", v: ddGate, x: "halt_on_max_drawdown" },
      { k: "开新仓", v: g.allow_new_entries ? `<span class="up">允许</span>` : `<span class="warn">禁止</span>`, x: "guardian.allow_new_entries" },
      { k: "冷却", v: (Number((st.cooldown_until || {}).short_term || 0) || Number((st.cooldown_until || {}).long_term || 0)) ? `短期 ${num((st.cooldown_until || {}).short_term, 0)} · 长期 ${num((st.cooldown_until || {}).long_term, 0)}` : "无", x: "冷却期内不开新仓" },
      { k: "日初权益", v: num(risk.dayStart, 2), x: "运行器记录" },
      { k: "权益峰值", v: num(risk.peakEq, 2), x: `记录回撤 ${pct(s.quality && s.quality.recorded_max_drawdown)}` },
    ]);

    // ---- 执行与保护
    setText("mprMeta", g.protection_ok ? "保护正常" : "保护异常", "ph-meta mono");
    const stops = g.stop_ids || {};
    const stopTxt = Object.keys(stops).length
      ? Object.entries(stops).map(([sym, id]) => `${coin(sym)} ${id}`).join(" · ")
      : "无登记";
    const allOpen = st.open_orders || [];
    const openProt = allOpen.filter((o) => o.reduceOnly).length;
    kvRows($("mprKv"), [
      { k: "保护状态", v: g.protection_ok ? `<span class="up">正常</span>` : `<span class="down">异常</span>`, x: g.health || "" },
      { k: "监护登记单", v: esc(stopTxt), x: `${Object.keys(stops).length} 张 · 运行器登记` },
      { k: "净仓保护", v: g.net_protection_id ? String(g.net_protection_id) : "无", x: "reduceOnly 全仓保护" },
      { k: "标记价监护", v: g.last_mark ? num(g.last_mark, 2) : "—", x: g.last_mark_age_sec != null ? `${Number(g.last_mark_age_sec).toFixed(1)}s 前` : "" },
      { k: "评分失败连击", v: String(g.score_fail_streak ?? "—"), x: ">阈值 暂停保护收紧" },
      { k: "最后错误", v: g.last_error ? `<span class="down">${esc(g.last_error)}</span>` : "无", x: "" },
      { k: "交易所挂单", v: allOpen.length ? `${allOpen.length} 张` : "无", x: allOpen.length ? `保护 ${openProt} / 开仓 ${allOpen.length - openProt}` : "无挂单" },
    ]);
    const acts = [].concat(g.recent_actions || [], st.last_actions || []);
    const actsBox = $("mprActions");
    if (actsBox) {
      actsBox.textContent = acts.length
        ? acts.slice(-12).map((a) => typeof a === "string" ? a : JSON.stringify(a)).join("\n")
        : "无最近动作";
    }

    // ---- 账本对账
    const rc = reconState();
    setText("mrcMeta", rc.ok ? "净仓一致" : "存在差异", "ph-meta mono");
    const observe = (r && r.mode) === "observation_only" || (r && r.mode) === "observe";
    const reconRows = [{
      k: "联动", v: observe ? `<span class="warn">不联动（观察模式）</span>` : `<span class="up">自动同步</span>`,
      x: `runner.mode = ${esc((r && r.mode) || "—")}`,
      cls: observe ? "is-warn" : "",
    }];
    ["BTCUSDT", "ETHUSDT"].forEach((sym) => {
      const want = Number((st.desired_nets || {})[sym] || 0);
      const have = nets[sym] ? Number(nets[sym].signed || 0) : 0;
      const gap = Math.abs(want - have);
      const ok = gap < 5e-4;
      reconRows.push({
        k: `${coin(sym)} 净额`,
        v: `期望 ${num(want, 4)} · 实际 ${num(have, 4)}`,
        x: ok ? `<span class="up">一致</span>` : `<span class="down">差 ${num(gap, 4)}</span>`,
        cls: ok ? "" : "is-bad",
      });
    });
    kvRows($("mrcKv"), reconRows);

    const ext = st.external_interventions || {};
    const extEntries = Object.entries(ext).filter(([, v]) => v);
    if (!extEntries.length) {
      kvRows($("mrcExt"), [{ k: "检测", v: "未检测到人工干预", x: "" }]);
    } else {
      kvRows($("mrcExt"), extEntries.map(([sym, v]) => ({
        k: coin(sym),
        v: `${v.last_kind === "increase" ? "人工加仓" : "人工减仓/平仓"} · ${fmtTime(v.detected_ms)}`,
        x: `${num(v.ex_before, 4)} → ${num(v.ex_after, 4)}${v.paused ? " · 已暂停" : " · 已跟随"}`,
        cls: v.paused ? "is-bad" : "is-warn",
      })));
    }

    // ---- 数据与成本核对（只留「数据是否可信」相关，统计成绩类行已移除）
    const cov = s.coverage || {};
    const costs = (s.quality && s.quality.costs) || {};
    const effFills = (tradesCache || []).filter(isHistoryRow).length;
    setText("mcvMeta", s.record_start ? `自 ${s.record_start}` : "—", "ph-meta mono");
    kvRows($("mcvKv"), [
      { k: "接口覆盖", v: `${cov.fills ?? "—"} 笔 <span class="sub">原始窗口</span>`, x: cov.first_fill_ms ? `${fmtMDHM(cov.first_fill_ms)} → ${fmtMDHM(cov.last_fill_ms)}` : "" },
      { k: "统计窗口", v: `${effFills} 笔 <span class="sub">起点后有效成交</span>`, x: `自 ${esc(s.record_start || "—")}` },
      { k: "起点裁剪", v: cov.clipped_at_start ? "已按记录起点裁剪" : "未裁剪", x: cov.truncated ? "接口截断" : "未截断" },
      { k: "孤儿平仓", v: String(cov.orphan_closes ?? "—"), x: "起点前开仓的平仓腿" },
      { k: "费用 · Maker", v: num(costs.maker_fee, 4), x: "逐笔成交" },
      { k: "费用 · Taker", v: num(costs.taker_fee, 4) },
      { k: "资金费", v: num(costs.funding_fee, 4), x: costs.funding_orphan ? `未归属 ${num(costs.funding_orphan, 4)}` : "" },
      { k: "滑点估算", v: costs.slippage_est == null ? "—" : num(costs.slippage_est, 2), x: "开仓价 − 参考价" },
    ]);

    // ---- 事件日志
    const all = alertsCache || [];
    const filtered = evtFilter === "all" ? all : all.filter((a) => String(a.level).toUpperCase() === evtFilter);
    setText("evtMeta", `${filtered.length} / ${all.length} 条 · 最新 ${all[0] ? fmtHMS(Number(all[0].epoch || 0) * 1000) : "—"}`, "ph-meta mono");
    const evtBox = $("evtBox");
    if (evtBox) {
      evtBox.innerHTML = filtered.length
        ? filtered.slice(0, 60).map((a) => feedItem(a, false)).join("")
        : `<div class="empty"><b>无匹配事件</b>—</div>`;
    }
    document.querySelectorAll("#evtFilters [data-evt]").forEach((b) =>
      b.classList.toggle("on", b.dataset.evt === evtFilter));
  }

  // ------------------------------------------------------------------ 设置视图
  function renderSettings() {
    const st = statusCache || {};
    const s = summaryCache || {};
    const r = runnerInfo();
    const rp = (r && r.params) || {};
    const mk = marketCache || {};

    // 连接状态（设置页已精简该节；元素不存在时整块跳过，避免脚本中断）
    const connKv = $("setConnKv");
    if (connKv) {
      const ok = st.connected !== false;
      const nets = exchangeNets();
      const liveNets = ["BTCUSDT", "ETHUSDT"].filter((sym) => nets[sym] && Number(nets[sym].quantity || 0));
      kvRows(connKv, [
        { k: "接口连接", v: ok ? `<span class="up">已连接</span>` : `<span class="down">未配置/不可达</span>`, x: mk.market_label || "" },
        { k: "账户余额", v: num(st.balance && st.balance.total_wallet_balance, 2), x: "USDT" },
        { k: "可用余额", v: num(st.balance && st.balance.available_balance, 2), x: "USDT" },
        { k: "交易所仓位", v: liveNets.length ? liveNets.map((sym) => `${coin(sym)} ${sideZh(nets[sym].side)} ${num(nets[sym].quantity, 4)}`).join(" · ") : "空仓" },
        { k: "对账状态", v: reconState().ok ? `<span class="up">一致</span>` : `<span class="down">不一致</span>`, x: "策略账本 vs 交易所" },
        { k: "交易所设置", v: st.exchange_settings ? `${st.exchange_settings.symbol || ""} · ${st.exchange_settings.leverage || "—"}x · ${st.exchange_settings.margin_type || ""}` : "—", x: "单向持仓 / 逐仓" },
        { k: "账户通道", v: esc(String(mk.account_base_url || "—").replace("https://", "")) },
        { k: "记录起点", v: esc(s.record_start || "—") },
      ]);
    }

    // 运行参数（设置页已精简该节；元素不存在时整块跳过）
    const paramsKv = $("setParamsKv");
    if (paramsKv) {
      setText("setParamsMeta", r && r.params_fingerprint ? `指纹 ${r.params_fingerprint}` : "—", "ph-meta mono");
      const layerR = rp.layer_risk_r || {};
      const fmtR = (arr) => (arr || []).map((x) => `${parseFloat((Number(x) * 100).toFixed(3))}%`).join(" / ");
      const lrKeys = Object.keys(layerR);
      const lrSame = lrKeys.length === 2 && JSON.stringify(layerR[lrKeys[0]]) === JSON.stringify(layerR[lrKeys[1]]);
      const layerText = !lrKeys.length ? "—"
        : lrSame ? `两策略均 ${fmtR(layerR[lrKeys[0]])}`
        : lrKeys.map((k2) => `${k2} ${fmtR(layerR[k2])}`).join(" · ");
      kvRows(paramsKv, [
        { k: "首层风险 r", v: String(rp.risk_r ?? "—"), x: "权益 × r ÷ (k × ATR)" },
        { k: "分层风险", v: esc(layerText), x: "首层 → 第 2/3 层" },
        { k: "杠杆", v: rp.leverage ? `${rp.leverage}x` : "—", x: "逐仓" },
        { k: "ATR 系数 k", v: String(rp.atr_mult_k ?? "—"), x: "仓位分母" },
        { k: "灾难止损", v: rp.disaster_atr ? `${rp.disaster_atr} × ATR_1H` : "—", x: "穿盘口限价平仓" },
        { k: "单标的保证金预算", v: rp.margin_budget_per_trade != null ? pct(rp.margin_budget_per_trade, 0) : "—" },
        { k: "组合保证金预算", v: rp.portfolio_margin_budget != null ? pct(rp.portfolio_margin_budget, 0) : "—", x: "BTC+ETH 合计" },
        { k: "最大层数", v: Object.entries(rp.max_layers || {}).map(([k2, v2]) => `${k2} ${v2}`).join(" · ") || "—", x: "同向回调加仓" },
        { k: "日亏闸门", v: rp.block_on_daily_loss === false ? `<span class="warn">关闭</span>` : `<span class="up">启用</span>`, x: "block_on_daily_loss" },
        { k: "回撤熔断", v: rp.halt_on_max_drawdown === false ? `<span class="warn">关闭</span>` : `<span class="up">启用</span>`, x: "halt_on_max_drawdown" },
      ]);
    }

    // 恢复按钮状态
    const ext = st.external_interventions || {};
    const paused = Object.entries(ext).filter(([, v]) => v && v.paused);
    const btn = $("externalResumeBtn");
    if (btn) {
      if (paused.length) {
        btn.disabled = false;
        btn.dataset.symbol = paused[0][0];
        btn.textContent = `确认恢复 ${coin(paused[0][0])} 自动开仓`;
        setText("dzResumeDesc", `已暂停：${paused.map(([sym]) => coin(sym)).join(" · ")}。确认后运行器下一轮恢复净仓同步。`);
      } else {
        btn.disabled = true;
        btn.dataset.symbol = "";
        btn.textContent = "确认恢复";
        const holds = Object.entries(ext).filter(([, v]) => v && v.hold && !v.paused);
        setText("dzResumeDesc", holds.length
          ? `当前无暂停；已跟随人工操作：${holds.map(([sym]) => coin(sym)).join(" · ")}（无需确认）。`
          : "未检测到需要确认的人工干预；出现时运行器会暂停该标的自动开仓并在此处提供确认。");
      }
    }

    // 主网执行状态依赖运行器心跳，而心跳随每轮刷新变化 —— 用缓存重绘一次，
    // 否则要等用户手动切到设置页才会更新（首屏时心跳往往还没到）。
    if (mainnetCfgCache) renderMainnetConfig(mainnetCfgCache);
  }

  // ------------------------------------------------------------------ 数据加载
  async function loadStatus() { statusCache = await api("/api/trading/status"); }
  async function loadSummary() { summaryCache = await api("/api/account/summary"); }
  async function loadReading() { readingCache = await api("/api/strategy/reading"); }
  async function loadEquity() { equityCache = await api("/api/equity/curve"); }
  async function loadMonitor() { monitorCache = await api("/api/monitor"); }
  async function loadAlerts() {
    const d = await api("/api/alerts?limit=150");
    alertsCache = (d && d.alerts) || [];
  }
  async function loadMarket() { marketCache = await api("/api/market"); }
  async function loadHistoryData() {
    const [o, t] = await Promise.allSettled([
      api("/api/binance/orders?limit=200"),
      api("/api/binance/trades?limit=200"),
    ]);
    ordersCache = o.status === "fulfilled" ? (o.value.orders || []) : [];
    tradesCache = t.status === "fulfilled" ? (t.value.trades || []) : [];
  }
  async function loadSparks() {
    if (Date.now() - sparkFetchedAt < 60000) return;
    const [b, e] = await Promise.allSettled([
      api("/api/chart?symbol=BTCUSDT&interval=15m&bars=96"),
      api("/api/chart?symbol=ETHUSDT&interval=15m&bars=96"),
    ]);
    const take = (res) => {
      if (res.status !== "fulfilled") return null;
      const bars = (res.value && res.value.bars) || [];
      const closes = bars.map((x) => Number(x.c)).filter(Number.isFinite);
      return closes.length > 1 ? closes : null;
    };
    const nb = take(b), ne = take(e);
    if (nb) sparks.BTCUSDT = nb;
    if (ne) sparks.ETHUSDT = ne;
    if (nb || ne) {
      sparkFetchedAt = Date.now(); // 全部失败则下一轮重试
      renderTicker(); // 拿到新序列后立即重绘，不等下一轮刷新
    }
  }

  function renderAll() {
    renderTopbar();
    renderTicker();
    renderBanner();
    renderKpis();
    renderPositions();
    renderOpenOrders();
    renderHistory();
    renderHealth();
    renderSignals();
    renderFeed();
    renderMonitorView();
    renderSettings();
  }

  async function refreshAll() {
    const res = await Promise.allSettled([
      loadStatus(), loadSummary(), loadReading(), loadEquity(),
      loadMonitor(), loadAlerts(), loadMarket(), loadHistoryData(),
    ]);
    const failed = res.filter((x) => x.status === "rejected").length;
    apiFailStreak = failed >= 6 ? apiFailStreak + 1 : failed > 0 ? Math.max(apiFailStreak, 1) : 0;
    if (failed >= 6) {
      // 面板整体不可达：明确提示，不保留「正在更新」的假象
      toast("面板接口不可达，页面数据已停止更新", "err");
    }
    loadSparks();
    lastSyncAt = Date.now();
    renderAll();
  }

  // ------------------------------------------------------------------ 动作
  function bindNav() {
    document.querySelectorAll(".nav-tab").forEach((b) =>
      b.addEventListener("click", () => { switchView(b.dataset.view); location.hash = b.dataset.view; }));
    document.querySelectorAll("[data-nav]").forEach((b) =>
      b.addEventListener("click", () => { switchView(b.dataset.nav); location.hash = b.dataset.nav; }));
  }

  function switchView(name) {
    if (!VIEWS.includes(name)) name = "desk";
    currentView = name;
    document.querySelectorAll(".view").forEach((v) =>
      v.classList.toggle("active", v.id === "view-" + name));
    document.querySelectorAll(".nav-tab").forEach((b) =>
      b.classList.toggle("active", b.dataset.view === name));
    if (name === "chart" && window.ChartModule && window.ChartModule.setActive) {
      window.ChartModule.setActive(true);
    } else if (window.ChartModule && window.ChartModule.setActive) {
      window.ChartModule.setActive(false);
    }
    if (name === "chart" && window.ChartModule && window.ChartModule.refresh) {
      window.ChartModule.refresh();
    }
    if (name === "settings") loadMainnetConfig();
  }

  async function postAction(path, body, label, btn) {
    const old = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "执行中…"; }
    try {
      const d = await api(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      toast(d.ok ? `${label}完成：${JSON.stringify(d).slice(0, 260)}` : `${label}未成功：${d.error || JSON.stringify(d).slice(0, 260)}`, d.ok ? "ok" : "err");
      await refreshAll();
    } catch (e) {
      toast(`${label}失败：${e.message || e}`, "err");
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = old; }
    }
  }

  async function resumeSymbol(sym) {
    if (!sym) return;
    const ok = window.confirm(`确认恢复 ${coin(sym)} 的自动开仓？\n运行器下一轮会恢复净仓同步，可能按策略账本重新建仓。`);
    if (!ok) return;
    await postAction("/api/external/resume", { symbol: sym }, "人工确认恢复");
  }

  async function doReconcile() {
    await postAction("/api/trading/reconcile", { reason: "frontend_manual" }, "对账");
  }

  // -------------------------------------------------------- 主网凭据（只读）
  //
  // 约束：保存/测试都不会启用主网交易。测试按钮只调用后端只读预检接口，
  // 后端只发 GET /fapi/v1/time 与签名 GET /fapi/v2/account。
  let mnVerifyState = "—";

  async function apiJson(path, opts) {
    // 与 api() 的区别：4xx 也读取 JSON 正文里的 error 字段（api() 直接抛错会丢正文）
    const res = await fetch(API + path, Object.assign({ cache: "no-store" }, opts || {}));
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!data) throw new Error(`HTTP ${res.status}`);
    return data;
  }

  // 主网执行是否启用：**以运行器心跳为准**。
  // 面板进程自己的 TRADING_MODE 可能与运行器不同（两个 launchd 配置各自独立），
  // 所以能拿到心跳时一律用心跳；老后端没有这些字段时才回退 /api/config。
  // 返回 { enabled: bool|null, known: bool, v: html, x: text }
  function mainnetExecState(cfg) {
    const c = cfg || {};
    const hb = (monitorCache && monitorCache.heartbeat) || null;
    const hasHb = Boolean(hb && hb.updated_ms);
    const fresh = hasHb && (Date.now() - Number(hb.updated_ms)) < 90000;

    if (hasHb && hb.market) {
      if (!fresh) {
        return { known: true, enabled: false, running: false,
          v: "未运行", x: "运行器未运行，不产生下单" };
      }
      const on = hb.market === "mainnet" && !isObserveMode(hb.mode);
      return {
        known: true, enabled: on, running: true,
        v: on ? `<span class="up">已启用（双重确认）</span>`
              : `<span class="warn">已禁用（安全闸门）</span>`,
        x: on ? "TRADING_MODE=live + CONFIRM_MAINNET · 真实资金" : "缺任一确认即阻断",
      };
    }

    // 心跳缺失，或心跳里没有 market 字段（旧后端）→ 回退面板后端判定
    const runtime = c.runtime || {};
    if (c.mainnet_execution_enabled === undefined && !c.runtime) {
      return { known: false, enabled: null, running: false,
        v: "未运行", x: "运行器未运行，不产生下单" };
    }
    const on = c.mainnet_execution_enabled === true
      || (runtime.mode === "live" && runtime.live_allowed === true);
    return {
      known: true, enabled: on, running: true,
      v: on ? `<span class="up">已启用（双重确认）</span>`
            : `<span class="warn">已禁用（安全闸门）</span>`,
      x: c.mainnet_execution_note
        || (on ? "TRADING_MODE=live + CONFIRM_MAINNET · 真实资金" : "缺任一确认即阻断"),
    };
  }

  function renderMainnetConfig(cfg) {
    const c = cfg || {};
    const ok = Boolean(c.mainnet_configured);
    const exec = mainnetExecState(c);

    const conn = $("mainnetConnection");
    if (conn) {
      conn.textContent = !ok ? "未配置"
        : exec.enabled ? "已保存 · 执行已启用"
        : "已保存 · 执行未启用";
      conn.className = "mn-big " + (ok && exec.enabled ? "ok" : "blocked");
    }

    const tag = $("mainnetExecTag");
    if (tag) {
      tag.textContent = !ok ? "真实资金 · 未配置凭据"
        : exec.enabled ? "真实资金 · 执行已启用"
        : "真实资金 · 执行未启用";
      tag.className = "tag " + (ok && exec.enabled ? "up" : "warn");
    }

    const verifyRow = { k: "只读预检", v: mnVerifyState, x: "GET /fapi/v1/time + /fapi/v2/account" };
    const execRow = { k: "主网执行", v: exec.v, x: exec.x,
      cls: exec.enabled ? "" : exec.running ? "is-warn" : "" };
    kvRows($("mnKv"), [
      { k: "已保存 Key", v: esc(c.mainnet_key_masked || "—"), x: "掩码显示" },
      // 地址一律由后端下发，前端不硬编码交易所域名（防止行情腿/下单腿分叉）。
      { k: "目标地址", v: esc(c.mainnet_base_url || "—"), x: "由后端下发" },
      verifyRow,
      execRow,
    ]);
  }

  let mainnetCfgCache = null;

  async function loadMainnetConfig() {
    try {
      mainnetCfgCache = await api("/api/config");
      renderMainnetConfig(mainnetCfgCache);
    } catch (e) { /* 保持原样 */ }
  }

  function bindMainnetForm() {
    const saveBtn = $("mainnetSaveBtn");
    const verifyBtn = $("mainnetVerifyBtn");
    const forgetBtn = $("mainnetForgetBtn");
    if (!saveBtn && !verifyBtn && !forgetBtn) return;
    const note = () => $("mainnetResult");
    const setNote = (t) => { const n = note(); if (n) n.textContent = t; };

    if (saveBtn)
      saveBtn.addEventListener("click", async () => {
        const key = ($("mainnetKeyInput").value || "").trim();
        const secret = ($("mainnetSecretInput").value || "").trim();
        if (!key || !secret) {
          setNote("请先填写主网 API Key 与 Secret。");
          return;
        }
        saveBtn.disabled = true;
        setNote("正在保存到本机…");
        try {
          const d = await apiJson("/api/mainnet/keys", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ api_key: key, api_secret: secret }),
          });
          if (d.ok) {
            setNote(`已保存（${d.mainnet_key_masked || "已掩码"}）。密钥已就绪，实盘运行器启动后生效。`);
            $("mainnetKeyInput").value = "";
            $("mainnetSecretInput").value = "";
          } else {
            setNote("保存失败：" + (d.error || "未知错误"));
          }
          await loadMainnetConfig();
        } catch (e) {
          setNote("保存失败：" + (e.message || e));
        } finally {
          saveBtn.disabled = false;
        }
      });

    if (verifyBtn)
      verifyBtn.addEventListener("click", async () => {
        verifyBtn.disabled = true;
        setNote("正在执行只读预检（GET /fapi/v1/time + /fapi/v2/account）…");
        mnVerifyState = "检测中…";
        try {
          const typedKey = ($("mainnetKeyInput").value || "").trim();
          const typedSecret = ($("mainnetSecretInput").value || "").trim();
          const d = await apiJson("/api/mainnet/verify", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ api_key: typedKey, api_secret: typedSecret }),
          });
          if (d.ok) {
            mnVerifyState = `<span class="up">只读通过</span>`;
            setNote(
              `只读预检通过 · 延迟 ${d.latency_ms}ms · 可用余额 ${num(d.available_balance_usdt, 2)} USDT · ` +
              `canTrade=${d.can_trade} · canWithdraw=${d.can_withdraw}。` +
              `注意：这不代表可以安全启动真实交易。`
            );
          } else {
            mnVerifyState = `<span class="down">失败</span>`;
            setNote("只读预检失败：" + (d.error || "未知错误"));
          }
        } catch (e) {
          mnVerifyState = `<span class="down">失败</span>`;
          setNote("只读预检失败：" + (e.message || e));
        } finally {
          verifyBtn.disabled = false;
          await loadMainnetConfig();
        }
      });

    if (forgetBtn)
      forgetBtn.addEventListener("click", async () => {
        if (!window.confirm("确认删除本机保存的主网凭据？\n仅删除本机 runtime/secrets.json 里的主网 Key；正在运行的运行器不受影响，但重启后将因缺少凭据拒绝启动。")) return;
        forgetBtn.disabled = true;
        try {
          const d = await apiJson("/api/mainnet/forget", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
          });
          setNote(d.ok ? "已删除本机保存的主网凭据。" : "删除失败：" + (d.error || "未知错误"));
          mnVerifyState = "—";
          await loadMainnetConfig();
        } catch (e) {
          setNote("删除失败：" + (e.message || e));
        } finally {
          forgetBtn.disabled = false;
        }
      });
  }

  function bindTestnetForm() {
    // 保存与通信测试分离：错误凭据不会覆盖已保存配置。
    const form = $("accountForm");
    const testnetTest = $("testnetTestBtn");
    const accountNote = () => $("accountResult");
    const accountPayload = (includeEmpty = false) => {
      const key = $("apiKeyInput").value.trim();
      const secret = $("apiSecretInput").value.trim();
      const payload = { base_url: $("baseUrlInput").value.trim() };
      if (includeEmpty || key) payload.api_key = key;
      if (includeEmpty || secret) payload.api_secret = secret;
      return payload;
    };
    if (form) form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const note = accountNote();
      const payload = accountPayload(true);
      if (!payload.api_key || !payload.api_secret || !payload.base_url) {
        note.textContent = "请填写 API Key、API Secret 和 Base URL。";
        note.className = "form-note err";
        return;
      }
      const saveBtn = form.querySelector('button[type="submit"]');
      if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = "保存中…"; }
      note.textContent = "正在保存到本机…";
      note.className = "form-note";
      try {
        const d = await apiJson("/api/trading/keys/save", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!d.ok) throw new Error(d.error || "保存失败");
        note.textContent = `已保存 ${d.key_masked || "密钥"}；尚未测试通信，可点击“测试通信”。`;
        note.className = "form-note ok";
        $("apiKeyInput").value = "";
        $("apiSecretInput").value = "";
        refreshAll();
      } catch (e) {
        note.textContent = "保存失败：" + (e.message || e);
        note.className = "form-note err";
      } finally {
        if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = "保存到本机"; }
      }
    });
    if (testnetTest) testnetTest.addEventListener("click", async () => {
      const note = accountNote();
      const payload = accountPayload(false);
      testnetTest.disabled = true;
      note.textContent = "正在测试币安测试网通信（只读）…";
      note.className = "form-note";
      try {
        const d = await apiJson("/api/trading/keys/test", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!d.ok) throw new Error(d.error || "通信测试失败");
        note.textContent = `通信正常 · 延迟 ${d.latency_ms ?? "—"}ms · 可用余额 ${num(d.available_balance, 2)} USDT；未下单。`;
        note.className = "form-note ok";
      } catch (e) {
        note.textContent = "通信测试失败：" + (e.message || e);
        note.className = "form-note err";
      } finally {
        testnetTest.disabled = false;
      }
    });
  }

  function bindActions() {
    const r = $("refreshBtn");
    if (r) r.addEventListener("click", () => refreshAll());
    document.addEventListener("keydown", (e) => {
      if (e.key.toLowerCase() === "r" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) {
        refreshAll();
      }
    });

    // 历史记录页签与条数
    document.querySelectorAll("#histTabs [data-hist]").forEach((b) =>
      b.addEventListener("click", () => {
        histMode = b.dataset.hist;
        document.querySelectorAll("#histTabs [data-hist]").forEach((x) =>
          x.classList.toggle("active", x === b));
        renderHistory();
      }));
    const syncLimits = () => document.querySelectorAll("#histLimits [data-limit]").forEach((x) =>
      x.classList.toggle("active", Number(x.dataset.limit) === historyLimit));
    document.querySelectorAll("#histLimits [data-limit]").forEach((b) =>
      b.addEventListener("click", () => {
        const n = Number(b.dataset.limit);
        if (HISTORY_LIMITS.includes(n)) {
          historyLimit = n;
          try { localStorage.setItem(historyLimitKey, String(n)); } catch (e2) { /* ignore */ }
        }
        syncLimits();
        renderHistory();
      }));
    syncLimits();

    // 事件过滤器
    document.querySelectorAll("#evtFilters [data-evt]").forEach((b) =>
      b.addEventListener("click", () => { evtFilter = b.dataset.evt; renderMonitorView(); }));

    const smoke = $("testnetSmokeBtn");
    if (smoke) smoke.addEventListener("click", () => {
      const answer = window.prompt("非策略功能测试：将在币安测试网实际下单、立即平仓并留下多笔委托/成交，产生手续费。若仍要测试，请输入「测试单」：");
      if (answer !== "测试单") return;
      postAction("/api/testnet/smoke-limit-close", { quantity: 0.001 }, "非策略功能测试", smoke);
    });
    const flat = $("testnetFlattenBtn");
    if (flat) flat.addEventListener("click", () => {
      const ok = window.confirm("确认按交易所净额平掉当前 Testnet 仓位？\n属于运行器策略账本的仓位会被后端拒绝。");
      if (!ok) return;
      postAction("/api/testnet/flatten-now", {}, "平仓", flat);
    });
    const rec = $("reconcileBtn");
    if (rec) rec.addEventListener("click", () => doReconcile());
    const extResume = $("externalResumeBtn");
    if (extResume) extResume.addEventListener("click", () => resumeSymbol(extResume.dataset.symbol));

    // 时钟与相对时间
    clockTimer = setInterval(() => { renderTopbar(); }, 1000);
  }

  // ------------------------------------------------------------------ 行情推送（WS）
  let markSocket = null;
  let marketWsUrl = "";
  async function resolveMarketWs() {
    try {
      const d = marketCache || (await api("/api/market"));
      if (d && d.ok && d.market_ws) {
        marketWsUrl = d.market_ws + "?streams=btcusdt@markPrice@1s/ethusdt@markPrice@1s";
      }
    } catch (_) { /* ignore */ }
    return marketWsUrl;
  }
  function connectMarkPrice() {
    const open = async () => {
      try {
        if (!marketWsUrl) await resolveMarketWs();
        if (!marketWsUrl) { setTimeout(open, 3000); return; }
        markSocket = new WebSocket(marketWsUrl);
        markSocket.onmessage = (e) => {
          wsAliveAt = Date.now();
          try {
            const packet = JSON.parse(e.data);
            const d = packet.data || packet;
            const sym = String(d.s || "").toUpperCase();
            const px = Number(d.p);
            if (!px) return;
            if (sym.startsWith("BTC")) {
              const el = $("tickBTCpx");
              if (el) { el.textContent = num(px, 2); flash(el, px); }
            } else if (sym.startsWith("ETH")) {
              const el = $("tickETHpx");
              if (el) { el.textContent = num(px, 2); flash(el, px); }
            }
          } catch (_) { /* ignore */ }
        };
        markSocket.onerror = () => { if (markSocket) markSocket.close(); };
        markSocket.onclose = () => {
          markSocket = null;
          setTimeout(open, 3000);
        };
      } catch (_) {
        setTimeout(open, 3000);
      }
    };
    open();
  }

  // ------------------------------------------------------------------ 启动
  function init() {
    bindNav();
    bindActions();
    bindTestnetForm();
    bindMainnetForm();
    loadMainnetConfig();

    if (location.hash) {
      const v = location.hash.slice(1);
      if (VIEWS.includes(v)) currentView = v;
    }
    switchView(currentView);

    connectMarkPrice();
    refreshAll();
    timer = setInterval(() => {
      if (document.hidden) return;
      refreshAll();
    }, REFRESH_MS);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshAll();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
