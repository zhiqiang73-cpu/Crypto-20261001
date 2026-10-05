# 任务：BTC 量化面板前端 —— 全局字体加大 + 设置页精简（2026-10-05）

## 工作目录（只改这个目录里的文件）
`/Users/zengyun/我的AI/crypto/.work/rt-dev/crypto`

前端文件在 `frontend/` 子目录：`index.html` / `styles.css` / `app.js` / `chart.js`。
这是运行在 http://127.0.0.1:8787/ 的本机交易面板（深色 "Swiss Desk" 风格）。

## 背景
用户反馈：**总体字体太小、看不清楚**；设置页还有两块多余内容要删掉。

## 要求 1：全局字体加大（重点，系统性做）
在 `frontend/styles.css` 中系统性提高可读性，目标是"一眼看清"：
- 全局基准：`body` 的 font-size 从 12px 提到 13px。
- 所有 `font-size: 9px` → `11px`；所有 `font-size: 10px` → `11px`；
  `font-size: 11px` → `12px`；`font-size: 12px` → `13px`；
  面板标题 `.ph h2` 13px → 14px。
- 表格 `.grid` 11px → 12px、表头 10px → 11px；`.kv-row` 三列 11px → 12px；
  按钮 `.btn` 11px → 12px（`.btn.sm` 10px → 11px）；`.tag` 10px → 11px；
  `.chip`/`.src` 10px → 11px；ticker 小字同步 +1px；
  `.kpi` 的 `.k`/`.d` 10px → 11px、`.v` 18px → 19px；
  `.footnote` 10px → 11px；`.empty`、`.hl`、`.feed-item` 等同步 +1px。
- 为容纳新字号可同步微调高度/行高：`.form input` 34 → 36px、
  `.btn` min-height 28 → 30px、`.kv-row` min-height 31 → 33px、
  `.grid td` padding 9px → 10px；输入框/按钮字号同步。
- 不要改颜色体系、不要改布局结构（grid 列数不动）、不要引入新依赖。
- 必要时把 `body { min-width: 1080px }` 微调到 1120px，避免字号变大后挤压。

## 要求 2：设置页去掉两块
在 `frontend/index.html` 的「视图 4 · 设置」（`<div class="set-grid">`）里：
- 删除「连接状态与交易所设置」整节（含 `id="setConnKv"` 的那个 `<section>`）。
- 删除「运行参数」整节（含 `id="setParamsKv"` 与 `id="setParamsMeta"` 的那个 `<section>`）。
- **保留**「主网 API 凭据」节（`mainnet-zone`）不动，保持 span12 全宽。

同时修改 `frontend/app.js`：
- `renderSettings()` 中对 `setConnKv` / `setParamsKv` / `setParamsMeta` 的填充
  要加空守卫：元素不存在就直接跳过（绝不能因为元素被删而抛 JS 错误，否则
  整个面板脚本会中断）。参考文件里已有的写法：先 `const el = $("id")`，
  再 `if (!el) return/skip`；`kvRows` 与 `setText` 也可以统一加内层守卫。
- 其余逻辑不动。

## 约束
- 只改 `frontend/` 下的文件；不要动 `review/`、`trading/`、`shadow/` 等其他目录。
- **不要重启、停止任何服务**（面板服务在运行中，静态文件改动会立即生效）。
- **不要执行 git commit / git add**（保持工作区改动，由外部统一处理）。

## 完成后自检并在最终回复中报告
1) `grep -n "font-size: 9px\|font-size: 10px" frontend/styles.css` —— 列出残留并说明保留原因（主体文字不得低于 11px）。
2) `grep -n "setConnKv\|setParamsKv\|setParamsMeta" frontend/index.html` —— 应无结果。
3) `grep -n "setConnKv\|setParamsKv\|setParamsMeta" frontend/app.js` —— 应只剩带守卫的引用。
4) 统计 `index.html` 中 `<section` 与 `</section>` 数量是否配平。
5) 若本机有 node，执行 `node --check frontend/app.js` 验证 JS 语法；没有就跳过并说明。
6) 输出：改了哪些文件、每个文件的关键改动摘要、自检结果。
