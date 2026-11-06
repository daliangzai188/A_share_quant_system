# coding: utf-8
"""Trade calendar feed (11-05): QMT's built-in Python knows no future trading day and may not reach
Tushare, and the shipped calendar ends 20261231 (from 12-02 the daemon would guess weekdays, from 2027
the collector finds no day to collect). The forwarder writes the SSE calendar into the registry:
  1. a feed agreeing with the known past extends the calendar into 2027; days up to today stay
     byte-identical; holidays closed; pretrade_date continues across the year end;
  2. the same feed again, or after a restart, gives the same file and the same mtime (step 13's
     input inventory does not change);
  3. a feed that disagrees about the past, or with a wrong checksum, changes nothing;
  4. with no feed and the calendar ending within 40 days: one push per day;
  5. the feed moving on by a day (the forwarder asks Tushare for today+400 days) rewrites the file but
     leaves step 13's decision valid (11-06: rewritten at 04:15, the 00:50 decision was taken for stale
     at 09:00); closing the next action day still invalidates it.

Usage: python test_calendar_feed.py STRATEGY_FILE   (QMT's Python 3.6)
"""
import datetime, hashlib, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_stop_threads import load
import run_system_local as rs

HOLIDAYS = ('20270101', '20270210', '20270211', '20270212', '20270215', '20270216')


def calendar(ns):
    rows = {}
    for line in ns['QN_VFS'].get_text('data/raw/trade_calendar.csv').splitlines()[1:]:
        cells = line.split(',')
        if len(cells) >= 4:
            rows[cells[1]] = (cells[2] in ('1', '1.0'), cells[3], line)
    return rows


def identity(ns, signal):
    import json
    pmf = ns['QnLoader'](ns['QnCodeBase'](ns['QN_MODULES'])).load('src.post_market_freshness')
    cfg = json.loads(ns['QN_VFS'].get_text('config/config.json'))
    return pmf.producer_identity(ns['QnPath']('/A_System'), signal, cfg)


def make_feed(known, first, last, flip=None, digest=None):
    d, bits = first, []
    while d <= last:
        key = d.strftime('%Y%m%d')
        is_open = known[key][0] if key in known and key <= TODAY else (d.weekday() < 5 and key not in HOLIDAYS)
        if key == flip:
            is_open = not is_open
        bits.append('1' if is_open else '0')
        d += datetime.timedelta(days=1)
    start, text = first.strftime('%Y%m%d'), ''.join(bits)
    return 'v1|SSE|%s|%s|%s' % (start, text, digest or hashlib.sha256((start + text).encode()).hexdigest()[:8])


def boot(path, feed, today=None):
    ns = load(path, {})
    ns['QN_DEPLOY']['calendar_warn_days'] = 40
    ns['QN_CALENDAR_READER'][0] = lambda: feed[0]
    if today is not None:
        real = ns['_bj_now']
        ns['_bj_now'] = lambda: real().replace(year=today.year, month=today.month, day=today.day)
    ns['init'](rs.MockContext())
    return ns, ns['QN_HOST'][0]


def main():
    global TODAY
    path = sys.argv[1]
    TODAY = datetime.date.today().strftime('%Y%m%d')
    feed = ['']
    ns, host = boot(path, feed)
    shipped = calendar(ns)
    first = datetime.date.today() - datetime.timedelta(days=90)
    last = datetime.date(2027, 12, 10)
    feed[0] = make_feed(shipped, first, last)
    host._merge_calendar_feed()
    merged = calendar(ns)
    mtime = ns['QN_VFS'].mtimes[ns['QN_VFS'].abspath('data/raw/trade_calendar.csv')]
    past_same = all(merged[d][2] == shipped[d][2] for d in shipped if d <= TODAY)
    opens = sorted(d for d, v in merged.items() if v[0])
    ok1 = (past_same and opens[-1] >= '20271201' and not merged['20270101'][0] and merged['20270104'][0]
           and merged['20270104'][1] == max(d for d in opens if d < '20270101') and not merged['20270211'][0])
    print('1. extended: open days through %s, past unchanged %s, 2027-01-04 pretrade %s' % (
        opens[-1], past_same, merged['20270104'][1]))
    host._merge_calendar_feed()
    again = ns['QN_VFS'].mtimes[ns['QN_VFS'].abspath('data/raw/trade_calendar.csv')]
    text1 = ns['QN_VFS'].get_text('data/raw/trade_calendar.csv')
    decided = identity(ns, TODAY)
    feed[0] = make_feed(shipped, first + datetime.timedelta(days=1), last + datetime.timedelta(days=1))
    host._merge_calendar_feed()
    moved = ns['QN_VFS'].mtimes[ns['QN_VFS'].abspath('data/raw/trade_calendar.csv')] != again
    still = identity(ns, TODAY) == decided
    next_open = min(d for d, v in calendar(ns).items() if v[0] and d > TODAY)
    feed[0] = make_feed(shipped, first + datetime.timedelta(days=1), last + datetime.timedelta(days=1), flip=next_open)
    host._merge_calendar_feed()
    closed = not calendar(ns)[next_open][0] and identity(ns, TODAY) != decided
    ok5 = moved and still and closed
    print('5. feed one day later: file rewritten %s, decision still valid %s; next action day %s closed: '
          'decision invalid %s' % (moved, still, next_open, closed))
    feed[0] = make_feed(shipped, first, last)
    ns['stop'](rs.MockContext())
    ns2, host2 = boot(path, feed)
    restarted = ns2['QN_VFS'].mtimes[ns2['QN_VFS'].abspath('data/raw/trade_calendar.csv')]
    same_text = ns2['QN_VFS'].get_text('data/raw/trade_calendar.csv') == text1
    ok2 = again == mtime == restarted and same_text
    print('2. same feed again: mtime unchanged %s; after a restart: same file %s, same mtime %s' % (
        again == mtime, same_text, restarted == mtime))
    ns2['stop'](rs.MockContext())
    past_day = max(d for d, v in shipped.items() if v[0] and d < TODAY)
    results = []
    for label, bad in (('past day flipped', make_feed(shipped, first, last, flip=past_day)),
                       ('bad checksum', make_feed(shipped, first, last, digest='00000000'))):
        feed[0] = bad
        ns3, host3 = boot(path, feed)
        unchanged = calendar(ns3) == shipped
        notes = [m for m in host3.messages if 'feed rejected' in m]
        results.append(unchanged and bool(notes))
        print('3. %s: calendar unchanged %s, logged %s' % (label, unchanged, notes[:1]))
        ns3['stop'](rs.MockContext())
    feed[0] = ''
    ns4, host4 = boot(path, feed, today=datetime.date(2026, 12, 1))
    host4.calendar_checked = 0.0
    host4._poll_calendar()
    host4.calendar_checked = 0.0
    host4._poll_calendar()
    pushes = [l for l in ns4['QN_PRINT_LINES'] if '[A_SYSTEM_NOTIFY' in l and '%E4%BA%A4%E6%98%93%E6%97%A5%E5%8E%86' in l]
    print('4. no feed on 2026-12-01: calendar warning pushes %d' % len(pushes))
    ns4['stop'](rs.MockContext())
    ok = ok1 and ok2 and all(results) and len(pushes) == 1 and ok5
    print('RESULT', 'PASS' if ok else 'FAIL')


if __name__ == '__main__':
    main()
