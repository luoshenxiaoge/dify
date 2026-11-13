import collections
import hashlib
import json
import math
import re
import struct
from pathlib import Path

import olefile
import xlrd


work = Path(__file__).parent
name = '大模型平台开发工具系统工程功能点清单V0.1(1)'
source = Path('C:/Users/Administrator/Desktop/工作文档/邮储/功能点梳理') / (name + '.xls')
result = work.parent / 'outputs/01a16409-eacd-7653-9f0a-6547f7ae8607' / (name + '-归类复核版.xls')
assert hashlib.sha256(source.read_bytes()).hexdigest() == '12dbd306fea352ed111dfe36d7a6d2e909d47c50c56fe8f5aa5fcd150d7910b1'
plan = json.loads((work / 'workbook-classification-plan.json').read_text(encoding='utf-8'))
a, b = [xlrd.open_workbook(p, formatting_info=True, ragged_rows=True) for p in (source, result)]
assert a.sheet_names() == b.sheet_names()
errors = []
changed = 0


def values_equal(left, right):
    return (math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)
            if isinstance(left, float) and isinstance(right, float) else left == right)


def note_content(sheet):
    return {k: (v.text, v.author) for k, v in sheet.cell_note_map.items()}


for s, t in zip(a.sheets(), b.sheets()):
    expected = {(int(re.search(r'\d+', addr).group()) - 1, ord(addr[0]) - 65): v
                for addr, v in plan['edits'].get(s.name, {}).items()}
    assert {r for r, info in s.rowinfo_map.items() if info.hidden} == {r for r, info in t.rowinfo_map.items() if info.hidden}, s.name
    assert sorted(s.merged_cells) == sorted(t.merged_cells), s.name
    assert note_content(s) == note_content(t), s.name
    assert (s.horz_split_pos, s.vert_split_pos, s.panes_are_frozen) == (t.horz_split_pos, t.vert_split_pos, t.panes_are_frozen), s.name
    assert {k for k, v in s.colinfo_map.items() if v.hidden} == {k for k, v in t.colinfo_map.items() if v.hidden}, s.name
    for i in range(max(s.nrows, t.nrows)):
        sv = s.row_values(i) if i < s.nrows else []
        tv = t.row_values(i) if i < t.nrows else []
        for j in range(max(len(sv), len(tv))):
            before = sv[j] if j < len(sv) else ''
            after = tv[j] if j < len(tv) else ''
            want = expected.pop((i, j), before)
            if not values_equal(want, after):
                errors.append((s.name, i+1, j+1, want, after))
            if not values_equal(before, after):
                changed += 1
    assert not expected, (s.name, expected)
    print(s.name, 'verified visible rows, merged cells and notes')


def biff_counts(path):
    with olefile.OleFileIO(path) as ole:
        contents = ole.openstream('Workbook').read()
    pos = 0
    totals = collections.Counter()
    while pos + 4 <= len(contents):
        kind, n = struct.unpack_from('<HH', contents, pos)
        if kind in (0x0006, 0x009B, 0x009D, 0x009E):
            totals[kind] += 1
        pos += n + 4
    return totals


print('FORMULA / filter BIFF records', biff_counts(source), biff_counts(result))
assert biff_counts(source) == biff_counts(result)
print('Cell deltas', changed, 'unexpected', len(errors))
if errors:
    print(errors[:30])
    raise SystemExit(1)
print(result, result.stat().st_size, 'bytes')
