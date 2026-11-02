# -*- coding: utf-8 -*-
"""
假攝影機（在家開發用）：模擬華夏 ECV87 的三個入口，資料來自 2026-11-02 在公司錄下的 captures_camera_snapshot/。

  TCP 8117：白名單讀取／新增／刪除／查詢、485 透傳（照 hx_whitelist.py 的協議）
  HTTP 8000：/cgi-bin/get.cgi、set.cgi、queryLocalPic.cgi
  HTTP 8081：SD 卡照片（真攝影機是 80 埠；一律回 sample 照片）

只綁本機。用法：python fake_camera.py [--host 127.0.0.2]
模擬多個場站時，每支假攝影機用不同的 127.0.0.x（Windows 的 127.x.x.x 都是本機）。
set.cgi 改的參數會記在記憶體裡，get.cgi 會讀回改過的值（重開假攝影機就恢復原狀）。
"""

import glob
import json
import os
import socketserver
import struct
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = sys.argv[sys.argv.index("--host") + 1] if "--host" in sys.argv else "127.0.0.1"
if not HOST.startswith("127."):
    raise SystemExit("假攝影機只能開在 127.x.x.x")
SDK_PORT, WEB_PORT, PHOTO_PORT = 8117, 8000, 8081
SNAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures_camera_snapshot")

settings = {}   # set.cgi 改過的參數：key → {欄位: 值}
whitelist = {}  # 車牌 → [開始日期, 結束日期, 開始時間, 結束時間, 類型]
lock = threading.Lock()


def log(text):
    print(f"[假攝影機 {datetime.now():%H:%M:%S}] {text}")


def load_whitelist():
    path = os.path.join(SNAP, "whitelist.txt")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        cols = line.strip().split("\t")
        if len(cols) >= 6 and cols[0] and "：" not in cols[0]:
            whitelist[cols[0]] = cols[1:6]


def field(rec, offset, size=11):
    return rec[offset:offset + size].split(b"\x00")[0].decode("gb2312", errors="replace")


def record(plate, values):
    buf = bytearray(135)
    for offset, text in zip((0, 16, 27, 38, 49, 60), [plate] + values):
        b = text.encode("gb2312")
        buf[offset:offset + len(b)] = b
    return bytes(buf)


class SdkHandler(socketserver.BaseRequestHandler):
    def handle(self):
        head = self.request.recv(32)
        if len(head) < 32:
            return
        total, cmd = struct.unpack("<II", head[:8])
        body = b""
        while len(body) < total - 32:
            chunk = self.request.recv(total - 32 - len(body))
            if not chunk:
                break
            body += chunk
        data, status = b"", 1
        with lock:
            if cmd == 20741:
                text = "".join("\t".join([p] + v) + "\t\n" for p, v in whitelist.items())
                data = text.encode("gb2312") + b"\x00"
                log(f"讀白名單（{len(whitelist)} 筆）")
            elif cmd == 20753:
                plate = field(body, 0, 16)
                whitelist[plate] = [field(body, o) for o in (16, 27, 38, 49, 60)]
                log(f"新增／更新白名單 {plate} {whitelist[plate][0]}～{whitelist[plate][1]}")
            elif cmd == 20755:
                plate = field(body, 0, 16)
                status = 1 if whitelist.pop(plate, None) is not None else 0
                log(f"刪除白名單 {plate}（{'有' if status else '沒有這筆'}）")
            elif cmd == 20759:
                plate = field(body, 0, 16)
                if plate in whitelist:
                    data = record(plate, whitelist[plate])
                else:
                    status = 0
            elif cmd in (4151, 4163):
                log(f"485-{1 if cmd == 4151 else 2} 透傳 {len(body)} bytes")
            else:
                status = 0
                log(f"不認得的指令 {cmd}")
        reply = struct.pack("<III", 32 + len(data), cmd + 1, status) + bytes(20) + data
        self.request.sendall(reply)


class WebHandler(BaseHTTPRequestHandler):
    def reply(self, body, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/cgi-bin/get.cgi":
            key = parse_qs(url.query).get("key", [""])[0]
            path = os.path.join(SNAP, f"get_{os.path.basename(key)}.json")
            with lock:
                if key in settings:
                    return self.reply(json.dumps({key: settings[key]}, ensure_ascii=False).encode("utf-8"))
            body = open(path, "rb").read() if os.path.exists(path) else b"{}"
            return self.reply(body)
        if url.path.endswith(".jpg"):
            samples = sorted(glob.glob(os.path.join(SNAP, "sample_*.jpg")))
            return self.reply(open(samples[0], "rb").read() if samples else b"", "application/octet-stream")
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True).items()}
        path = urlparse(self.path).path
        if path == "/cgi-bin/set.cgi":
            if form.get("key") == "camera_restart" and form.get("restart_manual") == "1":
                log("收到重開機指令（假攝影機不會真的重開）")
            else:
                key = form.get("key", "")
                path = os.path.join(SNAP, f"get_{os.path.basename(key)}.json")
                if key != "led" and os.path.exists(path):
                    with lock:
                        cur = settings.get(key) or json.load(open(path, encoding="utf-8")).get(key, {})
                        for k, v in form.items():
                            if k in cur:
                                cur[k] = int(v) if type(cur[k]) is int and v.lstrip("-").isdigit() else v
                        settings[key] = cur
                log(f"收到設定 key={key}（{len(form)} 個欄位）")
            return self.reply(b"0", "text/plain")
        if path == "/cgi-bin/queryLocalPic.cgi":
            data = json.load(open(os.path.join(SNAP, "queryLocalPic.json"), encoding="utf-8"))
            plate = form.get("plate_number", "")
            if plate:
                for day in data["getimg"]["list"]:
                    day["imglist"] = [p for p in day["imglist"] if plate in p]
            log(f"查照片 車牌={plate or '全部'} {form.get('start_time')}～{form.get('end_time')}")
            return self.reply(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        self.send_error(404)

    def log_message(self, *args):
        pass


def main():
    load_whitelist()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    servers = [socketserver.ThreadingTCPServer((HOST, SDK_PORT), SdkHandler),
               ThreadingHTTPServer((HOST, WEB_PORT), WebHandler),
               ThreadingHTTPServer((HOST, PHOTO_PORT), WebHandler)]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    log(f"啟動：白名單 {HOST}:{SDK_PORT}、網頁 {WEB_PORT}、照片 {PHOTO_PORT}，白名單 {len(whitelist)} 筆。按 Ctrl+C 結束。")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
