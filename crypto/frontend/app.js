/* BTC Quant Console — 两页前端
 * 第 1 页 交易总览: 余额 / 仓位 / 当前委托 / 策略 / 赚了多少 / 历史委托 / 历史成交
 * 第 2 页 账户连接: API Key 管理与连接状态
 *
 * 原则: 金额、仓位、委托、成交一律取自币安接口, 不由本地账本推算。
 */
(() => {
  "use strict";

  // 面板已同源服务前端：8787 直连用相对路径（无 CORS），
  // 其他端口（如 8788 静态服务）仍回落到本机 8787 API。
  const API = location.port === "8787" ? "" : "http://127.0.0.1:8787";
  const REFRESH_MS = 20000;
  const $ = (id) => document.getElementById(id);

  const VIEW_TITLES = {
    trading: "交易总览",
    account: "账户连接",
    chart: "图片显示确认模块",
  };
  let currentView = "trading";
  let timer = null;
  let markSocket = null;
  // 记录起点由 /api/account/summary.record_start_ms 下发（精确到分钟）；
  // 前端不定义起点，也不允许用「今天」这种粗口径把起点前的记录放进来。
  let historyStartMs = 0;
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

  // 交易所返回 LONG / SHORT / FLAT，界面统一显示中文。
  const sideZh = (side) => {
    const s = String(side || "FLAT").toUpperCase();
    return s === "LONG" ? "多" : s === "SHORT" ? "空" : "空仓";
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

  const isHistoryRow = (row) =>
    String(row.source || "") === "strategy" &&
    (!historyStartMs ||
      Number(row.time ?? row.updateTime ?? 0) >= historyStartMs);

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

  // 所有面向用户的交易所/策略时间统一为北京时间（UTC+8）。
  // 策略的 bar_ms 是K线开盘时刻；信号必须等待该根收盘，实际下单是下一根开盘。
  const fmtKlineBeijing = (ms, interval) => {
    if (!ms) return "—";
    const start = fmtTime(ms);
    const mins = String(interval || "15m").toLowerCase() === "5m" ? 5 : 15;
    const end = fmtTime(Number(ms) + mins * 60_000);
    if (start === "—" || end === "—") return "—";
    // 同一天时仅重复结束时分，不把“11:45”误解为这一刻即时下单。
    return `${start.slice(0, 16)}–${end.slice(11, 16)} 北京时间`;
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
    // 第 3 页自带渲染与刷新循环，进入时启动、离开时停止，避免后台空转取数。
    if (name === "chart") {
      if (window.ChartModule) window.ChartModule.enter();
    } else if (window.ChartModule) {
      window.ChartModule.leave();
    }
    if (name === "trading") refreshTrading();
    else if (name === "account") refreshAccount();
  }

  // ------------------------------------------------------------- 实时行情
  //
  // WS 地址**由后端下发**, 不再硬编码。
  // 历史事故：前端曾写死主网 WS 地址, 与测试网下单错位。
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
    const tag = marketLabel
      ? `币安${marketLabel.replace(/\s*\(.*?\)\s*/, "")}`
      : "币安";
    if ($("priceChange")) $("priceChange").innerHTML = `实时推送中 · <span>${tag} 行情推送</span>`;
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
      statusCache = s;
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
          ? liveNets.map((sym) => `${coin(sym)} ${sideZh(nets[sym].side)}`).join(" · ")
          : "空仓";
        $("posSideTag").className = "tag " + (liveNets.length ? "safe" : "");
      }
      if ($("posHint")) {
        $("posHint").textContent = liveNets.length
          ? liveNets.map((sym) => {
              const pos = nets[sym];
              return `${coin(sym)} ${sideZh(pos.side)} ${Number(pos.quantity || 0)}`;
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
          const lev = Number(pos.leverage || 10) || 10;
          html += lots.map((src) => {
            const zh = src.side === "LONG" ? "多" : "空";
            const lotSign = src.side === "LONG" ? 1 : -1;
            const est = lotPnl(mark, src.side, Number(src.qty), Number(src.px));
            // 数量一律用币安网页端的 USDT 口径（名义价值），小字保留币数量。
            const lotUsdt = mark ? Number(src.qty) * mark * lotSign : null;
            const lotMargin = lotUsdt == null ? null : Math.abs(lotUsdt) / lev;
            return `<div class="table-row cols-pos">
              <span>${coinCell(src.symbol || sym)}</span>
              <span>${esc(src.name || src.interval)}</span>
              <span class="${src.side === "LONG" ? "positive" : "negative"}">${zh}</span>
              <span>${lotUsdt == null ? "—" : `${num(lotUsdt, 2)} <small>USDT</small>`}<br><small class="net-notional">${num(src.qty, 4)} ${coin(src.symbol || sym)}</small></span>
              <span>${lotMargin == null ? "—" : `${num(lotMargin, 2)} <small>估算</small>`}</span>
              <span>${num(src.px, 2)}<small>参考价</small></span>
              <span>${fmtKlineBeijing(src.bar_ms, src.interval)}</span>
              <span>${esc(src.reason || "—")}</span>
              <span class="${est == null ? "" : pnlClass(est)}">${est == null ? "—" : `${signed(est)} <small>估算</small>`}</span>
            </div>`;
          }).join("");
          const netSide = side === "FLAT" || !qty ? "空仓" : (side === "LONG" ? "多" : "空");
          // 净仓行直接用交易所返回的 notional / isolatedMargin，与币安网页端逐位一致。
          const netSign = side === "LONG" ? 1 : side === "SHORT" ? -1 : 0;
          const exNotional = Number(pos.notional ?? 0);
          const netUsdt = exNotional !== 0 ? exNotional : (mark ? qty * mark * netSign : 0);
          const exMargin = Number(pos.margin ?? 0);
          const netMargin = exMargin > 0 ? exMargin : Math.abs(netUsdt) / lev;
          html += `<div class="table-row cols-pos is-net">
            <span>${coinCell(sym)}</span>
            <span>币安净仓</span>
            <span class="${!qty ? "" : side === "LONG" ? "positive" : "negative"}">${esc(netSide)}</span>
            <span>${qty ? `${num(netUsdt, 2)} <small>USDT</small><br><small class="net-notional">${num(qty, 4)} ${coin(sym)}</small>` : "—"}</span>
            <span>${qty ? `${num(netMargin, 2)} <small>交易所</small>` : "—"}</span>
            <span>${entry ? num(entry, 2) : "—"}${entry ? "<small>成交均价</small>" : ""}</span>
            <span>—</span>
            <span>${lots.length > 1 ? "同标的两条策略合成" : (lots[0] ? `${lots[0].name} 对应净仓` : "交易所账户")}</span>
            <span class="${pnlClass(upnl)}">${signed(upnl)} <small>交易所</small></span>
          </div>`;
        });
        $("posBox").innerHTML = html || empty("空仓", "当前无持仓");
        const totalMargin = symbols.reduce(
          (acc, sym) => acc + (Number((nets[sym] || {}).margin) || 0),
          0
        );
        if ($("posMarginTag")) {
          $("posMarginTag").textContent = totalMargin > 0
            ? `保证金 ${num(totalMargin, 2)} USDT`
            : "保证金 —";
        }
      }

      const recon = Boolean(s.reconciliation_needed);
      renderExternal(s.external_interventions);
      // 对账以「策略账本期望净额 vs 交易所真实净额」为准（每标的单独比）。
      // 旧 V7 的 reconciliation_needed 来自已不参与交易的模块，只作附注，不再冒充对账结论。
      const wantNets = s.desired_nets || {};
      const signedEx = (sym) => {
        const pos = nets[sym] || {};
        const sd = String(pos.side || "FLAT").toUpperCase();
        const q = Number(pos.quantity || 0);
        return sd === "LONG" ? q : sd === "SHORT" ? -q : 0;
      };
      const netGaps = symbols
        .map((sym) => ({ sym, want: Number(wantNets[sym] || 0), have: signedEx(sym) }))
        .map((r) => ({ ...r, gap: Math.abs(r.want - r.have) }))
        .filter((r) => r.gap >= 5e-4);
      if ($("reconcileState")) {
        if (!netGaps.length) {
          $("reconcileState").textContent = recon
            ? "一致（旧 V7 模块仍标记需对账，该模块已不参与交易）"
            : "一致";
          $("reconcileState").className = "positive";
        } else {
          $("reconcileState").textContent =
            "不一致：" +
            netGaps.map((r) => `${coin(r.sym)} 差 ${num(r.want - r.have, 4)}`).join(" · ");
          $("reconcileState").className = "blocked";
        }
      }
      if ($("accountPosition"))
        $("accountPosition").textContent = liveNets.length
          ? liveNets.map((sym) => `${coin(sym)} ${sideZh(nets[sym].side)} ${num(nets[sym].quantity, 4)}`).join(" · ")
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
        $("modeLabel").textContent = s.connected ? "测试网 · 接口已连接" : "测试网 · 接口未连接";
      if ($("engineState")) $("engineState").textContent = "后端已连接";
    } catch (e) {
      if ($("engineState")) $("engineState").textContent = "后端不可达";
    }
  }

  // ---------------------------------------------------- 核心指标（实时计算）
  //
  // 六项指标全部由已取到的实时数据推导，因此不存在“等待面板更新”这类占位。
  // 后端 quality 字段优先，缺失时由账户汇总就地推导，口径保持一致。
  let summaryCache = null;
  let strategyCache = null;
  // Swiss 面板复用已有请求结果，绝不重复查询交易所：
  // 同一轮刷新把同一个接口查两遍会触发币安 429 (-1003)。
  let statusCache = null;
  let swissReading = null;
  let swissEquity = null;
  let sparkFetchedAt = 0;

  const n0 = (v) => Number(v || 0);
  const numOrNull = (v) =>
    v === undefined || v === null || v === "" || Number.isNaN(Number(v))
      ? null
      : Number(v);

  function qualityModel() {
    const s = summaryCache;
    if (!s || s.connected === false) return null;
    const q = s.quality || {};
    const bySym = s.by_symbol || {};
    const posBy = s.positions_by_symbol || {};
    const costs = q.costs || {};
    const dd = q.drawdown || {};
    const sym = (code) => bySym[code] || {};
    const pos = (code) => posBy[code] || {};
    const realized =
      q.realized_net_pnl !== undefined
        ? n0(q.realized_net_pnl)
        : n0(sym("BTCUSDT").net_pnl) + n0(sym("ETHUSDT").net_pnl);
    // 未实现合计与分标的同源：有持仓行时用两个标的相加，保证卡片自身能对上，
    // 不会出现「合计 +0.05 而 BTC+ETH = +17.83」这种自相矛盾。
    const upB = n0(pos("BTCUSDT").unrealized_pnl);
    const upE = n0(pos("ETHUSDT").unrealized_pnl);
    const hasPosRow =
      pos("BTCUSDT").side !== undefined || pos("ETHUSDT").side !== undefined;
    return {
      window: q.window || s.record_start || s.stats_start || null,
      // ① 已实现净盈亏：扣手续费、分标的
      realized,
      realizedBy: {
        BTC: n0(sym("BTCUSDT").net_pnl),
        ETH: n0(sym("ETHUSDT").net_pnl),
      },
      // ② 未实现盈亏：单独列出，不与已实现混合
      unrealized: hasPosRow
        ? upB + upE
        : q.unrealized_pnl !== undefined
        ? n0(q.unrealized_pnl)
        : n0(s.unrealized_pnl),
      unrealizedBy: {
        BTC: upB,
        ETH: upE,
      },
      // ③ 单笔净边际：开仓名义金额口径，不依赖复利
      edge: numOrNull(q.unit_edge_bps) ?? numOrNull(s.unit_edge_bps),
      openNotional:
        numOrNull(q.open_notional) ??
        (n0(sym("BTCUSDT").open_notional) + n0(sym("ETHUSDT").open_notional)),
      // ④ 最大回撤：账户权益 + 按标的分组
      ddAccount: numOrNull(q.recorded_max_drawdown),
      ddBy: {
        BTC:
          numOrNull((dd.by_symbol || {}).BTCUSDT) ??
          numOrNull(sym("BTCUSDT").realized_max_drawdown),
        ETH:
          numOrNull((dd.by_symbol || {}).ETHUSDT) ??
          numOrNull(sym("ETHUSDT").realized_max_drawdown),
      },
      // ⑤ 胜率与盈亏比：不能只看胜率
      winRate: numOrNull(q.win_rate) ?? numOrNull(s.win_rate),
      profitFactor: numOrNull(q.profit_factor) ?? numOrNull(s.profit_factor),
      payoffRatio: numOrNull(q.payoff_ratio) ?? numOrNull(s.payoff_ratio),
      wins: n0(q.wins ?? s.wins),
      losses: n0(q.losses ?? s.losses),
      avgWin: numOrNull(q.avg_win) ?? numOrNull(s.avg_win),
      avgLoss: numOrNull(q.avg_loss) ?? numOrNull(s.avg_loss),
      // ⑥ 交易成本：maker / taker / 资金费 / 滑点逐项单列
      costs: {
        maker: n0(costs.maker_fee ?? s.maker_fee),
        taker: n0(costs.taker_fee ?? s.taker_fee),
        funding: numOrNull(costs.funding_fee),
        slippage: numOrNull(costs.slippage_est),
        fundingBasis: costs.funding_basis,
        slippageBasis: costs.slippage_basis,
      },
      edgeBasis: q.unit_edge_basis,
      drawdownBasis: q.drawdown_basis,
      reasons: Array.isArray(q.reasons) ? q.reasons : null,
      actions: Array.isArray(q.next_actions) ? q.next_actions : null,
      ctx: q.context || {},
      bySymbol: bySym,
    };
  }

  function derivedReasons(m, strategies) {
    const out = [];
    if (strategies.length) {
      out.push({
        text: `当前运行 ${strategies.length} 条策略：${strategies.join(" + ")}`,
        count: 1,
      });
    }
    const ctx = m.ctx || {};
    if (ctx.five_minute_disabled) {
      out.push({
        text: "5m 已停用：趋势中缺少正常出口，最终只能吃 3×ATR 灾难止损",
        count: 1,
      });
    }
    if (ctx.halted) {
      out.push({ text: "运行器处于熔断状态，已停止开新仓", count: 1 });
    }
    if (ctx.missed_bars) {
      out.push({ text: `停机期间漏记 K 线 ${num(ctx.missed_bars, 0)} 根（只记账不补单）`, count: 1 });
    }
    if (!out.length) {
      out.push({ text: "运行记录中没有异常跳过或阻塞项", count: 1 });
    }
    return out;
  }

  const BASE_ACTIONS = [
    "固定 15m-only 观察窗口，不在盈利后临时改参数或放大仓位",
    "累计 ≥4 周或 30 个完整平仓样本，再评估单笔净边际与跨币一致性",
    "补齐逐笔开平仓配对与完整权益曲线，当前边际为成交腿近似",
  ];

  const METRIC_IDS = [
    "qRealized", "qRealBTC", "qRealETH",
    "qUnrealized", "qUnrealBTC", "qUnrealETH",
    "qEdge", "qOpenNotional", "qNetForEdge",
    "qDrawdown", "qDdBTC", "qDdETH",
    "qWinRate", "qProfitFactor", "qAvgWinLoss", "qWinLossCount",
    "qCostTotal", "qFeeMaker", "qFeeTaker", "qFeeFunding", "qSlippage",
  ];

  function renderSymDetail(m) {
    const box = $("symDetailBox");
    if (!box) return;
    if (!m) {
      box.innerHTML = empty("账户未连接", "连接测试网后按标的拆分");
      return;
    }
    box.innerHTML = ["BTCUSDT", "ETHUSDT"]
      .map((code) => {
        const b = m.bySymbol[code] || {};
        const has = b.trade_count !== undefined;
        const net = n0(b.net_pnl);
        const edge = numOrNull(b.unit_edge_bps);
        const pf = numOrNull(b.profit_factor);
        const dd = numOrNull(b.realized_max_drawdown);
        return `<div class="table-row cols-sym">
          <span>${coinCell(code)}</span>
          <span class="${pnlClass(net)}">${has ? signed(net, 2) : "—"}</span>
          <span>${b.win_rate == null ? "—" : pct(b.win_rate, 1)}</span>
          <span>${pf == null ? "—" : num(pf, 2)}</span>
          <span>${num(b.wins, 0)} / ${num(b.losses, 0)}</span>
          <span>${num(b.open_notional, 2)}</span>
          <span class="${pnlClass(edge)}">${edge == null ? "—" : signed(edge, 2) + " bp"}</span>
          <span class="${dd != null && dd > 0 ? "negative" : ""}">${dd == null ? "—" : num(dd, 2)}</span>
          <span>${num(b.maker_fee, 4)} / ${num(b.taker_fee, 4)}</span>
        </div>`;
      })
      .join("");
  }

  function renderQuality() {
    const strategies = strategyCache || [];
    const m = qualityModel();
    if ($("qualityWindow"))
      $("qualityWindow").textContent = m?.window
        ? `记录起点 ${m.window}`
        : "实时口径";
    if (!m) {
      METRIC_IDS.forEach((id) => {
        const el = $(id);
        if (el) {
          el.textContent = "—";
          el.classList.remove("positive", "negative");
        }
      });
      if ($("qualityReasons"))
        $("qualityReasons").innerHTML = empty(
          "账户未连接",
          summaryCache?.reason || "请在账户连接页配置测试网密钥"
        );
      if ($("qualityActions"))
        $("qualityActions").innerHTML = BASE_ACTIONS.map((t) => `<li>${esc(t)}</li>`).join("");
      renderSymDetail(null);
      return;
    }

    // ① 已实现净盈亏（扣手续费、区分 BTC/ETH）
    setPnl($("qRealized"), m.realized, 2);
    setPnl($("qRealBTC"), m.realizedBy.BTC, 2);
    setPnl($("qRealETH"), m.realizedBy.ETH, 2);

    // ② 未实现盈亏（单独列出）
    setPnl($("qUnrealized"), m.unrealized, 2);
    setPnl($("qUnrealBTC"), m.unrealizedBy.BTC, 2);
    setPnl($("qUnrealETH"), m.unrealizedBy.ETH, 2);

    // ③ 单笔净边际（开仓名义金额口径）
    const edgeEl = $("qEdge");
    if (edgeEl) {
      edgeEl.textContent = m.edge == null ? "—" : signed(m.edge, 2) + " bp";
      edgeEl.classList.remove("positive", "negative");
      if (m.edge != null && m.edge !== 0)
        edgeEl.classList.add(m.edge > 0 ? "positive" : "negative");
    }
    if ($("qOpenNotional"))
      $("qOpenNotional").textContent = m.openNotional ? num(m.openNotional, 2) : "—";
    if ($("qNetForEdge")) setPnl($("qNetForEdge"), m.realized, 2);
    if ($("qEdgeBasis"))
      $("qEdgeBasis").textContent =
        m.edgeBasis || "净额 ÷ 开仓腿名义 × 10000，不依赖复利";

    // ④ 最大回撤（账户权益 + 按标的分组）
    const ddEl = $("qDrawdown");
    if (ddEl) {
      ddEl.textContent = m.ddAccount == null ? "—" : pct(m.ddAccount, 2);
      ddEl.classList.toggle("negative", m.ddAccount != null && m.ddAccount > 0);
    }
    [
      ["qDdBTC", m.ddBy.BTC],
      ["qDdETH", m.ddBy.ETH],
    ].forEach(([id, v]) => {
      const el = $(id);
      if (!el) return;
      el.textContent = v == null ? "—" : num(v, 2);
      el.classList.toggle("negative", v != null && v > 0);
    });
    if ($("qDrawdownBasis"))
      $("qDrawdownBasis").textContent =
        m.drawdownBasis || "账户权益回撤（%）＋分标的已实现回撤（USDT）";

    // ⑤ 胜率与盈亏比（不能只看胜率）
    if ($("qWinRate"))
      $("qWinRate").textContent = m.winRate == null ? "—" : pct(m.winRate, 1);
    if ($("qProfitFactor"))
      $("qProfitFactor").textContent = m.profitFactor == null ? "—" : num(m.profitFactor, 2);
    if ($("qAvgWinLoss"))
      $("qAvgWinLoss").textContent =
        m.avgWin == null && m.avgLoss == null
          ? "—"
          : `${m.avgWin == null ? "—" : num(m.avgWin, 2)} / ${
              m.avgLoss == null ? "—" : num(m.avgLoss, 2)
            }`;
    if ($("qWinLossCount"))
      $("qWinLossCount").textContent = `${num(m.wins, 0)} / ${num(m.losses, 0)}`;

    // ⑥ 交易成本（maker / taker / 资金费 / 滑点单列）
    const c = m.costs;
    const total =
      n0(c.maker) + n0(c.taker) + n0(c.funding) + n0(c.slippage);
    if ($("qCostTotal")) $("qCostTotal").textContent = num(total, 2);
    if ($("qFeeMaker")) $("qFeeMaker").textContent = num(c.maker, 4);
    if ($("qFeeTaker")) $("qFeeTaker").textContent = num(c.taker, 4);
    if ($("qFeeFunding"))
      $("qFeeFunding").textContent = c.funding == null ? "未接入" : num(c.funding, 4);
    if ($("qSlippage"))
      $("qSlippage").textContent = c.slippage == null ? "—" : num(c.slippage, 2);
    if ($("qCostBasis"))
      $("qCostBasis").textContent = `USDT · ${
        c.fundingBasis || "资金费口径未知"
      }；${c.slippageBasis || "滑点口径未知"}`;

    if ($("qStrategyMode"))
      $("qStrategyMode").textContent = strategies.length
        ? strategies.join(" + ")
        : "策略列表读取中";

    renderSymDetail(m);

    const reasons = m.reasons && m.reasons.length ? m.reasons : derivedReasons(m, strategies);
    if ($("qualityReasons")) {
      $("qualityReasons").innerHTML = reasons
        .map(
          (r) =>
            `<div class="quality-reason"><span class="reason-dot"></span><span>${esc(r.text)}${
              r.count > 1 ? `<small>记录 ${num(r.count, 0)} 次</small>` : ""
            }</span></div>`
        )
        .join("");
    }
    if ($("qualityActions")) {
      const actions = m.actions && m.actions.length ? m.actions : BASE_ACTIONS;
      $("qualityActions").innerHTML = actions.map((t) => `<li>${esc(t)}</li>`).join("");
    }
  }

  async function loadSummary() {
    try {
      const s = await api("/api/account/summary");
      summaryCache = s;
      renderQuality();
      renderSwiss();
      if (!s.connected) {
        if ($("mWallet")) $("mWallet").textContent = "未连接";
        ["mUnrealBTC", "mUnrealETH", "mRealBTC", "mRealETH"].forEach((id) => {
          if ($(id)) $(id).textContent = "—";
        });
        if ($("realHint")) $("realHint").textContent = s.reason || "密钥未配置";
        return;
      }
      if ($("mWallet")) $("mWallet").textContent = num(s.wallet_balance, 2);
      // 分标的显示：BTC 与 ETH 各自统计，绝不混加。
      // 未实现 = 交易所逐标的未实现盈亏；已实现用净额（已实现 − 手续费）。
      const posBy = s.positions_by_symbol || {};
      const bySym = s.by_symbol || {};
      setPnl($("mUnrealBTC"), (posBy.BTCUSDT || {}).unrealized_pnl ?? 0);
      setPnl($("mUnrealETH"), (posBy.ETHUSDT || {}).unrealized_pnl ?? 0);
      setPnl($("mRealBTC"), (bySym.BTCUSDT || {}).net_pnl ?? 0);
      setPnl($("mRealETH"), (bySym.ETHUSDT || {}).net_pnl ?? 0);
      if ($("realHint")) {
        historyStartMs = Number(s.record_start_ms || 0);
        $("realHint").textContent =
          `净 = 已实现 − 手续费 · 自 ${s.record_start || s.stats_start || "—"} 起`;
      }
    } catch (e) {
      summaryCache = null;
      renderQuality();
      renderSwiss();
      if ($("mWallet")) $("mWallet").textContent = "读取失败";
      ["mUnrealBTC", "mUnrealETH", "mRealBTC", "mRealETH"].forEach((id) => {
        if ($(id)) $(id).textContent = "—";
      });
    }
  }

  async function loadOpenOrders() {
    if (!$("openOrdersBox")) return;
    try {
      // 复用 loadStatus 的同一轮结果，缓存未就绪时才补查。
      const s = statusCache || (await api("/api/trading/status"));
      const orders = s.open_orders || s.orders || [];
      if ($("openOrdersCount")) $("openOrdersCount").textContent = String(orders.length);
      if (!orders.length) {
        $("openOrdersBox").innerHTML = empty("无挂单", "追价完成后不留挂单");
        return;
      }
      $("openOrdersBox").innerHTML = orders
        .map(
          (o) => `<div class="kv-row"><span>${esc(coin(o.symbol))} ${esc(o.side === "BUY" ? "买入" : "卖出")} ${esc(ORDER_TYPE_ZH[o.type] || o.type || "")}</span>
            <b>${num(o.price, 2)} × ${num(o.origQty ?? o.quantity, 4)}</b></div>`
        )
        .join("");
    } catch (e) {
      /* 保持原样 */
    }
  }

  // 运行器心跳：只更新状态文字。「最近一根信号读数」板块已按用户要求移除。
  async function loadRunner() {
    try {
      const d = await api("/api/strategy/reading");
      if ($("runnerState")) $("runnerState").textContent = runnerStatusText(d.runner);
    } catch (e) {
      if ($("runnerState")) $("runnerState").textContent = "运行器状态读取失败";
    }
  }

  async function loadStrategies() {
    const sidebarBox = $("sidebarStrategyList");
    const sidebarCount = $("sidebarStrategyCount");
    try {
      const d = await api("/api/strategies/active");
      const list = d.strategies || [];
      // 质量卡的策略口径与侧边栏共用同一份实时数据，避免两处不一致。
      strategyCache = list
        .filter((s) => s.enabled)
        .map((s) => `${coin(s.symbol)} ${s.timeframe || ""}`.trim());
      renderQuality();
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
          const title = `${coin(s.symbol)} ${s.timeframe || ""}`.trim() || (s.name || s.strategy_id);
          return `<article class="sidebar-strategy-item${on ? " is-enabled" : ""}">
            <div class="sidebar-strategy-top">
              <span class="sidebar-strategy-dot ${on && !rt.halted ? "online" : "offline"}"></span>
              <b>${esc(title)}</b>
              <span class="sidebar-strategy-state ${on && !rt.halted ? "on" : "off"}">${esc(state)}</span>
            </div>
            <p>${esc(s.symbol || "BTCUSDT")} · ${esc(s.name || s.kind || "策略")}</p>
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

  // 交易所返回的委托类型与时效也是英文枚举，界面统一显示中文。
  const ORDER_TYPE_ZH = {
    LIMIT: "限价",
    MARKET: "市价",
    LIMIT_MAKER: "只挂单限价",
    STOP: "止损限价",
    STOP_MARKET: "止损市价",
    TAKE_PROFIT: "止盈限价",
    TAKE_PROFIT_MARKET: "止盈市价",
    TRAILING_STOP_MARKET: "跟踪止损市价",
  };
  const TIME_IN_FORCE_ZH = {
    GTC: "撤销前有效",
    GTX: "只挂单",
    IOC: "立即成交或取消",
    FOK: "全部成交或取消",
  };

  async function loadHistory() {
    const orderBox = $("histOrdersBox");
    const tradeBox = $("histTradesBox");
    if (!orderBox && !tradeBox) return;
    syncHistoryLimitButtons();
    try {
      // 账户汇总在 loadSummary 里已经查过，这里复用，避免同一轮刷新
      // 把 /api/account/summary 查两遍而触发币安 429；缓存未就绪时才补查。
      const [od, td] = await Promise.all([
        api("/api/binance/orders?limit=200"),
        api("/api/binance/trades?limit=200"),
      ]);
      const summary = summaryCache || (await api("/api/account/summary"));
      if (summary && summary.record_start_ms) {
        historyStartMs = Number(summary.record_start_ms);
      }
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
                <span class="${o.side === "BUY" ? "positive" : "negative"}">${esc(o.side === "BUY" ? "买入" : "卖出")}<small>${esc(ORDER_TYPE_ZH[o.type] || o.type)} · ${esc(TIME_IN_FORCE_ZH[o.timeInForce] || o.timeInForce || "—")}</small></span>
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
                <span class="${t.side === "BUY" ? "positive" : "negative"}">${esc(t.side === "BUY" ? "买入" : "卖出")}<small>${t.maker ? "挂单" : "吃单"}</small></span>
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

  // ------------------------------------------- Swiss Terminal 面板（只读）
  const STRAT_ID = {
    "BTCUSDT|15m": "kdj15", "ETHUSDT|15m": "eth15",
    "BTCUSDT|5m": "kdj5", "ETHUSDT|5m": "eth5",
  };

  function sparkPath(values, w, h, pad) {
    if (!values || values.length < 2) return "";
    const min = Math.min.apply(null, values);
    const max = Math.max.apply(null, values);
    const span = (max - min) || 1;
    return values
      .map((v, i) => {
        const x = pad + (i * (w - pad * 2)) / (values.length - 1);
        const y = h - pad - ((v - min) / span) * (h - pad * 2);
        return `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
  }

  function drawSpark(svg, values, color) {
    if (!svg) return false;
    const vb = (svg.getAttribute("viewBox") || "0 0 200 56").split(/\s+/).map(Number);
    const w = vb[2], h = vb[3];
    const d = sparkPath(values, w, h, 4);
    if (!d) { svg.innerHTML = ""; return false; }
    svg.innerHTML =
      `<path d="${d} L${(w - 4).toFixed(1)},${h} L4,${h} Z" fill="${color}" opacity="0.10"/>` +
      `<path d="${d}" fill="none" stroke="${color}" stroke-width="1.5" ` +
      `vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/>`;
    return true;
  }

  function drawEquity(svg, values) {
    if (!svg) return false;
    const vb = (svg.getAttribute("viewBox") || "0 0 640 130").split(/\s+/).map(Number);
    const w = vb[2], h = vb[3];
    const d = sparkPath(values, w, h, 6);
    if (!d) { svg.innerHTML = ""; return false; }
    svg.innerHTML =
      `<path d="${d} L${(w - 6).toFixed(1)},${h} L6,${h} Z" fill="#4c8dff" opacity="0.09"/>` +
      `<path d="${d}" fill="none" stroke="#4c8dff" stroke-width="1.6" ` +
      `vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/>`;
    return true;
  }

  function ageText(ms) {
    if (!ms) return "—";
    const s = Math.max(0, Math.round((Date.now() - Number(ms)) / 1000));
    if (s < 60) return `${s}s 前`;
    if (s < 3600) return `${Math.round(s / 60)}m 前`;
    return `${(s / 3600).toFixed(1)}h 前`;
  }

  // 运行器口径的风控量：mtm = 钱包余额 + 未实现
  function runnerRisk(summary) {
    const q = (summary && summary.quality) || {};
    const ctx = q.context || {};
    const wallet = Number((summary && summary.wallet_balance) || 0);
    const upnl = Number((summary && summary.unrealized_pnl) || 0);
    const mtm = wallet + upnl;
    const dayStart = Number(ctx.day_start_eq || 0);
    const peakEq = Number(ctx.peak || 0);
    return {
      ctx: ctx,
      q: q,
      mtm: mtm,
      dayStart: dayStart,
      peakEq: peakEq,
      dayPnl: dayStart > 0 ? (mtm - dayStart) / dayStart : null,
      dayLoss: dayStart > 0 ? (dayStart - mtm) / dayStart : null,
      dd: peakEq > 0 ? Math.max(0, (peakEq - mtm) / peakEq) : null,
    };
  }

  // 常驻风控状态条
  function renderRiskStrip(reading, status, summary) {
    const runner = (reading && reading.runner) || {};
    const r = runnerRisk(summary);
    const mode = runner.mode || "";
    const modeZh =
      mode === "testnet_orders" ? "测试网 · 实盘下单"
      : mode === "observe" ? "测试网 · 只观察"
      : mode === "paper" ? "纸面 · Paper"
      : mode || "未上报";
    if ($("rsMode")) {
      $("rsMode").innerHTML =
        `<i class="dot ${mode === "testnet_orders" ? "green" : "amber"}"></i>${esc(modeZh)}`;
    }
    if ($("rsDayPnl")) {
      const el = $("rsDayPnl");
      el.textContent = r.dayPnl == null ? "—" : signed(r.dayPnl * 100, 2) + "%";
      el.className = r.dayPnl == null ? "" : r.dayPnl < 0 ? "negative" : "positive";
    }
    if ($("rsDayLoss")) {
      const el = $("rsDayLoss");
      const used = r.dayLoss == null ? null : Math.max(0, r.dayLoss);
      el.textContent = used == null ? "—" : pct(used, 2);
      el.className =
        used == null ? "" : used >= 0.03 ? "negative" : used >= 0.02 ? "warn" : "";
    }
    if ($("rsDrawdown")) {
      const el = $("rsDrawdown");
      el.textContent = r.dd == null ? "—" : pct(r.dd, 2);
      el.className =
        r.dd == null ? "" : r.dd >= 0.1 ? "negative" : r.dd >= 0.05 ? "warn" : "";
    }
    if ($("rsHalted")) {
      const el = $("rsHalted");
      const halted = !!r.ctx.halted;
      el.textContent = halted ? "true" : "false";
      el.className = halted ? "negative" : "";
    }
    if ($("rsHeartbeat")) {
      const el = $("rsHeartbeat");
      el.textContent = (runner.status || "—") + " · " + ageText(runner.updated_ms);
      el.className = runner.status === "running" ? "positive" : "negative";
    }
    if ($("rsMissed")) {
      $("rsMissed").textContent =
        `${r.ctx.missed_bars ?? 0} 根 / ${r.ctx.missed_signals ?? 0} 信号`;
    }
    if ($("rsStamp")) {
      $("rsStamp").textContent = runner.updated_ms ? fmtTime(runner.updated_ms) : "—";
    }
  }

  // 告警四级阶梯
  function renderAlertLadder(reading, status, summary) {
    const runner = (reading && reading.runner) || {};
    const r = runnerRisk(summary);
    const ext = (status && status.external_interventions) || {};
    const extEntries = Object.entries(ext).filter(([, v]) => v);
    const extAll = extEntries.map(([, v]) => v);
    const extPaused = extEntries.filter(([, v]) => v && v.paused);
    const stale = runner.updated_ms
      ? Date.now() - Number(runner.updated_ms) > 90 * 1000
      : true;

    const dayLoss = r.dayLoss || 0;
    const dd = r.dd || 0;
    let level = 0;
    let detail = [];
    if (r.ctx.halted || dd >= 0.1 || dayLoss >= 0.03 || extPaused.length) {
      level = 3;
      if (r.ctx.halted) detail.push("熔断已触发 · 停止开新仓，等待人工解除");
      if (dayLoss >= 0.03) detail.push(`日亏 ${pct(dayLoss, 2)} ≥ 3% 额度，已停止开新仓`);
      if (dd >= 0.1) detail.push(`回撤 ${pct(dd, 2)} ≥ 10% 熔断线`);
      if (extPaused.length)
        detail.push(`人工干预未确认 · ${extPaused.map(([s]) => coin(s)).join(" · ")}`);
    } else if (dayLoss >= 0.02 || dd >= 0.05 || stale) {
      level = 2;
      if (dayLoss >= 0.02) detail.push(`日亏 ${pct(dayLoss, 2)} 接近 3% 额度`);
      if (dd >= 0.05) detail.push(`回撤 ${pct(dd, 2)} 已过 5%`);
      if (stale) detail.push(`运行器心跳 ${ageText(runner.updated_ms)}，可能已停`);
    } else if (extAll.length || r.ctx.missed_bars) {
      level = 1;
      if (extAll.length)
        detail.push(`已跟随人工操作 · ${extEntries.map(([s]) => coin(s)).join(" · ")}`);
      if (r.ctx.missed_bars)
        detail.push(`停机期间错过 ${r.ctx.missed_bars} 根 K 线（只记账不补单）`);
    }
    if (!detail.length) detail = ["心跳正常、无异常挂单、无人工干预"];

    const labels = {
      0: `正常运行 · ${detail[0]}`,
      1: `信息 · ${detail.join("；")}`,
      2: `注意 · ${detail.join("；")}`,
      3: `危险 · ${detail.join("；")}`,
    };
    for (let i = 0; i < 4; i += 1) {
      const row = $("lvRow" + i);
      const body = $("lvBody" + i);
      if (row) row.classList.toggle("is-active", i === level);
      if (body) body.textContent = i === level ? labels[i] : "未触发";
    }
  }

  // 权益曲线
  function renderEquityCurve(equity, summary) {
    const r = runnerRisk(summary);
    if ($("acctDrawdown")) $("acctDrawdown").textContent = r.dd == null ? "—" : pct(r.dd, 2);
    if ($("acctDrawdownBasis")) {
      $("acctDrawdownBasis").textContent = `峰值 ${num(r.peakEq, 2)} · 运行器口径`;
    }
    if (!equity || !equity.ok || !(equity.points || []).length) {
      if ($("equityMeta")) $("equityMeta").textContent = "无运行记录";
      if ($("equityRange")) $("equityRange").textContent = "—";
      if ($("equityPeak")) $("equityPeak").textContent = "—";
      if ($("equityDd")) $("equityDd").textContent = "—";
      if ($("equityStep")) $("equityStep").textContent = "—";
      drawEquity($("equitySvg"), []);
      return;
    }
    const pts = equity.points;
    const values = pts.map((p) => Number(p.equity));
    const ok = drawEquity($("equitySvg"), values);
    if ($("equityMeta")) {
      $("equityMeta").textContent = `${pts.length} 个记录点 · 运行记录口径`;
    }
    if ($("equityRange")) {
      $("equityRange").textContent = ok
        ? `${num(values[0], 2)} → ${num(values[values.length - 1], 2)} USDT`
        : "—";
    }
    if ($("equityPeak")) {
      $("equityPeak").textContent = `峰值 ${num(equity.peak, 2)} USDT`;
    }
    if ($("equityDd")) {
      $("equityDd").textContent = `记录峰值回撤 ${pct(equity.max_drawdown, 2)}`;
    }
    if ($("equityStep")) {
      // 权益是运行器逐根落盘的记录值；出入金或口径切换会造成台阶跳变，
      // 单看曲线容易误读成交易盈亏，这里把最大单步变化显式标出。
      let maxStep = 0;
      for (let i = 1; i < values.length; i += 1) {
        const step = Math.abs(values[i] - values[i - 1]);
        if (step > maxStep) maxStep = step;
      }
      $("equityStep").textContent =
        `最大单步 ${signed(maxStep, 2)} USDT（含出入金/口径变化）`;
    }
  }

  // 信号判定：KDJ 刻度 + MACD 正负柱
  function renderSignalTable(reading) {
    const box = $("signalBox");
    if (!box) return;
    const rows = ((reading && reading.readings) || []).filter(
      (r) => r && r.symbol && r.interval === "15m"
    );
    if (!rows.length) {
      box.innerHTML = empty("等待读数", "运行器尚未落盘 latest_reading");
      return;
    }
    const maxHist = Math.max.apply(
      null,
      rows.map((r) => Math.abs(Number(r.MACD_HIST || 0))).concat([1])
    );
    box.innerHTML = rows
      .map((r) => {
        const k = Number(r.K || 0);
        const d = Number(r.D || 0);
        const hist = Number(r.MACD_HIST || 0);
        const cross = r.gold ? "金叉" : r.dead ? "死叉" : "—";
        const crossCls = r.gold ? "positive" : r.dead ? "negative" : "muted";
        const gate = hist > 0 ? "柱为正" : hist < 0 ? "柱为负" : "柱为 0";
        const gateCls = hist > 0 ? "positive" : hist < 0 ? "negative" : "muted";
        let result = r.position || "无信号";
        let cls = "muted";
        if (r.signal_long) { result = "做多"; cls = "positive"; }
        else if (r.signal_short) { result = "做空"; cls = "negative"; }
        else if (
          (r.gold && hist <= 0) || (r.dead && hist >= 0)
        ) { result = "方向背离 · 丢弃"; cls = "warn"; }
        const sid = STRAT_ID[`${r.symbol}|${r.interval}`] || r.symbol;
        const barW = Math.max(2, Math.round((Math.abs(hist) / maxHist) * 20));
        const kp = Math.max(0, Math.min(100, k));
        const dp = Math.max(0, Math.min(100, d));
        return `<div class="sig-row">
          <span class="sig-id">${esc(sid)}</span>
          <span class="sig-sym">${esc(coin(r.symbol))} <em>${esc(r.interval)}</em></span>
          <span class="sig-kdj">
            <span class="meter"><span class="fill" style="width:${kp.toFixed(1)}%"></span><i class="mk-k" style="left:${kp.toFixed(1)}%"></i><i class="mk-d" style="left:${dp.toFixed(1)}%"></i></span>
            <span class="num">${num(k, 2)} / ${num(d, 2)}</span>
          </span>
          <span class="sig-macd">
            <span class="mb"><i class="${hist >= 0 ? "p" : "n"}" style="width:${barW}px"></i></span>
            <span class="num ${hist >= 0 ? "positive" : "negative"}">${signed(hist, 4)}</span>
          </span>
          <span class="${crossCls}">${esc(cross)}</span>
          <span class="${gateCls}">${esc(gate)}</span>
          <span class="sig-result ${cls}">${esc(result)}<small>信号 K 线 ${esc(r.bar_utc || "—")} UTC</small></span>
        </div>`;
      })
      .join("");
  }

  function renderSwiss() {
    renderRiskStrip(swissReading, statusCache, summaryCache);
    renderAlertLadder(swissReading, statusCache, summaryCache);
    renderEquityCurve(swissEquity, summaryCache);
    renderSignalTable(swissReading);
  }

  async function loadSwissPanels() {
    // 只读本机落盘的读数与权益记录（不联网、不查交易所）。
    // 账户与交易状态复用 loadSummary / loadStatus 的结果，避免同一轮
    // 刷新把同一个币安接口查两遍而触发 429。
    const res = await Promise.allSettled([
      api("/api/strategy/reading"),
      api("/api/equity/curve"),
    ]);
    swissReading = res[0].status === "fulfilled" ? res[0].value : null;
    swissEquity = res[1].status === "fulfilled" ? res[1].value : null;
    renderSwiss();
  }

  async function loadMarketHero() {
    try {
      const m = await api("/api/market");
      if (m && m.ok) {
        if ($("mktSource")) {
          $("mktSource").textContent = (m.market_label || m.market || "Binance") + " · 标记价";
        }
        if ($("mktSpread")) {
          $("mktSpread").textContent = `${num(m.best_bid, 2)} / ${num(m.best_ask, 2)}`;
        }
        if ($("mktFunding")) {
          $("mktFunding").textContent =
            m.funding_rate_annualized == null ? "—" : pct(m.funding_rate_annualized, 2);
        }
        if ($("mktStamp")) $("mktStamp").textContent = fmtTime(m.event_time_ms);
      }
    } catch (e) { /* 保持原样 */ }
    // ETH 价格直接取运行器落盘的 ETH 读数，不再单独请求一次币安 K 线。
    const eth = ((swissReading && swissReading.readings) || []).find(
      (r) => r && r.symbol === "ETHUSDT"
    );
    if ($("ethMark")) {
      $("ethMark").textContent =
        eth && Number.isFinite(Number(eth.close)) ? num(eth.close, 2) : "—";
    }
    // 走势图来自 /api/chart（币安 K 线，后端有缓存）。60s 节流，
    // 避免每轮刷新都新增交易所请求。
    if (Date.now() - sparkFetchedAt < 60000) return;
    sparkFetchedAt = Date.now();
    try {
      const c = await api("/api/chart?symbol=BTCUSDT&interval=15m&bars=96");
      const bars = (c && c.bars) || [];
      const closes = bars.map((b) => Number(b.c)).filter(Number.isFinite);
      const drew = drawSpark($("mktSpark"), closes, "#4c8dff");
      if ($("mktRange")) {
        $("mktRange").textContent = drew
          ? `区间 ${num(Math.min.apply(null, closes), 2)} — ${num(Math.max.apply(null, closes), 2)} · ${closes.length} 根 15m`
          : "—";
      }
    } catch (e) { /* 保持原样 */ }
  }

  async function refreshTrading() {
    await Promise.allSettled([
      loadMarket(),
      loadStatus(),
      loadSummary(),
      loadOpenOrders(),
      loadStrategies(),
      loadRunner(),
      loadHistory(),
      loadSwissPanels().then(() => loadMarketHero()),
    ]);
    lastSyncAt = Date.now();
    renderSyncStamp();
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
  let lastSyncAt = 0;
  let clockTimer = null;

  function renderSyncStamp() {
    const el = $("liveStamp");
    if (!el) return;
    if (!lastSyncAt) {
      el.textContent = "正在取数…";
      el.className = "sync";
      return;
    }
    const age = Math.floor((Date.now() - lastSyncAt) / 1000);
    el.textContent = age <= 1 ? "刚刚更新" : `${age} 秒前更新`;
    el.className = "sync" + (age > (REFRESH_MS / 1000) * 2 ? " is-stale" : "");
  }

  function startTimer() {
    if (timer) clearInterval(timer);
    timer = setInterval(() => {
      if (currentView === "trading") refreshTrading();
      else if (currentView === "account") refreshAccount();
    }, REFRESH_MS);
    if (clockTimer) clearInterval(clockTimer);
    clockTimer = setInterval(renderSyncStamp, 1000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && currentView === "trading") refreshTrading();
    });
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
        else if (currentView === "account") refreshAccount();
        else if (window.ChartModule) window.ChartModule.refresh();
      });

    document.addEventListener("keydown", (e) => {
      if (
        e.key.toLowerCase() === "r" &&
        !["INPUT", "TEXTAREA"].includes(document.activeElement.tagName)
      ) {
        if (currentView === "trading") refreshTrading();
        else if (currentView === "account") refreshAccount();
        else if (window.ChartModule) window.ChartModule.refresh();
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
        const answer = window.prompt("这是非策略功能测试：将在币安测试网实际下单、立即平仓并留下多笔委托/成交，产生手续费。若仍要测试，请输入「测试单」：");
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
    const extResume = $("externalResumeBtn");
    if (extResume)
      extResume.addEventListener("click", async () => {
        const sym = extResume.dataset.symbol;
        if (!sym) return;
        const ok = window.confirm(
          `确认恢复 ${coin(sym)} 的自动开仓？\n运行器下一轮会恢复净仓同步，可能按策略账本重新建仓。`
        );
        if (!ok) return;
        await postAction("externalResumeBtn", "/api/external/resume",
                         "人工确认恢复", { symbol: sym });
        await loadStatus();
      });

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

  function renderExternal(map) {
    const bar = $("externalBar");
    if (!bar) return;
    const entries = Object.entries(map || {});
    if (!entries.length) {
      bar.hidden = true;
      return;
    }
    const paused = entries.filter(([, v]) => v && v.paused);
    const holds = entries.filter(([, v]) => v && v.hold && !v.paused);
    const lines = entries.map(([sym, v]) => {
      const when = v.detected_ms ? fmtTime(v.detected_ms) : "—";
      const what = v.last_kind === "increase" ? "人工加仓" : "人工减仓/平仓";
      return `${coin(sym)} ${what} · ${when} · 交易所净额 ${num(v.ex_before, 4)} → ${num(v.ex_after, 4)}`;
    });
    if ($("externalTitle")) {
      $("externalTitle").textContent = paused.length
        ? `已暂停自动开仓：${paused.map(([s]) => coin(s)).join(" · ")}`
        : `已跟随人工操作：${holds.map(([s]) => coin(s)).join(" · ")}`;
    }
    if ($("externalDetail")) {
      $("externalDetail").textContent =
        lines.join("；") +
        (paused.length
          ? "。确认后运行器会恢复该标的的自动开仓与净仓同步。"
          : "。该标的策略账本已清零，等下一根信号再开仓。");
    }
    const btn = $("externalResumeBtn");
    if (btn) {
      btn.hidden = !paused.length;
      btn.dataset.symbol = paused.length ? paused[0][0] : "";
    }
    bar.hidden = false;
  }

})();
