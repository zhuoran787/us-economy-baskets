"""Native-currency regional baskets, using exchange sessions and the shared EW engine."""
import argparse
import concurrent.futures
import datetime as dt
import json
import math
from pathlib import Path
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
from update import SKILL, load_config, digest, write_json, fetch_one, basket_with_gaps, default_end, repair_prices
from regional_sources import repair_native


def sessions(c, market, start, end):
    spec = c['regional_markets'][market]
    if spec.get('calendar') == 'explicit_weekdays':
        if end > spec['valid_through']:
            raise ValueError(market + ': 须核验并延长交易所假日日历')
        days = { (dt.date.fromisoformat(start)+dt.timedelta(days=i)).isoformat()
                 for i in range((dt.date.fromisoformat(end)-dt.date.fromisoformat(start)).days+1)
                 if (dt.date.fromisoformat(start)+dt.timedelta(days=i)).weekday() < 5 }
    else:
        cal = xcals.get_calendar(spec['calendar'], start=start, end=end)
        days = {str(d.date()) for d in cal.sessions}
    days -= set(spec.get('extra_closed', []))
    days |= {d for d in spec.get('extra_open', []) if start <= d <= end}
    return days


def parse(raw, stock, start, end):
    result = json.loads(raw)['chart']['result'][0]
    meta = result['meta']
    if meta['symbol'] != stock['ticker'] or meta.get('currency') != stock['currency']:
        raise ValueError(stock['ticker'] + ': 代码或报价币种不符')
    if meta.get('exchangeTimezoneName') != stock['timezone']:
        raise ValueError(stock['ticker'] + ': 交易所时区变化，须检查')
    stamps = result.get('timestamp', [])
    adjusted = result['indicators'].get('adjclose', [{}])[0].get('adjclose', [])
    if not stamps or len(stamps) != len(adjusted):
        raise ValueError(stock['ticker'] + ': 缺少复权历史')
    prices = {}
    for stamp, price in zip(stamps, adjusted):
        day = dt.datetime.fromtimestamp(stamp, ZoneInfo(stock['timezone'])).date().isoformat()
        if not start <= day <= end or price is None:
            continue
        if day in prices or not math.isfinite(price) or price <= 0:
            raise ValueError(stock['ticker'] + ': 重复日期或非正价格')
        prices[day] = float(price)
    if len(prices) < 2:
        raise ValueError(stock['ticker'] + ': 历史不足')
    return prices


