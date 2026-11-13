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
PLAN = Path('.codex/workbook-v02-dedup-plan.json')


def hidden(sheet):
    return {row for row, info in sheet.rowinfo_map.items() if info.hidden}


def make_plan(path):
    book = xlrd.open_workbook(path, formatting_info=True)
    sheet = book.sheet_by_name(SHEET)
    assert sheet.cell_value(4, 12) == '与立项阶段功能点对应类型'
    assert sheet.cell_value(4, 15) == '立项/采购阶段-序号'
    groups = collections.defaultdict(list)
    invisible = hidden(sheet)
    for row in range(5, sheet.nrows):
        if row in invisible or not sheet.cell_value(row, 4):
            continue
        key = sheet.cell_value(row, 15)
        if str(key).strip():
            groups[key].append(row + 1)
    duplicates = [rows for rows in groups.values() if len(rows) > 1]
    assert duplicates, 'No duplicate visible P values'
    keep = [rows[0] for rows in duplicates]
    clear = sorted(row for rows in duplicates for row in rows[1:])
    PLAN.write_text(json.dumps({'hash': hashlib.sha256(path.read_bytes()).hexdigest(), 'keep': keep, 'clear': clear}), encoding='utf-8')
    print(f'{len(keep)} duplicate groups; clear P/Q on {len(clear)} rows.')


def records(path):
    with olefile.OleFileIO(path) as ole:
        stream = ole.openstream('Workbook').read()
    counts = collections.Counter()
    pos = 0
    while pos + 4 <= len(stream):
        kind, size = struct.unpack_from('<HH', stream, pos)
        if kind in (6, 0x9b, 0x9d, 0x9e):
            counts[kind] += 1
        pos += size + 4
    return counts


def verify(before, after):
    plan = json.loads(PLAN.read_text(encoding='utf-8'))
    assert plan['hash'] == hashlib.sha256(before.read_bytes()).hexdigest()
    a, b = (xlrd.open_workbook(p, formatting_info=True, ragged_rows=True) for p in (before, after))
    assert a.sheet_names() == b.sheet_names() and a.datemode == b.datemode
    assert records(before) == records(after)
    for s, t in zip(a.sheets(), b.sheets()):
        assert hidden(s) == hidden(t) and sorted(s.merged_cells) == sorted(t.merged_cells)
        expected = {}
        if s.name == SHEET:
            expected.update({(r-1,12): '原功能点' for r in plan['keep']})
            expected.update({(r-1,c): '' for r in plan['clear'] for c in (15,16)})
        for i in range(max(s.nrows, t.nrows)):
            left = s.row_values(i) if i < s.nrows else []
            right = t.row_values(i) if i < t.nrows else []
            for j in range(max(len(left), len(right))):
                old = left[j] if j < len(left) else ''
                new = right[j] if j < len(right) else ''
                want = expected.pop((i,j), old)
                same = math.isclose(want,new,rel_tol=1e-13,abs_tol=1e-13) if isinstance(want,float) and isinstance(new,float) else want == new
                assert same, (s.name, i+1, j+1, want, new)
            if s.name == SHEET and len(left) > 17 and len(right) > 17:
                assert a.xf_list[s.cell_xf_index(i,17)].format_key == b.xf_list[t.cell_xf_index(i,17)].format_key
        assert not expected
    print('Verified all target edits, unchanged other values, R date formats and filter visibility.')


source = Path(sys.argv[1])
if len(sys.argv) == 2:
    make_plan(source)
else:
    verify(source, Path(sys.argv[2]))
