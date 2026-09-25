"""Two cross-market cases: known holiday vs missing trading-day quote."""
import math
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from regions import align, sessions, region_groups
from regional_sources import repair_native
from update import basket_with_gaps, load_config, validate_memberships

def close(a,b):assert all(math.isclose(x,y,rel_tol=1e-10) for x,y in zip(a,b)),(a,b)
# A venue closes on day 2. Its known quote is unchanged while B trades.
# Multiplying either quote by an arbitrary currency unit cannot change returns.
dates=['2026-09-21','2026-09-22','2026-09-23']
a,_=align({dates[0]:100,dates[2]:110},{dates[0],dates[2]},dates)
b=dict(zip(dates,[20,22,22]))
x,flags,_=basket_with_gaps(['A','B'],{'A':a,'B':b},dates,100,True)
close(x,[100,105,110.25]);assert not flags
scaled={d:v*1000 for d,v in b.items()}
y,_,_=basket_with_gaps(['A','B'],{'A':a,'B':scaled},dates,100,True);close(x,y)
# A quote is missing on an actual open day. The entire basket carries; the
# following known holiday must NOT silently heal the missing open-day quote.
dates=['2026-09-21','2026-09-22','2026-09-23','2026-09-24']
a,_=align({dates[0]:100,dates[3]:120},{dates[0],dates[1],dates[3]},dates)
b=dict(zip(dates,[100,110,120,130]))
x,flags,_=basket_with_gaps(['A','B'],{'A':a,'B':b},dates,100,True)
close(x,[100,100,100,125]);assert [f['kind'] for f in flags]==['carried','carried','catchup']
c,_,_=load_config()
for r in c['regions']:
 assert len(set(s['ticker'] for s in r['stocks']))==len(r['stocks'])
 assert set(s['group'] for s in r['stocks'])=={g['id'] for g in region_groups(c,r)}
 assert all(s['selection_market_cap_usd']>=c['regional_selection']['minimum_market_cap_usd_at_selection'] for s in r['stocks'])
 assert not any(s['ticker']=='PHI' or s['market']=='PH' for s in r['stocks'])
assert '2024-10-31' not in sessions(c,'XTAI','2024-10-30','2024-11-01')
assert '2026-07-17' not in sessions(c,'XKRX','2026-07-16','2026-07-20')
assert '2026-06-03' not in sessions(c,'XKRX','2026-06-02','2026-06-04')
# Reject both the same symbol in another region and another listing of one issuer.
for symbol in ['NVDA','TSM']:
 bad=copy.deepcopy(c)
 bad['regions'][0]['stocks'][0]['ticker']=symbol
 try:validate_memberships(bad)
 except ValueError as exc:assert '重复' in str(exc)
 else:raise AssertionError('Cross-basket duplicate must stop publication: '+symbol)
assert '2024-03-27' in sessions(c,'XKLS','2024-03-27','2024-03-29')
assert '2024-03-28' not in sessions(c,'XKLS','2024-03-27','2024-03-29')
assert '2026-01-02' not in sessions(c,'VN','2026-01-01','2026-01-05')
try:sessions(c,'VN','2027-01-01','2027-01-05')
except ValueError:pass
else:raise AssertionError('Unverified holiday year must fail closed')
# A genuine native close can fill a hole at a verified constant adjustment factor;
# a corporate-action boundary or a wrong security must not be bridged.
with tempfile.TemporaryDirectory() as temp:
 folder=Path(temp);days=['2025-08-04','2025-08-05','2025-08-06']
 stamps=[int(dt.datetime.fromisoformat(d).replace(tzinfo=dt.timezone.utc).timestamp()) for d in days]
 raw={'chart':{'result':[{'timestamp':stamps,'indicators':{'quote':[{'close':[100,None,110]}]}}]}}
 (folder/'2330.TW.json').write_text(json.dumps(raw))
 stock=dict(ticker='2330.TW',market='XTAI',currency='TWD',timezone='Asia/Taipei')
 class Response:
  def __init__(self,title):self.title=title
  def read(self):return json.dumps(dict(stat='OK',title=self.title,fields=['日期','收盤價'],data=[['114/08/04','100'],['114/08/05','105'],['114/08/06','110']])).encode()
 for title,right,expected_value in [('114年08月 2330 台積電',55,52.5),('114年08月 2330 台積電',54,None),('114年08月 2317 鴻海',55,None)]:
  prices={days[0]:50,days[2]:right}
  with patch('regional_sources.urllib.request.urlopen',return_value=Response(title)):
   repair_native(stock,prices,set(days),folder)
  assert prices.get(days[1])==expected_value, (title,right,prices)
print('PASS: regional currency-unit invariance; true holiday; missing-open-day carry and recovery; membership; calendar limits')
print('PASS: native close repair; reject changed adjustment factors and wrong security; cross-basket ticker and issuer aliases')
