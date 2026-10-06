from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = Path(__file__).with_name("index.html").read_bytes()
STATIC_DIR = Path(__file__).with_name("static")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
            return
        if self.path.startswith("/static/"):
            fs_path = STATIC_DIR / self.path[len("/static/"):]
            if fs_path.is_file():
                self.send_response(200)
                if fs_path.suffix == ".js":
                    self.send_header("Content-Type", "application/javascript")
                self.end_headers()
                self.wfile.write(fs_path.read_bytes())
                return
            self.send_error(404)
            return
        if self.path != "/":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