def supplement(stock, primary, expected, folder, start, end, output=None):
    """Preserve an actual alternate request; never mix an unadjusted close into adjclose."""
    ticker = stock['ticker']
    missing = sorted(expected - set(primary))
    receipt = dict(ticker=ticker, missing=missing, repaired=[], attempts=[])
    if not missing:
        return receipt
    if stock['market']=='US' and output is not None:
        cache=Path(output)/'raw'/(ticker+'.json')
        if cache.exists():
            repair=repair_prices(ticker,(folder/(ticker+'.json')).read_bytes(),primary,sorted(expected),Path(output),folder)
            receipt['attempts'].append(dict(source='Comein historical K-line cache',details=repair))
            receipt['repaired'].extend(repair['repaired'])
            missing=sorted(expected-set(primary))
            if not missing:
                receipt['remaining']=[]
                return receipt
    # A different Yahoo response is a fallback route, not an independent provider.
    p1 = int(dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc).timestamp())-86400
    p2 = int(dt.datetime.fromisoformat(end).replace(tzinfo=dt.timezone.utc).timestamp())+86400
    url = 'https://query2.finance.yahoo.com/v8/finance/chart/'+urllib.parse.quote(ticker)+'?'+urllib.parse.urlencode(dict(period1=p1,period2=p2,interval='1d',events='div,splits'))
    attempt = dict(source='Yahoo alternate endpoint (same provider)', url=url)
    try:
        raw = urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=25).read()
        (folder/('alternate-'+ticker+'.json')).write_bytes(raw)
        other = parse(raw,stock,start,end)
        anchors = sorted(set(primary)&set(other))
        if len(anchors)<2 or any(abs(other[d]/primary[d]-1)>.00001 for d in anchors):
            raise ValueError('复权基准不一致')
        for d in missing:
            if d in other:
                primary[d]=other[d];receipt['repaired'].append(d)
        attempt.update(sha256=digest(raw),status='verified')
    except Exception as exc:
        attempt.update(status='unavailable',error=str(exc))
    receipt['attempts'].append(attempt)
    remaining=sorted(expected-set(primary))
    if remaining and stock['market'] in ('XTAI','XKRX'):
        native=repair_native(stock,primary,expected,folder)
        receipt['attempts'].append(native)
        receipt['repaired'].extend(item['date'] for item in native['repaired'])
        remaining=sorted(expected-set(primary))
    if remaining:
        # Stooq is independent. Its daily-close adjustment policy is not certified
        # for every venue: archive the response but do not invent a conversion.
        url='https://stooq.com/q/d/l/?'+urllib.parse.urlencode(dict(s=stock['stooq'],d1=start.replace('-',''),d2=end.replace('-',''),i='d'))
        attempt=dict(source='Stooq',url=url)
        try:
            raw=urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=20).read()
            (folder/('stooq-'+ticker+'.csv')).write_bytes(raw)
            attempt.update(sha256=digest(raw),status='not_used',reason='未确认身份及分红复权口径，不能直接拼接')
        except Exception as exc:
            attempt.update(status='unavailable',error=str(exc))
        receipt['attempts'].append(attempt)
    receipt['remaining']=remaining
    return receipt


def align(prices, expected, dates):
    """A known closure carries a known prior quote; a missing open-day quote stays unknown."""
    result={}; previous=None; closed=[]
    for day in dates:
        if day in expected:
            previous=prices.get(day)
        elif previous is not None:
            closed.append(day)
        if previous is not None:
            result[day]=previous
    return result,closed


def region_groups(c, region):
    return region.get('groups') or [g for g in c['groups'] if g['panel']=='economy']


