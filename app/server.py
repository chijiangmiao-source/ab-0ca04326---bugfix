"""低温联锁规程补传服务的 HTTP 入口（仅依赖 Python 标准库）。

接口
----
GET  /healthz                健康检查
GET  /api/document           当前全文与修订号
POST /api/drafts             建立草案：返回当前全文与基准修订号
POST /api/patches            提交补丁（insert/delete），见 service.Service.submit
GET  /                       值班长操作页面（静态资源）

宿主机与端口分别由环境变量 HOST、PORT 配置（默认 0.0.0.0:8000）。
数据文件由 DATA_FILE 配置（默认 /data/state.json）。
"""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .service import Reject, Service
from .store import Store

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def build_handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "InterlockOT/1.0"

        def log_message(self, fmt, *args):  # 安静一些，便于看 verify 输出
            if os.environ.get("HTTP_LOG"):
                super().log_message(fmt, *args)

        def _send_json(self, status: int, body: dict) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise Reject(f"请求体不是合法 JSON：{exc}", "bad_json")
            if not isinstance(body, dict):
                raise Reject("请求体必须是 JSON 对象", "bad_json")
            return body

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/healthz":
                snap = service.document()
                self._send_json(HTTPStatus.OK, {"status": "ok",
                                                "revision": snap["revision"]})
                return
            if path == "/api/document":
                self._send_json(HTTPStatus.OK, service.document())
                return
            self._serve_static(path)

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                body = self._read_json()
                if path == "/api/drafts":
                    self._send_json(HTTPStatus.CREATED, service.create_draft())
                elif path == "/api/patches":
                    result = service.submit(body)
                    self._send_json(HTTPStatus.OK, result)
                else:
                    self._send_json(HTTPStatus.NOT_FOUND,
                                    {"error": "not_found", "message": path})
            except Reject as exc:
                status = (HTTPStatus.BAD_REQUEST
                          if exc.code in ("bad_request", "bad_json")
                          else HTTPStatus.CONFLICT)
                # 拒绝时附带当前权威全文与修订号，页面可据此恢复
                snap = service.document()
                self._send_json(status, {"error": exc.code,
                                         "message": str(exc),
                                         "text": snap["text"],
                                         "revision": snap["revision"]})

        def _serve_static(self, path: str) -> None:
            if path == "/":
                path = "/index.html"
            name = os.path.normpath(path).lstrip("/")
            full = os.path.join(STATIC_DIR, name)
            if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
                self.send_error(HTTPStatus.NOT_FOUND, "not found")
                return
            ext = os.path.splitext(full)[1]
            with open(full, "rb") as fh:
                data = fh.read()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type",
                             _CONTENT_TYPES.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    data_file = os.environ.get("DATA_FILE", "/data/state.json")
    store = Store(data_file)
    service = Service(store)
    httpd = ThreadingHTTPServer((host, port), build_handler(service))
    print(f"低温联锁规程服务监听 http://{host}:{port} 数据文件={data_file}",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
