"""Regression checks for missing calendar-reference quotes and bounded retries."""
import subprocess
from unittest.mock import patch
from market_calendar import sessions
from update import load_config
from cloud_refresh import refresh_with_retries

# A missing SPY close must not erase an open day. A true holiday stays excluded.
c, _, _ = load_config()
assert sorted(sessions(c, 'US', '2026-09-24', '2026-09-26')) == ['2026-09-24', '2026-09-25']
assert sorted(sessions(c, 'US', '2026-09-04', '2026-09-08')) == ['2026-09-04', '2026-09-08']
assert 'calendar_reference' not in c

# A failed regional attempt must repeat BOTH refreshes with the same cutoff.
error = subprocess.CalledProcessError(1, ['regions.py'])
with patch('cloud_refresh.subprocess.run', side_effect=[None, error, None, None]) as run, patch('cloud_refresh.time.sleep') as sleep:
    refresh_with_retries('2026-09-25', [60])
    assert run.call_count == 4 and sleep.call_count == 1
    assert all(call.args[0][-1] == '2026-09-25' for call in run.call_args_list)
    assert 'update.py' in run.call_args_list[2].args[0][1]
# Persistent errors cannot be swallowed or turn into a successful publication.
with patch('cloud_refresh.subprocess.run', side_effect=error) as run, patch('cloud_refresh.time.sleep') as sleep:
    try:
        refresh_with_retries('2026-09-25', [60,60])
    except subprocess.CalledProcessError:
        assert run.call_count == 3 and sleep.call_count == 2
    else:
        raise AssertionError('Exhausted attempts must fail closed')
print('PASS: common exchange calendar; same-cutoff complete retry; persistent failure rejected')
