# -*- coding: utf-8 -*-
"""
華夏攝影機重開機與 SD 卡照片（照後台 js/equipment.js、preview_picture.html，2026-11-02 讀出）。

  讀：GET  get.cgi?key=camera_restart → restart_timing（定時重開時間）、restart_timingenbale（拼字照原樣）、restart_interval（每幾天）
  手動重開：POST set.cgi，key=camera_restart＋原本欄位＋restart_manual=1，回 "0" 表示成功
"""

import json
import re
import urllib.parse
import urllib.request

from hx_led import _url


def read_restart(ip):
    with urllib.request.urlopen(_url(ip, "get.cgi?key=camera_restart"), timeout=8) as r:
        cfg = json.loads(r.read().decode("utf-8"))["camera_restart"]
    return {
        "auto": cfg.get("restart_timingenbale") == 1,
        "time": str(cfg.get("restart_timing", "")),
        "every_days": cfg.get("restart_interval") if type(cfg.get("restart_interval")) is int else "?",
    }, cfg


PHOTO_PATH = re.compile(r"^/root/disk/picture/[0-9A-Za-z_/.\-一-鿿]+\.jpg$")


def query_photos(ip, plate, start, end):
    """SD 卡照片查詢（後台 preview_picture.html）：時間格式 yyyy-MM-dd HH:mm:ss。回傳 [{date, paths}]。"""
    body = urllib.parse.urlencode({"plate_number": plate, "start_time": start, "end_time": end}).encode("utf-8")
    req = urllib.request.Request(_url(ip, "queryLocalPic.cgi"), data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    with urllib.request.urlopen(req, timeout=20) as r:
        days = json.loads(r.read().decode("utf-8"))["getimg"]["list"]
    return [{"date": d["date"], "paths": [p for p in d["imglist"] if PHOTO_PATH.match(p)]} for d in days]


def get_photo(ip, path, port=80, max_bytes=2_000_000):
    """下載一張 SD 卡照片（攝影機 80 埠直接給檔案）。"""
    if not PHOTO_PATH.match(path) or ".." in path:
        raise ValueError("照片路徑不對")
    with urllib.request.urlopen(f"http://{ip}:{port}{urllib.parse.quote(path)}", timeout=15) as r:
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes or not data.startswith(b"\xff\xd8"):
        raise ValueError("攝影機回來的不是照片")
    return data


def reboot(ip):
    _, cfg = read_restart(ip)
    fields = {**cfg, "key": "camera_restart", "restart_manual": "1"}
    req = urllib.request.Request(_url(ip, "set.cgi"), data=urllib.parse.urlencode(fields).encode("utf-8"),
                                 headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    with urllib.request.urlopen(req, timeout=10) as r:
        reply = r.read().strip()
    if reply != b"0":
        raise ValueError(f"攝影機沒有接受重開機（回覆 {reply[:50]!r}）")
