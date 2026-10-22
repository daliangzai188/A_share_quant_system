"""Temporary upload receiver for the phase-0 export (VM -> Mac).

Mac Syncthing is sendonly by design (docs/new_mac_rebuild_migration.md), so the VM
export cannot come back that way and that setting must not be changed.

Binds only to the vmnet8 address, accepts only the VM's address, only PUT
/upload/<safe-name>, and writes only into the received/ folder. Stop it when done.
Python 3.9 compatible (runs with the Apple-signed system python).
"""
import datetime
import http.server
import os
import re
import sys

BIND = ('172.16.136.1', 8765)
ALLOWED_CLIENT = '172.16.136.128'
DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'received')
SAFE = re.compile(r'^[A-Za-z0-9._-]{1,80}$')
MAX_BYTES = 4 * 1024 ** 3


def log(msg):
    line = '%s %s' % (datetime.datetime.now().strftime('%H:%M:%S'), msg)
    print(line, flush=True)


class Handler(http.server.BaseHTTPRequestHandler):
    def _reject(self, code, why):
        log('REJECT %s %s %s' % (self.client_address[0], self.path, why))
        self.send_response(code)
        self.end_headers()

    def do_PUT(self):
        if self.client_address[0] != ALLOWED_CLIENT:
            return self._reject(403, 'client')
        parts = self.path.split('/')
        if len(parts) != 3 or parts[1] != 'upload' or not SAFE.match(parts[2]):
            return self._reject(400, 'path')
        length = int(self.headers.get('Content-Length') or 0)
        if length <= 0 or length > MAX_BYTES:
            return self._reject(411, 'length')
        target = os.path.join(DEST, parts[2])
        tmp = target + '.part'
        remaining = length
        with open(tmp, 'wb') as handle:
            while remaining:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                handle.write(chunk)
                remaining -= len(chunk)
        if remaining:
            os.remove(tmp)
            return self._reject(400, 'short body')
        os.replace(tmp, target)
        log('SAVED %s %d bytes' % (parts[2], length))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'ok')

    def do_GET(self):
        self._reject(405, 'get')

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    os.makedirs(DEST, exist_ok=True)
    log('LISTEN %s:%d -> %s' % (BIND[0], BIND[1], DEST))
    http.server.ThreadingHTTPServer(BIND, Handler).serve_forever()
