import collections
import hashlib
import json
import math
import struct
import sys
from pathlib import Path

import olefile
import xlrd


SHEET = '附件4-2'
FORMAT = 'yyyy"年"m"月"d"日"'
PLAN = Path('.codex/workbook-v02-rs-plan.json')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hidden(s):
    return {i for i, v in s.rowinfo_map.items() if v.hidden}


def filtered_groups(s):
    groups = collections.defaultdict(list)
    masked = hidden(s)
    assert s.row_values(4)[15:19] == ['立项/采购阶段-序号', '立项/采购阶段-功能点名称', '投产日期', '投产版本号']
    for i in range(5, s.nrows):
        if i in masked or not s.cell_value(i, 4):
            continue
        p, r, v = (s.cell_value(i, j) for j in (15, 17, 18))
        assert p != '' and s.cell_type(i, 17) == xlrd.XL_CELL_DATE, i+1
        groups[p].append((i, r, v))
    return groups


def plan(source):
    book = xlrd.open_workbook(source, formatting_info=True)
    groups = filtered_groups(book.sheet_by_name(SHEET))
    rows = []
    for items in groups.values():
        first, date, version = items[0]
        for i, old_date, old_version in items:
            rows.append(dict(row=i+1, first=first+1, date=date, version=version,
                             change_r=old_date != date, change_s=old_version != version))
    counts = dict(visible=len(rows), groups=len(groups), conflicts=sum(len({x[1:] for x in g}) > 1 for g in groups.values()),
                  changed_rows=sum(x['change_r'] or x['change_s'] for x in rows))
    PLAN.write_text(json.dumps(dict(source_hash=digest(source), rows=sorted(rows,key=lambda x:x['row']),
                                    stats=counts), ensure_ascii=False), encoding='utf-8')
    print(counts)


def records(path):
    with olefile.OleFileIO(path) as f:
        stream = f.openstream('Workbook').read()
    pos = 0
    retained = collections.Counter()
    while pos + 4 <= len(stream):
        kind, size = struct.unpack_from('<HH', stream, pos)
        if kind in (6, 0x9b, 0x9d, 0x9e):
            retained[kind] += 1
        pos += size + 4
    return retained


def verify(source, output):
    info = json.loads(PLAN.read_text(encoding='utf-8'))
    assert info['source_hash'] == digest(source)
    a, b = (xlrd.open_workbook(p, formatting_info=True, ragged_rows=True) for p in (source, output))
    assert a.sheet_names() == b.sheet_names() and a.datemode == b.datemode
    assert records(source) == records(output)
    groups = filtered_groups(b.sheet_by_name(SHEET))
    assert all(len({x[1:] for x in g}) == 1 for g in groups.values())
    changes = 0
    for s, t in zip(a.sheets(), b.sheets()):
        assert hidden(s) == hidden(t)
        assert sorted(s.merged_cells) == sorted(t.merged_cells)
        assert {i for i,x in s.colinfo_map.items() if x.hidden} == {i for i,x in t.colinfo_map.items() if x.hidden}
        assert {k:(v.author,v.text) for k,v in s.cell_note_map.items()} == {k:(v.author,v.text) for k,v in t.cell_note_map.items()}
        expected = {}
        if s.name == SHEET:
            for v in info['rows']:
                i = v['row'] - 1
                expected[i,17] = v['date']
                expected[i,18] = v['version']
                key = b.xf_list[t.cell_xf_index(i,17)].format_key
                f = b.format_map[key].format_str
                # WPS uses a built-in regional date format for this pattern.
                # Its displayed text is separately checked after reopening.
                assert f == FORMAT or (key == 31 and f is None), (i+1, key, f)
        for i in range(max(s.nrows,t.nrows)):
            left = s.row_values(i) if i < s.nrows else []
            right = t.row_values(i) if i < t.nrows else []
            for j in range(max(len(left),len(right))):
                old = left[j] if j < len(left) else ''
                new = right[j] if j < len(right) else ''
                want = expected.pop((i,j),old)
                same = math.isclose(want,new,rel_tol=1e-13,abs_tol=1e-13) if isinstance(want,float) and isinstance(new,float) else want == new
                assert same, (s.name,i+1,j+1,want,new)
                if want != old:
                    changes += 1
        assert not expected
    print('Verified values, date formats, formula/filter counts, visibility and unchanged original:', changes, 'value changes.')


src = Path(sys.argv[1])
if len(sys.argv) == 2:
    plan(src)
else:
    verify(src, Path(sys.argv[2]))
