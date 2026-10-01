from datetime import datetime, timezone

from dbs_reporting.eastern import localize, stamp


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_daylight_saving_and_12_hour_clock():
    assert stamp(utc(2026, 9, 30, 15, 13)) == "09/30/2026 11:13 AM ET"   # EDT, UTC-4
    assert stamp(utc(2026, 1, 15, 17, 5)) == "01/15/2026 12:05 PM ET"    # EST, UTC-5
    assert stamp(utc(2026, 1, 15, 4, 30)) == "01/14/2026 11:30 PM ET"    # crosses back a day
    assert stamp(utc(2026, 1, 15, 5, 0)) == "01/15/2026 12:00 AM ET"
    # 2026: EDT runs from Mar 8 7:00 UTC to Nov 1 6:00 UTC.
    assert stamp(utc(2026, 3, 8, 6, 59)) == "03/08/2026 1:59 AM ET"
    assert stamp(utc(2026, 3, 8, 7, 0)) == "03/08/2026 3:00 AM ET"
    assert stamp(utc(2026, 11, 1, 5, 59)) == "11/01/2026 1:59 AM ET"
    assert stamp(utc(2026, 11, 1, 6, 0)) == "11/01/2026 1:00 AM ET"


def test_localize_tool_results():
    text = '{"entered": "2026-09-30T15:13:00Z", "end": "2026-10-15T00:00:00Z", "noted": "2026-09-30T15:13:00.123Z"}'
    assert localize(text) == ('{"entered": "09/30/2026 11:13 AM ET", "end": "10/15/2026", '
                              '"noted": "09/30/2026 11:13 AM ET"}')
