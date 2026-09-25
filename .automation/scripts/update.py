#!/usr/bin/env python3
"""Download, audit and render the approved baskets. Standard library only."""
import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import math
import pathlib
import sys
import time
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

SKILL = pathlib.Path(__file__).resolve().parents[1]
CONFIG = SKILL / 'assets/config.json'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def validate_memberships(c):
    """One direct listed-company exposure across every basket, including ADR aliases."""
    memberships = [(t, 'us/'+g['id']) for g in c['groups'] for t in g['tickers']]
    memberships += [(s['ticker'], r['id']+'/'+s['group'])
                    for r in c.get('regions', []) for s in r['stocks']]
    identities = c['issuer_by_ticker']
    tickers, issuers = {}, {}
    for ticker, location in memberships:
        if ticker in tickers:
            raise ValueError(f'跨篮子股票重复: {ticker}: {tickers[ticker]} / {location}')
        issuer = identities.get(ticker, '').strip().casefold()
        if not issuer:
            raise ValueError(f'{ticker}: 须先核验上市公司身份并登记 issuer_by_ticker')
        if issuer in issuers:
            raise ValueError(f'同一公司重复（含双重上市/ADR）: {ticker}: {issuers[issuer]} / {location}')
        tickers[ticker] = location
        issuers[issuer] = location
    return dict(stock_memberships=len(memberships), unique_tickers=len(tickers),
                unique_issuers=len(issuers), duplicate_tickers=0, duplicate_issuers=0)


def load_config():
    raw = CONFIG.read_bytes()
    c = json.loads(raw)
    tickers = [t for g in c['groups'] for t in g['tickers']]
    if len(tickers) != len(set(tickers)) or not tickers:
        raise ValueError('成分股重复或为空')
    validate_memberships(c)
    if c['rebalance'] != 'daily_equal_weight_returns':
        raise ValueError('当前实现仅接受每日等权收益')
    return c, digest(raw), tickers


def default_end():
    now = dt.datetime.now(ZoneInfo('America/New_York'))
    # Exclude a potentially incomplete regular session; allow provider settlement time.
    day = now.date() if now.hour >= 17 else now.date() - dt.timedelta(days=1)
    return day.isoformat()


def fetch_one(ticker, start, end, folder):
    tz = ZoneInfo('America/New_York')
    p1 = int(dt.datetime.combine(dt.date.fromisoformat(start), dt.time(), tz).timestamp())
    p2 = int(dt.datetime.combine(dt.date.fromisoformat(end) + dt.timedelta(days=1), dt.time(), tz).timestamp())
    params = urllib.parse.urlencode(dict(period1=p1, period2=p2, interval='1d', events='div,splits'))
    url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + urllib.parse.quote(ticker) + '?' + params
    error = None
    for attempt in range(2):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(request, timeout=25) as response:
                raw = response.read()
            data = json.loads(raw)
            result = data['chart']['result'][0]
            if data['chart'].get('error') or result['meta']['symbol'] != ticker:
                raise ValueError('代码或响应错误')
            path = folder / (ticker + '.json')
            path.write_bytes(raw)
            return dict(ticker=ticker, sha256=digest(raw), url=url,
                        retrieved_at=dt.datetime.now(dt.timezone.utc).isoformat())
        except Exception as exc:
            error = str(exc)
            if attempt == 0:
                time.sleep(1)
    return dict(ticker=ticker, error=error, url=url)


def fetch(c, fingerprint, tickers, end):
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder = pathlib.Path(c['output_dir']) / 'runs' / stamp
    folder.mkdir(parents=True)
    manifest = dict(config_hash=fingerprint, requested_end=end, history_start=c['history_start'], files=[])
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch_one, t, c['history_start'], end, folder)
                   for t in list(dict.fromkeys(tickers + [c['calendar_reference'], c['benchmark']['ticker']]))]
        for future in concurrent.futures.as_completed(futures):
            manifest['files'].append(future.result())
    write_json(folder / 'manifest.json', manifest)
    failures = [f for f in manifest['files'] if f.get('error')]
    print(json.dumps(dict(run=str(folder), files=len(manifest['files']), failures=failures), ensure_ascii=False), flush=True)
    if failures:
        raise ValueError('取数失败，保留旧报告；见本次manifest')
    return folder


