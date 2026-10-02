"""用户 2026-10-02 新规则：金叉/死叉即信号，不再要求 K<30/K>70。"""
import json
import pathlib
import unittest

from shadow.signals import crossing

ROOT = pathlib.Path(__file__).resolve().parents[1]


class CrossOnlyTests(unittest.TestCase):
    def test_golden_cross_even_when_k_above_70(self):
        self.assertEqual(crossing(81, 83, 86, 84), (True, False))

    def test_dead_cross_even_when_k_below_30(self):
        self.assertEqual(crossing(18, 16, 12, 14), (False, True))

    def test_no_cross_when_alignment_unchanged(self):
        self.assertEqual(crossing(73, 61, 75, 65), (False, False))

    def test_equal_previous_counts_as_cross(self):
        self.assertEqual(crossing(50, 50, 51, 50), (True, False))
        self.assertEqual(crossing(50, 50, 49, 50), (False, True))

    def test_nan_never_trades(self):
        self.assertEqual(crossing(float('nan'), 50, 51, 50), (False, False))

    def test_all_three_runners_use_same_signal(self):
        for name in ('engine.py', 'live.py'):
            source = (ROOT / 'shadow' / name).read_text(encoding='utf-8')
            self.assertIn('crossing(', source, name)
            self.assertNotIn('k[i] < K_LONG_MAX', source, name)
            self.assertNotIn('k[i] > K_SHORT_MIN', source, name)
        deploy_src = (ROOT / 'shadow' / 'deploy.py').read_text(encoding='utf-8')
        self.assertIn('entry_signal(', deploy_src)
        self.assertIn('confirmed_signal(', deploy_src)
        self.assertNotIn('k[i] < K_LONG_MAX', deploy_src)
        self.assertNotIn('k[i] > K_SHORT_MIN', deploy_src)

    def test_strategy_card_matches_actual_rules(self):
        cfg = json.loads((ROOT / 'config/strategies/deployed_kdj_extreme_v1.json').read_text(encoding='utf-8'))
        self.assertIn('涨破上一根最高', cfg['entry']['long'])
        self.assertIn('跌破上一根最低', cfg['entry']['short'])
        self.assertIn('下一根开盘', cfg['entry']['long'])
        self.assertEqual(cfg['position_sizing']['r'], 0.03)
        self.assertEqual(cfg['runtime_key'], 'kdj15')
        self.assertIn('GTX', cfg['execution_style']['description'])

    def test_5m_strategy_card_keeps_k_thresholds(self):
        cfg = json.loads((ROOT / 'config/strategies/deployed_kdj_5m_extreme_v1.json').read_text(encoding='utf-8'))
        self.assertIn('K<30', cfg['entry']['long'])
        self.assertIn('K>70', cfg['entry']['short'])
        self.assertEqual(cfg['runtime_key'], 'kdj5')
        self.assertEqual(cfg['execution_style']['tag'], 'kd5')
        self.assertEqual(cfg['position_sizing']['r'], 0.03)

    def test_eth_strategy_cards_match_btc_rules(self):
        eth15 = json.loads((ROOT / 'config/strategies/deployed_kdj_eth_extreme_v1.json').read_text(encoding='utf-8'))
        eth5 = json.loads((ROOT / 'config/strategies/deployed_kdj_eth_5m_extreme_v1.json').read_text(encoding='utf-8'))
        self.assertEqual(eth15['runtime_key'], 'eth15')
        self.assertEqual(eth15['symbol'], 'ETHUSDT')
        self.assertIn('涨破上一根最高', eth15['entry']['long'])
        self.assertIn('下一根开盘', eth15['entry']['long'])
        self.assertNotIn('K<30', eth15['entry']['long'])
        self.assertEqual(eth15['position_sizing']['r'], 0.03)
        self.assertEqual(eth5['runtime_key'], 'eth5')
        self.assertEqual(eth5['symbol'], 'ETHUSDT')
        self.assertIn('K<30', eth5['entry']['long'])
        self.assertIn('K>70', eth5['entry']['short'])
        self.assertEqual(eth5['execution_style']['tag'], 'e5')
        self.assertEqual(eth5['position_sizing']['r'], 0.03)


if __name__ == '__main__':
    unittest.main()
