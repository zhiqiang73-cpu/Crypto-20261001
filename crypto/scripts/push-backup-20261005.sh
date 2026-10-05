#!/bin/bash
# 把本地全部分支备份推送到 GitHub 备份库 Crypto-20261001
# 全部为快进/新建分支：无覆盖、无删除。
# 用法: bash scripts/push-backup-20261005.sh
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"
echo "仓库: $(git rev-parse --show-toplevel)"
echo "提交: $(git log --oneline -1)"
echo
echo '== 1) deploy 分支（019ea6d→9243888 快进）=='
git push backup deploy/kdj-macd-pyramiding-20261003:refs/heads/deploy/kdj-macd-pyramiding-20261003
echo
echo '== 2) 归档分支 ×4（新建）=='
git push backup manus/15m-validation-eBYui9:refs/heads/manus/15m-validation-eBYui9
git push backup manus/order-safety-20261003:refs/heads/manus/order-safety-20261003
git push backup manus/portfolio-risk-audit-NaDEXVX9:refs/heads/manus/portfolio-risk-audit-NaDEXVX9
git push backup archive/task3-15m-validation-20261005:refs/heads/archive/task3-15m-validation-20261005
echo
echo '== 3) 真实交易线（含实盘接线）=='
git push backup feature/exchange-protective-stops-20261004:refs/heads/feature/exchange-protective-stops-20261004
git push backup feature/exchange-protective-stops-20261004:refs/heads/backup-real-trading-candidate-20261005-0927
echo
echo '== 完成：远程最新 =='
git ls-remote --heads backup | sed -n '1,25p'
echo
echo '全部完成。若中途报错请把输出发给我。'