def parse_prices(raw, ticker, start, end):
    result = json.loads(raw)['chart']['result'][0]
    meta = result['meta']
    if meta.get('symbol') != ticker or meta.get('currency') != 'USD':
        raise ValueError(f'{ticker}: 代码或币种异常')
    timestamps = result.get('timestamp', [])
    adjusted = result['indicators'].get('adjclose', [{}])[0].get('adjclose', [])
    if len(timestamps) != len(adjusted):
        raise ValueError(f'{ticker}: 无完整复权字段')
    prices = {}
    for timestamp, price in zip(timestamps, adjusted):
        day = dt.datetime.fromtimestamp(timestamp, ZoneInfo('America/New_York')).date().isoformat()
        if not start <= day <= end:
            continue
        if price is None:
            continue
        if day in prices or not math.isfinite(price) or price <= 0:
            raise ValueError(f'{ticker}: {day}重复或无效复权价')
        prices[day] = float(price)
    if len(prices) < 2:
        raise ValueError(f'{ticker}: 历史不足；检查停牌/退市/改名')
    return prices


def repair_prices(ticker, raw, prices, dates, output, folder):
    """Use observed MCP prices only when two Yahoo anchors confirm the adjustment basis."""
    missing = [d for d in dates if d not in prices]
    receipt = dict(ticker=ticker, missing=missing, repaired=[], attempts=['Yahoo daily history'])
    if not missing:
        return receipt
    cache = output / 'raw' / (ticker + '.json')
    receipt['attempts'].append('Comein historical K-line cache')
    if not cache.exists():
        receipt['needs_source_attempt'] = True
        receipt['reason'] = '需调用进门历史K线；无可用回执'
        return receipt
    saved = cache.read_bytes()
    envelope = json.loads(saved)
    if envelope.get('requested_end', '') < missing[-1]:
        receipt['needs_source_attempt'] = True
        receipt['reason'] = '补充回执未覆盖缺失日期，需重新查询'
        return receipt
    try:
        text = next(b['text'] for b in envelope['response']['content'] if b['type'] == 'text')
        response = json.loads(text)
    except (KeyError, StopIteration, json.JSONDecodeError):
        (folder / ('comein-' + ticker + '.json')).write_bytes(saved)
        receipt['secondary_sha256'] = digest(saved)
        receipt['reason'] = '补充源实际请求失败，保留原始回执'
        return receipt
    payload = response.get('data') or {}
    info = payload.get('standardized_info', {})
    if info.get('stock_code') != ticker or info.get('currency') != 'USD':
        receipt['needs_source_attempt'] = True
        receipt['reason'] = '补充来源代码/币种不符或无数据'
        return receipt
    bars = {b['period_end_date']: b for b in payload.get('bars', [])}
    yahoo = json.loads(raw)['chart']['result'][0]
    timestamps = yahoo['timestamp']
    closes = yahoo['indicators']['quote'][0]['close']
    close = {dt.datetime.fromtimestamp(t, ZoneInfo('America/New_York')).date().isoformat():v
             for t,v in zip(timestamps,closes)}
    original = dict(prices)
    for day in missing:
        i = dates.index(day)
        left = next((d for d in reversed(dates[:i]) if d in original), None)
        right = next((d for d in dates[i+1:] if d in original), None)
        if not left or not right or any(d not in bars for d in (left, day, right)):
            continue
        try:
            factors = [original[d] / float(bars[d]['front_adjust_close_price']) for d in (left,right)]
            if abs(factors[0]/factors[1]-1) > .0001:
                continue
            if any(close.get(d) is None or abs(close[d]-float(bars[d]['close_price'])) > .03 for d in (left,right)):
                continue
            value = float(bars[day]['front_adjust_close_price']) * factors[1]
            if not math.isfinite(value) or value <= 0:
                continue
            prices[day] = value
            receipt['repaired'].append(dict(date=day, adjusted_close=value, anchors=[left,right], conversion=factors[1]))
        except (ValueError, TypeError, ZeroDivisionError, KeyError):
            continue
    # Preserve the exact secondary response within this immutable source run.
    (folder / ('comein-' + ticker + '.json')).write_bytes(saved)
    receipt['secondary_sha256'] = digest(saved)
    receipt['remaining'] = [d for d in dates if d not in prices]
    return receipt


