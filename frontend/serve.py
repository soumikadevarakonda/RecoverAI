"""
RecoverAI control plane dev server.

Serves the static frontend and proxies /api/* and /health to the FastAPI
backend (BACKEND_URL, default http://127.0.0.1:8000), so the UI and API share
one origin.

    python frontend/serve.py            # http://127.0.0.1:5173 (falls back to 5174 if busy)
"""
import errno
import http.server
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

PRIMARY_PORT = 5173
FALLBACK_PORT = 5174
DIRECTORY = os.path.dirname(os.path.abspath(__file__))
BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000")
PROXY_TIMEOUT_SECONDS = float(os.environ.get("PROXY_TIMEOUT_SECONDS", "30"))

# Hop-by-hop headers must not be forwarded by a proxy (RFC 9110 section 7.6.1).
HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}

# Only these static file types are served; build scripts, sources and docs are not.
SERVABLE_EXTENSIONS = {
    ".html", ".js", ".css", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".json",
}
BLOCKED_PREFIXES = ("/views/", "/stitch_imports/", "/src/", "/node_modules/")
BLOCKED_FILES = {"/package.json", "/package-lock.json", "/tailwind.config.js"}


def _is_proxied(path: str) -> bool:
    route = urllib.parse.urlsplit(path).path
    return route.startswith("/api/") or route == "/health"


class ProxyAndStaticHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    # --- Static files -------------------------------------------------

    def list_directory(self, path):
        self.send_error(404, "Not Found")
        return None

    def send_head(self):
        route = urllib.parse.urlsplit(self.path).path
        if route.endswith("/"):
            route += "index.html"
        ext = os.path.splitext(route)[1].lower()
        if route.startswith(BLOCKED_PREFIXES) or route in BLOCKED_FILES or ext not in SERVABLE_EXTENSIONS:
            self.send_error(404, "Not Found")
            return None
        return super().send_head()

    # --- Methods ------------------------------------------------------

    def do_GET(self):
        if _is_proxied(self.path):
            self.proxy_request("GET")
        else:
            super().do_GET()

    def do_HEAD(self):
        if _is_proxied(self.path):
            self.proxy_request("HEAD")
        else:
            super().do_HEAD()

    def do_POST(self):
        if _is_proxied(self.path):
            self.proxy_request("POST")
        else:
            self.send_error(405, "Method Not Allowed")

    def do_OPTIONS(self):
        if _is_proxied(self.path):
            self.proxy_request("OPTIONS")
        else:
            self.send_response(204)
            self.send_header("Allow", "GET, HEAD, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()

    # --- Proxy --------------------------------------------------------

    def _relay(self, status, headers, body):
        self.send_response(status)
        for key, val in headers:
            if key.lower() not in HOP_BY_HOP_HEADERS:
                self.send_header(key, val)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def proxy_request(self, method):
        target_url = f"{BACKEND_URL}{self.path}"
        content_length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(content_length) if content_length > 0 else None

        req = urllib.request.Request(target_url, data=body, method=method)
        for key, val in self.headers.items():
            if key.lower() not in HOP_BY_HOP_HEADERS:
                req.add_header(key, val)

        try:
            with urllib.request.urlopen(req, timeout=PROXY_TIMEOUT_SECONDS) as response:
                self._relay(response.status, response.getheaders(), response.read())
        except urllib.error.HTTPError as e:
            self._relay(e.code, e.headers.items(), e.read())
        except Exception as e:  # backend down, timeout, DNS, ...
            self.log_error("Proxy to %s failed: %r", BACKEND_URL, e)
            self.send_error(502, "Bad Gateway: backend unavailable")


class ControlPlaneServer(http.server.ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind a port that is already
    # in use, so it is only enabled elsewhere (where it just skips TIME_WAIT).
    allow_reuse_address = not sys.platform.startswith("win")
    daemon_threads = True


def _bind(port):
    return ControlPlaneServer(("127.0.0.1", port), ProxyAndStaticHTTPRequestHandler)


def main():
    try:
        httpd = _bind(PRIMARY_PORT)
    except OSError as exc:
        if exc.errno not in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1), 10013, 10048):
            raise
        print(f"Port {PRIMARY_PORT} is in use; trying {FALLBACK_PORT}.")
        httpd = _bind(FALLBACK_PORT)

    port = httpd.server_address[1]
    with httpd:
        print(f"RecoverAI Control Plane running at http://127.0.0.1:{port} (proxying API -> {BACKEND_URL})")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
