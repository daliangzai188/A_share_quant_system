# coding: utf-8
"""Rebuild Tushare limit_list_d fields from QMT 3-second snapshots.

Python 3.6 compatible, standard library only, so the same file can later be
embedded unchanged in the QMT built-in strategy.

Tushare definitions (doc_id=298): first_time = first seal time, last_time = last
seal time, open_times = times the seal was broken, fd_amount = buy orders queued at
the limit price (CNY). No snapshot-level algorithm is published, so rule variants
are exposed and validated against Tushare history before one is chosen.

Each snapshot is SEALED, OPEN or UNKNOWN. UNKNOWN never creates a transition.
"""

SEALED = 'S'
OPEN = 'O'
UNKNOWN = '?'
AUCTION_OPEN = '092500'

_BASE = dict(require_last=False, exclude_close_auction=True, min_open_snaps=1,
             ignore_sellers=False, normalize_auction=True, fd_after_close=False,
             merge='none', merge_secs=0)


def _v(**kw):
    d = dict(_BASE)
    d.update(kw)
    return d


VARIANTS = {
    # Previous agent's bid-queue rule over every session snapshot.
    'queue_all': _v(exclude_close_auction=False),
    # Ignore 14:57:00-14:59:59 closing call auction indications.
    'queue_no_close_auction': _v(),
    'queue_no_close_auction_debounce2': _v(min_open_snaps=2),
    'queue_last_no_close_auction': _v(require_last=True),
    # 2026-10-20 round 2 after real QMT data:
    # sealed = bid1 at limit with volume, sellers at the limit do not break the seal;
    # first/last time keep the raw snapshot stamp; seal amount from the post-close book.
    'bid_raw_postclose': _v(ignore_sellers=True, normalize_auction=False, fd_after_close=True),
    'bid_raw_postclose_debounce2': _v(ignore_sellers=True, normalize_auction=False, fd_after_close=True,
                                      min_open_snaps=2),
    # Round 3: Tushare merges breaks that re-seal quickly.
    'bid_raw_merge_minute': _v(ignore_sellers=True, normalize_auction=False, fd_after_close=True,
                               merge='minute'),
    'bid_raw_merge_30s': _v(ignore_sellers=True, normalize_auction=False, fd_after_close=True,
                            merge='reseal', merge_secs=30),
    'bid_raw_merge_60s': _v(ignore_sellers=True, normalize_auction=False, fd_after_close=True,
                            merge='reseal', merge_secs=60),
    'bid_raw_merge_120s': _v(ignore_sellers=True, normalize_auction=False, fd_after_close=True,
                             merge='reseal', merge_secs=120),
    # Round 4: minute-level state machines.
    'bid_raw_minute_any_open': _v(ignore_sellers=True, normalize_auction=False, fd_after_close=True,
                                  merge='minute_any'),
    'bid_raw_minute_last': _v(ignore_sellers=True, normalize_auction=False, fd_after_close=True,
                              merge='minute_last'),
}


def _secs(t):
    return int(t[0:2]) * 3600 + int(t[2:4]) * 60 + int(t[4:6])