def run_region(c, fingerprint, region, end, seed=None):
    output=Path(c['output_dir'])/'regions'/region['id']
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder=output/'runs'/stamp;folder.mkdir(parents=True)
    stocks=region['stocks'];start=c['history_start']
    tickers=[s['ticker'] for s in stocks]
    if len(set(tickers))!=len(tickers):raise ValueError('地区成分重复')
    group_defs=region_groups(c,region)
    if set(s['group'] for s in stocks)!=set(g['id'] for g in group_defs):raise ValueError('配置分组与成分不一致')
    panel_id=region.get('panel_id','economy')
    manifest=dict(config_hash=fingerprint,requested_end=end,files=[])
    def get(stock):
        ticker=stock['ticker']
        if seed:
            # Research receipts are copied, never silently refreshed or changed.
            source=Path(seed)/(ticker+'-history.json');raw=source.read_bytes()
            (folder/(ticker+'.json')).write_bytes(raw)
            metadata=json.loads((Path(seed)/(ticker+'.json')).read_text())
            return dict(ticker=ticker,sha256=digest(raw),source='research preflight receipt',url=metadata['history_url'],retrieved_at=metadata['retrieved_at'])
        return fetch_one(ticker,(dt.date.fromisoformat(start)-dt.timedelta(days=1)).isoformat(),end,folder)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        manifest['files']=list(pool.map(get,stocks))
    write_json(folder/'manifest.json',manifest)
    if any(f.get('error') for f in manifest['files']):raise ValueError('地区取数失败，见manifest')
    calendars={s['market']:sessions(c,s['market'],start,end) for s in stocks}
    us=json.loads((Path(c['output_dir'])/'latest.json').read_text())
    if us['config_hash']!=fingerprint or us['requested_end']!=end:raise ValueError('美国报告需先以相同配置和日期更新')
    us_sessions=sessions(c,'US',start,end)
    dates=sorted(set.union(us_sessions,*calendars.values()))
    if not dates or dates[0]!=start:raise ValueError('不能建立共同历史起点')
    source_prices={}
    for stock,receipt in zip(stocks,manifest['files']):
        raw=(folder/(stock['ticker']+'.json')).read_bytes()
        if digest(raw)!=receipt['sha256']:raise ValueError('行情指纹不符')
        source_prices[stock['ticker']]=parse(raw,stock,start,end)
    def repair(stock):
        return supplement(stock,source_prices[stock['ticker']],calendars[stock['market']],folder,start,end,c['output_dir'])
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        repairs=list(pool.map(repair,stocks))
    write_json(folder/'repairs.json',repairs)
    prices={};closures={};anomalies=[];last_quotes={}
    for stock in stocks:
        t=stock['ticker'];expected=calendars[stock['market']]
        observed={d:v for d,v in source_prices[t].items() if d in expected}
        if not observed or (dt.date.fromisoformat(end)-dt.date.fromisoformat(max(observed))).days>10:
            raise ValueError(t+': 价格明显滞后，检查停牌或数据源')
        last_quotes[t]=max(observed)
        for a,b in zip(sorted(observed),sorted(observed)[1:]):
            if abs(observed[b]/observed[a]-1)>.5:
                anomalies.append(dict(ticker=t,from_date=a,date=b,return_pct=100*(observed[b]/observed[a]-1)))
        prices[t],closures[t]=align(observed,expected,dates)
    write_json(folder/'calendar-audit.json',dict(sessions={m:sorted(v) for m,v in calendars.items()},closed_days=closures))
    if anomalies:
        write_json(folder/'anomalies.json',anomalies);raise ValueError('地区复权价格异动超过50%，须核验')
    series=[]
    for group in group_defs:
        members=[s for s in stocks if s['group']==group['id']];ts=[s['ticker'] for s in members]
        values,flags,complete=basket_with_gaps(ts,prices,dates,c['base'],True)
        series.append(dict(id=group['id'],name=group['name'],panel=panel_id,tickers=ts,
                           values=values,flags=flags,last_complete=complete,history_start=start,members=members,last_quotes={t:last_quotes[t] for t in ts}))
    ub=us['benchmark'];known={d:v for d,v in zip(us['dates'],ub['values']) if v is not None}
    benchmark_values=[];prev=None
    for d in dates:
        if d in known:prev=known[d]
        elif d in us_sessions:raise ValueError('美国基准缺少交易日，不能按休市处理')
        if prev is None:raise ValueError('基准共同起点不完整')
        benchmark_values.append(prev)
    report=dict(config_hash=fingerprint,source_run=str(folder),source='Yahoo adjusted close; alternate receipts retained',
                dates=dates,as_of=dates[-1],requested_end=end,generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                methodology=c['rebalance'],selection_date=c['selection_date'],series=series,
                benchmark=dict(ub,values=benchmark_values),chart=c['chart'],
                carried_days=sum(sum(f['kind']=='carried' for f in g['flags']) for g in series),
                missing_day_policy=c['missing_day_policy'],region=region['id'],title=region.get('title',region['name']+'各经济板块股票表现'),
                region_note=region['note'],currency_note='各股本币复权收益 · 不加入货币换算收益',
                panels=[dict(id=panel_id,name=region.get('panel_name','传统板块4个＋银行1个'))])
    audit=dict(status='passed',config_hash=fingerprint,stocks=len(stocks),baskets=len(series),first=start,last=dates[-1],carried_days=report['carried_days'])
    write_json(folder/'report.json',report);write_json(folder/'audit.json',audit)
    output.mkdir(parents=True,exist_ok=True);write_json(output/'latest.json',report)
    print(json.dumps(dict(region=region['id'],**audit),ensure_ascii=False),flush=True)
    return report


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--end',default=default_end());parser.add_argument('--seed');args=parser.parse_args()
    c,fingerprint,_=load_config()
    for region in c['regions']:run_region(c,fingerprint,region,args.end,args.seed)


if __name__=='__main__':main()
