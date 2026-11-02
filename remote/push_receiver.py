# -*- coding: utf-8 -*-
"""
現場小主機的「攝影機推送接收」：攝影機認到車牌（type=online）或心跳（type=heartbeat）時，
照後台「HTTP POST」設定推到這台小主機（格式見 協議實測紀錄.md，form-urlencoded，圖片是 URL-safe Base64、補位 = 改成 .）。

  - 只收設定檔裡那幾支攝影機 IP 送來的資料，其他來源一律不理。
  - ⚠️ 一律只回「HTTP 200、空內容」：絕不回任何開閘指令（gpio_data 之類），開閘由攝影機自己依白名單判斷。
  - 認到的車牌先排在記憶體（最多 MAX_EVENTS 筆，斷網時保留），由送出執行緒盡快送到 PSC。
  - 照片只在「不是白名單車」時才送（使用者 2026-11-02 決定：一般通行不存照片，只有異常才存）。
"""

import base64
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

MAX_EVENTS = 2000          # 斷網時最多暫存幾筆，超過丟掉最舊的
MAX_BODY = 2_000_000       # 一筆推送（含兩張圖）實測約 80KB
MAX_PHOTO = 400_000
PLATE_RE = re.compile(r"^[A-Z0-9]{2,12}$")

events = []                # 還沒送到平台的進出紀錄
lock = threading.Condition()
heartbeat = {}             # 攝影機代號 → 最後一次收到心跳（或辨識）的時間


def decode_picture(text):
    """攝影機的 URL-safe Base64（. 代替 =）→ 標準 Base64；不是 JPEG 或太大就不要。"""
    try:
        data = base64.b64decode(text.replace("-", "+").replace("_", "/").replace(".", "="), validate=True)
    except (ValueError, TypeError):
        return None
    if not data.startswith(b"\xff\xd8") or len(data) > MAX_PHOTO:
        return None
    return base64.b64encode(data).decode("ascii")


def to_event(cam_id, form):
    plate = form.get("plate_num", "").strip().upper()
    if not PLATE_RE.match(plate):
        plate = "無法辨識"
    try:
        ts = datetime.fromtimestamp(int(form.get("start_time", ""))).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OverflowError, OSError):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    is_white = form.get("is_whitelist") == "true"
    try:
        confidence = max(0, min(int(form.get("confidence", "")), 1000))
    except ValueError:
        confidence = None
    event = {"camera": cam_id, "plate": plate, "time": ts, "direction": "out" if form.get("vdc_type") == "out" else "in",
             "is_whitelist": is_white, "confidence": confidence}
    if not is_white and form.get("picture"):
        photo = decode_picture(form["picture"])
        if photo:
            event["photo_b64"] = photo
    return event


def handle(cam_id, form):
    kind = form.get("type")
    if kind not in ("heartbeat", "online"):
        return None
    with lock:
        heartbeat[cam_id] = time.time()
        if kind == "online":
            event = to_event(cam_id, form)
            events.append(event)
            del events[:-MAX_EVENTS]
            lock.notify_all()
            return event
    return None


def take(limit=20):
    """等到有紀錄（最多 20 秒），一次拿走最多 limit 筆。"""
    with lock:
        if not events:
            lock.wait(20)
        batch, events[:limit] = events[:limit], []
        return batch


def give_back(batch):
    """送不出去就放回最前面，下次再送。"""
    with lock:
        events[:0] = batch
        del events[:-MAX_EVENTS]


def camera_status():
    now = time.time()
    with lock:
        return {cam: int(now - t) for cam, t in heartbeat.items()}


class PushHandler(BaseHTTPRequestHandler):
    cams_by_ip = {}        # 攝影機 IP → 代號（從設定檔來）
    on_event = None
    timeout = 20

    def do_POST(self):
        try:
            cam_id = self.cams_by_ip.get(self.client_address[0])
            length = int(self.headers.get("Content-Length") or 0)
            if cam_id and 0 < length <= MAX_BODY:
                body = self.rfile.read(length).decode("utf-8", errors="replace")
                form = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
                event = handle(cam_id, form)
                if event and self.on_event:
                    self.on_event(event)
        except Exception:  # 推送資料怪怪的也不能讓接收端掛掉
            pass
        finally:
            # 只回空白 200：絕不下開閘指令
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def do_GET(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def start(cameras, host, port, on_event=None):
    PushHandler.cams_by_ip = {c["ip"]: c["id"] for c in cameras}
    PushHandler.on_event = staticmethod(on_event) if on_event else None
    server = ThreadingHTTPServer((host, port), PushHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
