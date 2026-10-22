import re, sys
ns = {}
exec(open('inqmt/qn_core_generated.py').read(), ns)
pats = {
 'pd.NA': r'\bpd\.NA\b', 'Int64/string/boolean dtype': r'["\'](Int64|Int32|string|boolean|Float64)["\']', 'pd.*Dtype': r'pd\.\w+Dtype',
 'to_list(': r'\.to_list\(', 'explode(': r'\.explode\(', 'convert_dtypes': r'convert_dtypes', 'ignore_index= in sort/dedup': r'(sort_values|drop_duplicates|sort_index)\([^)]*ignore_index',
 'dropna= in groupby': r'groupby\([^)]*dropna', 'named agg': r'\.agg\(\s*\w+\s*=', 'NamedAgg': r'NamedAgg', 'key= in sort_values': r'sort_values\([^)]*key\s*=',
 'removeprefix/suffix': r'remove(prefix|suffix)', 'between inclusive=': r'between\([^)]*inclusive', '.array': r'\.array\b', 'droplevel': r'droplevel',
 'items()': r'\.items\(\)', 'iteritems': r'iteritems', 'value_counts(dropna': r'value_counts\([^)]*dropna', 'astype(str)': r'astype\(str\)', 'str.': r'\.str\.\w+',
 'merge validate': r'validate=', 'infer_objects': r'infer_objects', 'pd.isna/notna': r'pd\.(isna|notna)\b', 'to_numeric': r'to_numeric', 'nullable fillna': r'fillna\(pd\.NA',
 'DataFrame.map/applymap': r'\.(applymap|map)\(', 'pd.Series.str.fullmatch': r'fullmatch', 'replace regex': r'\.replace\([^)]*regex', 'mode()': r'\.mode\(', 'clip(': r'\.clip\(',
 'groupby(..., as_index': r'as_index', 'transform(': r'\.transform\(', 'rolling(': r'\.rolling\(', 'rank(': r'\.rank\(', 'qcut/cut': r'pd\.(q?cut)\(', 'pd.Timestamp': r'pd\.Timestamp', 'to_datetime': r'to_datetime',
 'set_axis': r'set_axis', 'select_dtypes': r'select_dtypes', 'df.style': r'\.style\b', 'observed=': r'observed=', 'min_count': r'min_count', 'numeric_only': r'numeric_only',
 'squeeze': r'squeeze', 'pd.option_context': r'option_context', 'loc[ list': r'\.loc\[', 'nunique': r'nunique', 'combine_first': r'combine_first', 'sort=False groupby': r'groupby\([^)]*sort=',
 'pipe(': r'\.pipe\(', 'assign(': r'\.assign\(', 'where(': r'\.where\(', 'mask(': r'\.mask\(', 'any/all bool_only': r'bool_only', 'shift(fill_value': r'fill_value', 'idxmax': r'idx(max|min)',
}
src = {name: v[2] for name, v in ns['QN_MODULES'].items()}
for k, p in pats.items():
    hits = [(n, len(re.findall(p, s))) for n, s in src.items() if re.search(p, s)]
    tot = sum(h for _, h in hits)
    if tot:
        print('%-28s %4d  %s' % (k, tot, ', '.join('%s:%d' % (n.split('.')[-1], h) for n, h in hits)[:150]))
