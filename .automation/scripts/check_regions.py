"""Two cross-market cases: known holiday vs missing trading-day quote."""
import math
from regions import align, sessions
from update import basket_with_gaps, load_config

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
 assert set(s['group'] for s in r['stocks'])=={g['id'] for g in c['groups'] if g['panel']=='economy'}
 assert all(s['selection_market_cap_usd']>=c['regional_selection']['minimum_market_cap_usd_at_selection'] for s in r['stocks'])
 assert not any(s['ticker']=='PHI' or s['market']=='PH' for s in r['stocks'])
assert '2024-03-27' in sessions(c,'XKLS','2024-03-27','2024-03-29')
assert '2024-03-28' not in sessions(c,'XKLS','2024-03-27','2024-03-29')
assert '2026-01-02' not in sessions(c,'VN','2026-01-01','2026-01-05')
try:sessions(c,'VN','2027-01-01','2027-01-05')
except ValueError:pass
else:raise AssertionError('Unverified holiday year must fail closed')
print('PASS: regional currency-unit invariance; true holiday; missing-open-day carry and recovery; membership; calendar limits')
