"""標記網頁（第 2 階段）：在這台電腦的瀏覽器看車牌切圖、點對或改字。

    python label_server.py            （或 plate_collector.exe --label）
然後用這台電腦的瀏覽器開 http://127.0.0.1:8790

只開在本機（127.0.0.1），別台電腦、網路上都連不到；不連任何雲端。
要在別處標記，用遠端桌面連進這台電腦再開瀏覽器。
"""
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import labels
import storage

MAX_BODY = 10_000
PAGE = Path(__file__).resolve().parent / "label_page.html"
_export_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    timeout = 30          # 連線卡住 30 秒就斷，不讓網頁被拖住
    data_dir = None
    cfg = None
    port = None

    # ---- 共用 ----
    def log_message(self, fmt, *args):
        pass  # 不在畫面印每個請求（網址裡有車牌編號）

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        """只接受用 127.0.0.1／localhost 打開的頁面（擋 DNS rebinding）。"""
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def _origin_ok(self):
        """寫入動作只接受本頁送來的（擋別的網站偷偷送請求）。"""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return origin.lower() in (f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}")

    # ---- GET ----
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "只能用 http://127.0.0.1 開"})
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/":
                return self._send(200, PAGE.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            if url.path == "/api/pending":
                try:
                    limit = max(1, min(int(q.get("limit", ["30"])[0]), 100))
                except ValueError:
                    limit = 30
                items, total = labels.pending(self.data_dir, limit)
                return self._send(200, {"items": items, "total": total})
            if url.path == "/api/item":
                item = labels.find_item(self.data_dir, q.get("key", [""])[0])
                if item is None:
                    return self._send(404, {"error": "找不到"})
                item["label"] = labels.load_labels(self.data_dir).get(item["key"])
                return self._send(200, item)
            if url.path == "/api/summary":
                return self._send(200, labels.summary(self.data_dir))
            if url.path.startswith("/img/"):
                return self._image(unquote(url.path[len("/img/"):]))
        except Exception as e:  # 單一請求出錯不影響網頁其他功能
            return self._send(500, {"error": f"程式出錯：{type(e).__name__}"})
        return self._send(404, {"error": "沒有這個頁面"})

    def _image(self, rel):
        day, _, sub = rel.partition("/")
        if not storage.DAY_RE.match(day) or not sub.lower().endswith(".jpg"):
            return self._send(404, {"error": "沒有這張"})
        root = (storage.captures_dir(self.data_dir) / day).resolve()
        path = (root / sub).resolve()
        if root not in path.parents or not path.is_file():  # 不能跳出那天的資料夾
            return self._send(404, {"error": "沒有這張"})
        return self._send(200, path.read_bytes(), "image/jpeg")

    # ---- POST ----
    def do_POST(self):
        if not self._host_ok() or not self._origin_ok():
            return self._send(403, {"error": "不接受別處送來的請求"})
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._send(415, {"error": "格式不對"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, {"error": "格式不對"})
        if length < 0 or length > MAX_BODY:
            return self._send(413, {"error": "資料太大"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("格式不對")
            if self.path == "/api/label":
                include = body.get("include")
                if include is not None and not (isinstance(include, list) and
                                                all(isinstance(i, int) for i in include)):
                    raise ValueError("include 格式不對")
                rec = labels.save_label(self.data_dir, str(body.get("key", "")),
                                        str(body.get("status", "")), str(body.get("text", "")),
                                        include)
                return self._send(200, rec)
            if self.path == "/api/export":
                if not _export_lock.acquire(blocking=False):
                    return self._send(409, {"error": "正在匯出中"})
                try:
                    result = labels.export_dataset(self.data_dir, self.cfg["dataset_retention_days"])
                finally:
                    _export_lock.release()
                return self._send(200, result)
        except (ValueError, json.JSONDecodeError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": f"程式出錯：{type(e).__name__}"})
        return self._send(404, {"error": "沒有這個功能"})


def serve(cfg, data_dir, open_browser=True):
    port = int(cfg["label_port"])
    Handler.data_dir, Handler.cfg, Handler.port = data_dir, cfg, port
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"標記網頁已開：{url}（只有這台電腦能開）。關掉這個視窗或按 Ctrl+C 停止。")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    import argparse
    ap = argparse.ArgumentParser(description="車牌標記網頁（只開在本機）")
    ap.add_argument("--config", help="設定檔（預設是程式旁的 config.json）")
    ap.add_argument("--no-browser", action="store_true", help="不要自動開瀏覽器")
    ap.add_argument("--export", action="store_true", help="只匯出訓練資料就結束")
    args = ap.parse_args()
    cfg = storage.load_config(args.config)
    data_dir = storage.resolve_data_dir(cfg)
    if args.export:
        print(labels.export_dataset(data_dir, cfg["dataset_retention_days"]))
        return
    serve(cfg, data_dir, not args.no_browser)


if __name__ == "__main__":
    main()
