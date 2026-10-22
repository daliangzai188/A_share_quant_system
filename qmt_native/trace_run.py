"""Run one pipeline step with an audit hook that records every file it touches."""
import json, os, runpy, sys
LOG = os.environ['TRACE_LOG']
seen = []
ws = os.getcwd()
def hook(event, args):
    if event == 'open':
        path, mode = args[0], args[1]
        if isinstance(path, int) or path is None:
            return
        p = os.path.abspath(os.fsdecode(path))
        if p.startswith(ws):
            seen.append(('open', os.path.relpath(p, ws), str(mode)))
    elif event in ('os.listdir', 'os.scandir', 'glob.glob', 'os.remove', 'os.rename', 'os.replace', 'os.mkdir', 'os.unlink'):
        a = args[0]
        if isinstance(a, (str, bytes, os.PathLike)):
            p = os.path.abspath(os.fsdecode(a))
            if p.startswith(ws):
                seen.append((event, os.path.relpath(p, ws), ''))
sys.addaudithook(hook)
import atexit
@atexit.register
def dump():
    with open(LOG, 'w') as f:
        json.dump(seen, f)
script = sys.argv[1]
sys.argv = sys.argv[1:]
sys.path.insert(0, ws)
runpy.run_path(script, run_name='__main__')
