"""Deployable anonymous music API, separate from the private LAN receiver.
No filesystem browsing, account cookies, pairing endpoints or arbitrary URLs.
"""
import os, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import catalog, connect

LIMIT_LOCK = threading.Lock()
REQUESTS = {}
WORKERS = threading.BoundedSemaphore(12)

class Handler(BaseHTTPRequestHandler):
    public_catalog = True
    def log_message(self, *args): pass
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Range')
        self.send_header('Access-Control-Expose-Headers', 'Content-Range, Content-Length, Accept-Ranges')
        super().end_headers()
    def do_OPTIONS(self): self.send_response(204); self.end_headers()
    def do_HEAD(self): self.route()
    def do_GET(self): self.route()
    def do_POST(self): self.route()
    def route(self):
        path = urlsplit(self.path).path
        if self.command in ('GET', 'HEAD') and path == '/healthz':
            if self.command == 'HEAD':
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                return
            return connect.reply(self, 200, {'ok': True, 'service': 'aleztesia-music', 'version': '0.7.5', 'commit': 'direct-audio-selection'})
        if not path.startswith('/api/music/'):
            return connect.reply(self, 404, {})
        if not path.startswith('/api/music/stream/'):
            key = (self.client_address[0], path.endswith('/resolve'))
            now = time.monotonic()
            with LIMIT_LOCK:
                if len(REQUESTS) > 2048:
                    for old, times in list(REQUESTS.items()):
                        if not times or now - times[-1] > 60: REQUESTS.pop(old, None)
                    if len(REQUESTS) > 2048: return connect.reply(self, 503, {'message': 'Servicio ocupado.'})
                recent = [t for t in REQUESTS.get(key, []) if now - t < 60]
                if len(recent) >= (20 if key[1] else 180):
                    return connect.reply(self, 429, {'message': 'Espera un momento antes de volver a intentar.'})
                REQUESTS[key] = recent + [now]
        if not WORKERS.acquire(blocking=False):
            return connect.reply(self, 503, {'message': 'Servicio ocupado. Vuelve a intentar.'})
        try:
            catalog.http(self)
        finally:
            WORKERS.release()

if __name__ == '__main__':
    host = os.environ.get('CATALOG_HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', '8080'))
    print(f"Aleztesia Music Catalog Server listening on {host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
