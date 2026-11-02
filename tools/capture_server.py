# -*- coding: utf-8 -*-
"""
華夏攝影機推送測試接收端（只記錄，不開閘）

用法（Windows）：
  1. 安裝 Python 3（https://www.python.org/downloads/ ，安裝時勾選 "Add python.exe to PATH"）
  2. 在這個檔案所在的資料夾開啟「命令提示字元」，執行：
         python capture_server.py
     第一次執行若跳出 Windows 防火牆提示，請選「允許」。
  3. 畫面會顯示這台電腦的 IP。到攝影機後台的 HTTP 推送設定填入：
         伺服器 IP：畫面顯示的 IP
         埠號：8080
         路徑：/api/lpr/notify （任何路徑都會被記錄）
  4. 讓車子（或拿一張車牌照片）經過攝影機，等心跳送幾次後按 Ctrl+C 結束。
  5. 把 captures 資料夾整個壓縮後傳給 Claude。

本程式只回應 HTTP 200 與空內容，不會下達任何開閘指令。
"""

import json
import os
import socket
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

PORT = 8080
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures")


def shorten(value):
    """把很長的字串（多半是 base64 圖片）縮短，方便在畫面上閱讀。"""
    if isinstance(value, str) and len(value) > 200:
        return value[:60] + f"...（共 {len(value)} 字元）"
    if isinstance(value, dict):
        return {k: shorten(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shorten(v) for v in value]
    return value


def parse_body(body, content_type):
    text = body.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        pass
    if "x-www-form-urlencoded" in content_type:
        # 保留空白欄位（例如 Province=），才看得出攝影機送了哪些欄位
        return {k: v[0] if len(v) == 1 else v for k, v in parse_qs(text, keep_blank_values=True).items()}
    return text


class Handler(BaseHTTPRequestHandler):
    out_dir = OUT_DIR

    def reply(self, parsed):
        """回給攝影機的內容：(Content-Type, bytes)。這裡一律回空白，不下任何指令。"""
        return None, b""

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        base = os.path.join(self.out_dir, f"{stamp}_{self.command}")

        with open(base + "_raw.txt", "wb") as f:
            f.write(f"{self.command} {self.path}\r\n".encode())
            f.write(f"From: {self.client_address[0]}\r\n".encode())
            for k, v in self.headers.items():
                f.write(f"{k}: {v}\r\n".encode())
            f.write(b"\r\n")
            f.write(body)

        parsed = parse_body(body, self.headers.get("Content-Type", ""))
        if not isinstance(parsed, str):
            with open(base + "_parsed.json", "w", encoding="utf-8") as f:
                json.dump(parsed, f, ensure_ascii=False, indent=2)

        print(f"\n[{stamp}] {self.command} {self.path} 來自 {self.client_address[0]}，{len(body)} bytes")
        print(json.dumps(shorten(parsed), ensure_ascii=False, indent=2)[:3000])

        content_type, reply = self.reply(parsed)
        self.send_response(200)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle

    def log_message(self, *args):
        pass


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"接收端已啟動：http://{local_ip()}:{PORT}/api/lpr/notify")
    print(f"記錄會存到：{OUT_DIR}")
    print("按 Ctrl+C 結束。")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n已結束。")
