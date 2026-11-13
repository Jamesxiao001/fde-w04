"""青禾 W04 客户层：剖析（profile）、适配（adapt）、对账（reconcile）。

课程工具只负责读写文件和导入；字段含义、异常判断、清洗决定都在这里。
约定：
- 原始记录只读，`row['raw']` 永远不改；
- 所有输出按确定顺序生成（不依赖 set 的迭代顺序），保证重复运行逐字节一致；
- 工作账两个文件可能不存在（公开样本只有 4 个 CRM 文件），用到前先判断。
"""
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import re
import unicodedata
from pathlib import Path

CONTRACT = 'w04-0.1.0'
MISSING = ('', 'NULL', '-', '无')          # 公开预检的缺失口径
SAMPLE = 20                                 # 证据里最多列多少条记录序号

FILES = {
    'member': 'crm_member_export.csv',
    'order': 'crm_order_export.csv',
    'card': 'crm_card_export.csv',
    'shop': 'crm_shop.csv',
    'revisit': '小林工作账_回访名单.csv',
    'dnd': '小林工作账_勿扰名单.csv',
}
KEYS = {'member': 'hy_id', 'order': 'ord_no', 'card': 'card_no', 'shop': 'shop_cd'}
DATE_FIELDS = {'member': ['birth', 'join_dt'], 'card': ['st_dt', 'ed_dt', 'stop_dt'], 'shop': ['open_dt']}
DATETIME_FIELDS = {'member': ['create_tm'], 'order': ['ord_tm']}
MONEY_FIELDS = {'order': ['amt_total', 'amt_pay', 'amt_rfd'], 'card': ['pay_amt']}
DATE_FORMATS = ('%Y-%m-%d', '%Y/%m/%d', '%Y%m%d')
DATETIME_FORMATS = ('%Y-%m-%d %H:%M:%S',)
TAG_SPLIT = re.compile(r'[,，;；/ ]+')
NEW_NOTE_START = re.compile(r'^[^\s\-，,]{1,4}-')       # “联系状态-”“卡务-”“VIP-”这类新备注的开头


# ---------------------------------------------------------------- 通用小工具

def is_missing(value):
    return value in MISSING


def parse_date(value, formats=DATE_FORMATS):
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return None


def norm_code(value):
    """门店编码：全角转半角、去首尾空格、字母转大写（录入写法问题，不改含义）。"""
    return unicodedata.normalize('NFKC', value).strip().upper()


def to_decimal(value):
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def rows_of(tables, kind):
    table = tables.get(FILES[kind])
    return table['rows'] if table else []


def has_field(tables, kind, field):
    table = tables.get(FILES[kind])
    return bool(table) and field in table['fields']


def evidence(kind_or_file, rows, query=None):
    source = FILES.get(kind_or_file, kind_or_file)
    item = {'source_file': source, 'sample_rows': sorted(rows)[:SAMPLE]}
    if query:
        item['query'] = query
    return item


def infer_type(present):
    if not present:
        return 'empty'
    if all(re.fullmatch(r'-?\d+', v) for v in present):
        return 'integer'
    if all(re.fullmatch(r'-?\d+(\.\d+)?', v) for v in present):
        return 'decimal'
    if all(parse_date(v, DATETIME_FORMATS) for v in present):
        return 'datetime'
    if all(parse_date(v) for v in present):
        return 'date'
    return 'string'


# ---------------------------------------------------------------- 数据字典解析

def parse_dictionary(text):
    """把字典里的 markdown 表格解析成 {表名: {字段: {name,type,desc}}}，另取附录标签表。

    公开样本的字典只有一段文字，没有表格，此时返回空结构，后面的比对改用关键词兜底。
    """
    tables, tags, current, in_tags = {}, [], None, False
    for line in text.splitlines():
        heading = re.match(r'^#+\s*(.*)$', line)
        if heading:
            title = heading.group(1)
            found = re.search(r'[（(]\s*(crm_[a-z_]+)\s*[)）]', title)
            current = found.group(1) if found else None
            in_tags = '标签' in title and not found
            continue
        if not line.strip().startswith('|'):
            continue
        cells = [c.strip().strip('`') for c in line.strip().strip('|').split('|')]
        if not cells or set(cells[0]) <= set('-: ') or cells[0] in ('字段', '标签'):
            continue
        if current and len(cells) >= 4:
            tables.setdefault(current, {})[cells[0]] = {'name': cells[1], 'type': cells[2], 'desc': cells[3]}
        elif in_tags:
            tags.append(cells[0])
    return tables, tags


def listed_values(desc):
    """'门店、小程序、商城' -> 列表；'1 普通，2 银卡，3 金卡' -> {'1':'普通',...}。"""
    codes = dict(re.findall(r'(\d+)\s*([^\d，,、\s]+)', desc))
    if len(codes) >= 2:
        return 'codes', codes
    if '、' in desc and not re.search(r'\d', desc):
        return 'values', [v.strip() for v in desc.split('、') if v.strip()]
    return None, None


# ---------------------------------------------------------------- 第 3 步：剖析

def field_statistics(tables):
    fields = []
    for filename in sorted(tables):
        table = tables[filename]
        for field in table['fields']:
            values = [row['raw'][field] for row in table['rows']]
            present = [v for v in values if not is_missing(v)]
            kind = infer_type(present)
            counts = Counter(values)
            stat = {'source_file': filename, 'field': field, 'inferred_type': kind,
                    'total_count': len(values), 'missing_count': len(values) - len(present)}
            if 0 < len(counts) <= 30:
                stat['distribution'] = [{'value': v, 'count': n} for v, n in sorted(counts.items())]
            else:
                if kind in ('integer', 'decimal'):
                    numbers = [Decimal(v) for v in present]
                    lo, hi = (str(min(numbers)), str(max(numbers))) if numbers else (None, None)
                else:
                    lo, hi = (min(present), max(present)) if present else (None, None)
                stat['range'] = {'min': lo, 'max': hi}
            fields.append(stat)
    return fields


