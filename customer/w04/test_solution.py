"""青禾适配器的自写测试：期望值按访谈和规则手写，不从适配器结果反推。

覆盖：合法标签（含连字符和 2025 新增标签）、备注保留与触达限制、金额元转分、
隔离（孤儿、超额退款、契约外卡种、无效日期）、整行去重、门店编码规范化、重复运行一致。
"""
import importlib.util
import json
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RULES_DIR = ROOT / 'artifacts' / 'w04'
spec = importlib.util.spec_from_file_location('solution', HERE / 'solution.py')
solution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(solution)

RULES = yaml.safe_load((RULES_DIR / 'cleaning_rules.yaml').read_text(encoding='utf-8'))
CONTEXT = {'rules_dir': str(RULES_DIR), 'data_version': 'unit-test'}

MEMBER_FIELDS = ['hy_id', 'nm', 'mobile', 'birth', 'join_dt', 'create_tm', 'lv', 'stat', 'src_ch', 'reg_shop', 'reg_dev', 'tag']
ORDER_FIELDS = ['ord_no', 'hy_id', 'shop_cd', 'ord_tm', 'ord_stat', 'amt_total', 'amt_pay', 'amt_rfd', 'qty', 'ch']
CARD_FIELDS = ['card_no', 'hy_id', 'card_kind', 'st_dt', 'ed_dt', 'stop_dt', 'pay_amt']
SHOP_FIELDS = ['shop_cd', 'shop_nm', 'area', 'open_dt', 'shop_stat']


def table(filename, fields, rows):
    return {'fields': fields, 'rows': [{'source_file': filename, 'source_row': i, 'raw': dict(zip(fields, r))}
                                       for i, r in enumerate(rows, 1)]}


def member(hy_id, tag='', join='2025-03-16', stat='1', shop='9801', src='门店'):
    return [hy_id, '测试', '139****0000', '', join, '2025-03-16 10:00:00', '普通', stat, src, shop, 'abc', tag]


def order(no, hy_id, pay='79.90', rfd='0.00', stat='已支付', tm='2026-08-03 10:00:00', shop='9801'):
    return [no, hy_id, shop, tm, stat, '99.90', pay, rfd, '2', '门店']


def build(members=(), orders=(), cards=(), shops=(('9801', '杭州测试店', '华东', '2023-06-01', '营业'),)):
    return {
        'crm_shop.csv': table('crm_shop.csv', SHOP_FIELDS, [list(s) for s in shops]),
        'crm_member_export.csv': table('crm_member_export.csv', MEMBER_FIELDS, list(members)),
        'crm_order_export.csv': table('crm_order_export.csv', ORDER_FIELDS, list(orders)),
        'crm_card_export.csv': table('crm_card_export.csv', CARD_FIELDS, list(cards)),
    }


def run(tables):
    return solution.adapt(tables, RULES, CONTEXT)


def reasons(result):
    return sorted((q['record_type'], q['source_row'], q['reason_code']) for q in result['tables']['quarantine'])


class TagAndNoteTests(unittest.TestCase):
    def test_legal_tags_kept_including_hyphen_and_new_tags(self):
        r = run(build(members=[member('QH00000001', '母婴-0到3岁,小程序偏好;生鲜偏好 银发族')]))
        self.assertEqual([t['tag'] for t in r['tables']['member_tags']], ['母婴-0到3岁', '小程序偏好', '生鲜偏好', '银发族'])
        self.assertEqual(r['tables']['member_notes'], [])

    def test_unknown_content_becomes_note_with_full_source(self):
        raw = '价格敏感,联系状态-优先留微信消息,勿打电话-本人要求'
        r = run(build(members=[member('QH00000001', raw)]))
        self.assertEqual([t['tag'] for t in r['tables']['member_tags']], ['价格敏感'])
        notes = r['tables']['member_notes']
        self.assertEqual([(n['note_text'], n['category']) for n in notes],
                         [('联系状态-优先留微信消息', '联系状态'), ('勿打电话-本人要求', '触达限制')])
        for n in notes:
            self.assertEqual((n['raw_text'], n['source_field'], n['source_row'], n['source_file']),
                             (raw, 'tag', 1, 'crm_member_export.csv'))

    def test_every_contact_restriction_is_flagged(self):
        r = run(build(members=[member('QH00000001', '勿发短信-营销通知先停/电话暂时打不通'),
                               member('QH00000002', '周末家庭 勿打电话-仅保留到店服务')]))
        restricted = {n['member_id'] for n in r['tables']['member_notes'] if n['category'] == '触达限制'}
        self.assertEqual(restricted, {'QH00000001', 'QH00000002'})
        status = [n for n in r['tables']['member_notes'] if n['note_text'] == '电话暂时打不通']
        self.assertEqual(status[0]['category'], '联系状态')   # 小林：打不通是联系状态，不是限制

    def test_sentence_note_kept_whole_but_new_keyword_note_split(self):
        r = run(build(members=[member('QH00000001', '价格敏感,卡务，6月办的卡，店长知道。'),
                               member('QH00000002', '电话暂时打不通，客户关系-常在附近上班')]))
        texts = [n['note_text'] for n in r['tables']['member_notes']]
        self.assertIn('卡务，6月办的卡，店长知道。', texts)
        self.assertIn('电话暂时打不通', texts)
        self.assertIn('客户关系-常在附近上班', texts)

    def test_missing_marker_gives_no_tags_or_notes(self):
        r = run(build(members=[member('QH00000001', '-'), member('QH00000002', 'NULL')]))
        self.assertEqual((r['tables']['member_tags'], r['tables']['member_notes']), ([], []))


