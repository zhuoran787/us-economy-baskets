"""Two behavioral cases: daily equal weighting and explicitly requested missing-day carry."""
from update import equal_weight_index, basket_with_gaps

# A doubles and then halves; B is flat. Daily rebalancing differs from buy-and-hold.
assert equal_weight_index([[100,100],[200,100],[100,100]],100) == [100,150,112.5]

# An incomplete basket day stays flat; the next complete observation catches up.
dates=['2026-09-21','2026-09-22','2026-09-23']
prices={'A':{dates[0]:100,dates[2]:150},'B':dict.fromkeys(dates,100)}
values,flags,last=basket_with_gaps(['A','B'],prices,dates,100,True)
assert values == [100,100,125]
assert [f['kind'] for f in flags] == ['carried','catchup']
assert last == dates[-1]
try:
    basket_with_gaps(['A','B'],prices,dates,100,False)
except ValueError:
    pass
else:
    raise AssertionError('Strict mode must reject missing data')
print('PASS: daily equal weights; missing-day carry, catch-up and strict mode')

# Newly listed constituents do not create fictitious pre-IPO prices or change weights.
dates=['2024-03-19','2024-03-20','2024-03-21']
prices={'A':dict(zip(dates,[100,200,220])),'B':{dates[1]:100,dates[2]:90}}
values,flags,last=basket_with_gaps(['A','B'],prices,dates,100,True,dates[1])
assert values == [None,100,100] and not flags
try:
    basket_with_gaps(['A','B'],prices,dates,100,True,dates[0])
except ValueError:
    pass
else:
    raise AssertionError('Missing initial post-listing prices must still reject')
print('PASS: pre-listing nulls; full-basket weights; missing initial observation rejects')
