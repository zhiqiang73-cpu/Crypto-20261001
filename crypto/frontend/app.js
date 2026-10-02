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
  // 默认只看真实交易；功能测试单可随时切回查看（交易所记录不删不改）。
  let hideTests = true;

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

  const isTest = (row) => String(row.source || "") === "function_test";

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
      const pos = s.exchange_position || {};
      const side = String(pos.side || "FLAT").toUpperCase();
      const qty = Number(pos.quantity || pos.position_amt || 0);

      if ($("posSideTag")) {
        $("posSideTag").textContent = side === "FLAT" ? "FLAT" : side;
        $("posSideTag").className = "tag " + (side === "FLAT" ? "" : "safe");
      }
      if ($("posHint"))
        $("posHint").textContent = side === "FLAT" ? "无持仓" : `${side} ${qty}`;

      if ($("posBox")) {
        if (side === "FLAT" || !qty) {
          $("posBox").innerHTML = empty("空仓", "当前无持仓");
        } else {
          const upnl = Number(pos.unrealized_pnl ?? pos.unrealizedProfit ?? 0);
          const entry = Number(pos.entry_price ?? pos.entryPrice ?? 0);
          $("posBox").innerHTML = `
            <div class="kv-row"><span>方向</span><b>${esc(side)}</b></div>
            <div class="kv-row"><span>数量</span><b>${num(qty, 4)} BTC</b></div>
            <div class="kv-row"><span>开仓价</span><b>${num(entry, 2)}</b></div>
            <div class="kv-row"><span>未实现盈亏</span><b class="${pnlClass(upnl)}">${signed(upnl)} USDT</b></div>`;
        }
      }

      const recon = Boolean(s.reconciliation_needed);
      if ($("reconcileState")) {
        $("reconcileState").textContent = recon ? "需对账（已阻止开仓）" : "一致";
        $("reconcileState").className = recon ? "blocked" : "positive";
      }
      if ($("accountPosition"))
        $("accountPosition").textContent =
          side === "FLAT" ? "空仓" : `${side} ${num(qty, 4)}`;
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

      if ($("posBox") && s.position && s.position.side && s.position.side !== "FLAT") {
        const p = s.position;
        $("posBox").innerHTML = `
          <div class="kv-row"><span>方向</span><b>${esc(p.side)}</b></div>
          <div class="kv-row"><span>数量</span><b>${num(p.quantity, 4)} BTC</b></div>
          <div class="kv-row"><span>开仓价</span><b>${num(p.entry_price, 2)}</b></div>
          <div class="kv-row"><span>未实现盈亏</span><b class="${pnlClass(s.unrealized_pnl)}">${signed(s.unrealized_pnl)} USDT</b></div>`;
      }
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
          (o) => `<div class="kv-row"><span>${esc(o.side)} ${esc(o.type || "")}</span>
            <b>${num(o.price, 2)} × ${num(o.origQty ?? o.quantity, 4)}</b></div>`
        )
        .join("");
    } catch (e) {
      /* 保持原样 */
    }
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
      const mk =
        d.market === "testnet" ? "测试网" : d.market === "mainnet" ? "主网" : d.market;
      if ($("readingMarketTag")) {
        $("readingMarketTag").textContent = `${mk} · ${d.interval || ""} · ${d.bar_utc || ""} UTC`;
      }
      if ($("runnerState")) $("runnerState").textContent = runnerStatusText(d.runner);
      const f = (v, n) => (v === null || v === undefined || Number.isNaN(Number(v)) ? "—" : Number(v).toFixed(n));
      const cells = [
        ["市场", mk],
        ["K 线 OHLC", `${f(d.open, 2)} / ${f(d.high, 2)} / ${f(d.low, 2)} / ${f(d.close, 2)}`],
        ["K", f(d.K, 2)],
        ["D", f(d.D, 2)],
        ["ATR_1H", f(d.ATR_1H, 2)],
        ["持仓", d.position || "—"],
        ["做多信号", d.signal_long ? "是" : "否"],
        ["做空信号", d.signal_short ? "是" : "否"],
        ["停机漏过 K 线", `${d.missed_bars ?? 0} 根`],
        ["其中带交叉信号", `${d.missed_signals ?? 0} 根`],
        ["K 线数据源", d.kline_url || "—"],
        ["账户地址", d.account_base_url || "—"],
        ["行情 WS", d.ws || "—"],
        ["更新于", d.updated_ms ? fmtTime(d.updated_ms) : "—"],
      ];
      box.innerHTML = cells
        .map(([k, v]) => `<div class="kv-row"><span>${esc(k)}</span><b>${esc(String(v))}</b></div>`)
        .join("");
    } catch (e) {
      box.innerHTML = empty("读数读取失败", String(e.message || e));
    }
  }

  async function loadStrategies() {
    const box = $("strategyList");
    if (!box) return;
    try {
      const d = await api("/api/strategies/active");
      const list = d.strategies || [];
      if (!list.length) {
        box.innerHTML = empty("暂无策略", "config/strategies 为空");
        return;
      }
      box.innerHTML = list
        .map((s) => {
          const on = !!s.enabled;
          const rt = s.runtime || {};
          const e = s.entry || {};
          const ps = s.position_sizing || {};
          const risk = s.risk || {};
          const rows = [
            ["做多", e.long],
            ["做空", e.short],
            ["出场", s.exit?.mode_a || s.exit?.rule],
            ["仓位", ps.formula],
            ["杠杆", risk.leverage ? risk.leverage + "x 逐仓" : null],
          ]
            .filter(([, v]) => v)
            .map(
              ([k, v]) =>
                `<div class="kv-row"><span>${esc(k)}</span><b>${esc(v)}</b></div>`
            )
            .join("");
          const live = on
            ? `<div class="strategy-stats">
                 <div><small>周期</small><b>${esc(s.timeframe || "—")}</b></div>
                 <div><small>最近K线</small><b>${rt.last_ts ? fmtTime(rt.last_ts) : "—"}</b></div>
                 <div><small>熔断</small><b>${rt.halted ? "已熔断" : "正常"}</b></div>
               </div>`
            : `<div class="strategy-stats">
                 <div><small>周期</small><b>${esc(s.timeframe || "—")}</b></div>
                 <div><small>状态</small><b>未启用</b></div>
                 <div><small>来源</small><b>${esc(s.source || "—")}</b></div>
               </div>`;
          return `<article class="strategy-card${on ? "" : " dim"}">
            <div class="strategy-head">
              <span class="strategy-icon ${on ? "purple" : "blue"}">${on ? "ON" : "OFF"}</span>
              <div><h3>${esc(s.name || s.strategy_id)}</h3><p>${esc(s.kind || "")} · ${esc(s.symbol || "BTCUSDT")} · ${esc(s.market || "")}</p></div>
              <span class="tag ${on ? "safe" : "paused"}">${on ? "配置已启用" : "未启用"}</span>
            </div>
            ${live}
            <div class="kv-list">${rows}</div>
          </article>`;
        })
        .join("");
    } catch (e) {
      box.innerHTML = empty("策略读取失败", String(e.message || e));
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

  function renderSourceSummary(counts, hidden) {
    const el = $("sourceSummary");
    if (!el) return;
    if (!counts) {
      el.textContent = "来源统计不可用";
      return;
    }
    const parts = ["strategy", "manual_web", "function_test", "user_action", "unknown"]
      .filter((k) => counts[k])
      .map((k) => `${SOURCE_ZH[k]} ${counts[k]}`);
    el.textContent =
      "来源统计（最近 7 天）：" +
      (parts.join(" · ") || "无记录") +
      (hidden ? ` · 已隐藏测试 ${hidden} 笔` : "");
  }

  async function loadOrders() {
    const box = $("histOrdersBox");
    if (!box) return;
    try {
      const d = await api("/api/binance/orders?limit=200");
      if (!d.connected) {
        if ($("histOrdersTag")) $("histOrdersTag").textContent = "未连接";
        box.innerHTML = empty("币安未连接", d.reason || "请到账户连接页配置密钥");
        return;
      }
      const list = (d.orders || []).slice().reverse();
      const shown = hideTests ? list.filter((o) => !isTest(o)) : list;
      const hidden = list.length - shown.length;
      renderSourceSummary(d.source_counts, hidden);
      if ($("histOrdersTag")) {
        $("histOrdersTag").textContent =
          shown.length + " 条" + (hidden ? ` · 隐藏测试 ${hidden}` : "");
        if (d.truncated) $("histOrdersTag").textContent += " · 上限";
      }
      if (!shown.length) {
        box.innerHTML = empty(
          hideTests ? "没有真实交易委托" : "暂无历史委托",
          hideTests ? "当前只显示非测试单；可取消勾选查看全部" : "币安返回空列表"
        );
        return;
      }
      box.innerHTML = shown
        .map(
          (o) => `<div class="table-row cols-o8">
            <span>${fmtTime(o.time ?? o.updateTime)}</span>
            <span class="${o.side === "BUY" ? "positive" : "negative"}">${esc(o.side === "BUY" ? "买入" : "卖出")}<small>${esc(o.type)} · ${esc(o.timeInForce || "—")}</small></span>
            <span class="mono id-cell">${esc(o.orderId)}</span>
            <span>${sourceCell(o)}</span>
            <span>${Number(o.price) ? num(o.price, 2) : "市价"}<small>成交均价 ${Number(o.avgPrice) ? num(o.avgPrice, 2) : "—"}</small></span>
            <span>${num(o.origQty, 4)}</span>
            <span>${num(o.executedQty, 4)}</span>
            <span>${esc(ORDER_STATUS_ZH[o.status] || o.status)}</span>
          </div>`
        )
        .join("");
    } catch (e) {
      box.innerHTML = empty("历史委托读取失败", String(e.message || e));
    }
  }

  async function loadTrades() {
    const box = $("histTradesBox");
    if (!box) return;
    try {
      const d = await api("/api/binance/trades?limit=200");
      if (!d.connected) {
        if ($("histTradesTag")) $("histTradesTag").textContent = "未连接";
        box.innerHTML = empty("币安未连接", d.reason || "请到账户连接页配置密钥");
        return;
      }
      const list = (d.trades || []).slice().reverse();
      const shown = hideTests ? list.filter((t) => !isTest(t)) : list;
      const hidden = list.length - shown.length;
      if ($("histTradesTag")) {
        $("histTradesTag").textContent =
          shown.length + " 条" + (hidden ? ` · 隐藏测试 ${hidden}` : "");
        if (d.truncated) $("histTradesTag").textContent += " · 上限";
      }
      if (!shown.length) {
        box.innerHTML = empty(
          hideTests ? "没有真实交易成交" : "暂无历史成交",
          hideTests ? "当前只显示非测试单；可取消勾选查看全部" : "币安返回空列表"
        );
        return;
      }
      box.innerHTML = shown
        .map((t) => {
          const p = Number(t.realizedPnl || 0);
          return `<div class="table-row cols-t7">
            <span>${fmtTime(t.time)}</span>
            <span class="mono id-cell">${esc(t.orderId)}<small>成交 ${esc(t.id)}</small></span>
            <span>${sourceCell(t)}</span>
            <span class="${t.side === "BUY" ? "positive" : "negative"}">${esc(t.side === "BUY" ? "买入" : "卖出")}<small>${t.maker ? "Maker" : "Taker"}</small></span>
            <span>${num(t.price, 2)}<small>× ${num(t.qty, 4)} BTC</small></span>
            <span class="${pnlClass(p)}">${signed(p, 4)}</span>
            <span>${num(t.commission, 4)}</span>
          </div>`;
        })
        .join("");
    } catch (e) {
      box.innerHTML = empty("历史成交读取失败", String(e.message || e));
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
      loadOrders(),
      loadTrades(),
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

    const hideToggle = $("hideTestToggle");
    if (hideToggle) {
      hideToggle.checked = hideTests;
      hideToggle.addEventListener("change", () => {
        hideTests = !!hideToggle.checked;
        loadOrders();
        loadTrades();
      });
    }

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
