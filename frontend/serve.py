import http.server
import urllib.request
import urllib.error
import os
import sys

PORT = 5174
DIRECTORY = os.path.dirname(os.path.abspath(__file__))
BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000")

class ProxyAndStaticHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def do_GET(self):
        if self.path.startswith('/api/') or self.path == '/health':
            self.proxy_request('GET')
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith('/api/') or self.path == '/health':
            self.proxy_request('POST')
        else:
            self.send_error(405, "Method not allowed")

    def proxy_request(self, method):
        target_url = f"{BACKEND_URL}{self.path}"
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length > 0 else None
        
        req = urllib.request.Request(target_url, data=body, method=method)
        for key, val in self.headers.items():
            if key.lower() not in ['host', 'content-length']:
                req.add_header(key, val)
                
        try:
            with urllib.request.urlopen(req) as response:
                self.send_response(response.status)
                for key, val in response.getheaders():
                    if key.lower() not in ['transfer-encoding', 'content-length']:
                        self.send_header(key, val)
                res_body = response.read()
                self.send_header('Content-Length', str(len(res_body)))
                self.end_headers()
                self.wfile.write(res_body)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            for key, val in e.headers.items():
                if key.lower() not in ['transfer-encoding', 'content-length']:
                    self.send_header(key, val)
            res_body = e.read()
            self.send_header('Content-Length', str(len(res_body)))
            self.end_headers()
            self.wfile.write(res_body)
        except Exception as e:
            self.send_error(502, f"Bad Gateway to {BACKEND_URL}: {e}")

class ReusableServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True

if __name__ == '__main__':
    try:
        with ReusableServer(('127.0.0.1', 5173), ProxyAndStaticHTTPRequestHandler) as httpd:
            print('RecoverAI Control Plane running at http://127.0.0.1:5173 (Proxying API -> ' + BACKEND_URL + ')')
            httpd.serve_forever()
    except Exception:
        with ReusableServer(('127.0.0.1', PORT), ProxyAndStaticHTTPRequestHandler) as httpd:
            print(f'RecoverAI Control Plane running at http://127.0.0.1:{PORT} (Proxying API -> {BACKEND_URL})')
            httpd.serve_forever()
