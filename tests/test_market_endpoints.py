"""行情腿与下单腿的市场一致性测试。

这些测试存在的唯一理由：2026-10-02 的事故**绝不能再发生**。

事故回顾：`shadow/live.py` 硬编码主网 K 线，下单走测试网。
bot 用主网 KDJ 在测试网下单，信号错位 2 根 K 线（30 分钟），
同一笔空单毛利从 +103.5 点掉到 +37.0 点，扣手续费后净亏。

因此这里不只测「解析函数对不对」，更要钉死两条**结构性**约束：
  1. 实际生效的 K 线地址必须与账户地址同市场；
  2. 源码里不得再出现硬编码的主网行情地址。
"""

import os
import unittest

from config.market_endpoints import (MAINNET_REST, MAINNET_WS,
                                     MARKET_MAINNET, MARKET_TESTNET,
                                     MARKET_UNKNOWN, TESTNET_REST, TESTNET_WS,
                                     MarketMismatchError,
                                     assert_market_consistency,
                                     is_forbidden_host, market_of_url,
                                     resolve_market_endpoints)

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


class TestMarketClassification(unittest.TestCase):
    def test_mainnet_urls(self):
        self.assertEqual(market_of_url(MAINNET_REST), MARKET_MAINNET)
        self.assertEqual(market_of_url(MAINNET_WS), MARKET_MAINNET)
        self.assertEqual(market_of_url("https://fapi.binance.com/fapi/v1/klines"),
                         MARKET_MAINNET)

    def test_testnet_urls(self):
        self.assertEqual(market_of_url(TESTNET_REST), MARKET_TESTNET)
        self.assertEqual(market_of_url(TESTNET_WS), MARKET_TESTNET)

    def test_testnet_alias_hosts_are_testnet(self):
        # demo-fapi 与 legacy 实测返回完全相同的 K 线, 是同一个市场的两个域名
        self.assertEqual(market_of_url("https://demo-fapi.binance.com"),
                         MARKET_TESTNET)
        self.assertEqual(market_of_url("wss://demo-fstream.binance.com"),
                         MARKET_TESTNET)

    def test_unknown_host(self):
        self.assertEqual(market_of_url("https://example.com"), MARKET_UNKNOWN)

    def test_forbidden_host_is_flagged(self):
        # stream.binancefuture.com 实测报价与测试网 REST 不一致, 属于其他市场
        self.assertTrue(is_forbidden_host("wss://stream.binancefuture.com/stream"))
        self.assertFalse(is_forbidden_host(TESTNET_WS))


class TestResolve(unittest.TestCase):
    def test_testnet_account_resolves_to_testnet_market_data(self):
        ep = resolve_market_endpoints("https://testnet.binancefuture.com")
        self.assertEqual(ep.market, MARKET_TESTNET)
        self.assertEqual(ep.rest, TESTNET_REST)
        self.assertEqual(ep.ws, TESTNET_WS)
        self.assertEqual(ep.source, "account_base_url")
        self.assertTrue(ep.is_testnet)

    def test_mainnet_account_resolves_to_mainnet_market_data(self):
        ep = resolve_market_endpoints("https://fapi.binance.com")
        self.assertEqual(ep.market, MARKET_MAINNET)
        self.assertEqual(ep.rest, MAINNET_REST)
        self.assertEqual(ep.ws, MAINNET_WS)

    def test_demo_alias_account_resolves_to_testnet(self):
        ep = resolve_market_endpoints("https://demo-fapi.binance.com")
        self.assertEqual(ep.market, MARKET_TESTNET)
        self.assertEqual(ep.rest, TESTNET_REST)

    def test_forbidden_account_base_is_rejected(self):
        with self.assertRaises(MarketMismatchError):
            resolve_market_endpoints("wss://stream.binancefuture.com/stream")

    def test_klines_url_follows_market(self):
        testnet = resolve_market_endpoints("https://testnet.binancefuture.com")
        mainnet = resolve_market_endpoints("https://fapi.binance.com")
        self.assertTrue(testnet.klines_url("15m", limit=5)
                        .startswith(TESTNET_REST + "/fapi/v1/klines"))
        self.assertTrue(mainnet.klines_url("15m", limit=5)
                        .startswith(MAINNET_REST + "/fapi/v1/klines"))
        self.assertIn("interval=15m", testnet.klines_url("15m", limit=5))
        self.assertIn("limit=5", testnet.klines_url("15m", limit=5))


class TestConsistencyGate(unittest.TestCase):
    def test_same_market_passes(self):
        assert_market_consistency(TESTNET_REST, TESTNET_REST)
        assert_market_consistency(TESTNET_REST, TESTNET_WS)
        assert_market_consistency(MAINNET_REST, MAINNET_WS)

    def test_the_actual_2026_10_02_mismatch_is_blocked(self):
        """事故当天的真实组合: 测试网下单 + 主网 K 线。必须抛错。"""
        with self.assertRaises(MarketMismatchError) as ctx:
            assert_market_consistency(TESTNET_REST, MAINNET_REST)
        self.assertIn("不在同一个市场", str(ctx.exception))

    def test_reverse_mismatch_is_blocked(self):
        with self.assertRaises(MarketMismatchError):
            assert_market_consistency(MAINNET_REST, TESTNET_REST)

    def test_forbidden_market_url_is_blocked(self):
        with self.assertRaises(MarketMismatchError):
            assert_market_consistency(TESTNET_REST,
                                      "wss://stream.binancefuture.com/stream")

    def test_unknown_urls_are_blocked(self):
        with self.assertRaises(MarketMismatchError):
            assert_market_consistency("https://example.com", TESTNET_REST)
        with self.assertRaises(MarketMismatchError):
            assert_market_consistency(TESTNET_REST, "https://example.com")


