"""第 8 步：人工追踪一条合法记录和一条失败记录，从原始 CSV 追到清洗结果和两次导入的库。"""
import json
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, 'platform')
from fde_product.w04.io import read_inputs

RUN = Path('.runtime/w04/run-01')
SOURCE = 'crm_order_export.csv'
src = read_inputs('materials/data/qinghe-w04-v1.1')[SOURCE]['rows']
cleaned = read_inputs(RUN / 'adapt' / 'cleaned')
result = []
for row_no in (1, 2851):
    raw = src[row_no - 1]['raw']
    order_id = raw['ord_no']
    in_orders = [i for i, r in enumerate(cleaned['orders.csv']['rows'], 1) if r['raw']['order_id'] == order_id]
    in_quarantine = [(i, r['raw']['reason_code']) for i, r in enumerate(cleaned['quarantine.csv']['rows'], 1)
                     if r['raw']['source_file'] == SOURCE and r['raw']['source_row'] == str(row_no)]
    database = {}
    for project in ('project-a', 'project-b'):
        con = duckdb.connect(str(RUN / project / 'data.duckdb'), read_only=True)
        try:
            database[project] = {
                'raw_orders': con.execute('select order_id, member_id, store_id, ordered_at, status, gross_cents, paid_cents, refund_cents '
                                          'from raw_orders where order_id = ?', [order_id]).fetchall(),
                'raw_quarantine': con.execute('select reason_code from raw_quarantine where source_file = ? and source_row = ?',
                                              [SOURCE, row_no]).fetchall()}
        finally:
            con.close()
    result.append({'source_file': SOURCE, 'source_row': row_no, 'raw_record': raw,
                   'cleaned_orders_record': in_orders, 'cleaned_orders_row': cleaned['orders.csv']['rows'][in_orders[0] - 1]['raw'] if in_orders else None,
                   'quarantine_record': in_quarantine, 'database': database})
Path('artifacts/w04/trace_records.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
print(json.dumps(result, ensure_ascii=False, indent=2, default=str))