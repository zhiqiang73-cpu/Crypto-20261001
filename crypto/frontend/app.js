/* BTC Quant Console — 两页前端
 * 第 1 页 交易总览: 余额 / 仓位 / 当前委托 / 策略 / 赚了多少 / 历史委托 / 历史成交
 * 第 2 页 账户连接: API Key 管理与连接状态
 *
 * 原则: 金额、仓位、委托、成交一律取自币安接口, 不由本地账本推算。
 */
(() => {
  "use strict";

  const API = "http://127.0.0.1:8787";
  const REFRESH_MS = 30000;
  const $ = (id) => document.getElementById(id);

  const VIEW_TITLES = { trading: "交易总览", account: "账户连接" };
  let currentView = "trading";
  let timer = null;
  let markSocket = null;
  // 面板历史只展示北京时间 2026-10-02 的策略自动单。
  const HISTORY_DAY = "2026-10-02";
  const HISTORY_LIMITS = [10, 20];
  const historyLimitKey = "crypto.histLimit";
  let historyLimit = 10;
  try {
    const saved = Number(localStorage.getItem(historyLimitKey) || 10);
    if (HISTORY_LIMITS.includes(saved)) historyLimit = saved;
  } catch (e) {
    historyLimit = 10;
  }

  const coin = (symbol) => {
    const text = String(symbol || "").toUpperCase();
    if (text.startsWith("ETH")) return "ETH";
    if (text.startsWith("BTC")) return "BTC";
    return text.replace("USDT", "") || "—";
  };

  const coinCell = (symbol) =>
    `<span class="sym-chip">${esc(coin(symbol))}</span>`;

  const SOURCE_ZH = {
    strategy: "策略自动",
    manual_web: "网页手动",
    function_test: "功能测试",
    user_action: "面板手动",
    unknown: "未判定",
  };

  const sourceCell = (row) => {
    const key = String(row.source || "unknown");
    return `<span class="src src-${esc(key)}">${esc(
      SOURCE_ZH[key] || row.source_label || key
    )}</span>`;
  };

  const beijingDay = (ms) => {
    const text = fmtTime(ms);
    return text === "—" ? "" : text.slice(0, 10);
  };

  const isHistoryRow = (row) =>
    String(row.source || "") === "strategy" &&
    beijingDay(row.time ?? row.updateTime) === HISTORY_DAY;

  const isFilledOrder = (row) =>
    isHistoryRow(row) && Number(row.executedQty || 0) > 0;

  const newestFirst = (rows, getMs) =>
    rows.slice().sort((a, b) => Number(getMs(b) || 0) - Number(getMs(a) || 0));

  const takeHistory = (rows) => rows.slice(0, historyLimit);

  const collapseTradesByOrder = (trades) => {
    const by = {};
    (trades || []).filter(isHistoryRow).forEach((t) => {
      const id = String(t.orderId || "");
      if (!id) return;
      if (!by[id]) {
        by[id] = {
          orderId: id,
          time: t.time,
          source: t.source,
          source_label: t.source_label,
          symbol: t.symbol || "",
          side: t.side,
          maker: true,
          qty: 0,
          notional: 0,
          realized: 0,
          commission: 0,
          fills: 0,
        };
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
    return Object.values(by);
  };

  const syncHistoryLimitButtons = () => {
    document.querySelectorAll(".hist-limit-btn").forEach((btn) => {
      btn.classList.toggle("is-on", Number(btn.dataset.limit) === historyLimit);
    });
  };

  const setHistoryLimit = (n) => {
    const next = HISTORY_LIMITS.includes(Number(n)) ? Number(n) : 10;
    historyLimit = next;
    try {
      localStorage.setItem(historyLimitKey, String(next));
    } catch (e) {
      /* ignore */
    }
    syncHistoryLimitButtons();
  };

  const orderPnlMap = (trades) => {
    const map = {};
    (trades || []).forEach((t) => {
      const id = String(t.orderId || "");
      if (!id) return;
      if (!map[id]) map[id] = { realized: 0, commission: 0 };
      map[id].realized += Number(t.realizedPnl || 0);
      map[id].commission += Number(t.commission || 0);
    });
    return map;
  };

  const netPnl = (realized, commission) =>
    Number(realized || 0) - Number(commission || 0);

  const runnerStatusText = (runner) => {
    if (!runner) return "无运行器心跳 · 当前未确认运行";
    const age = Date.now() - Number(runner.updated_ms || 0);
    const mode = runner.mode === "testnet_orders" ? "测试网自动下单"
      : runner.mode === "observation_only" ? "仅观察" : runner.mode || "未知模式";
    if (age > 60 * 1000) return `心跳过期 ${Math.floor(age / 1000)} 秒 · ${mode}`;
    if (runner.status === "running") return `运行中 · ${mode}`;
    if (runner.status === "starting") return `正在启动 · ${mode}`;
    if (runner.status === "blocked") return `启动被拦截 · ${runner.detail || mode}`;
    if (runner.status === "error") return `运行异常 · ${runner.detail || mode}`;
    return `已停止 · ${runner.detail || mode}`;
  };

  // ------------------------------------------------------------------ utils
  const num = (v, d = 2) =>
    v === null || v === undefined || Number.isNaN(Number(v))
      ? "—"
      : Number(v).toLocaleString("en-US", {
          minimumFractionDigits: d,
          maximumFractionDigits: d,
        });

  const signed = (v, d = 2) => {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    const n = Number(v);
    return (n >= 0 ? "+" : "") + num(n, d);
  };

  const pct = (v, d = 1) =>
    v === null || v === undefined || Number.isNaN(Number(v))
      ? "—"
      : (Number(v) * 100).toFixed(d) + "%";

  const pnlClass = (v) => {
    const n = Number(v);
    if (!Number.isFinite(n) || n === 0) return "";
    return n > 0 ? "positive" : "negative";
  };

  const setPnl = (el, v, d = 2) => {
    if (!el) return;
    el.textContent = signed(v, d);
    el.classList.remove("positive", "negative");
    const c = pnlClass(v);
    if (c) el.classList.add(c);
  };

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

  const esc = (s) =>
    String(s === null || s === undefined ? "" : s).replace(
      /[&<>"']/g,
      (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );

  const empty = (msg, sub = "") =>
    `<div class="empty-state"><span>○</span><div><b>${esc(msg)}</b>${
      sub ? `<small>${esc(sub)}</small>` : ""
    }</div></div>`;

  async function api(path, opts) {
    const res = await fetch(API + path, opts);
    const text = await res.text();
    let data;
    try {
      data = text ? JSON.parse(text) : {};
    } catch (e) {
      throw new Error(`返回非 JSON (HTTP ${res.status}): ${text.slice(0, 120)}`);
    }
    return data;
  }

  // ------------------------------------------------------------------- nav
  function switchView(name) {
    currentView = name;
    document.querySelectorAll(".nav-item").forEach((b) =>
      b.classList.toggle("active", b.dataset.view === name)
    );
    document.querySelectorAll(".view").forEach((s) =>
      s.classList.toggle("active", s.id === "view-" + name)
    );
    if ($("pageTitle")) $("pageTitle").textContent = VIEW_TITLES[name] || name;
    if (name === "trading") refreshTrading();
    else refreshAccount();
  }

  // ------------------------------------------------------------- 实时行情
  //
  // WS 地址**由后端下发**, 不再硬编码。
  // 2026-10-02 事故: 前端曾写死主网 WS 地址, 与测试网下单错位。
  // 现在拿不到后端下发的地址就只重试, 绝不退回主网 ——
  // 宁可暂时无行情, 也不显示另一个市场的价格。
  let marketWsUrl = null;
  let marketLabel = "";

  function renderPrice(price) {
    const n = Number(price);
    if (!Number.isFinite(n)) return;
    if ($("markPrice"))
      $("markPrice").textContent =
        "$" + n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const tag = marketLabel ? `Binance ${marketLabel}` : "Binance";
    if ($("priceChange")) $("priceChange").innerHTML = `实时推送中 <span>${tag} WebSocket</span>`;
    if ($("syncText")) $("syncText").textContent = `${tag} 实时同步`;
    if ($("engineState")) $("engineState").textContent = "行情已连接";
  }

  async function resolveMarketWs() {
    try {
      const d = await api("/api/market");
      if (d && d.ok && d.market_ws) {
        marketWsUrl = d.market_ws + "?streams=btcusdt@markPrice@1s";
        marketLabel = d.market_label || d.market || "";
      }
    } catch (_) {}
    return marketWsUrl;
  }

  function connectMarkPrice() {
    const open = async () => {
      try {
        if (!marketWsUrl) await resolveMarketWs();
        if (!marketWsUrl) {
          if ($("syncText")) $("syncText").textContent = "等待后端下发行情地址";
          setTimeout(open, 3000);
          return;
        }
        markSocket = new WebSocket(marketWsUrl);
        markSocket.onmessage = (e) => {
          try {
            const packet = JSON.parse(e.data);
            const d = packet.data || packet;
            renderPrice(d.p);
          } catch (_) {}
        };
        markSocket.onerror = () => { if (markSocket) markSocket.close(); };
        markSocket.onclose = () => {
          markSocket = null;
          if ($("syncText")) $("syncText").textContent = "行情重连中";
          setTimeout(open, 3000);
        };
      } catch (_) {
        setTimeout(open, 3000);
      }
    };
    open();
  }

  // -------------------------------------------------------- 第 1 页 数据加载
  async function loadMarket() {
    try {
      const d = await api("/api/market");
      const price = d.mark_price ?? d.price ?? d.last_price;
      if (price) renderPrice(price);
    } catch (e) {
      if ($("markPrice")) $("markPrice").textContent = "行情不可用";
    }
  }

  async function loadStatus() {
    try {
      const s = await api("/api/trading/status");
      const nets = Object.assign(
        {},
        s.exchange_position ? { BTCUSDT: s.exchange_position } : {},
        s.exchange_positions || {}
      );
      const sources = s.position_sources || [];
      const symbols = ["BTCUSDT", "ETHUSDT"];
      const liveNets = symbols.filter((sym) => {
        const pos = nets[sym] || {};
        const qty = Number(pos.quantity || pos.position_amt || 0);
        const side = String(pos.side || "FLAT").toUpperCase();
        return side !== "FLAT" && qty;
      });

      if ($("posSideTag")) {
        $("posSideTag").textContent = liveNets.length
          ? liveNets.map((sym) => `${coin(sym)} ${String(nets[sym].side).toUpperCase()}`).join(" · ")
          : "FLAT";
        $("posSideTag").className = "tag " + (liveNets.length ? "safe" : "");
      }
      if ($("posHint")) {
        $("posHint").textContent = liveNets.length
          ? liveNets.map((sym) => {
              const pos = nets[sym];
              return `${coin(sym)} ${String(pos.side).toUpperCase()} ${Number(pos.quantity || 0)}`;
            }).join(" · ")
          : "无持仓";
      }

      if ($("posBox")) {
        const lotPnl = (mark, lotSide, lotQty, lotPx) => {
          if (!mark || !lotPx || !lotQty) return null;
          const sign = lotSide === "LONG" ? 1 : -1;
          return (mark - lotPx) * lotQty * sign;
        };
        let html = "";
        symbols.forEach((sym) => {
          const lots = sources.filter((src) => (src.symbol || "BTCUSDT") === sym);
          const pos = nets[sym] || {};
          const side = String(pos.side || "FLAT").toUpperCase();
          const qty = Number(pos.quantity || pos.position_amt || 0);
          if (!lots.length && (side === "FLAT" || !qty)) return;
          const mark = Number(pos.mark_price ?? pos.markPrice ?? 0);
          const upnl = Number(pos.unrealized_pnl ?? pos.unrealizedProfit ?? 0);
          const entry = Number(pos.entry_price ?? pos.entryPrice ?? 0);
          html += lots.map((src) => {
            const zh = src.side === "LONG" ? "多" : "空";
            const est = lotPnl(mark, src.side, Number(src.qty), Number(src.px));
            return `<div class="table-row cols-pos">
              <span>${coinCell(src.symbol || sym)}</span>
              <span>${esc(src.name || src.interval)}</span>
              <span class="${src.side === "LONG" ? "positive" : "negative"}">${zh}</span>
              <span>${num(src.qty, 4)}</span>
              <span>${num(src.px, 2)}</span>
              <span>${fmtTime(src.bar_ms)}</span>
              <span>${esc(src.reason || "—")}</span>
              <span class="${est == null ? "" : pnlClass(est)}">${est == null ? "—" : `${signed(est)} <small>估算</small>`}</span>
            </div>`;
          }).join("");
          const netSide = side === "FLAT" || !qty ? "空仓" : (side === "LONG" ? "多" : "空");
          html += `<div class="table-row cols-pos is-net">
            <span>${coinCell(sym)}</span>
            <span>币安净仓</span>
            <span class="${!qty ? "" : side === "LONG" ? "positive" : "negative"}">${esc(netSide)}</span>
            <span>${num(qty, 4)}</span>
            <span>${entry ? num(entry, 2) : "—"}</span>
            <span>—</span>
            <span>${lots.length > 1 ? "同标的两条策略合成" : (lots[0] ? `${lots[0].name} 对应净仓` : "交易所账户")}</span>
            <span class="${pnlClass(upnl)}">${signed(upnl)} <small>交易所</small></span>
          </div>`;
        });
        $("posBox").innerHTML = html || empty("空仓", "当前无持仓");
      }

      const recon = Boolean(s.reconciliation_needed);
      if ($("reconcileState")) {
        $("reconcileState").textContent = recon ? "需对账（已阻止开仓）" : "一致";
        $("reconcileState").className = recon ? "blocked" : "positive";
      }
      if ($("accountPosition"))
        $("accountPosition").textContent = liveNets.length
          ? liveNets.map((sym) => `${coin(sym)} ${String(nets[sym].side).toUpperCase()} ${num(nets[sym].quantity, 4)}`).join(" · ")
          : "空仓";
      if ($("accountBalance"))
        $("accountBalance").textContent = num(
          s.balance?.total_wallet_balance ?? s.balance?.available_balance,
          2
        );
      if ($("accountConnection")) {
        const ok = s.connected !== false;
        $("accountConnection").textContent = ok ? "已连接" : "未配置";
        $("accountConnection").className =
          "connection-big " + (ok ? "positive" : "blocked");
      }
      if ($("accountMode"))
        $("accountMode").textContent = "测试网 API · 策略进程状态另查";
      if ($("modeLabel"))
        $("modeLabel").textContent = s.connected ? "TESTNET · API CONNECTED" : "TESTNET · DISCONNECTED";
      if ($("engineState")) $("engineState").textContent = "后端已连接";
    } catch (e) {
      if ($("engineState")) $("engineState").textContent = "后端不可达";
    }
  }

  async function loadSummary() {
    try {
      const s = await api("/api/account/summary");
      if (!s.connected) {
        if ($("mWallet")) $("mWallet").textContent = "未连接";
        if ($("mUnreal")) $("mUnreal").textContent = "—";
        if ($("mReal")) $("mReal").textContent = "—";
        ["pCount", "pWinLoss", "pWinRate", "pPF", "pFee", "atCount", "atReal", "atFee", "atNet"].forEach(id => { if ($(id)) $(id).textContent = "—"; });
        if ($("realHint")) $("realHint").textContent = s.reason || "密钥未配置";
        return;
      }
      if ($("mWallet")) $("mWallet").textContent = num(s.wallet_balance, 2);
      setPnl($("mUnreal"), s.unrealized_pnl);
      setPnl($("mReal"), s.realized_pnl);
      if ($("realHint")) $("realHint").textContent = `净盈亏 ${signed(s.net_pnl)} USDT`;

      if ($("statsStart")) $("statsStart").textContent = s.stats_start || "—";
      if ($("statsStart2")) $("statsStart2").textContent = s.stats_start || "—";
      if ($("pCount")) $("pCount").textContent = String(s.closed_trades ?? "—");
      if ($("pCountHint")) $("pCountHint").textContent = `非零盈亏 · 共 ${s.trade_count ?? 0} 笔逐笔成交`;
      if ($("pWinLoss")) $("pWinLoss").textContent = `${s.wins ?? 0} / ${s.losses ?? 0}`;
      if ($("pWinRate")) $("pWinRate").textContent = pct(s.win_rate);
      if ($("pPF")) {
        $("pPF").textContent = (s.profit_factor === null || s.profit_factor === undefined)
          ? "—"
          : Number(s.profit_factor).toFixed(2);
      }
      if ($("pFee")) $("pFee").textContent = num(s.commission, 4);

      // 与下方最近七天、最多200笔逐笔成交表对齐；不冒充账户全历史。
      const at = s.available_history || {};
      if ($("atCount")) $("atCount").textContent = String(at.trade_count ?? "—");
      if ($("atReal")) $("atReal").textContent = signed(at.realized_pnl);
      if ($("atFee")) $("atFee").textContent = num(at.commission, 4);
      if ($("atNet")) $("atNet").textContent = signed(at.net_pnl);
      if ($("scopeWarning")) $("scopeWarning").textContent = s.truncated ? " · 已达 200 笔上限，合计不完整" : " · 仅查询窗口内";
    } catch (e) {
      if ($("mWallet")) $("mWallet").textContent = "读取失败";
      if ($("mReal")) $("mReal").textContent = "—";
    }
  }

  async function loadOpenOrders() {
    if (!$("openOrdersBox")) return;
    try {
      const s = await api("/api/trading/status");
      const orders = s.open_orders || s.orders || [];
      if ($("openOrdersCount")) $("openOrdersCount").textContent = String(orders.length);
      if (!orders.length) {
        $("openOrdersBox").innerHTML = empty("无挂单", "追价完成后不留挂单");
        return;
      }
      $("openOrdersBox").innerHTML = orders
        .map(
          (o) => `<div class="kv-row"><span>${esc(coin(o.symbol))} ${esc(o.side)} ${esc(o.type || "")}</span>
            <b>${num(o.price, 2)} × ${num(o.origQty ?? o.quantity, 4)}</b></div>`
        )
        .join("");
    } catch (e) {
      /* 保持原样 */
    }
  }

  function inferCross(d) {
    if (d.gold === true || d.dead === true) {
      return { gold: !!d.gold, dead: !!d.dead };
    }
    const ks = (d.series && d.series.k) || [];
    const ds = (d.series && d.series.d) || [];
    if (ks.length >= 2 && ds.length >= 2) {
      const k0 = Number(ks[ks.length - 2]);
      const d0 = Number(ds[ds.length - 2]);
      const k1 = Number(ks[ks.length - 1]);
      const d1 = Number(ds[ds.length - 1]);
      if ([k0, d0, k1, d1].every(Number.isFinite)) {
        return { gold: k0 <= d0 && k1 > d1, dead: k0 >= d0 && k1 < d1 };
      }
    }
    return { gold: false, dead: false };
  }

  function signalView(d) {
    const cross = inferCross(d);
    if (d.signal_long) {
      return { text: "开多", cls: "is-long", sub: d.signal_needs_k_extreme ? "金叉且 K<30" : "金叉" };
    }
    if (d.signal_short) {
      return { text: "开空", cls: "is-short", sub: d.signal_needs_k_extreme ? "死叉且 K>70" : "死叉" };
    }
    if (cross.gold) return { text: "金叉未开", cls: "is-wait", sub: "K 还没到 30" };
    if (cross.dead) return { text: "死叉未开", cls: "is-wait", sub: "K 还没到 70" };
    const above = Number(d.K) > Number(d.D);
    return { text: "无新交叉", cls: "is-wait", sub: above ? "K 在 D 上方" : "K 在 D 下方" };
  }

  function kdjChart(d) {
    const ks = ((d.series && d.series.k) || [d.K]).map(Number).filter(Number.isFinite);
    const ds = ((d.series && d.series.d) || [d.D]).map(Number).filter(Number.isFinite);
    const w = 560;
    const h = 150;
    const pad = { l: 28, r: 12, t: 14, b: 18 };
    const innerW = w - pad.l - pad.r;
    const innerH = h - pad.t - pad.b;
    const n = Math.max(ks.length, ds.length, 2);
    const xAt = (i) => pad.l + (i / (n - 1)) * innerW;
    const yAt = (v) => pad.t + (1 - Math.min(100, Math.max(0, v)) / 100) * innerH;
    const poly = (arr) =>
      arr.map((v, i) => `${xAt(i).toFixed(1)},${yAt(v).toFixed(1)}`).join(" ");
    const zones = d.signal_needs_k_extreme
      ? `<line x1="${pad.l}" x2="${w - pad.r}" y1="${yAt(70)}" y2="${yAt(70)}" stroke="#594324" stroke-dasharray="4 5"/>
         <line x1="${pad.l}" x2="${w - pad.r}" y1="${yAt(30)}" y2="${yAt(30)}" stroke="#594324" stroke-dasharray="4 5"/>
         <text x="${pad.l - 4}" y="${yAt(70) + 4}" text-anchor="end" fill="#d4a574" font-size="11">70</text>
         <text x="${pad.l - 4}" y="${yAt(30) + 4}" text-anchor="end" fill="#d4a574" font-size="11">30</text>`
      : `<text x="${pad.l - 4}" y="${yAt(100) + 4}" text-anchor="end" fill="#a39a8e" font-size="11">100</text>
         <text x="${pad.l - 4}" y="${yAt(0) + 4}" text-anchor="end" fill="#a39a8e" font-size="11">0</text>`;
    const lastK = ks.length ? ks[ks.length - 1] : null;
    const lastD = ds.length ? ds[ds.length - 1] : null;
    const dots = `${lastD == null ? "" : `<circle cx="${xAt(ds.length - 1)}" cy="${yAt(lastD)}" r="3.2" fill="#c8c0b4"/>`}
      ${lastK == null ? "" : `<circle cx="${xAt(ks.length - 1)}" cy="${yAt(lastK)}" r="3.6" fill="#d97757"/>`}`;
    return `<svg class="kdj-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="K与D">
      ${zones}
      <polyline fill="none" stroke="#8a8478" stroke-width="2" points="${poly(ds)}"/>
      <polyline fill="none" stroke="#d97757" stroke-width="2.3" points="${poly(ks)}"/>
      ${dots}
    </svg>`;
  }

  function readingBlock(d) {
    const sig = signalView(d);
    const f = (v, n) => (v === null || v === undefined || Number.isNaN(Number(v)) ? "—" : Number(v).toFixed(n));
    const pos = d.position || "空仓";
    return `<article class="reading-card">
      <header class="reading-card-head">
        <div>
          <p>${esc(coin(d.symbol))} · ${esc(d.interval || "K 线")} · ${esc(fmtTime(d.bar_ms))}</p>
          <strong class="reading-signal ${sig.cls}">${esc(sig.text)}</strong>
          <small>${esc(sig.sub)}</small>
        </div>
        <span class="reading-pos">${esc(pos === "多" || pos === "空" ? `虚拟${pos}` : pos)}</span>
      </header>
      ${kdjChart(d)}
      <footer class="reading-card-foot">
        <span><i class="swatch k"></i>K ${f(d.K, 1)}</span>
        <span><i class="swatch d"></i>D ${f(d.D, 1)}</span>
        <span>收 ${f(d.close, 1)}</span>
      </footer>
    </article>`;
  }

  // 策略此刻的读数 —— 核对信号请以此为准, 不要拿交易所图表比对。
  async function loadReading() {
    const box = $("readingBox");
    if (!box) return;
    try {
      const d = await api("/api/strategy/reading");
      if (!d.available) {
        box.innerHTML = empty("运行器尚未写入读数", d.reason || "");
        if ($("readingMarketTag")) $("readingMarketTag").textContent = "—";
        if ($("runnerState")) $("runnerState").textContent = runnerStatusText(d.runner);
        return;
      }
      const list = (d.readings && d.readings.length) ? d.readings : [d];
      const mk =
        d.market === "testnet" ? "测试网" : d.market === "mainnet" ? "主网" : d.market;
      if ($("readingMarketTag")) {
        $("readingMarketTag").textContent = `${mk} · ${list.map((x) => `${coin(x.symbol)} ${x.interval || ""}`.trim()).filter(Boolean).join(" + ")}`;
      }
      if ($("runnerState")) $("runnerState").textContent = runnerStatusText(d.runner);
      box.className = "reading-board";
      box.innerHTML = list.map(readingBlock).join("");
    } catch (e) {
      box.innerHTML = empty("读数读取失败", String(e.message || e));
    }
  }

  async function loadStrategies() {
    const sidebarBox = $("sidebarStrategyList");
    const sidebarCount = $("sidebarStrategyCount");
    if (!sidebarBox && !sidebarCount) return;
    try {
      const d = await api("/api/strategies/active");
      const list = d.strategies || [];
      if (!list.length) {
        if (sidebarBox) sidebarBox.innerHTML = `<div class="sidebar-strategy-loading">暂无策略</div>`;
        if (sidebarCount) sidebarCount.textContent = "0";
        return;
      }
      if (sidebarCount) sidebarCount.textContent = String(list.length);
      if (sidebarBox) {
        sidebarBox.innerHTML = list.map((s) => {
          const on = !!s.enabled;
          const e = s.entry || {};
          const rt = s.runtime || {};
          const state = on ? (rt.halted ? "已熔断" : "已启用") : "未启用";
          const signal = on
            ? `${e.long || "金叉做多"} / ${e.short || "死叉做空"}`
            : (s.status || "等待启用");
          return `<article class="sidebar-strategy-item${on ? " is-enabled" : ""}">
            <div class="sidebar-strategy-top">
              <span class="sidebar-strategy-dot ${on && !rt.halted ? "online" : "offline"}"></span>
              <b>${esc(s.name || s.strategy_id)}</b>
              <span class="sidebar-strategy-state ${on && !rt.halted ? "on" : "off"}">${esc(state)}</span>
            </div>
            <p>${esc(s.symbol || "BTCUSDT")} · ${esc(s.timeframe || "—")} · ${esc(s.kind || "策略")}</p>
            <small>${esc(signal)}</small>
          </article>`;
        }).join("");
      }
    } catch (e) {
      if (sidebarBox) sidebarBox.innerHTML = `<div class="sidebar-strategy-loading">策略读取失败</div>`;
      if (sidebarCount) sidebarCount.textContent = "!";
    }
  }

  const ORDER_STATUS_ZH = {
    NEW: "挂单中",
    PARTIALLY_FILLED: "部分成交",
    FILLED: "已成交",
    CANCELED: "已撤销",
    EXPIRED: "已过期",
    REJECTED: "已拒绝",
  };

  async function loadHistory() {
    const orderBox = $("histOrdersBox");
    const tradeBox = $("histTradesBox");
    if (!orderBox && !tradeBox) return;
    syncHistoryLimitButtons();
    try {
      const [od, td] = await Promise.all([
        api("/api/binance/orders?limit=200"),
        api("/api/binance/trades?limit=200"),
      ]);
      if (!od.connected) {
        if (orderBox) orderBox.innerHTML = empty("币安未连接", od.reason || "请到账户连接页配置密钥");
      }
      if (!td.connected) {
        if (tradeBox) tradeBox.innerHTML = empty("币安未连接", td.reason || "请到账户连接页配置密钥");
      }
      if (!od.connected && !td.connected) return;

      const pnlMap = orderPnlMap(td.trades || []);
      const orders = takeHistory(
        newestFirst((od.orders || []).filter(isFilledOrder), (o) => o.updateTime ?? o.time)
      );
      const trades = takeHistory(
        newestFirst(collapseTradesByOrder(td.trades || []), (t) => t.time)
      );

      if (orderBox && od.connected) {
        if (!orders.length) {
          orderBox.innerHTML = empty("没有已成交的策略委托", "撤单已隐藏");
        } else {
          orderBox.innerHTML = orders
            .map((o) => {
              const p = pnlMap[String(o.orderId)] || { realized: 0, commission: 0 };
              const net = netPnl(p.realized, p.commission);
              return `<div class="table-row cols-o10">
                <span>${fmtTime(o.time ?? o.updateTime)}</span>
                <span>${coinCell(o.symbol)}</span>
                <span class="${o.side === "BUY" ? "positive" : "negative"}">${esc(o.side === "BUY" ? "买入" : "卖出")}<small>${esc(o.type)} · ${esc(o.timeInForce || "—")}</small></span>
                <span class="mono id-cell">${esc(o.orderId)}</span>
                <span>${sourceCell(o)}</span>
                <span>${Number(o.price) ? num(o.price, 2) : "市价"}<small>成交均价 ${Number(o.avgPrice) ? num(o.avgPrice, 2) : "—"}</small></span>
                <span>${num(o.origQty, 4)}</span>
                <span>${num(o.executedQty, 4)}</span>
                <span class="${pnlClass(net)}">${signed(net, 4)}<small>已实现 ${signed(p.realized, 4)}</small></span>
                <span>${esc(ORDER_STATUS_ZH[o.status] || o.status)}</span>
              </div>`;
            })
            .join("");
        }
      }

      if (tradeBox && td.connected) {
        if (!trades.length) {
          tradeBox.innerHTML = empty("没有已成交的策略成交", "按委托号合并，不含撤单");
        } else {
          tradeBox.innerHTML = trades
            .map((t) => {
              const realized = Number(t.realized || 0);
              const fee = Number(t.commission || 0);
              const net = netPnl(realized, fee);
              const avg = t.qty ? t.notional / t.qty : 0;
              return `<div class="table-row cols-t9">
                <span>${fmtTime(t.time)}</span>
                <span>${coinCell(t.symbol)}</span>
                <span class="mono id-cell">${esc(t.orderId)}<small>${t.fills} 笔逐笔</small></span>
                <span>${sourceCell(t)}</span>
                <span class="${t.side === "BUY" ? "positive" : "negative"}">${esc(t.side === "BUY" ? "买入" : "卖出")}<small>${t.maker ? "Maker" : "Taker"}</small></span>
                <span>${num(avg, 2)}<small>× ${num(t.qty, 4)} ${esc(coin(t.symbol))}</small></span>
                <span class="${pnlClass(realized)}">${signed(realized, 4)}</span>
                <span>${num(fee, 4)}</span>
                <span class="${pnlClass(net)}">${signed(net, 4)}</span>
              </div>`;
            })
            .join("");
        }
      }
    } catch (e) {
      if (orderBox) orderBox.innerHTML = empty("历史委托读取失败", String(e.message || e));
      if (tradeBox) tradeBox.innerHTML = empty("历史成交读取失败", String(e.message || e));
    }
  }

  async function refreshTrading() {
    await Promise.allSettled([
      loadMarket(),
      loadStatus(),
      loadSummary(),
      loadOpenOrders(),
      loadStrategies(),
      loadReading(),
      loadHistory(),
    ]);
  }

  async function refreshAccount() {
    await loadStatus();
  }

  // ------------------------------------------------------------------ 表单
  function bindAccountForm() {
    const form = $("accountForm");
    if (!form) return;
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const note = $("accountResult");
      note.textContent = "正在验证连接…";
      try {
        const d = await api("/api/trading/keys", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            api_key: $("apiKeyInput").value.trim(),
            api_secret: $("apiSecretInput").value.trim(),
            base_url: $("baseUrlInput").value.trim(),
          }),
        });
        if (d.ok) {
          note.textContent = "连接成功，密钥已保存在本机。";
          $("apiKeyInput").value = "";
          $("apiSecretInput").value = "";
        } else {
          note.textContent = "失败：" + (d.error || JSON.stringify(d).slice(0, 200));
        }
        refreshTrading();
      } catch (e) {
        note.textContent = "连接失败：" + (e.message || e);
      }
    });
  }

  async function postAction(btnId, path, label, body) {
    const btn = $(btnId);
    if (!btn) return;
    const note = $("accountResult");
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = "执行中…";
    note.textContent = label + " 执行中…";
    try {
      const d = await api(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      });
      note.textContent = d.ok
        ? `${label} 完成：${JSON.stringify(d).slice(0, 400)}`
        : `${label} 未成功：${d.error || JSON.stringify(d).slice(0, 400)}`;
      refreshTrading();
    } catch (e) {
      note.textContent = `${label} 失败：${e.message || e}`;
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  // ------------------------------------------------------------------ 启动
  function startTimer() {
    if (timer) clearInterval(timer);
    timer = setInterval(() => {
      if (currentView === "trading") refreshTrading();
      else refreshAccount();
    }, REFRESH_MS);
  }

  function init() {
    document.querySelectorAll(".nav-item").forEach((b) =>
      b.addEventListener("click", () => {
        switchView(b.dataset.view);
        location.hash = b.dataset.view;
      })
    );
    document.querySelectorAll("[data-nav]").forEach((b) =>
      b.addEventListener("click", () => switchView(b.dataset.nav))
    );

    const r = $("refreshBtn");
    if (r)
      r.addEventListener("click", () => {
        if (currentView === "trading") refreshTrading();
        else refreshAccount();
      });

    document.addEventListener("keydown", (e) => {
      if (
        e.key.toLowerCase() === "r" &&
        !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)
      ) {
        if (currentView === "trading") refreshTrading();
        else refreshAccount();
      }
    });

    bindAccountForm();
    document.querySelectorAll(".hist-limit-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        setHistoryLimit(btn.dataset.limit);
        loadHistory();
      });
    });
    syncHistoryLimitButtons();

    const smoke = $("testnetSmokeBtn");
    if (smoke)
      smoke.addEventListener("click", () => {
        const answer = window.prompt("这是非策略功能测试：将在 Binance 测试网实际下单、立即平仓并留下多笔委托/成交，产生手续费。若仍要测试，请输入「测试单」：");
        if (answer !== "测试单") return;
        postAction("testnetSmokeBtn", "/api/testnet/smoke-limit-close", "非策略功能测试", {
          quantity: 0.001,
        });
      });
    const flat = $("testnetFlattenBtn");
    if (flat)
      flat.addEventListener("click", () =>
        postAction("testnetFlattenBtn", "/api/testnet/flatten-now", "平仓")
      );
    const rec = $("reconcileBtn");
    if (rec)
      rec.addEventListener("click", () =>
        postAction("reconcileBtn", "/api/trading/reconcile", "对账", {
          reason: "frontend_manual",
        })
      );

    if (location.hash) {
      const v = location.hash.slice(1);
      if (VIEW_TITLES[v]) currentView = v;
    }
    switchView(currentView);
    connectMarkPrice();
    startTimer();
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", init);
  else init();
})();
