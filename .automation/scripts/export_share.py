"""Export the audited report to a portable, offline-capable public document."""
from pathlib import Path
import argparse
import hashlib
import html
import json
import re
from update import SKILL, load_config


def export(target):
    config, fingerprint, _ = load_config()
    output = Path(config['output_dir'])
    data = json.loads((output / 'latest.json').read_text())
    if data['config_hash'] != fingerprint:
        raise ValueError('配置已更改，须先刷新数据，再发布')
    audit = json.loads((Path(data['source_run']) / 'audit.json').read_text())
    if audit['status'] != 'passed' or audit['config_hash'] != fingerprint:
        raise ValueError('本次数据审计未通过')
    if any(g['history_start'] != data['dates'][0] for g in data['series']):
        raise ValueError('全部篮子必须具有相同历史起点')
    data.update(region='us', region_name='美国', title='美国各经济板块股票表现')
    reports=[data]
    for region in config.get('regions',[]):
        report=json.loads((output/'regions'/region['id']/'latest.json').read_text())
        ra=json.loads((Path(report['source_run'])/'audit.json').read_text())
        if report['config_hash']!=fingerprint or ra['config_hash']!=fingerprint or ra['status']!='passed':
            raise ValueError('地区配置或审计不符')
        if report['requested_end']!=data['requested_end'] or report['dates'][0]!=data['dates'][0]:
            raise ValueError('地区请求日期或起点不一致')
        report['region_name']=region['name'];reports.append(report)
    for report in reports: report.pop('source_run',None)
    source = (SKILL / 'assets/chart.html').read_text()
    source = source.replace('__BASKET_DATA__', json.dumps({'reports':reports}, ensure_ascii=False).replace('</', '<\\/'))
    d3 = (SKILL / 'assets/d3.min.js').read_text().replace('</script', '<\\/script')
    source = re.sub(r'<script src="https://cdn.jsdelivr.net/npm/d3@7\.9\.0/dist/d3\.min\.js"></script>', lambda _: '<script>'+d3+'</script>', source)
    notes = [g['name']+'：'+str(sum(f['kind']=='carried' for f in g['flags']))+'个交易日沿用前值'
             for g in data['series'] if any(f['kind']=='carried' for f in g['flags'])]
    notice = config['publication']['schedule']['label']+'自动更新；当前数据截至'+data['as_of']+'。'+'；'.join(notes)
    source = source.replace('<details>', '<p class="text-small">'+html.escape(notice)+'</p>\n<details>', 1)
    page = (SKILL / 'assets/share-shell.html').read_text().replace('__BASKET_FRAGMENT__', html.escape(source))
    page = page.replace('__BASKET_TITLE__', html.escape('各经济板块股票表现｜美国·欧洲·日本·东南亚｜截至'+data['as_of']))
    if '/Users/' in page or re.search(r'<script[^>]*src=', html.unescape(page)):
        raise ValueError('分享页面包含本地路径或外部脚本')
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page)
    receipt = dict(as_of=data['as_of'], history_start=data['dates'][0], config_hash=fingerprint,
                   html_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                   baskets=sum(len(r['series']) for r in reports), stocks=sum(len(g['tickers']) for r in reports for g in r['series']),
                   regions=[dict(id=r['region'],as_of=r['as_of'],baskets=len(r['series']),stocks=sum(len(g['tickers']) for g in r['series']),carried_days=r['carried_days']) for r in reports],
                   carried_days=sum(r['carried_days'] for r in reports), url=config['publication']['url'])
    (target.parent / 'publication.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(receipt, ensure_ascii=False))
    return receipt


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('target', type=Path)
    export(p.parse_args().target)
