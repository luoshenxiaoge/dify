import collections
import datetime as dt
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

folder = Path('C:/Users/Administrator/Desktop/工作文档/邮储/功能点梳理')
ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
rel = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'

with ZipFile(folder / '大模型上线计划统计1105.xlsx') as z:
    book = ET.fromstring(z.read('xl/workbook.xml'))
    refs = {item.attrib['Id']: item.attrib['Target'] for item in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
    name = next(x for x in book.find('s:sheets', ns) if x.attrib['name'] == '2-工具上线')
    target = refs[name.attrib[rel]]
    strings = []
    if 'xl/sharedStrings.xml' in z.namelist():
        strings = [''.join(x.text or '' for x in item.findall('.//s:t', ns)) for item in ET.fromstring(z.read('xl/sharedStrings.xml'))]
    sheet = ET.fromstring(z.read(target[1:] if target.startswith('/') else 'xl/' + target))
    epoch = dt.datetime(1899, 12, 30)
    props = book.find('s:workbookPr', ns)
    if props is not None and props.get('date1904') in ('1', 'true'):
        epoch = dt.datetime(1904, 1, 1)


def extract(cell):
    v = cell.findtext('s:v', namespaces=ns)
    if cell.get('t') == 's':
        return strings[int(v)]
    if cell.get('t') == 'inlineStr':
        return ''.join(x.text or '' for x in cell.findall('.//s:t', ns))
    return v or ''


def date(v):
    if re.fullmatch(r'4\d{4}(\.\d+)?', v):
        return (epoch + dt.timedelta(days=float(v))).date().isoformat()
    return v


stories = []
for row in sheet.findall('s:sheetData/s:row', ns):
    cols = {re.match(r'[A-Z]+', cell.get('r')).group(): extract(cell) for cell in row}
    r = int(row.get('r'))
    if r == 1 or not cols.get('C'):
        continue
    stories.append(dict(row=r, id=cols.get('A'), module=cols.get('B'), name=cols['C'], description=cols.get('D',''),
                        plan=date(cols.get('H','')), status=cols.get('I'), actual=date(cols.get('AL',''))))

print('actual dates:', collections.Counter(x['actual'] for x in stories).most_common(35))
Path('.codex/workbook-release-stories.json').write_text(json.dumps(stories, ensure_ascii=False), encoding='utf-8')
kw = r'知识库|文档解析|OCR|混合检索|流水线|rabbitmq|pandas|孤立文件|数据库备份|pvc备份|mcp|插件自动|工作流|API文档|知识检索|文档上传|检索优化'
for x in stories:
    if re.search(kw, f"{x['name']} {x['description']}", re.I):
        print(x['row'], x['id'], x['actual'], x['plan'], x['status'], x['name'].replace('\n',' / '),
              x['description'].replace('\n',' / ')[:900], sep=' | ')
