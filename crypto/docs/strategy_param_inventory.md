# 策略参数清单与生产消费位置

敏感凭据（密钥、secrets）**不入**策略版本。日志路径/时间戳不参与 parameters_hash。

| 参数组 | 定义位置 | 生产使用 | 修复前版本化 | 修复后 |
|---|---|---|---|---|
| DIMENSION_WEIGHTS | config/weights.py | engine/scorer, live_loop, panel | 部分（ACTIVE flat） | parameters 全集 |
| DECISION_THRESHOLDS | weights.py | scorer | 部分 | 全集 |
| SAFETY_VALVE_THRESHOLD | weights.py | scorer | 是 | 是 |
| NEWS_SUB_WEIGHTS | weights.py | news_mapper | **否** | 是 |
| DATA_LAYER / ONCHAIN / DERIV / MICRO | weights.py | data_mapper | **否** | 是 |
| TECH_INDICATOR_WEIGHTS + ADX/BOLL | weights.py | tech_mapper | ADX 部分可调 | 全集 |
| PREDICTION_SUB_WEIGHTS | weights.py | prediction_mapper | **否** | 是 |
| COLLINEAR_GROUPS / ENABLE_AGREEMENT_BOOST | weights.py | utils/scoring, engine | **否** | 是 |
| MAPPING 锚点与关键标量 | config/mapping.py | mappers / scoring | **否** | parameters.MAPPING |
| RISK / MAX_NOTIONAL / LEVERAGE / POSITION_* | config/review.py | position_manager | **否** | 是 |
| EXIT_STRATEGY | review.py | exit_checker, guardian | **否** | 是 |
| STALENESS_LIMITS | review.py | executor, live_loop | **否** | 是 |
| PRETRADE_LIMITS | trading/pretrade.py | executor | **否** | 是 |
| TUNABLE_PARAMS 白名单 | review.py | AI 调参 | 仅白名单可自动改 | **不扩大** |

运行环境（面板端口、路径）与凭据保持环境配置，不进 strategy_identity。

实现身份文件见 `config/strategy_bundle.py` 中 `_IMPL_FILES`。
