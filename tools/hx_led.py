# -*- coding: utf-8 -*-
"""
華夏攝影機字幕機（LED）設定：讀取與修改攝影機後台「字幕機參數」那一頁的內容。

做法是照攝影機後台網頁（led.html／js/led.js，2026-11-02 讀出）送一樣的請求：
  讀：GET  http://{攝影機}:8000/cgi-bin/get.cgi?key=led   → JSON，led.rule 是第一～四行
  寫：POST http://{攝影機}:8000/cgi-bin/set.cgi            → 表單，key=led＋攤平的欄位，回 "0" 表示成功
  攤平規則：rule[i] 的欄位加上行號（i+1），例如 entrance_idle_custom1；*_extcfg_* 只取 rule[1]、不加行號。
⚠️ 攝影機這兩個網址不用登入就能讀寫（2026-11-02 實測），正式部署要網段隔離。

只改「入口」的設定（這台攝影機是入口）；出口與其他欄位原樣送回。
"""

import json
import urllib.parse
import urllib.request

WEB_PORT = 8000
LINES = 4
TEXT_MAX_BYTES = 63  # 後台限制：GBK 63 位元組（中文字算 2）
MODES = {0: "離線時才顯示", 1: "一律顯示", 2: "不使用"}
WELCOMES = ("歡迎光臨", "歡迎回家")

IDLE_FLAGS = ("date", "time", "idle")              # entrance_{x}_enable：日期、時間、自訂文字
BUSY_FLAGS = ("type", "plate", "days", "busy")     # entrance_{x}_enable：車輛類型、車牌、月租剩餘天數、自訂文字


def _url(ip, path):
    return f"http://{ip}:{WEB_PORT}/cgi-bin/{path}"


def read_raw(ip):
    with urllib.request.urlopen(_url(ip, "get.cgi?key=led"), timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))["led"]


def flatten(led):
    """照後台網頁的方式把 rule 陣列攤平成表單欄位。"""
    flat = {k: v for k, v in led.items() if k not in ("rule", "protocol_list")}
    for i, rule in enumerate(led.get("rule", [])):
        for k, v in rule.items():
            if "_extcfg_" in k:
                if i == 1:
                    flat[k] = v
            else:
                flat[f"{k}{i + 1}"] = v
    flat["key"] = "led"
    return flat


def to_view(led):
    """攝影機的原始設定 → 網頁要顯示的入口設定。"""
    lines = []
    for rule in led.get("rule", [])[:LINES]:
        lines.append({
            "idle": {f: rule.get(f"entrance_{f}_enable", 0) == 1 for f in IDLE_FLAGS},
            "idle_text": rule.get("entrance_idle_custom", ""),
            "idle_color": rule.get("entrance_color", 0),
            "busy": {f: rule.get(f"entrance_{f}_enable", 0) == 1 for f in BUSY_FLAGS},
            "busy_text": rule.get("entrance_busy_custom", ""),
            "busy_color": rule.get("entrance_card_color", 0),
        })
    return {
        "mode": led.get("led_mode", 0),
        "protocol": led.get("led_protocol", 0),
        "lines": lines,
        "voice_plate": led.get("entrance_voice_plate_enable", 0) == 1,
        "voice_welcome_on": led.get("entrance_voice_welcome_enable", 0) == 1,
        "voice_welcome": led.get("entrance_voice_welcome", ""),
    }


def check_text(text):
    if not isinstance(text, str):
        raise ValueError("文字格式不對")
    try:
        size = len(text.encode("gbk"))
    except UnicodeEncodeError:
        raise ValueError(f"「{text}」有字幕機顯示不了的字")
    if size > TEXT_MAX_BYTES:
        raise ValueError(f"「{text}」太長了（中文最多約 31 個字）")
    return text


def apply_view(led, view):
    """把網頁送來的入口設定套回攝影機的原始設定（其他欄位不動）。"""
    mode = int(view["mode"])
    if mode not in MODES:
        raise ValueError("字幕機模式不對")
    led["led_mode"] = mode
    led["entrance_voice_plate_enable"] = 1 if view["voice_plate"] else 0
    led["entrance_voice_welcome_enable"] = 1 if view["voice_welcome_on"] else 0
    if view["voice_welcome"] not in WELCOMES + (led.get("entrance_voice_welcome"),):
        raise ValueError("歡迎語只能選歡迎光臨或歡迎回家")
    led["entrance_voice_welcome"] = view["voice_welcome"]
    rules = led.get("rule", [])
    if len(view["lines"]) != len(rules[:LINES]):
        raise ValueError("行數不對，請重新讀取")
    for rule, line in zip(rules, view["lines"]):
        for f in IDLE_FLAGS:
            rule[f"entrance_{f}_enable"] = 1 if line["idle"][f] else 0
        for f in BUSY_FLAGS:
            rule[f"entrance_{f}_enable"] = 1 if line["busy"][f] else 0
        rule["entrance_idle_custom"] = check_text(line["idle_text"])
        rule["entrance_busy_custom"] = check_text(line["busy_text"])
        for key, value in (("entrance_color", line["idle_color"]), ("entrance_card_color", line["busy_color"])):
            if int(value) not in (0, 1, 2):
                raise ValueError("顏色不對")
            rule[key] = int(value)
        rule["entrance_temp_color"] = rule["entrance_card_color"]  # 臨停車、月租車先用同一個顏色
    return led


def write(ip, view):
    led = apply_view(read_raw(ip), view)
    body = urllib.parse.urlencode(flatten(led)).encode("utf-8")
    req = urllib.request.Request(_url(ip, "set.cgi"), data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    with urllib.request.urlopen(req, timeout=10) as r:
        reply = r.read().strip()
    if reply != b"0":
        raise ValueError(f"攝影機沒有接受字幕機設定（回覆 {reply[:50]!r}）")
    return to_view(read_raw(ip))
