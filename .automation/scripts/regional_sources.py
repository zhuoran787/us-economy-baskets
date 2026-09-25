"""Native close repairs only after both raw-price and adjustment anchors agree."""
import datetime as dt
import json
import math
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo
from update import digest


def repair_native(stock, primary, expected, folder):
    ticker=stock['ticker'];market=stock['market']
    receipt=dict(source='TWSE official daily closes' if market=='XTAI' else 'Naver Finance daily history',requests=[],repaired=[])
    dates=sorted(expected);original=dict(primary)
    raw=json.loads((folder/(ticker+'.json')).read_text())['chart']['result'][0]
    yahoo_close={dt.datetime.fromtimestamp(t,ZoneInfo(stock['timezone'])).date().isoformat():v
                 for t,v in zip(raw['timestamp'],raw['indicators']['quote'][0]['close']) if v is not None}
    anchors={}
    for i,day in enumerate(dates):
        if day in primary:continue
        left=next((d for d in reversed(dates[:i]) if d in original and d in yahoo_close),None)
        right=next((d for d in dates[i+1:] if d in original and d in yahoo_close),None)
        if left and right:anchors[day]=(left,right)
    if not anchors:
        receipt['status']='no_two_sided_anchors';return receipt

    def request(url):
        item=dict(url=url,retrieved_at=dt.datetime.now(dt.timezone.utc).isoformat())
        receipt['requests'].append(item)
        data=urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=25).read()
        item['sha256']=digest(data)
        name='native-'+ticker+'-'+str(len(receipt['requests']))+'.raw'
        (folder/name).write_bytes(data);item['file']=name
        return data

    try:
        native={}
        if market=='XTAI':
            code=ticker.removesuffix('.TW')
            if not code.isdigit() or stock['currency']!='TWD':raise ValueError('TWSE identity mismatch')
            months=sorted({d[:7] for day,ends in anchors.items() for d in (day,*ends)})
            for month in months:
                url='https://www.twse.com.tw/exchangeReport/STOCK_DAY?'+urllib.parse.urlencode(dict(response='json',date=month.replace('-','')+'01',stockNo=code))
                data=json.loads(request(url))
                if data.get('stat')!='OK' or not re.search(r'\b'+re.escape(code)+r'\b',data.get('title','')):raise ValueError('TWSE response identity/status mismatch')
                col=data['fields'].index('收盤價')
                for row in data['data']:
                    year,month_,day_=map(int,row[0].split('/'))
                    date=dt.date(year+1911,month_,day_).isoformat()
                    native[date]=float(row[col].replace(',',''))
        elif market=='XKRX':
            code=ticker.removesuffix('.KS')
            if not code.isdigit() or stock['currency']!='KRW':raise ValueError('Naver identity mismatch')
            url='https://fchart.stock.naver.com/sise.nhn?'+urllib.parse.urlencode(dict(symbol=code,timeframe='day',count=1000,requestType=0))
            xml=ET.fromstring(request(url).decode('euc-kr'))
            chart=xml.find('.//chartdata')
            if chart is None or chart.attrib.get('symbol')!=code:raise ValueError('Naver response identity mismatch')
            for row in chart.findall('item'):
                fields=row.attrib['data'].split('|');date=dt.datetime.strptime(fields[0],'%Y%m%d').date().isoformat()
                native[date]=float(fields[4])
        else:raise ValueError('Unsupported native source')
        for day,(left,right) in anchors.items():
            if not all(d in native and math.isfinite(native[d]) and native[d]>0 for d in (left,day,right)):continue
            if any(abs(native[d]-yahoo_close[d])>max(.03,abs(yahoo_close[d])*1e-5) for d in (left,right)):continue
            factors=[original[d]/native[d] for d in (left,right)]
            if abs(factors[0]/factors[1]-1)>1e-4:continue
            # A constant verified factor spans the gap. Never interpolate returns.
            primary[day]=native[day]*factors[0]
            receipt['repaired'].append(dict(date=day,close=native[day],adjusted=primary[day],anchors=[left,right],factors=factors))
        receipt['status']='verified' if receipt['repaired'] else 'not_used_anchor_or_date_mismatch'
    except Exception as exc:
        receipt.update(status='unavailable',error=str(exc))
    return receipt