class TestEffectiveRuntimeWiring(unittest.TestCase):
    """结构性约束: 真正跑起来的代码必须已经接上解析器。"""

    def test_shadow_live_klines_follow_account_market(self):
        """回归闸门 —— 这条测试若失败, 说明 2026-10-02 的 bug 回来了。

        `shadow.live` 实际生效的 K 线基准地址, 必须与账户地址同市场。
        """
        from shadow import live

        account = live.ENDPOINTS.account_base_url
        self.assertTrue(account, "账户地址缺失, 行情地址来源不可信")
        self.assertEqual(live.MARKET, market_of_url(account))
        self.assertEqual(market_of_url(live.BASE), market_of_url(account))
        assert_market_consistency(account, live.BASE)

    def test_shadow_live_is_testnet_under_testnet_mode(self):
        from shadow import live

        self.assertEqual(live.MARKET, MARKET_TESTNET)
        self.assertEqual(market_of_url(live.BASE), MARKET_TESTNET)

    def test_mapping_collectors_follow_account_market(self):
        from config import mapping
        from config.market_endpoints import resolve_for_account

        ep = resolve_for_account()
        self.assertEqual(market_of_url(mapping.BINANCE_FUTURES_REST), ep.market)
        self.assertEqual(market_of_url(mapping.BINANCE_FUTURES_WS), ep.market)

    def test_no_hardcoded_mainnet_market_data_in_source(self):
        """源码里不得再出现硬编码的主网行情地址。

        只检查**行情**用途的文件; 主网域名本身仍合法地出现在
        `config/market_endpoints.py`(地址表) 与 `trading/runtime_mode.py`(识别主网),
        以及测试里, 这些属于白名单。
        """
        watched = [
            "shadow/live.py",
            "config/mapping.py",
            "frontend/app.js",
        ]
        for rel in watched:
            path = os.path.join(ROOT, rel)
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            self.assertNotIn(
                "fapi.binance.com", src,
                f"{rel} 仍硬编码主网 REST 行情地址 —— 会造成行情腿与下单腿错位",
            )
            self.assertNotIn(
                "fstream.binance.com", src,
                f"{rel} 仍硬编码主网 WS 行情地址 —— 会造成行情腿与下单腿错位",
            )

    def test_frontend_does_not_fall_back_to_mainnet(self):
        path = os.path.join(ROOT, "frontend", "app.js")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("resolveMarketWs", src)
        self.assertIn("market_ws", src)


class TestMarketReport(unittest.TestCase):
    def test_report_shape(self):
        from config.market_endpoints import market_report

        rep = market_report()
        for key in ("market", "market_label", "rest", "ws", "source",
                    "trading_mode"):
            self.assertIn(key, rep)
        self.assertEqual(rep["market"], MARKET_TESTNET)
        self.assertEqual(rep["ws"], TESTNET_WS)


class TestSentimentFeedsAreSeparate(unittest.TestCase):
    """外部情绪数据恒为主网, 且必须与「交易市场」明确区分。

    `/futures/data/*`（多空比、持仓量历史）只有主网提供, 测试网返回 301。
    它不参与下单决策, 因此指向主网是**有意的** —— 但必须与交易市场分开命名,
    否则又会变成「两个市场混在一起」的模糊地带。
    """

    def test_sentiment_rest_is_mainnet(self):
        from config.market_endpoints import SENTIMENT_REST, sentiment_rest

        self.assertEqual(SENTIMENT_REST, MAINNET_REST)
        self.assertEqual(sentiment_rest(), MAINNET_REST)

    def test_mapping_keeps_sentiment_and_trading_base_separate(self):
        from config import mapping

        self.assertEqual(mapping.BINANCE_SENTIMENT_REST, MAINNET_REST)
        # 交易市场基准仍是测试网 —— 两者绝不可混用
        self.assertEqual(market_of_url(mapping.BINANCE_FUTURES_REST),
                         MARKET_TESTNET)
        self.assertNotEqual(mapping.BINANCE_SENTIMENT_REST,
                            mapping.BINANCE_FUTURES_REST)

    def test_derivatives_collector_routes_futures_data_to_sentiment_base(self):
        import inspect

        from collectors import free_derivatives

        src = inspect.getsource(free_derivatives.FreeDerivativesCollector)
        self.assertIn("self.sentiment_base}/futures/data", src)
        # /futures/data 不得再用交易市场基准拼接
        self.assertNotIn("self.rest_base}/futures/data", src)


if __name__ == "__main__":
    unittest.main()