def find_anomalies(tables):
    """每条异常带记录序号（不含表头、从 1 起）。只描述数据里发生了什么，原因留给访谈核实。"""
    out = []

    def add(pattern, kind, rows, query=None):
        if rows:
            out.append({'pattern': pattern, 'evidence': evidence(kind, rows, query)})

    members = rows_of(tables, 'member')
    orders = rows_of(tables, 'order')
    cards = rows_of(tables, 'card')
    shops = rows_of(tables, 'shop')
    member_ids = {r['raw']['hy_id'] for r in members}
    shop_ids = {norm_code(r['raw']['shop_cd']) for r in shops}

    # 1) 完全重复行 与 主键冲突（四张 CRM 表都查）
    for kind in ('member', 'order', 'card', 'shop'):
        rows = rows_of(tables, kind)
        if not rows:
            continue
        first_seen, full_dup, key_rows = {}, [], defaultdict(list)
        for row in rows:
            content = tuple(sorted(row['raw'].items()))
            if content in first_seen:
                full_dup.append(row['source_row'])
            else:
                first_seen[content] = row['source_row']
                key_rows[row['raw'].get(KEYS[kind], '').strip()].append(row)
        if full_dup:
            near_page = sum(1 for n in full_dup if min(n % 1000, 1000 - n % 1000) <= 5)
            add(f'完全重复行（duplicate）：{len(full_dup)} 条与前面某条逐字段相同；'
                f'其中 {near_page} 条位于每 1000 条的分页边界附近，疑为分页导出重叠', kind, full_dup)
        conflict = [r['source_row'] for k, rs in key_rows.items() if len(rs) > 1 for r in rs]
        add(f'主键冲突：{KEYS[kind]} 相同但内容不同 {len(conflict)} 条', kind, conflict)

    # 2) 关联：孤儿订单／孤儿卡／门店关联不到
    if members:
        add(f'孤儿订单（orphan）：订单会员号在会员表中关联不到，{sum(1 for r in orders if r["raw"]["hy_id"] not in member_ids)} 条',
            'order', [r['source_row'] for r in orders if r['raw']['hy_id'] not in member_ids])
        add('孤儿会员卡（orphan）：卡的会员号在会员表中关联不到',
            'card', [r['source_row'] for r in cards if r['raw']['hy_id'] not in member_ids])
    if shops:
        lost = [r for r in orders if norm_code(r['raw']['shop_cd']) not in shop_ids]
        codes = Counter(norm_code(r['raw']['shop_cd']) for r in lost)
        add(f'孤儿关联：订单成交门店规范化后仍在门店表中关联不到 {len(lost)} 条，门店编码 {dict(sorted(codes.items()))}',
            'order', [r['source_row'] for r in lost])
        lost_m = [r for r in members if norm_code(r['raw']['reg_shop']) not in shop_ids]
        codes_m = Counter(norm_code(r['raw']['reg_shop']) for r in lost_m)
        add(f'孤儿关联：会员注册门店规范化后仍在门店表中关联不到 {len(lost_m)} 条，门店编码 {dict(sorted(codes_m.items()))}',
            'member', [r['source_row'] for r in lost_m])

    # 3) 门店编码写法：首尾空格、字母前缀
    for kind, field in (('order', 'shop_cd'), ('member', 'reg_shop'), ('shop', 'shop_cd')):
        rows = rows_of(tables, kind)
        odd = [r for r in rows if r['raw'][field] != norm_code(r['raw'][field])]
        shapes = Counter('首尾空格' if r['raw'][field] != r['raw'][field].strip() else
                         ('全角字符' if r['raw'][field] != unicodedata.normalize('NFKC', r['raw'][field]) else '小写字母')
                         for r in odd)
        add(f'门店编码写法不规范 {field}：{len(odd)} 条 {dict(sorted(shapes.items()))}，'
            f'全角转半角、去空格、转大写后才能和门店表对上', kind, [r['source_row'] for r in odd])
    lettered = [r['source_row'] for r in shops if not re.fullmatch(r'\d{4}', norm_code(r['raw']['shop_cd']))]
    add(f'门店编码不是 4 位数字（带字母前缀）：{len(lettered)} 家', 'shop', lettered)

    # 4) 金额：退款超过实付、实付超过原价、带小数
    def cents(value):
        number = to_decimal(value)
        return None if number is None else number * 100
    over = [r['source_row'] for r in orders
            if cents(r['raw']['amt_rfd']) is not None and cents(r['raw']['amt_pay']) is not None
            and cents(r['raw']['amt_rfd']) > cents(r['raw']['amt_pay'])]
    add(f'超额退款：累计退款金额大于实付金额 {len(over)} 条', 'order', over)
    over_total = [r['source_row'] for r in orders
                  if cents(r['raw']['amt_pay']) is not None and cents(r['raw']['amt_total']) is not None
                  and cents(r['raw']['amt_pay']) > cents(r['raw']['amt_total'])]
    add(f'实付金额大于原价金额 {len(over_total)} 条', 'order', over_total)
    for kind, fields in MONEY_FIELDS.items():
        for field in fields:
            rows = [r['source_row'] for r in rows_of(tables, kind) if '.' in r['raw'].get(field, '')]
            add(f'金额字段 {field} 带小数（{len(rows)} 条），与字典“整数、单位分”不符', kind, rows)
    bad_money = [r['source_row'] for kind, fields in MONEY_FIELDS.items() for r in rows_of(tables, kind)
                 for f in fields if to_decimal(r['raw'].get(f, '0')) is None]
    if bad_money:
        add(f'金额无法解析为数字 {len(bad_money)} 条', 'order', bad_money)

    # 5) 状态与金额的组合
    status_mismatch = []
    for r in orders:
        p, f = cents(r['raw']['amt_pay']), cents(r['raw']['amt_rfd'])
        s = r['raw']['ord_stat']
        if p is None or f is None:
            continue
        if (s == '全额退款' and f != p) or (s == '已取消' and p != 0) or (s in ('已支付', '已完成') and f != 0):
            status_mismatch.append(r['source_row'])
    add(f'订单状态与金额矛盾 {len(status_mismatch)} 条（如全额退款但退款≠实付、已取消但实付>0）', 'order', status_mismatch)

    # 6) 日期：无效日期、多种写法、订单号日期与下单时间不一致、卡日期先后
    for kind, fields in DATE_FIELDS.items():
        for field in fields:
            if not has_field(tables, kind, field):
                continue
            rows = rows_of(tables, kind)
            invalid = [r['source_row'] for r in rows if not is_missing(r['raw'][field]) and parse_date(r['raw'][field]) is None]
            add(f'无效日期：{field} 无法按日期解析 {len(invalid)} 条', kind, invalid)
            non_iso = [r['source_row'] for r in rows if not is_missing(r['raw'][field])
                       and parse_date(r['raw'][field]) and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', r['raw'][field])]
            shapes = Counter(re.sub(r'\d', '9', r['raw'][field]) for r in rows if r['source_row'] in set(non_iso))
            add(f'日期写法混用：{field} 有 {len(non_iso)} 条不是 YYYY-MM-DD（写法 {dict(sorted(shapes.items()))}）', kind, non_iso)
    for kind, fields in DATETIME_FIELDS.items():
        for field in fields:
            if not has_field(tables, kind, field):
                continue
            invalid = [r['source_row'] for r in rows_of(tables, kind)
                       if not is_missing(r['raw'][field]) and parse_date(r['raw'][field], DATETIME_FORMATS) is None]
            add(f'无效日期：{field} 无法按“年-月-日 时:分:秒”解析 {len(invalid)} 条', kind, invalid)
    mismatch = []
    for r in orders:
        embedded = re.match(r'O(\d{8})', r['raw']['ord_no'])
        if embedded and r['raw']['ord_tm'][:10].replace('-', '') != embedded.group(1):
            mismatch.append(r['source_row'])
    add(f'订单号内嵌日期与下单时间日期不一致 {len(mismatch)} 条', 'order', mismatch)
    card_order = [r['source_row'] for r in cards
                  if parse_date(r['raw']['st_dt']) and parse_date(r['raw']['ed_dt'])
                  and parse_date(r['raw']['ed_dt']) < parse_date(r['raw']['st_dt'])]
    add(f'会员卡到期日早于生效日 {len(card_order)} 条', 'card', card_order)

    # 7) 会员表：系统导入、未在字典中的状态、占位姓名
    if has_field(tables, 'member', 'src_ch'):
        imported = [r for r in members if r['raw']['src_ch'] == '系统导入']
        late = [r['source_row'] for r in imported
                if parse_date(r['raw']['join_dt']) and parse_date(r['raw']['create_tm'][:10])
                and parse_date(r['raw']['create_tm'][:10]) > parse_date(r['raw']['join_dt'])]
        no_dev = sum(1 for r in imported if is_missing(r['raw'].get('reg_dev', '')))
        add(f'注册渠道“系统导入” {len(imported)} 条（字典未列）：其中 {len(late)} 条 create_tm 晚于 join_dt，'
            f'{no_dev} 条 reg_dev 为空；疑为旧系统迁移的账户', 'member', [r['source_row'] for r in imported])
    if has_field(tables, 'member', 'create_tm'):
        by_day = Counter(r['raw']['create_tm'][:10] for r in members)
        if by_day:
            day, n = by_day.most_common(1)[0]
            add(f'建档时间扎堆：create_tm 在 {day} 当天有 {n} 条（日均约 {len(members) // max(len(by_day), 1)} 条），疑为批量导入',
                'member', [r['source_row'] for r in members if r['raw']['create_tm'].startswith(day)])
    if has_field(tables, 'member', 'nm'):
        placeholder = [r['source_row'] for r in members if r['raw']['nm'] == '顾客']
        add(f'姓名为占位值“顾客” {len(placeholder)} 条', 'member', placeholder)

    # 8) 门店：同名多编码
    if shops:
        names = defaultdict(list)
        for r in shops:
            names[r['raw']['shop_nm']].append(r['source_row'])
        multi = [n for rows in names.values() if len(rows) > 1 for n in rows]
        add(f'同一门店名称对应多个门店编码：{sum(1 for v in names.values() if len(v) > 1)} 个名称、{len(multi)} 家门店', 'shop', multi)

    # 9) 标签字段：分隔符混用、标签里混着备注、触达限制
    if has_field(tables, 'member', 'tag'):
        seps = Counter(ch for r in members for ch in r['raw']['tag'] if ch in ',，;；/ 。')
        mixed = [r['source_row'] for r in members if len(set(re.findall(r'[,，;；/ ]', r['raw']['tag']))) > 1]
        add(f'标签分隔符混用：出现 {dict(sorted(seps.items()))}，同一格里混用两种以上分隔符 {len(mixed)} 条', 'member', mixed)
        touch = [r['source_row'] for r in members if re.search(r'勿打电话|勿发短信|不要打电话|别打电话|不再联系', r['raw']['tag'])]
        add(f'标签字段里写着触达限制（勿打电话／勿发短信等）{len(touch)} 条，属于必须遵守的备注', 'member', touch)
        sentence = [r['source_row'] for r in members if '。' in r['raw']['tag']]
        add(f'标签字段里写着整句备注（含句号）{len(sentence)} 条', 'member', sentence)

    # 10) 会员卡：同一会员多张卡
    if cards:
        per = Counter(r['raw']['hy_id'] for r in cards)
        multi = [r['source_row'] for r in cards if per[r['raw']['hy_id']] > 1]
        add(f'同一会员持有多张卡：{sum(1 for v in per.values() if v > 1)} 个会员、{len(multi)} 张卡', 'card', multi)

    # 11) 小林工作账（可能不存在）：回访结果里提到用老卡在门店买
    revisit = rows_of(tables, 'revisit')
    if revisit and '回访结果' in tables[FILES['revisit']]['fields']:
        oldcard = [r['source_row'] for r in revisit if '老卡' in r['raw']['回访结果'] + r['raw'].get('备注', '')]
        add(f'工作账回访结果提到“在门店用老卡买” {len(oldcard)} 条，CRM 订单可能未覆盖旧卡消费', 'revisit', oldcard)
    return out


def dictionary_differences(tables, dictionary):
    spec, legal_tags = parse_dictionary(dictionary)
    out = []

    def add(filename, field, claim, actual, impact):
        out.append({'source_file': filename, 'field': field, 'dictionary_claim': claim, 'actual': actual, 'impact': impact})

    for filename in sorted(tables):
        stem = filename[:-4]
        if stem not in spec:
            continue
        table, fields = tables[filename], spec[stem]
        for extra in [f for f in table['fields'] if f not in fields]:
            add(filename, extra, '字典未列出该字段', f'导出里有字段 {extra}，示例 {table["rows"][0]["raw"][extra] if table["rows"] else ""}',
                '含义未经确认前不进入指标口径；向老周核实含义')
        for missing in [f for f in fields if f not in table['fields']]:
            add(filename, missing, f'字典列出字段 {missing}', '导出里没有该字段', '依赖该字段的口径无法计算，列入待补数据')
        for field, info in fields.items():
            if field not in table['fields']:
                continue
            values = [r['raw'][field] for r in table['rows']]
            present = [v for v in values if not is_missing(v)]
            distinct = sorted(set(norm_code(v) if field in ('shop_cd', 'reg_shop') else v.strip() for v in present))
            desc, typ = info['desc'], info['type']
            if '分' in desc and typ == '整数':
                decimals = [v for v in present if '.' in v]
                if decimals:
                    add(filename, field, f'{typ}，{desc}', f'{len(decimals)}/{len(present)} 条带两位小数，示例 {decimals[0]}，量级像“元”',
                        '按元×100 换算为整数分，若按分直接读会把金额放大 100 倍')
            if typ == '日期':
                non_iso = [v for v in present if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', v)]
                if non_iso:
                    shapes = Counter(re.sub(r'\d', '9', v) for v in non_iso)
                    add(filename, field, '日期', f'{len(non_iso)} 条不是 YYYY-MM-DD，写法 {dict(sorted(shapes.items()))}',
                        '多格式显式解析并统一为 ISO 日期；解析不了的进隔离')
            if '可为空' not in desc and typ != '整数':
                blanks = Counter(v for v in values if is_missing(v))
                if blanks and field not in ('tag',):
                    add(filename, field, f'{info["name"]}（字典未写可为空）', f'缺失 {sum(blanks.values())} 条，写法 {dict(sorted(blanks.items()))}',
                        '统一空值写法；缺失不能当作取值参与统计')
            if '4 位数字' in desc:
                odd = [v for v in distinct if not re.fullmatch(r'\d{4}', v)]
                if odd:
                    add(filename, field, desc, f'{len(odd)} 个编码不是 4 位数字，如 {odd[:5]}', '编码按文本处理，按实际编码关联门店表')
            if '8 位数字' in desc:
                odd = [v for v in distinct if not re.fullmatch(r'QH\d{8}', v)]
                if odd:
                    add(filename, field, desc, f'{len(odd)} 个编号格式不符，如 {odd[:5]}', '格式不符的会员号需隔离核实')
            mode, listed = listed_values(desc)
            if mode == 'codes' and distinct:
                if set(distinct) <= set(listed.values()):
                    add(filename, field, desc, f'实际存的是文字 {distinct}，不是代码', '按文字直接映射，不做代码转换')
                else:
                    extra = [v for v in distinct if v not in listed]
                    if extra:
                        counts = Counter(v.strip() for v in present)
                        add(filename, field, desc, f'出现字典没有的代码 {extra}（{ {v: counts[v] for v in extra} }）',
                            '新增代码含义需客户确认后映射，不能当错误数据删除')
            elif mode == 'values' and distinct and len(distinct) <= 30:
                extra = [v for v in distinct if v not in listed]
                if extra:
                    counts = Counter(v.strip() for v in present)
                    add(filename, field, desc, f'出现字典没有的取值 { {v: counts[v] for v in extra} }',
                        '按实际取值映射并请客户确认；契约外的取值进隔离')
            if field == 'tag':
                tokens = Counter(t for v in present for t in TAG_SPLIT.split(v) if t and not is_missing(t))
                unknown = {t: n for t, n in tokens.items() if legal_tags and t not in legal_tags}
                seps = sorted({ch for v in present for ch in v if ch in ',，;；/ '})
                add(filename, field, desc + '；附录标签表 ' + str(len(legal_tags)) + ' 个',
                    f'分隔符实际有 {seps}；拆开后有 {len(unknown)} 种字典外内容（共 {sum(unknown.values())} 处），'
                    f'既有疑似新标签（如 {[t for t, _ in Counter(unknown).most_common(40) if "-" not in t and "。" not in t][:5]}），'
                    f'也有备注（如 {[t for t, _ in Counter(unknown).most_common(40) if "-" in t][:4]}）',
                    '标签只收客户确认清单内的；其余按完整备注保留原文和来源，触达限制单独识别；新标签待小林确认')
    if not out:
        out.extend(keyword_differences(tables, dictionary))
    return out


def keyword_differences(tables, dictionary):
    """字典没有表格（如公开样本）时，按字典里的文字声明兜底比对。"""
    out = []
    if '分' in dictionary:
        for kind, fields in MONEY_FIELDS.items():
            for field in fields:
                rows = [r['raw'][field] for r in rows_of(tables, kind) if '.' in r['raw'].get(field, '')]
                if rows:
                    out.append({'source_file': FILES[kind], 'field': field, 'dictionary_claim': '金额声明为分',
                                'actual': f'{len(rows)} 条带小数，示例 {rows[0]}', 'impact': '按元×100 换算为整数分'})
    if 'ISO' in dictionary or '日期' in dictionary:
        for kind, fields in DATE_FIELDS.items():
            for field in fields:
                rows = [r['raw'][field] for r in rows_of(tables, kind)
                        if not is_missing(r['raw'].get(field, '-')) and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', r['raw'][field])]
                if rows:
                    out.append({'source_file': FILES[kind], 'field': field, 'dictionary_claim': '日期声明为 ISO',
                                'actual': f'{len(rows)} 条不是 YYYY-MM-DD，示例 {rows[0]}', 'impact': '多格式解析后统一为 ISO 日期'})
    if not out:
        first = sorted(tables)[0]
        out.append({'source_file': first, 'field': tables[first]['fields'][0], 'dictionary_claim': '字典未给出可逐字段比对的说明',
                    'actual': '无法逐字段比对', 'impact': '需向客户索取字段说明'})
    return out


def profile(tables, dictionary, context):
    return {'contract_version': CONTRACT, 'data_version': context['data_version'],
            'fields': field_statistics(tables),
            'anomalies': find_anomalies(tables),
            'dictionary_differences': dictionary_differences(tables, dictionary)}


# ---------------------------------------------------------------- 第 6、7 步：执行清洗规则（适配器）

KINDS = ('shop', 'member', 'order', 'card')          # 处理顺序：先门店，再会员，再订单和卡（关联要先有父表）
TARGET = {'member': 'members', 'order': 'orders', 'card': 'cards', 'shop': 'stores'}
OUT_TABLES = ('members', 'orders', 'stores', 'cards', 'member_tags', 'member_notes', 'quarantine')
EXPORT_RANGE = ('2024-09-01', '2026-09-01')          # 老周导出说明：订单 2024-09-01 至 2026-08-31（结束日不含写法）
FORMAT_CODES = {'YYYY-MM-DD': '%Y-%m-%d', 'YYYY/MM/DD': '%Y/%m/%d', 'YYYYMMDD': '%Y%m%d'}


class Reject(Exception):
    """一条记录不能接受：带上规则里写的原因码。"""


def bracket_list(text):
    found = re.search(r'\[(.*?)\]', text)
    return [v.strip() for v in found.group(1).split(',') if v.strip()] if found else []


def pairs(text):
    """'已取消=cancelled；已支付、已完成=paid' -> {'已取消':'cancelled','已支付':'paid',...}"""
    out = {}
    for left, right in re.findall(r'([^\s；=]+)=([^\s；]+)', text):
        for key in left.split('、'):
            out[key] = right
    return out


def keywords(text):
    found = re.search(r'含\s*(.+)$', text)
    return [k for k in found.group(1).split('|') if k] if found else []


class Rules:
    """把 YAML 解析成执行用的配置。规则改了，行为跟着变，代码不用改。"""

    def __init__(self, rules, context):
        self.raw = rules
        self.by_id = {r['rule_id']: r for r in rules['abnormal_records']}
        cfg = rules['tag_cleaning']
        self.delimiters = cfg['delimiters']
        self.split = re.compile('(' + '|'.join(re.escape(d) for d in sorted(cfg['delimiters'], key=len, reverse=True)) + ')+')
        tag_path = Path(context['rules_dir']) / cfg['legal_tags_source']
        self.legal_tags = [t.strip() for t in tag_path.read_text(encoding='utf-8-sig').splitlines() if t.strip()]
        self.missing = {}
        for item in rules['invalid_values']:
            if item['action'] == 'treat_as_missing':
                for field in item['source_fields']:
                    self.missing[field] = set(str(v) for v in item['values'])
        self.status = pairs(self.condition('member_status'))
        self.tier = pairs(self.condition('member_tier'))
        self.order_status = pairs(self.condition('order_status'))
        self.member_channels = bracket_list(self.condition('member_channel'))
        self.order_channels = bracket_list(self.condition('order_channel'))
        self.card_types = bracket_list(self.condition('supported_card_types'))
        self.date_formats = tuple(FORMAT_CODES[c] for c in re.findall(r'YYYY[-/]?MM[-/]?DD', self.condition('date_formats')))
        self.categories = [(self.by_id[rid]['reason_code'], keywords(self.by_id[rid]['condition']))
                           for rid in ('contact_restriction', 'contact_status', 'card_note', 'relationship_note') if rid in self.by_id]

    def condition(self, rule_id):
        return self.by_id[rule_id]['condition'] if rule_id in self.by_id else ''

    def action(self, rule_id):
        return self.by_id[rule_id]['action'] if rule_id in self.by_id else 'quarantine'

    def code(self, rule_id, default):
        return self.by_id[rule_id]['reason_code'] if rule_id in self.by_id else default

    def enforce(self, rule_id, default_code):
        """规则动作为 quarantine/reject 时拒收；为 keep 时放行。"""
        if self.action(rule_id) in ('quarantine', 'reject'):
            raise Reject(self.code(rule_id, default_code))

    def is_missing(self, field, value):
        return value in self.missing.get(field, set(MISSING))

    def category(self, text):
        for name, words in self.categories:
            if any(w in text for w in words):
                return name
        return '其他'


def cents_strict(value):
    number = to_decimal(value)
    if number is None or not number.is_finite():
        raise Reject('INVALID_MONEY')
    number = number * 100
    if number != number.to_integral_value():
        raise Reject('INVALID_MONEY')
    return int(number)


def iso_date(value, formats):
    parsed = parse_date(value, formats)
    if parsed is None:
        raise Reject('INVALID_DATE')
    return parsed.isoformat()


def split_tag_field(value, rules):
    """返回 (标签列表, 备注列表)。备注保留原文标点，只在分隔符处切开。"""
    pos = 0
    pieces, gaps = [], []
    for m in re.finditer('(?:' + '|'.join(re.escape(d) for d in rules.delimiters) + ')+', value):
        pieces.append(value[pos:m.start()])
        gaps.append(m.group(0))
        pos = m.end()
    pieces.append(value[pos:])
    tags, notes = [], []
    previous_gap = None
    for index, piece in enumerate(pieces):
        gap_before = gaps[index - 1] if index > 0 else None
        if not piece:
            continue
        if piece in rules.legal_tags:
            if piece not in tags:
                tags.append(piece)
            previous_gap = 'tag'
            continue
        # 中文逗号既当分隔符、也出现在整句备注里：只有“上一段没以句号结束、这一段不是新的‘关键词-’开头”时才接成一条
        mergeable = (notes and previous_gap == 'note' and gap_before == '，'
                     and not notes[-1].endswith('。') and not NEW_NOTE_START.match(piece))
        if mergeable:
            notes[-1] = notes[-1] + gap_before + piece
        else:
            notes.append(piece)
        previous_gap = 'note'
    return tags, notes


def adapt(tables, rules, context):
    R = Rules(rules, context)
    out = {name: [] for name in OUT_TABLES}
    counts = {kind: {'read': len(rows_of(tables, kind)), 'accepted': 0, 'quarantined': 0, 'deduplicated': 0} for kind in KINDS}
    reasons = Counter()
    samples = []
    stores, members = set(), set()

    def quarantine(kind, row, code):
        counts[kind]['quarantined'] += 1
        reasons[(kind, code)] += 1
        out['quarantine'].append({'record_type': kind, 'source_file': row['source_file'], 'source_row': row['source_row'],
                                  'reason_code': code, 'raw_json': json.dumps(row['raw'], ensure_ascii=False)})
        if not any(s['status'] == 'quarantined' for s in samples):
            samples.append({'source_file': row['source_file'], 'source_row': row['source_row'], 'status': 'quarantined',
                            'destination': f'quarantine.csv 记录 {len(out["quarantine"])}', 'reason': code})

    for kind in KINDS:
        rows = rows_of(tables, kind)
        key_field = KEYS[kind]
        # 主键冲突：同一主键（规范化后）出现不同内容 → 这些行都隔离
        contents = defaultdict(set)
        for row in rows:
            key = norm_code(row['raw'][key_field]) if kind == 'shop' else row['raw'][key_field].strip()
            contents[key].add(tuple(sorted(row['raw'].items())))
        seen = set()
        for row in rows:
            raw = row['raw']
            identity = tuple(sorted(raw.items()))
            if identity in seen and R.action('exact_duplicate') == 'deduplicate':
                counts[kind]['deduplicated'] += 1
                continue
            seen.add(identity)
            key = norm_code(raw[key_field]) if kind == 'shop' else raw[key_field].strip()
            try:
                if not key:
                    raise Reject(R.code('duplicate_key', 'DUPLICATE_PRIMARY_KEY'))
                if len(contents[key]) > 1:
                    R.enforce('duplicate_key', 'DUPLICATE_PRIMARY_KEY')
                if kind == 'shop':
                    mapped = {'store_id': key, 'region': raw['area'].strip(), 'store_name': raw['shop_nm'].strip()}
                    stores.add(key)
                elif kind == 'member':
                    mapped = map_member(raw, R, stores)
                elif kind == 'order':
                    mapped = map_order(raw, R, members, stores)
                else:
                    mapped = map_card(raw, R, members)
            except Reject as exc:
                quarantine(kind, row, str(exc))
                continue
            out[TARGET[kind]].append(mapped)
            counts[kind]['accepted'] += 1
            if kind == 'member':
                members.add(mapped['member_id'])
                emit_tags_and_notes(row, R, out)
            if kind == 'order' and not any(s['status'] == 'accepted' for s in samples):
                samples.append({'source_file': row['source_file'], 'source_row': row['source_row'], 'status': 'accepted',
                                'destination': f'orders.csv 记录 {len(out["orders"])}（order_id={mapped["order_id"]}）',
                                'reason': '会员、门店能关联，日期可解析，金额元转分，状态映射为 ' + mapped['status']})

    coverage = {
        'source': ('本次只含新 CRM 导出的会员、订单、会员卡、门店 4 个文件；门店老收银系统的会员消费未导出（老周 MSG-05458、05502）；'
                   '已注销会员资料按规定清理，其订单无法关联、已隔离未纳入（MSG-05470）；导出中没有员工身份数据，员工卡已隔离（MSG-05500）；'
                   '小林工作账只用于核对，未导入。未覆盖部分是不知道，不能当作 0。'),
        'time_range': {'start': EXPORT_RANGE[0], 'end_exclusive': EXPORT_RANGE[1]},
        'quarantined': {kind: counts[kind]['quarantined'] for kind in ('member', 'order', 'card', 'shop')},
        'quarantine_reasons': {f'{kind}:{code}': n for (kind, code), n in sorted(reasons.items())},
        'deduplicated': {kind: counts[kind]['deduplicated'] for kind in ('member', 'order', 'card', 'shop')},
    }
    return {'tables': out, 'record_counts': {k: counts[k] for k in ('member', 'order', 'card', 'shop')},
            'field_mapping': FIELD_MAPPING, 'samples': samples, 'coverage_notes': coverage}


def map_member(raw, R, stores):
    home = norm_code(raw['reg_shop'])
    if home not in stores:
        R.enforce('orphan_store', 'UNLINKED_STORE')
    tier = raw['lv'].strip()
    tier = R.tier.get(tier, tier)
    if tier not in set(R.tier.values()):
        raise Reject('UNSUPPORTED_TIER')
    status = R.status.get(raw['stat'].strip())
    if status is None:
        raise Reject('UNSUPPORTED_ACCOUNT_STATUS')
    channel = raw['src_ch'].strip()
    if channel not in R.member_channels:
        raise Reject('UNSUPPORTED_CHANNEL')
    created = parse_date(raw['create_tm'].strip()[:10])
    if created is None:
        raise Reject(R.code('invalid_date', 'INVALID_DATE'))
    try:
        registered = iso_date(raw['join_dt'].strip(), R.date_formats)
    except Reject:
        raise Reject(R.code('invalid_date', 'INVALID_DATE'))
    return {'member_id': raw['hy_id'].strip(), 'registered_at': registered, 'home_store_id': home, 'tier': tier,
            'account_status': status, 'registered_channel': channel, 'created_at': created.isoformat()}


def map_order(raw, R, members, stores):
    member = raw['hy_id'].strip()
    if member not in members:
        R.enforce('orphan_member', 'UNLINKED_ACCOUNT')
    store = norm_code(raw['shop_cd'])
    if store not in stores:
        R.enforce('orphan_store', 'UNLINKED_STORE')
    ordered = parse_date(raw['ord_tm'].strip(), DATETIME_FORMATS) or parse_date(raw['ord_tm'].strip(), ('%Y-%m-%d',))
    if ordered is None:
        raise Reject(R.code('invalid_date', 'INVALID_DATE'))
    status = R.order_status.get(raw['ord_stat'].strip())
    if status is None:
        raise Reject('UNSUPPORTED_ORDER_STATUS')
    try:
        gross, paid, refund = (cents_strict(raw[f]) for f in ('amt_total', 'amt_pay', 'amt_rfd'))
    except Reject:
        raise Reject(R.code('invalid_money', 'INVALID_MONEY'))
    if refund > paid:
        R.enforce('refund_exceeds_paid', 'REFUND_EXCEEDS_PAID')
    if not re.fullmatch(r'\d+', raw['qty'].strip()):
        raise Reject('INVALID_QUANTITY')
    channel = raw['ch'].strip()
    if channel not in R.order_channels:
        raise Reject('UNSUPPORTED_CHANNEL')
    return {'order_id': raw['ord_no'].strip(), 'member_id': member, 'store_id': store, 'ordered_at': ordered.isoformat(),
            'status': status, 'gross_cents': gross, 'paid_cents': paid, 'refund_cents': refund,
            'quantity': int(raw['qty'].strip()), 'channel': channel}


def map_card(raw, R, members):
    member = raw['hy_id'].strip()
    if member not in members:
        R.enforce('orphan_member', 'UNLINKED_ACCOUNT')
    kind = raw['card_kind'].strip()
    if kind not in R.card_types:
        R.enforce('unsupported_card_type', 'UNSUPPORTED_CARD_TYPE')
    try:
        valid_from = iso_date(raw['st_dt'].strip(), ('%Y-%m-%d',))
        valid_until = iso_date(raw['ed_dt'].strip(), ('%Y-%m-%d',))
        stop = raw['stop_dt'].strip()
        ended = '' if R.is_missing('stop_dt', stop) else iso_date(stop, ('%Y-%m-%d',))
    except Reject:
        raise Reject(R.code('invalid_date', 'INVALID_DATE'))
    try:
        paid = cents_strict(raw['pay_amt'])
    except Reject:
        raise Reject(R.code('invalid_money', 'INVALID_MONEY'))
    return {'card_id': raw['card_no'].strip(), 'member_id': member, 'card_type': kind, 'valid_from': valid_from,
            'valid_until': valid_until, 'ended_at': ended, 'card_paid_cents': paid}


def emit_tags_and_notes(row, R, out):
    raw = row['raw']
    value = raw.get('tag', '')
    if R.is_missing('tag', value):
        return
    member = raw['hy_id'].strip()
    tags, notes = split_tag_field(value, R)
    for tag in tags:
        out['member_tags'].append({'member_id': member, 'tag': tag})
    emitted = []
    for text in notes:
        if text in emitted:
            # 同一条记录里出现两段相同备注：并入前一条，避免备注组合键重复，同时不丢文字
            previous = out['member_notes'][-1]
            previous['note_text'] = previous['note_text'] + '，' + text
            previous['category'] = R.category(previous['note_text'])
            emitted.append(previous['note_text'])
            continue
        emitted.append(text)
        out['member_notes'].append({'member_id': member, 'source_file': row['source_file'], 'source_row': row['source_row'],
                                    'source_field': 'tag', 'raw_text': value, 'note_text': text, 'category': R.category(text)})


FIELD_MAPPING = [
    {'source_file': FILES['shop'], 'source_fields': ['shop_cd'], 'target': 'stores.store_id', 'transformation': '全角转半角、去首尾空格、转大写；字母前缀为 2025 年后新门店编码，原样保留'},
    {'source_file': FILES['shop'], 'source_fields': ['area'], 'target': 'stores.region', 'transformation': '原值（含字典未列的华中、西南）'},
    {'source_file': FILES['shop'], 'source_fields': ['shop_nm'], 'target': 'stores.store_name', 'transformation': '原值'},
    {'source_file': FILES['member'], 'source_fields': ['hy_id'], 'target': 'members.member_id', 'transformation': '原值'},
    {'source_file': FILES['member'], 'source_fields': ['join_dt'], 'target': 'members.registered_at', 'transformation': '三种日期写法统一为 ISO；入会日期（老周 MSG-05464）'},
    {'source_file': FILES['member'], 'source_fields': ['reg_shop'], 'target': 'members.home_store_id', 'transformation': '同门店编码规范化，须能关联门店表'},
    {'source_file': FILES['member'], 'source_fields': ['lv'], 'target': 'members.tier', 'transformation': '文字原样；代码 1/2/3 按字典转普通/银卡/金卡'},
    {'source_file': FILES['member'], 'source_fields': ['stat'], 'target': 'members.account_status', 'transformation': '0 未激活、1 活跃、2 冻结、3 注销（老周 MSG-05462）'},
    {'source_file': FILES['member'], 'source_fields': ['src_ch'], 'target': 'members.registered_channel', 'transformation': '门店、小程序、商城、系统导入原样'},
    {'source_file': FILES['member'], 'source_fields': ['create_tm'], 'target': 'members.created_at', 'transformation': '取日期部分；新 CRM 建档时间'},
    {'source_file': FILES['member'], 'source_fields': ['tag'], 'target': 'member_tags.tag', 'transformation': '按 5 种分隔符切开，片段在 16 个确认标签内才进标签表，同一会员去重'},
    {'source_file': FILES['member'], 'source_fields': ['tag'], 'target': 'member_notes.note_text', 'transformation': '清单外片段作为完整备注保留原文与来源；勿打电话/勿发短信归触达限制'},
    {'source_file': FILES['order'], 'source_fields': ['ord_no'], 'target': 'orders.order_id', 'transformation': '原值；整行重复去重'},
    {'source_file': FILES['order'], 'source_fields': ['hy_id'], 'target': 'orders.member_id', 'transformation': '须关联已接受会员，否则隔离 UNLINKED_ACCOUNT'},
    {'source_file': FILES['order'], 'source_fields': ['shop_cd'], 'target': 'orders.store_id', 'transformation': '门店编码规范化后关联门店表'},
    {'source_file': FILES['order'], 'source_fields': ['ord_tm'], 'target': 'orders.ordered_at', 'transformation': '取本地日期'},
    {'source_file': FILES['order'], 'source_fields': ['ord_stat'], 'target': 'orders.status', 'transformation': '已取消→cancelled；已支付、已完成、部分退款、全额退款→paid'},
    {'source_file': FILES['order'], 'source_fields': ['amt_total'], 'target': 'orders.gross_cents', 'transformation': 'Decimal 元×100'},
    {'source_file': FILES['order'], 'source_fields': ['amt_pay'], 'target': 'orders.paid_cents', 'transformation': 'Decimal 元×100'},
    {'source_file': FILES['order'], 'source_fields': ['amt_rfd'], 'target': 'orders.refund_cents', 'transformation': 'Decimal 元×100；大于实付则隔离 REFUND_EXCEEDS_PAID'},
    {'source_file': FILES['order'], 'source_fields': ['qty'], 'target': 'orders.quantity', 'transformation': '整数'},
    {'source_file': FILES['order'], 'source_fields': ['ch'], 'target': 'orders.channel', 'transformation': '门店、小程序、商城原样'},
    {'source_file': FILES['card'], 'source_fields': ['card_no'], 'target': 'cards.card_id', 'transformation': '原值'},
    {'source_file': FILES['card'], 'source_fields': ['card_kind'], 'target': 'cards.card_type', 'transformation': '五种卡名原样，不合并悦享卡（年）与悦享年卡；员工卡隔离 UNSUPPORTED_CARD_TYPE'},
    {'source_file': FILES['card'], 'source_fields': ['st_dt', 'ed_dt', 'stop_dt'], 'target': 'cards.valid_from/valid_until/ended_at', 'transformation': 'ISO 日期；stop_dt 空值写法记为空'},
    {'source_file': FILES['card'], 'source_fields': ['pay_amt'], 'target': 'cards.card_paid_cents', 'transformation': 'Decimal 元×100'},
]


# ---------------------------------------------------------------- 第 8 步：对账

def reconcile(payload):
    meta, manifest = payload['metadata'], payload['manifest']
    first, second = payload['first'], payload['second']
    rows, balanced_all, details = [], True, []
    for kind in ('member', 'order', 'card', 'shop'):
        stated = meta['record_counts'][kind]
        read = len(payload['source'][FILES[kind]]['rows']) if FILES[kind] in payload['source'] else 0
        cleaned_rows = len(payload['cleaned'][TARGET[kind] + '.csv']['rows'])
        quarantined = sum(1 for r in payload['cleaned']['quarantine.csv']['rows'] if r['raw']['record_type'] == kind)
        in_db = first['counts'].get('raw_' + TARGET[kind], 0)
        balanced = (read == stated['read'] == stated['accepted'] + stated['quarantined'] + stated['deduplicated']
                    and stated['accepted'] == cleaned_rows == in_db and stated['quarantined'] == quarantined)
        balanced_all = balanced_all and balanced
        rows.append({'record_type': kind, 'read': stated['read'], 'accepted': stated['accepted'],
                     'quarantined': stated['quarantined'], 'deduplicated': stated['deduplicated'],
                     'reconciliation': {'balanced': balanced,
                                        'evidence': f'源文件 {read} = 接受 {stated["accepted"]} + 隔离 {stated["quarantined"]} + 去重 {stated["deduplicated"]}；'
                                                    f'清洗 CSV {cleaned_rows}、隔离文件 {quarantined}、库内 {in_db}'}})
    orders = payload['cleaned']['orders.csv']['rows']
    paid = sum(int(r['raw']['paid_cents']) for r in orders)
    # 金额独立核算：从原始订单按“去重后、未隔离”的记录重新计算元→分，不复用适配器结果
    quarantined_rows = {int(r['raw']['source_row']) for r in payload['cleaned']['quarantine.csv']['rows']
                        if r['raw']['source_file'] == FILES['order']}
    seen, source_paid = set(), 0
    for r in payload['source'].get(FILES['order'], {'rows': []})['rows']:
        identity = tuple(sorted(r['raw'].items()))
        if identity in seen:
            continue
        seen.add(identity)
        if r['source_row'] not in quarantined_rows:
            source_paid += int(Decimal(r['raw']['amt_pay']) * 100)
    money_ok = paid == source_paid == first['paid_cents'] == second['paid_cents']
    repeat = first['counts'] == second['counts'] and first['table_sha256'] == second['table_sha256']
    count_ok = len(orders) == first['order_count'] == second['order_count']
    details.append(f'四类记录对账{"全部平衡" if balanced_all else "有不平衡"}')
    details.append(f'订单接受 {len(orders)} 条；实付合计：清洗 CSV {paid} 分、原始独立重算 {source_paid} 分、'
                   f'第一次库 {first["paid_cents"]} 分、第二次库 {second["paid_cents"]} 分')
    details.append(f'两次独立全量导入行数{"一致" if first["counts"] == second["counts"] else "不一致"}、'
                   f'表内容哈希{"一致" if first["table_sha256"] == second["table_sha256"] else "不一致"}')
    return {'contract_version': CONTRACT, 'data_version': manifest['id'], 'field_mapping': meta['field_mapping'],
            'record_counts': rows, 'samples': meta['samples'],
            'repeat_import': {'first_run_id': payload['first_run_id'], 'second_run_id': payload['second_run_id'],
                              'before': first['counts'], 'after': second['counts'], 'row_counts_equal': first['counts'] == second['counts']},
            'test_result': {'passed': balanced_all and money_ok and repeat and count_ok, 'details': '；'.join(details)}}
