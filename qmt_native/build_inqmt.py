"""Bundle the production close-pipeline code (steps 2,3,4,6,7) into one QMT strategy core.

The strategy modules are embedded unchanged in logic. Only mechanical changes are
applied so the code runs in QMT's built-in Python 3.6 / pandas 0.22 without any file
or network access:
  * annotations and `from __future__ import annotations` are removed (3.6 syntax);
  * `x.to_csv(...)`, `x.to_markdown(...)`, `x.to_numpy(...)` become helper calls so
    writes land in an in-memory file store and missing pandas-0.22 methods are covered;
  * imports of os / pathlib / tempfile / logging / zoneinfo / dataclasses / pandas / sys
    are served by in-memory or 3.6 shims scoped to the embedded modules only.

The output file is ASCII only (QMT's editor may re-encode non-ASCII text).

Usage: venv/bin/python build_inqmt.py [REPO] [OUT]
"""
import ast
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('/Users/eass/Desktop/A_System')
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / 'inqmt' / 'qn_core_generated.py'
PROFILE = sys.argv[3] if len(sys.argv) > 3 else 'pipeline'

ROOT_SCRIPTS = ['scripts/clean_collected_data.py', 'scripts/build_dynamic_features.py',
                'scripts/score_limit_up_fill_probability.py', 'scripts/run_paper_ab_filtered_daily_ops.py',
                'scripts/run_strategy_e_signal.py']
STATIC_FILES = ['config/config.json', 'config/strategy_config.json', 'config/strategy_e_r1_scenarios.json']
PACKAGES = ('src', 'scripts')
# Full system: the trading daemon plus every script it runs as a child process.
FULL_ROOTS = ['scripts/trading_daemon.py', 'qmt_inner/engine.py', 'qmt_inner/protocol.py',
              'scripts/build_live_enhanced_features.py', 'scripts/strategy_health_monitor.py',
              'scripts/live_execution_audit.py', 'scripts/update_execution_completion.py',
              'scripts/report_rolling_live_performance.py', 'scripts/update_release_oos_observation.py',
              'scripts/update_equity_curve_stop.py', 'scripts/run_combined_live_plan.py',
              'scripts/monitor_strategy_d_intraday.py']
# Modules that are not in the repo, embedded the same way (name -> local file).
EXTRA_MODULES = {'qn.native': HERE / 'inqmt' / 'qn_native.py',
                 'qn.prod_equity_curve_stop': REPO / 'scripts' / 'update_equity_curve_stop.py',
                 'qn.limit_reconstruct': HERE / 'native_limit_reconstruct.py'}
# Repo modules replaced by a QMT-native implementation (name -> local file).
REPLACEMENTS = {'scripts.collect_all_data': HERE / 'inqmt' / 'replacements' / 'collect_all_data.py',
                'scripts.build_live_enhanced_features': HERE / 'inqmt' / 'replacements' / 'build_live_enhanced_features.py',
                'scripts.update_equity_curve_stop': HERE / 'inqmt' / 'replacements' / 'update_equity_curve_stop.py'}
if PROFILE == 'full':
    ROOT_SCRIPTS = ROOT_SCRIPTS + FULL_ROOTS
    PACKAGES = ('src', 'scripts', 'qmt_inner')
    STATIC_FILES = sorted(set(STATIC_FILES + [p.relative_to(REPO).as_posix() for p in (REPO / 'config').glob('*.json')]))


def module_name(rel):
    name = rel[:-3].replace('/', '.')
    return name[:-len('.__init__')] if name.endswith('.__init__') else name


def resolve(repo, name):
    p = repo / (name.replace('.', '/') + '.py')
    if p.exists():
        return p
    p = repo / name.replace('.', '/') / '__init__.py'
    return p if p.exists() else None