def to_cents(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return int(round(number * 100))


def to_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def hhmmss(stamp):
    text = str(stamp).split('.')[0]
    return text[8:14] if len(text) >= 14 else text[-6:]


def in_session(t):
    """09:25 opening auction result through 11:30, and 13:00 through 15:00.

    09:15-09:24:59 is indicative pre-open matching and never a seal source.
    The 09:25-09:29:59 wait and the lunch break keep the previous state.
    """
    return (AUCTION_OPEN <= t <= '113000') or ('130000' <= t <= '150000')


def classify(row, limit_cents, require_last, ignore_sellers=False):
    bid1 = to_cents(row.get('bidPrice1'))
    bid1_vol = to_float(row.get('bidVol1'))
    ask1 = to_cents(row.get('askPrice1'))
    ask1_vol = to_float(row.get('askVol1'))
    if bid1 is None or bid1_vol is None:
        return UNKNOWN
    if not bid1 and not bid1_vol and not ask1:
        return UNKNOWN  # empty book: halt or missing snapshot
    sellers_at_limit = ask1 is not None and ask1 > 0 and bool(ask1_vol) and ask1 <= limit_cents
    sealed = bid1 == limit_cents and bid1_vol > 0 and (ignore_sellers or not sellers_at_limit)
    if sealed and require_last:
        last = to_cents(row.get('lastPrice'))
        if last is not None and last != limit_cents:
            sealed = False
    return SEALED if sealed else OPEN


def _auction_normalized(t):
    return AUCTION_OPEN if t is not None and AUCTION_OPEN <= t < '093000' else t


def reconstruct(rows, limit_price, variant='queue_no_close_auction', lot_size=100):
    """rows: list of dicts with ts, lastPrice, bidPrice1, bidVol1, askPrice1, askVol1.

    Returns first_time, last_time, open_times, fd_amount (None when not sealed at
    close), closed_sealed and data-quality counters.
    """
    opts = VARIANTS[variant]
    limit_cents = to_cents(limit_price)
    ordered = sorted(rows, key=lambda r: hhmmss(r.get('ts')))
    seen = set()
    conflicts = 0
    states = []
    for row in ordered:
        t = hhmmss(row.get('ts'))
        if not in_session(t):
            continue
        if opts['exclude_close_auction'] and '145700' <= t < '150000':
            continue
        if t in seen:
            conflicts += 1
            continue
        seen.add(t)
        states.append((t, classify(row, limit_cents, opts['require_last'], opts['ignore_sellers'])))

    first_time = None
    last_seal_start = None
    open_times = 0
    breaks = []  # (break_time, preceding_seal_start)
    confirmed = None
    pending = 0
    unknown = 0
    for t, state in states:
        if state == UNKNOWN:
            unknown += 1
            continue
        if state == SEALED:
            if confirmed != SEALED:
                if first_time is None:
                    first_time = t
                last_seal_start = t
            confirmed = SEALED
            pending = 0  # a shorter-than-threshold break was a blip
        elif confirmed == SEALED:
            pending += 1
            if pending >= opts['min_open_snaps']:
                open_times += 1
                breaks.append((t, last_seal_start))
                confirmed = OPEN
                pending = 0
        else:
            confirmed = OPEN

    if opts['merge'] in ('minute_any', 'minute_last') and first_time is not None:
        per_min = []
        for tt, st in states:
            if st == UNKNOWN or tt < first_time:
                continue
            m = tt[:4]
            if not per_min or per_min[-1][0] != m:
                per_min.append([m, st])
            elif opts['merge'] == 'minute_any' and st == OPEN:
                per_min[-1][1] = OPEN
            elif opts['merge'] == 'minute_last':
                per_min[-1][1] = st
        open_times = sum(1 for a, b in zip(per_min, per_min[1:]) if a[1] == SEALED and b[1] == OPEN)
        # a minute that both sealed and opened is itself a break when it follows a sealed minute
    elif opts['merge'] == 'minute':
        open_times = len({b[0][:4] for b in breaks})
    elif opts['merge'] == 'reseal':
        counted = 0
        for i, (bt, seal_start) in enumerate(breaks):
            # the first break always counts; later ones only if the seal before it lasted long enough
            if i == 0 or seal_start is None or _secs(bt) - _secs(seal_start) >= opts['merge_secs']:
                counted += 1
        open_times = counted

    final_row = None
    cutoff = '150500' if opts['fd_after_close'] else '150000'
    for row in ordered:
        if hhmmss(row.get('ts')) <= cutoff:
            final_row = row
    fd_amount = None
    closed_sealed = False
    if final_row is not None:
        bid1 = to_cents(final_row.get('bidPrice1'))
        bid1_vol = to_float(final_row.get('bidVol1'))
        if bid1 == limit_cents and bid1_vol:
            closed_sealed = True
            fd_amount = limit_cents / 100.0 * bid1_vol * lot_size
    norm = _auction_normalized if opts['normalize_auction'] else (lambda x: x)
    return {
        'first_time': norm(first_time),
        'last_time': norm(last_seal_start),
        'open_times': open_times, 'fd_amount': fd_amount, 'closed_sealed': closed_sealed,
        'snapshots': len(states), 'unknown': unknown, 'conflicts': conflicts,
        'first_ts': states[0][0] if states else None,
        'last_ts': states[-1][0] if states else None,
    }
