import collections
import json
from pathlib import Path


work = Path(__file__).parent
data = json.loads((work / 'workbook-classification-data.json').read_text(encoding='utf-8'))
s2 = '附件4-2'
s4 = next(name for name in data if name.startswith('附件4-4'))


def visible(sheet, name_col):
    return {
        int(row['v'][0]): row
        for row in data[sheet]
        if not row['hidden'] and isinstance(row['v'][0], (int, float)) and row['v'][name_col]
    }


rows2 = visible(s2, 4)
rows4 = visible(s4, 5)
groups = {
    2185: [676, 686],  # 知识库 OCR
    2186: [677, 678, 679, 690, 699],  # 知识库解析服务和输入格式
    2188: [680, 687, 749],  # 工作流图像和多模态解析
    2189: [681],
    2190: [682, 683],
    2192: [684, 695, 696, 697, 698, 729, 753, 766],  # 文档接收及文件处理限制
    2193: [685],
    2194: [692, 693, 694, 700, 724, 725, 757, 758, 759],  # 异步处理与运行保障
    2195: [702],
    2196: [703, 705, 706],
    2197: [704, 752, 754, 755],  # API 及鉴权
    2198: [707],
    2199: [708],
    2200: [709, 710, 715],
    2201: [711, 714, 721],
    2202: [712, 718, 719, 722, 723, 760],
    2203: [713, 716, 717, 720, 744, 745, 746, 750],  # 检索和运行效果、调试
    2204: [740, 741, 743, 747, 748, 751, 764, 765],  # 工作流执行和节点相关能力
    2205: [742, 756],
    2206: [734, 736],
    2207: [689, 701, 726, 727, 728, 730, 731, 732, 733, 735, 737, 738, 739, 761, 762, 763],
    2208: [688, 691],
}

coverage = collections.Counter(source for sources in groups.values() for source in sources)
assert coverage == collections.Counter(rows2.keys()), (set(rows2) - set(coverage), coverage)
assert all(key in rows4 for key in groups)
edits = {s2: {}, s4: {}}
original = 0
duplicate_rows = 0
for number, ids in groups.items():
    target = rows4[number]
    assert target['v'][12] in ('', '已完成'), (number, target['v'][12])
    for source in sorted(ids):
        row = rows2[source]
        v = row['v']
        rn = row['r']
        if v[12] == '原功能点':
            assert v[15] == number and v[16] == target['v'][5]
            original += 1
        if v[15] != number:
            edits[s2][f'P{rn}'] = number
        if v[16] != target['v'][5]:
            edits[s2][f'Q{rn}'] = target['v'][5]
        if len(ids) > 1:
            edits[s2][f'T{rn}'] = f'4-4 {number} 拆分功能点'
            duplicate_rows += 1
    edits[s4][f'M{target["r"]}'] = (
        '拆分为 4-2附件的 ' + '、'.join(str(k) for k in sorted(ids))
        if len(ids) > 1 else '已完成'
    )

plan = {
    'edits': edits,
    'groups': groups,
    'stats': {
        'rows': len(rows2),
        'original': original,
        'duplicate_rows': duplicate_rows,
        'duplicate_groups': sum(len(ids) > 1 for ids in groups.values()),
        'target_remarks': len(edits[s4]),
    },
}
(work / 'workbook-classification-plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
print(plan['stats'])
