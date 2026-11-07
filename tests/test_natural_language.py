import json
import unittest
from decimal import Decimal

from astrbot_plugin_portfolio.services.natural_language import parse_model_response


def response(action='BUY', symbol='NTT', quantity=200, price='165.7', status='valid'):
    return json.dumps({
        'status': status,
        'action': action if status == 'valid' else None,
        'symbol': symbol if status == 'valid' else None,
        'quantity': quantity if status == 'valid' else None,
        'unit_price': price if status == 'valid' else None,
    }, ensure_ascii=False)


class NaturalLanguageParserTests(unittest.TestCase):
    def test_ntt_alias_and_decimal(self):
        p = parse_model_response(response())
        self.assertEqual((p.symbol, p.currency, p.quantity), ('9432.T', 'JPY', 200))
        self.assertEqual(p.unit_price, Decimal('165.7'))

    def test_known_aliases_and_fence(self):
        p = parse_model_response('```json\n'+response('SET','亚瑟士',100,'4564')+'\n```')
        self.assertEqual((p.symbol, p.currency), ('7936.T', 'JPY'))
        self.assertEqual(p.action, 'SET')

    def test_model_rejection_no_trade(self):
        with self.assertRaisesRegex(ValueError, '无法确定'):
            parse_model_response(response(status='reject'))

    def test_reject_strange_payloads(self):
        for raw in ['[]','{}','Hello world','<script>BUY</script>','{"status":"valid"}',
                    response().replace('"valid"','"root"')]:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_model_response(raw)

    def test_reject_hallucinated_extra_parameters(self):
        payload = json.loads(response())
        payload['owner_id'] = 'target-person'
        with self.assertRaises(ValueError):
            parse_model_response(json.dumps(payload))

    def test_reject_boolean_quantity(self):
        with self.assertRaisesRegex(ValueError, '整数'):
            parse_model_response(response(quantity=True))

    def test_reject_unsafe_prices(self):
        for price in [165.7,'1e999','NaN','-1','100,000','1.123456789']:
            with self.subTest(price=price), self.assertRaises(ValueError):
                parse_model_response(response(price=price))

    def test_reject_negative_or_zero_buy(self):
        with self.assertRaises(ValueError):
            parse_model_response(response(quantity=0))
        with self.assertRaises(ValueError):
            parse_model_response(response(quantity=-20))

    def test_set_zero_zero_allowed(self):
        p = parse_model_response(response('SET','9432.T',0,'0'))
        self.assertEqual(p.quantity, 0)

    def test_many_symbols_rejected(self):
        with self.assertRaises(ValueError):
            parse_model_response(response(symbol='NTT,ASICS'))

    def test_cannot_guess_chinese_unknown_company(self):
        with self.assertRaises(ValueError):
            parse_model_response(response(symbol='某日本公司'))

    def test_json_without_fences_only(self):
        with self.assertRaises(ValueError):
            parse_model_response('分析结果如下：\n'+response())


if __name__ == '__main__':
    unittest.main()