def closure(repo):
    seen, order = set(), []

    def walk(path):
        rel = path.relative_to(repo).as_posix()
        if rel in seen:
            return
        seen.add(rel)
        order.append(rel)
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for n in ast.walk(tree):
            names = []
            if isinstance(n, ast.Import):
                names = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                names = [n.module] + [n.module + '.' + a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level:
                raise ValueError('relative import not supported: %s' % rel)
            for nm in names:
                if nm.split('.')[0] in PACKAGES:
                    parts = nm.split('.')
                    for i in range(1, len(parts) + 1):
                        p = resolve(repo, '.'.join(parts[:i]))
                        if p:
                            walk(p)
    for r in ROOT_SCRIPTS:
        walk(repo / r)
    return order


class _WalrusFinder(ast.NodeVisitor):
    """Collect NamedExpr nodes that are safe to hoist: evaluated unconditionally and first."""

    def __init__(self):
        self.found = []
        self.unsafe = []

    def generic_visit(self, node):
        for field, value in ast.iter_fields(node):
            items = value if isinstance(value, list) else [value]
            for item in items:
                if isinstance(item, ast.AST):
                    self._visit_child(node, field, item)

    def _visit_child(self, parent, field, child):
        if isinstance(child, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
                              ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if any(isinstance(n, ast.NamedExpr) for n in ast.walk(child)):
                self.unsafe.append(child)
            return
        if isinstance(parent, ast.IfExp) and field in ('body', 'orelse'):
            if any(isinstance(n, ast.NamedExpr) for n in ast.walk(child)):
                self.unsafe.append(child)
            return
        if isinstance(parent, ast.BoolOp) and child is not parent.values[0]:
            if any(isinstance(n, ast.NamedExpr) for n in ast.walk(child)):
                self.unsafe.append(child)
            return
        if isinstance(child, ast.NamedExpr):
            self.found.append(child)
        self.generic_visit(child)


class WalrusHoist(ast.NodeTransformer):
    """Python 3.6 has no ':='. `stmt(... (x := e) ...)` becomes `x = e; stmt(... x ...)` when the
    assignment is evaluated first and unconditionally; `while (x := e):` becomes a loop with a break."""

    def __init__(self):
        self.count = 0

    def _hoist(self, expr_owner, attr):
        finder = _WalrusFinder()
        expr = getattr(expr_owner, attr)
        if expr is None:
            return []
        wrapper = ast.Expr(value=expr)
        finder.generic_visit(wrapper)
        if finder.unsafe:
            raise ValueError('walrus in a conditional/nested position cannot be rewritten for 3.6: %s'
                             % ast.unparse(finder.unsafe[0])[:120])
        assigns = []
        for ne in finder.found:
            assigns.append(ast.Assign(targets=[ast.Name(id=ne.target.id, ctx=ast.Store())], value=ne.value))
        if not finder.found:
            return []

        class Replace(ast.NodeTransformer):
            def visit_NamedExpr(self, node):
                return ast.Name(id=node.target.id, ctx=ast.Load())
        setattr(expr_owner, attr, Replace().visit(expr))
        self.count += len(assigns)
        return assigns

    def visit_While(self, node):
        self.generic_visit(node)
        if not any(isinstance(n, ast.NamedExpr) for n in ast.walk(node.test)):
            return node
        if node.orelse:
            raise ValueError('while-else with walrus not supported')
        assigns = self._hoist(node, 'test')
        brk = ast.If(test=ast.UnaryOp(op=ast.Not(), operand=node.test), body=[ast.Break()], orelse=[])
        return ast.While(test=ast.Constant(value=True), body=assigns + [brk] + node.body, orelse=[])

    def _stmt(self, node, attrs):
        self.generic_visit(node)
        pre = []
        for a in attrs:
            if getattr(node, a, None) is not None and any(isinstance(n, ast.NamedExpr) for n in ast.walk(getattr(node, a))):
                pre.extend(self._hoist(node, a))
        return pre + [node] if pre else node

    def visit_Expr(self, node):
        return self._stmt(node, ['value'])

    def visit_Assign(self, node):
        return self._stmt(node, ['value'])

    def visit_Return(self, node):
        return self._stmt(node, ['value'])

    def visit_If(self, node):
        return self._stmt(node, ['test'])


class Py36(ast.NodeTransformer):
    """Mechanical 3.6 / in-memory rewrites. Never touches strategy expressions."""

    HELPERS = {'to_csv': '__qn_to_csv__', 'to_markdown': '__qn_to_markdown__', 'to_numpy': '__qn_to_numpy__',
               'isascii': '__qn_isascii__', 'fromisoformat': '__qn_fromisoformat__'}

    def __init__(self):
        self.class_stack = []
        self.counts = {}

    def bump(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1

    def visit_ImportFrom(self, node):
        if node.module == '__future__':
            kept = [a for a in node.names if a.name != 'annotations']
            if not kept:
                self.bump('future_annotations_removed')
                return None
            node.names = kept
        return node

    def _strip_args(self, args):
        for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
            if a.annotation is not None:
                self.bump('arg_annotation_removed')
            a.annotation = None
        for a in (args.vararg, args.kwarg):
            if a is not None:
                a.annotation = None
        if args.posonlyargs:
            raise ValueError('positional-only parameters are not valid in Python 3.6')

    def visit_FunctionDef(self, node):
        self._strip_args(node.args)
        if node.returns is not None:
            self.bump('return_annotation_removed')
        node.returns = None
        self.class_stack.append(None)
        self.generic_visit(node)
        self.class_stack.pop()
        if not node.body:
            node.body = [ast.Pass()]
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self._strip_args(node.args)
        self.generic_visit(node)
        return node

    def visit_ClassDef(self, node):
        is_dc = any('dataclass' in ast.unparse(d) for d in node.decorator_list)
        self.class_stack.append('dataclass' if is_dc else 'class')
        self.generic_visit(node)
        self.class_stack.pop()
        if not node.body:
            node.body = [ast.Pass()]
        return node

    def visit_AnnAssign(self, node):
        self.generic_visit(node)
        in_dc = self.class_stack and self.class_stack[-1] == 'dataclass'
        if in_dc:
            # keep the field (order and defaults matter) but do not evaluate the type
            node.annotation = ast.Constant(value='object')
            self.bump('dataclass_field_annotation_stringified')
            return node
        self.bump('annassign_rewritten')
        if node.value is None:
            return None
        return ast.copy_location(ast.Assign(targets=[node.target], value=node.value), node)

    GENERIC_BASES = {'Callable', 'Mapping', 'MutableMapping', 'Sequence', 'MutableSequence', 'Iterable',
                     'Iterator', 'Generator', 'Collection', 'Container', 'Set', 'AbstractSet', 'dict', 'list',
                     'tuple', 'set', 'frozenset', 'type', 'Optional', 'Union', 'Dict', 'List', 'Tuple', 'Type'}

    def visit_Assign(self, node):
        self.generic_visit(node)
        # `Alias = Callable[[str], Any]` type aliases: collections.abc generics are not subscriptable
        # before Python 3.9. Aliases are only used in annotations, which are removed anyway.
        if not self.class_stack and isinstance(node.value, ast.Subscript):
            base = node.value.value
            name = base.id if isinstance(base, ast.Name) else (base.attr if isinstance(base, ast.Attribute) else None)
            if name in self.GENERIC_BASES and all(isinstance(t, ast.Name) for t in node.targets):
                self.bump('type_alias_stringified')
                node.value = ast.Constant(value=ast.unparse(node.value))
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in self.HELPERS:
            self.bump('call_' + f.attr)
            return ast.copy_location(ast.Call(func=ast.Name(id=self.HELPERS[f.attr], ctx=ast.Load()),
                                              args=[f.value] + node.args, keywords=node.keywords), node)
        return node

    def visit_NamedExpr(self, node):
        raise ValueError('walrus operator is not valid in Python 3.6')

    def visit_Match(self, node):
        raise ValueError('match statement is not valid in Python 3.6')


FORBIDDEN_METHODS = {'to_json', 'to_excel', 'to_pickle', 'to_parquet', 'to_hdf', 'to_feather', 'to_stata',
                     'tofile'}
FORBIDDEN_OWNED = {('pd', 'read_json'), ('pd', 'read_excel'), ('pd', 'read_pickle'), ('pd', 'read_parquet'),
                   ('pd', 'read_hdf'), ('pd', 'read_fwf'), ('pd', 'read_table'), ('np', 'save'), ('np', 'load'),
                   ('np', 'savez'), ('np', 'savetxt'), ('np', 'loadtxt'), ('np', 'fromfile'), ('np', 'memmap'),
                   ('os', 'walk'), ('os', 'chdir'), ('os', 'system'), ('os', 'popen'), ('os', 'startfile')}


class ForbiddenCalls(ast.NodeVisitor):
    """Disk/network entry points that the in-memory shims do not cover must not be called."""

    def __init__(self):
        self.hits = []

    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Attribute):
            owner = ast.unparse(f.value)
            if f.attr in FORBIDDEN_METHODS or (owner, f.attr) in FORBIDDEN_OWNED:
                self.hits.append('%s (line %s)' % (ast.unparse(f), node.lineno))
        self.generic_visit(node)


class Fstring36Check(ast.NodeVisitor):
    """Python 3.6 f-strings cannot contain backslashes or the outer quote inside {}."""

    def __init__(self):
        self.bad = []

    def visit_JoinedStr(self, node):
        for v in node.values:
            if isinstance(v, ast.FormattedValue):
                text = ast.unparse(v.value)
                if '\\' in text:
                    self.bad.append(text)
        self.generic_visit(node)


def transform(src, rel):
    tree = ast.parse(src)
    fc = ForbiddenCalls()
    fc.visit(tree)
    if fc.hits:
        raise ValueError('%s: calls not covered by the in-memory shims: %s' % (rel, fc.hits))
    wh = WalrusHoist()
    tree = wh.visit(tree)
    t = Py36()
    tree = t.visit(tree)
    if wh.count:
        t.counts['walrus_hoisted'] = wh.count
    ast.fix_missing_locations(tree)
    code = ast.unparse(tree)
    chk = Fstring36Check()
    chk.visit(ast.parse(code))
    if chk.bad:
        raise ValueError('%s: f-string expressions with backslash: %s' % (rel, chk.bad[:3]))
    return code, t.counts


def main():
    files = closure(REPO)
    pkgs = set()
    for rel in files:
        parts = module_name(rel).split('.')
        for i in range(1, len(parts)):
            pkgs.add('.'.join(parts[:i]))
    modules = {}
    report = {}
    files = [f for f in files if module_name(f) not in REPLACEMENTS]
    sources = [(rel, (REPO / rel).read_text(encoding='utf-8')) for rel in files]
    for name, path in list(EXTRA_MODULES.items()) + list(REPLACEMENTS.items()):
        rel = name.replace('.', '/') + '.py'
        sources.append((rel, Path(path).read_text(encoding='utf-8')))
        files.append(rel)
        parts = name.split('.')
        for i in range(1, len(parts)):
            pkgs.add('.'.join(parts[:i]))
    for rel, src in sources:
        code, counts = transform(src, rel)
        modules[module_name(rel)] = {'file': rel, 'source': code, 'is_pkg': rel.endswith('__init__.py'),
                                     'sha256': hashlib.sha256(src.encode('utf-8')).hexdigest()}
        report[rel] = counts
    for p in sorted(pkgs):
        if p not in modules:
            modules[p] = {'file': p.replace('.', '/') + '/__init__.py', 'source': '', 'is_pkg': True,
                          'sha256': None, 'namespace': True}
        else:
            modules[p]['is_pkg'] = True
    statics = {}
    for rel in STATIC_FILES:
        raw = (REPO / rel).read_bytes()
        statics[rel] = {'text': raw.decode('utf-8'), 'sha256': hashlib.sha256(raw).hexdigest()}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    lines = ['# coding: utf-8',
             '# Generated by build_inqmt.py from A_System %s. Do not edit by hand.' % repo_head(REPO),
             'QN_MODULES = {']
    for name in sorted(modules):
        m = modules[name]
        lines.append('    %s: (%s, %s, %s),' % (ascii(name), ascii(m['file']), m['is_pkg'], ascii(m['source'])))
    lines.append('}')
    lines.append('QN_STATIC_FILES = {')
    for rel in sorted(statics):
        lines.append('    %s: %s,' % (ascii(rel), ascii(statics[rel]['text'])))
    lines.append('}')
    lines.append('QN_SOURCE_SHA256 = %s' % ascii({m['file']: m['sha256'] for m in modules.values() if m['sha256']}))
    lines.append('QN_STATIC_SHA256 = %s' % ascii({k: v['sha256'] for k, v in statics.items()}))
    text = '\n'.join(lines) + '\n'
    assert all(ord(c) < 128 for c in text)
    OUT.write_text(text, encoding='ascii')
    print('modules', len(modules), 'static files', len(statics), 'bytes', len(text), '->', OUT)
    json.dump(report, open(OUT.with_suffix('.transform.json'), 'w'), indent=1, sort_keys=True)


def repo_head(repo):
    try:
        head = (repo / '.git' / 'HEAD').read_text().strip()
        if head.startswith('ref: '):
            ref = repo / '.git' / head[5:]
            if ref.exists():
                return ref.read_text().strip()[:12]
            packed = (repo / '.git' / 'packed-refs').read_text()
            for line in packed.splitlines():
                if line.endswith(head[5:]):
                    return line.split()[0][:12]
        return head[:12]
    except OSError:
        return 'unknown'


if __name__ == '__main__':
    main()
