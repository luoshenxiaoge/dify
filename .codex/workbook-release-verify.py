import collections
import json
import math
import struct
import sys
from pathlib import Path

import olefile
import xlrd

plan = json.loads(Path('.codex/workbook-release-plan.json').read_text(encoding='utf-8'))
baseline, result = map(Path, sys.argv[1:])
a, b = [xlrd.open_workbook(x, formatting_info=True, ragged_rows=True) for x in (baseline, result)]
assert a.sheet_names() == b.sheet_names()


def records(path):
    with olefile.OleFileIO(path) as ole:
        stream = ole.openstream('Workbook').read()
    pos = 0
    count = collections.Counter()
    while pos + 4 <= len(stream):
        kind, length = struct.unpack_from('<HH', stream, pos)
        if kind in (0x0006, 0x009B, 0x009D, 0x009E):
            count[kind] += 1
        pos += length + 4
    return count


def equal(x, y):
    if isinstance(x, float) and isinstance(y, float):
        return math.isclose(x, y, rel_tol=1e-12, abs_tol=1e-12)
    return x == y


assert records(baseline) == records(result)
changed = 0
for s, t in zip(a.sheets(), b.sheets()):
    expected = {}
    if s.name == '附件4-2':
        for u in plan['updates']:
            i = u['row'] - 1
            assert s.cell_value(i, 0) == u['id'] and not s.rowinfo_map[i].hidden
            assert t.cell_type(i, 17) == xlrd.XL_CELL_DATE
            expected[i, 17] = u['serial']
            expected[i, 18] = u['code']
    assert {r for r, x in s.rowinfo_map.items() if x.hidden} == {r for r, x in t.rowinfo_map.items() if x.hidden}
    assert {c for c, x in s.colinfo_map.items() if x.hidden} == {c for c, x in t.colinfo_map.items() if x.hidden}
    assert sorted(s.merged_cells) == sorted(t.merged_cells)
    assert {k: (v.author, v.text) for k, v in s.cell_note_map.items()} == {k: (v.author, v.text) for k, v in t.cell_note_map.items()}
    for i in range(max(s.nrows, t.nrows)):
        sv = s.row_values(i) if i < s.nrows else []
        tv = t.row_values(i) if i < t.nrows else []
        for j in range(max(len(sv), len(tv))):
            before = sv[j] if j < len(sv) else ''
            after = tv[j] if j < len(tv) else ''
            want = expected.pop((i, j), before)
            assert equal(want, after), (s.name, i+1, j+1, want, after)
            changed += not equal(before, after)
    assert not expected
assert changed == 2 * len(plan['updates'])
print(f'Verified {changed} cell changes; other values, notes, merged cells and filtered rows unchanged.')
