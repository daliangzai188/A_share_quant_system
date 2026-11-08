# coding: utf-8
"""Test harness for the QMT log + registry state store (11-08): the registry key is a dict kept in
the test's key-value dict (so it survives a simulated QMT restart like the broker store does), and
every printed state line goes straight through vm_scripts/qn_state_sink.py as the forwarder would.
hold[0] = True keeps lines back (a forwarder that is not running); release() feeds them."""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'vm_scripts'))
import qn_state_sink


def attach_log_store(ns, kv, mode=None):
    ns['QN_DEPLOY']['state_store'] = 'log'
    mode = mode or ('live' if ns['QN_DEPLOY'].get('mode') == 'live' else 'shadow')
    values = kv.setdefault('__registry_%s__' % mode, {})  # StateLive / StateShadow
    sink = qn_state_sink.StateSink(qn_state_sink.DictRegistry(values), mode=mode)
    lock = threading.Lock()
    held, hold, lines = [], [False], []

    def tap(line):
        lines.append(line)
        if hold[0]:
            held.append(line)
            return
        with lock:
            sink.feed((line + '\n').encode('ascii'))

    def release():
        with lock:
            while held:
                sink.feed((held.pop(0) + '\n').encode('ascii'))
    ns['QN_STATE_TAP'][0] = tap
    ns['QN_STATE_READER'][0] = values.get
    return dict(values=values, sink=sink, hold=hold, held=held, release=release, lines=lines)
