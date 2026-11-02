# -*- coding: utf-8 -*-
"""
華夏攝影機重開機（照後台「設備維護」頁 js/equipment.js，2026-11-02 讀出）。

  讀：GET  get.cgi?key=camera_restart → restart_timing（定時重開時間）、restart_timingenbale（拼字照原樣）、restart_interval（每幾天）
  手動重開：POST set.cgi，key=camera_restart＋原本欄位＋restart_manual=1，回 "0" 表示成功
"""

import json
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


def reboot(ip):
    _, cfg = read_restart(ip)
    fields = {**cfg, "key": "camera_restart", "restart_manual": "1"}
    req = urllib.request.Request(_url(ip, "set.cgi"), data=urllib.parse.urlencode(fields).encode("utf-8"),
                                 headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    with urllib.request.urlopen(req, timeout=10) as r:
        reply = r.read().strip()
    if reply != b"0":
        raise ValueError(f"攝影機沒有接受重開機（回覆 {reply[:50]!r}）")
