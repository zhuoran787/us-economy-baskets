"""Shared exchange sessions: a missing quote never removes a trading day."""
import datetime as dt
import exchange_calendars as xcals


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