def basket_with_gaps(tickers, prices, dates, base, allow_carry, inception=None):
    value = float(base)
    values, flags = [], []
    last_complete = None
    for day in dates:
        if inception and day < inception:
            values.append(None)
            continue
        missing = [t for t in tickers if day not in prices[t]]
        if missing:
            if not allow_carry or last_complete is None:
                raise ValueError(f'{day}: 无法建立完整起点或缺失数据，缺少{missing}')
            flags.append(dict(date=day, kind='carried', missing=missing))
        else:
            if last_complete is not None:
                ret = math.fsum(prices[t][day]/prices[t][last_complete]-1 for t in tickers)/len(tickers)
                value *= 1+ret
                if dates.index(day)-dates.index(last_complete) > 1:
                    flags.append(dict(date=day, kind='catchup', from_date=last_complete))
            last_complete = day
        values.append(round(value,6))
    return values, flags, last_complete


def equal_weight_index(price_rows, base):
    value = float(base)
    result = [value]
    for previous, current in zip(price_rows, price_rows[1:]):
        # Each day starts with identical stock weights. Never average price levels.
        daily_return = math.fsum(b / a - 1 for a, b in zip(previous, current)) / len(previous)
        value *= 1 + daily_return
        result.append(value)
    return result


def build(c, fingerprint, tickers, folder, visual=None):
    manifest = json.loads((folder / 'manifest.json').read_text())
    if manifest['config_hash'] != fingerprint:
        raise ValueError('config已改，先重新取数')
    receipts = {f['ticker']: f for f in manifest['files']}
    prices, primary_raw = {}, {}
    for ticker in list(dict.fromkeys(tickers + [c['calendar_reference'], c['benchmark']['ticker']])):
        raw = (folder / (ticker + '.json')).read_bytes()
        if digest(raw) != receipts[ticker]['sha256']:
            raise ValueError(f'{ticker}: 原始数据指纹不匹配')
        prices[ticker] = parse_prices(raw, ticker, c['history_start'], manifest['requested_end'])
        primary_raw[ticker] = raw
    dates = sorted(prices[c['calendar_reference']])
    latest_listing = max((x['date'] for x in c.get('listing_starts', {}).values()), default=dates[0])
    if dates[0] < latest_listing:
        raise ValueError('统一历史起点早于全部成分开始交易日，请修改history_start后重新取数')
    expected = set(dates)
    if (dt.date.fromisoformat(manifest['requested_end']) - dt.date.fromisoformat(dates[-1])).days > 4:
        raise ValueError('行情源明显滞后；不更新图形')
    output = pathlib.Path(c['output_dir'])
    listing_starts = c.get('listing_starts', {})
    eligible_dates = {t: [d for d in dates if d >= listing_starts.get(t, {}).get('date', dates[0])] for t in tickers}
    repair_receipts = [repair_prices(t, primary_raw[t], prices[t], eligible_dates[t], output, folder) for t in tickers]
    write_json(folder / 'repairs.json', repair_receipts)
    unresolved = [r for r in repair_receipts if any(d not in prices[r['ticker']] for d in eligible_dates[r['ticker']])]
    # A new unresolved gap requires an explicit second-source retrieval attempt first.
    blocked = [r for r in unresolved if r.get('needs_source_attempt')]
    if blocked:
        write_json(folder / 'repair-requests.json', blocked)
        raise ValueError('先查询进门补充历史，保存回执后重跑build；见repair-requests.json')
    warnings, verified_moves = [], []
    verified = {(r['ticker'],r['from_date'],r['date']):r for r in c.get('verified_large_moves', [])}
    for ticker in tickers:
        for a, b in zip(dates, dates[1:]):
            if a not in prices[ticker] or b not in prices[ticker]:
                continue
            move = prices[ticker][b] / prices[ticker][a] - 1
            if abs(move) > .5:
                observation = dict(ticker=ticker, date=b, return_pct=round(move*100, 2))
                review = verified.get((ticker,a,b))
                if review and abs(move-review['return']) < .000001 and digest((SKILL/review['receipt']).read_bytes()) == review['receipt_sha256']:
                    verified_moves.append(dict(**observation, source=review['source'], receipt=review['receipt']))
                else:
                    warnings.append(observation)
    write_json(folder / 'verified-large-moves.json', verified_moves)
    if warnings:
        write_json(folder / 'anomalies.json', warnings)
        raise ValueError('存在单日超过50%的复权变化，先核验公司行为；见anomalies.json')
    series = []
    for group in c['groups']:
        inception = max(listing_starts.get(t, {}).get('date', dates[0]) for t in group['tickers'])
        values, flags, last_complete = basket_with_gaps(group['tickers'], prices, dates, c['base'],
                                                       c['missing_day_policy']=='carry_basket_after_source_attempts', inception)
        if last_complete is None:
            raise ValueError(group['name'] + ': 无完整上市后起点')
        series.append(dict(id=group['id'], name=group['name'], panel=group['panel'],
                           tickers=group['tickers'], values=values, flags=flags, last_complete=last_complete,
                           history_start=next(d for d,v in zip(dates,values) if v is not None)))
    bt = c['benchmark']['ticker']
    bp = prices[bt]
    missing_benchmark = [d for d in dates if d not in bp]
    benchmark_repairs = []
    if missing_benchmark:
        cache = output / 'raw' / 'SPX-index.json'
        if not cache.exists():
            raise ValueError('标普500缺数：先调用进门指数时序 SPX/美股，保存 raw/SPX-index.json')
        saved = cache.read_bytes()
        envelope = json.loads(saved)
        if envelope.get('requested_end', '') < missing_benchmark[-1]:
            raise ValueError('标普500补充回执已过期，先更新 SPX 指数时序')
        payload = json.loads(next(x['text'] for x in envelope['response']['content'] if x['type']=='text'))
        meta = payload.get('metadata', {})
        if meta.get('index_name') != '标普500指数' or meta.get('market_name') != '美股':
            raise ValueError('补充指数身份错误：必须是标普500指数/美股')
        secondary = {r['trading_day']: float(r['close']) for r in payload['data']}
        overlap = set(bp) & set(secondary)
        if len(overlap) < 2 or any(abs(bp[d]-secondary[d]) > .05 for d in overlap):
            raise ValueError('标普500双源收盘点位校验失败')
        for day in missing_benchmark:
            if day in secondary and math.isfinite(secondary[day]) and secondary[day] > 0:
                bp[day] = secondary[day]
                benchmark_repairs.append(dict(date=day, close=bp[day]))
        (folder / 'comein-SPX-index.json').write_bytes(saved)
        write_json(folder / 'benchmark-repairs.json', dict(repairs=benchmark_repairs, sha256=digest(saved), overlap=len(overlap)))
    benchmark_values, benchmark_flags, benchmark_complete = basket_with_gaps([bt], prices, dates, c['base'], True)
    benchmark = dict(id='sp500', name=c['benchmark']['name'], tickers=[bt], values=benchmark_values,
                     flags=benchmark_flags, last_complete=benchmark_complete, benchmark=True)
    report = dict(config_hash=fingerprint, source=c['source'], source_run=str(folder),
                  dates=dates, as_of=dates[-1], requested_end=manifest['requested_end'],
                  generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                  methodology=c['rebalance'], selection_date=c['selection_date'], series=series, benchmark=benchmark,
                  chart=c['chart'],
                  repaired_observations=sum(len(r['repaired']) for r in repair_receipts),
                  carried_days=sum(sum(f['kind']=='carried' for f in g['flags']) for g in series),
                  missing_day_policy=c['missing_day_policy'])
    template = (SKILL / 'assets/chart.html').read_text()
    fragment = template.replace('__BASKET_DATA__', json.dumps(report, ensure_ascii=False).replace('</', '<\\/'))
    # Only publish after every stock and configuration passes audit.
    write_json(folder / 'report.json', report)
    write_json(output / 'latest.json', report)
    (output / 'latest-chart.html').write_text(fragment)
    if visual:
        visual.parent.mkdir(parents=True, exist_ok=True)
        visual.write_text(fragment)
    write_json(folder / 'audit.json', dict(status='passed', stocks=len(tickers), baskets=len(series),
                                         sessions=len(dates), first=dates[0], last=dates[-1], config_hash=fingerprint))
    print(json.dumps(dict(status='passed', stocks=len(tickers), baskets=len(series), sessions=len(dates),
                         first=dates[0], last=dates[-1], report=str(output/'latest-chart.html')), ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['fetch', 'build', 'refresh'])
    parser.add_argument('--end', default=default_end())
    parser.add_argument('--run', type=pathlib.Path)
    parser.add_argument('--visual', type=pathlib.Path)
    args = parser.parse_args()
    c, fingerprint, tickers = load_config()
    folder = args.run
    if args.command in ('fetch', 'refresh'):
        folder = fetch(c, fingerprint, tickers, args.end)
    if args.command in ('build', 'refresh'):
        if folder is None:
            parser.error('build需要--run')
        build(c, fingerprint, tickers, folder, args.visual)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        sys.exit(1)
