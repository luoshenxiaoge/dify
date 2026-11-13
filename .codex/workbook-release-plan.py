import collections
import datetime as dt
import json
import re
from pathlib import Path

import xlrd

work = Path(__file__).parent
folder = Path('C:/Users/Administrator/Desktop/工作文档/邮储/功能点梳理')
stories = {x['row']: x for x in json.loads((work / 'workbook-release-stories.json').read_text(encoding='utf-8'))}
target = json.loads((work / 'workbook-release-target.json').read_text(encoding='utf-8'))
rows = {x['id']: x for x in target['rows']}
number_by_date = {}
for line in (folder / '上线编号.txt').read_text(encoding='utf-8-sig').splitlines():
    if line.strip():
        day, code = line.split()
        number_by_date[dt.datetime.strptime(day, '%Y/%m/%d').date()] = code

# 2-工具上线 Excel row -> visible 4-2 feature IDs, matched using story C/D.
matches = {
    4: [676, 677, 680, 681, 684, 695, 709, 710, 743],
    8: [678, 679, 682, 683],
    9: [714, 740, 741],
    27: [703, 704, 707, 708],
    30: [711],
    32: [688],
    44: [696, 724],
    51: [756],
    52: [686, 687, 690, 700],
    66: [692, 693, 694, 715],
    107: [730, 731, 732, 733, 734, 735, 736, 738],
    125: [697, 705, 737, 739],
    134: [748],
    137: [698, 747, 753, 761, 762, 763, 765, 766],
    158: [725],
    161: [719, 720, 760],
}

updates = []
seen = set()
for story_row, ids in matches.items():
    story = stories[story_row]
    assert story['status'] == '投产完成'
    actual = story['actual']
    if re.fullmatch(r'\d{8}', actual):
        day = dt.datetime.strptime(actual, '%Y%m%d').date()
    else:
        day = dt.date.fromisoformat(actual)
    code = number_by_date.get(day, f'DMXPTKFGJXTCA_{day:%Y%m%d}_V1')
    for feature in ids:
        assert feature not in seen
        seen.add(feature)
        row = rows[feature]
        assert not row['R'] and not row['S']
        updates.append({'id': feature, 'row': row['r'], 'source_row': story_row, 'actual': day.isoformat(),
                        'serial': xlrd.xldate.xldate_from_date_tuple((day.year, day.month, day.day), 0),
                        'code': code, 'code_in_txt': day in number_by_date})

unmatched = [row for feature, row in rows.items() if feature not in seen]
print('Matched:', len(updates), 'unmatched:', len(unmatched))
print('Dates:', collections.Counter((x['actual'], x['code_in_txt']) for x in updates))
print('Unmatched:', '、'.join(str(x['id']) for x in unmatched))
(work / 'workbook-release-plan.json').write_text(json.dumps({'source_sha256': target['sha256'],
    'updates': sorted(updates, key=lambda x: x['row']), 'unmatched': unmatched}, ensure_ascii=False), encoding='utf-8')
