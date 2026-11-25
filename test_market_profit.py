from datetime import date
from decimal import Decimal
import unittest
from unittest.mock import Mock, patch

import market_profit as profit
from test_mobile import m


class MarketProfitTests(unittest.TestCase):
    def test_nbu_rate_requires_current_official_value(self):
        response = Mock(status_code=200)
        response.json.return_value = [{'cc': 'USD', 'r030': 840, 'rate_per_unit': 44.12, 'units': 1,
                                      'exchangedate': '25.11.2026'}]
        with patch.object(profit.requests, 'get', return_value=response) as request:
            data = profit._retrieve_rate(1000, date(2026, 11, 25))
            self.assertEqual(data['value'], '44.12')
            self.assertEqual(data['effective_on'], '2026-11-25')
            self.assertEqual(data['source'], profit.RATE_URL)
            self.assertIsNone(profit._retrieve_rate(1001, date(2026, 12, 1)))
            request.assert_called_with(profit.RATE_URL, params={
                'valcode': 'usd', 'start': '20261128', 'end': '20261201', 'sort': 'exchangedate', 'order': 'desc', 'json': '',
            }, timeout=(2, 4), allow_redirects=False)

    def test_sold_and_asking_prices_have_separate_evidence_and_counts(self):
        urls = [f'https://www.ebay.com/itm/{number}' for number in (123456789, 223456789, 323456789)]
        lots = [dict(url=url, status='ACTIVE', price='900', currency='USD',
                     checked_on='2026-11-25', match_evidence='Exact part number') for url in urls]
        lots[1].update(status='SOLD', sold_date='2026-11-23', sale_evidence='Sold listing', price='110')
        lots[2].update(status='SOLD', sold_date='2026-11-23', sale_evidence='', price='1')
        evidence = profit.currency_evidence(lots, urls, 'USD', date(2026, 11, 25))
        self.assertEqual(evidence['value'], '110')
        self.assertEqual(evidence['kind'], 'REPORTED_SOLD')
        self.assertEqual(evidence['sold_count'], 1)
        self.assertEqual(evidence['asking_count'], 1)
        self.assertEqual(evidence['sources'], [urls[1]])

    def test_intermediate_profit_when_purchase_price_and_costs_are_unknown(self):
        values = {'us_price_usd': '100', 'fx_uah_per_usd': '40', 'us_fee_pct': '13',
                  'us_fixed_fee': '.4', 'us_postage_uah': '200', 'ai_cost_usd': '0.15'}
        result = profit.projection(values, 'us')
        self.assertEqual(result['net'], Decimal('3258'))
        self.assertIn('cost', result['missing'])
        self.assertIn('tax_uah', result['missing'])
        self.assertNotIn('ai', result['missing'])
        self.assertIsNone(result['roi'])

    def test_market_page_accepts_partial_inputs_and_clears_legacy_overrides(self):
        client = m.app.test_client()
        with client.session_transaction() as session:
            session.update(picker_session_id='test-profit', hold_csrf='fixture-token')
        rate = {'value': '40', 'effective_on': '2026-11-25'}
        with (patch.object(m, 'official_usd_rate', return_value=rate),
              patch.object(m, '_p051_restore_missing_runtime_state', return_value=None),
              patch.object(m, '_restore_active_lot_checkpoint', return_value=False),
              patch.object(m, '_save_active_lot_checkpoint', return_value=True),
              patch.dict(m.MARKET_COMPARISON_CACHE, {'test-profit': {'inputs': {'us_fixed_fee_usd': '0.4'}}})):
            starting = client.get('/markets/compare').get_data(as_text=True)
            self.assertIn('name="us_fixed_fee" inputmode="decimal" value="0.4"', starting)
            self.assertIn('Офіційний курс НБУ на 2026-11-25', starting)
            response = client.post('/markets/compare', data={
                'hold_csrf': 'fixture-token', 'us_price_usd': '100', 'us_fixed_fee': '',
            })
            body = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn('Після відомих витрат', body)
            self.assertIn('4000.00 грн', body)
            self.assertIn('Собівартість', body)
            self.assertNotIn('За цим сценарієм вигідніше', body)