class MappingTests(unittest.TestCase):
    def test_money_yuan_to_cents(self):
        self.assertEqual(solution.cents_strict('54.91'), 5491)
        self.assertEqual(solution.cents_strict('199.00'), 19900)
        with self.assertRaises(solution.Reject):
            solution.cents_strict('0.001')

    def test_member_status_dates_and_store_code(self):
        r = run(build(members=[member('QH00000001', join='20230603', stat='0', shop=' ９８０１ ', src='系统导入')]))
        m = r['tables']['members'][0]
        self.assertEqual((m['registered_at'], m['account_status'], m['home_store_id'], m['registered_channel'], m['created_at']),
                         ('2023-06-03', '未激活', '9801', '系统导入', '2025-03-16'))

    def test_order_mapping(self):
        r = run(build(members=[member('QH00000001')], orders=[order('O1', 'QH00000001', stat='已取消', pay='0.00')]))
        o = r['tables']['orders'][0]
        self.assertEqual((o['status'], o['paid_cents'], o['gross_cents'], o['ordered_at']), ('cancelled', 0, 9990, '2026-08-03'))


class QuarantineTests(unittest.TestCase):
    def test_orphan_refund_and_invalid_date_are_quarantined_not_dropped(self):
        r = run(build(members=[member('QH00000001')],
                      orders=[order('O20260803000001', 'QH00000001'),
                              order('O20260803000002', 'QH99999999'),
                              order('O20260803000003', 'QH00000001', pay='26.78', rfd='34.81', stat='部分退款'),
                              order('O20260228000004', 'QH00000001', tm='2026-02-30 10:00:00')]))
        self.assertEqual(reasons(r), [('order', 2, 'UNLINKED_ACCOUNT'), ('order', 3, 'REFUND_EXCEEDS_PAID'), ('order', 4, 'INVALID_DATE')])
        q = r['tables']['quarantine'][0]
        self.assertEqual(json.loads(q['raw_json']), dict(zip(ORDER_FIELDS, order('O20260803000002', 'QH99999999'))))
        self.assertEqual(r['record_counts']['order'], {'read': 4, 'accepted': 1, 'quarantined': 3, 'deduplicated': 0})

    def test_exact_duplicate_deduplicated_and_counts_balance(self):
        row = order('O1', 'QH00000001')
        r = run(build(members=[member('QH00000001')], orders=[row, row]))
        self.assertEqual(r['record_counts']['order'], {'read': 2, 'accepted': 1, 'quarantined': 0, 'deduplicated': 1})
        for c in r['record_counts'].values():
            self.assertEqual(c['read'], c['accepted'] + c['quarantined'] + c['deduplicated'])

    def test_employee_card_quarantined_old_card_name_kept(self):
        cards = [['C1', 'QH00000001', '员工卡', '2023-08-14', '2024-08-14', '', '0.00'],
                 ['C2', 'QH00000001', '悦享卡（年）', '2023-01-01', '2024-01-01', 'NULL', '199.00']]
        r = run(build(members=[member('QH00000001')], cards=cards))
        self.assertEqual(reasons(r), [('card', 1, 'UNSUPPORTED_CARD_TYPE')])
        kept = r['tables']['cards'][0]
        self.assertEqual((kept['card_type'], kept['ended_at'], kept['card_paid_cents']), ('悦享卡（年）', '', 19900))


class RepeatTests(unittest.TestCase):
    def test_repeat_run_identical(self):
        tables = build(members=[member('QH00000001', '母婴-3到6岁/勿打电话-投诉过'), member('QH00000002', '夜间购物')],
                       orders=[order('O1', 'QH00000001'), order('O2', 'QH00000003')])
        first = json.dumps(run(tables), ensure_ascii=False, sort_keys=True)
        second = json.dumps(run(tables), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)


if __name__ == '__main__':
    unittest.main()
