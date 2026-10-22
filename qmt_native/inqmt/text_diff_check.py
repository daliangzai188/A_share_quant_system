import pandas as pd, sys
ref, run = sys.argv[1], sys.argv[2]
tot = exact = 0; ex = []; notexact = []
for d in open('inqmt/days15.txt').read().split():
    for name in ['live_limit_up_fill_scored.csv', 'live_limit_up_merged.csv', 'live_theme_heat_features.csv', 'live_market_emotion_features.csv']:
        a = pd.read_csv(f'{ref}/snapshots/{d}/{name}', dtype=str, keep_default_na=False)
        b = pd.read_csv(f'{run}/snapshots/{d}/{name}', dtype=str, keep_default_na=False)
        for c in a.columns:
            m = a[c] != b[c]
            if m.any():
                xa = pd.to_numeric(a.loc[m, c]).values; xb = pd.to_numeric(b.loc[m, c]).values
                tot += m.sum(); exact += (xa == xb).sum()
                if len(ex) < 4: ex.append((d, name, c, a.loc[m, c].iloc[0], b.loc[m, c].iloc[0]))
                for i in range(len(xa)):
                    if xa[i] != xb[i] and len(notexact) < 6: notexact.append((d, name, c, a.loc[m, c].iloc[i], b.loc[m, c].iloc[i]))
print('text-different cells', tot, 'numerically exactly equal', exact)
for e in ex: print('example', e)
for e in notexact: print('NOT EXACT', e)
