# -*- coding: utf-8 -*-
"""
華夏攝影機參數（PSC 遠端可讀寫的範圍）：照後台各頁 get.cgi／set.cgi（2026-11-02 讀出）。

  讀：GET  get.cgi?key=XXX → {"XXX": {欄位: 值}}
  寫：POST set.cgi，key=XXX＋該頁「全部」欄位（先讀回原值，只換掉允許改的欄位），回 "0" 表示成功

只開放 GROUPS 裡列的欄位；改 IP、恢復原廠、帳號、清資料等高風險的 key 一律不碰。
欄位中文名與範圍照後台 ch.js 與各頁 input 的 min／max。
"""

import json
import urllib.parse
import urllib.request

from hx_led import _url

GROUPS = [
    {"key": "camera_info", "title": "基本資料", "fields": [
        {"name": "camera_name", "label": "攝影機名稱", "type": "text", "max": 31},
        {"name": "camera_vdc_type", "label": "出入口類型", "type": "select", "options": {0: "未配置", 1: "入口", 2: "出口"}},
    ]},
    {"key": "black_whitelist", "title": "白名單開閘規則", "warn": "會影響閘門開不開，改之前請確認現場狀況。臨時車規則只能在現場改", "fields": [
        {"name": "whitelist_mode", "label": "白名單車", "type": "select", "options": {1: "自動開閘", 0: "離線後才自動開閘", 2: "不開閘"}},
        {"name": "whitelistOutput", "label": "白名單開閘輸出口", "type": "select", "options": {1: "OUT1", 2: "OUT2", 3: "OUT1＋OUT2", 0: "不輸出"}},
        # 臨時車（不在白名單的車）改成自動開閘＝陌生車也會開，遠端一律不准改，只能在現場後台改（安全審查 2026-11-03）
        {"name": "tmp_mode", "label": "臨時車（不在白名單）", "type": "select", "options": {0: "不開閘", 1: "自動開閘", 2: "離線後自動開閘"}, "readonly": True},
    ]},
    {"key": "io_param", "title": "閘門輸出", "fields": [
        {"name": "io_out_1_time", "label": "OUT1 開閘訊號長度（秒）", "type": "int", "min": 1, "max": 10},
    ]},
    {"key": "volume", "title": "音量", "fields": [
        {"name": "volume_in", "label": "輸入音量", "type": "int", "min": 0, "max": 10},
        {"name": "volume_out", "label": "喇叭音量", "type": "int", "min": 0, "max": 10},
    ]},
    {"key": "algo", "title": "辨識", "fields": [
        {"name": "trigger_mode", "label": "觸發方式", "type": "select", "options": {0: "地感線圈觸發", 1: "影像動態觸發", 2: "混合模式"}},
        {"name": "sensitivity_value", "label": "靈敏度", "type": "int", "min": 1, "max": 100},
        {"name": "trigger_interval", "label": "觸發間隔", "type": "int", "min": 3, "max": 500},
        {"name": "sameplate_outinterval", "label": "同一車牌重複輸出間隔（秒）", "type": "int", "min": 1, "max": 3600},
        {"name": "recognized_value", "label": "辨識準確度", "type": "int", "min": 0, "max": 28},
    ]},
]
GROUP_BY_KEY = {g["key"]: g for g in GROUPS}

# 只讀不寫的資訊
INFO = [
    ("device_param", "extern_model", "型號"),
    ("device_param", "device_mac", "MAC"),
    ("device_param", "device_version", "韌體版本"),
]


def read_key(ip, key):
    with urllib.request.urlopen(_url(ip, f"get.cgi?key={urllib.parse.quote(key)}"), timeout=8) as r:
        cfg = json.loads(r.read().decode("utf-8"))[key]
    if not isinstance(cfg, dict):
        raise ValueError("攝影機回傳格式不對")
    return cfg


def read_all(ip):
    """回傳 {groups: [{key, title, warn, fields:[{..., value}]}], info: [{label, value}]}，前端照這個畫表單。"""
    groups = []
    for g in GROUPS:
        cfg = read_key(ip, g["key"])
        fields = []
        for f in g["fields"]:
            v = cfg.get(f["name"])
            item = {k: v2 for k, v2 in f.items() if k != "options"}
            if "options" in f:
                item["options"] = [[k, label] for k, label in f["options"].items()]
            item["value"] = v if type(v) in (int, str) else None  # 攝影機回來的值不直接信任
            fields.append(item)
        groups.append({"key": g["key"], "title": g["title"], "warn": g.get("warn", ""), "fields": fields})
    info, cache = [], {}
    for key, name, label in INFO:
        if key not in cache:
            try:
                cache[key] = read_key(ip, key)
            except (OSError, ValueError, KeyError):
                cache[key] = {}
        v = cache[key].get(name, "")
        info.append({"label": label, "value": str(v).replace("\r", "").replace("\n", " ")[:200]})
    return {"groups": groups, "info": info}


def check_value(field, value):
    t = field["type"]
    if t == "text":
        if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError(f"{field['label']}格式不對")
        try:
            size = len(value.encode("gbk"))
        except UnicodeEncodeError:
            raise ValueError(f"{field['label']}有攝影機存不了的字")
        if not value.strip() or size > field["max"]:
            raise ValueError(f"{field['label']}不能空白，也不能太長（中文約 {field['max'] // 2} 字）")
        return value
    if type(value) is not int:
        raise ValueError(f"{field['label']}要是數字")
    if t == "select" and value not in field["options"]:
        raise ValueError(f"{field['label']}的選項不對")
    if t == "int" and not field["min"] <= value <= field["max"]:
        raise ValueError(f"{field['label']}要在 {field['min']}～{field['max']} 之間")
    return value


def write(ip, key, values):
    """只改 values 裡允許的欄位，其他欄位照原值送回。回傳改了哪些欄位（寫進操作紀錄）。"""
    group = GROUP_BY_KEY.get(key)
    if not group:
        raise ValueError("這個分類不能改")
    if not isinstance(values, dict) or not values:
        raise ValueError("沒有要改的欄位")
    allowed = {f["name"]: f for f in group["fields"]}
    for name in values:
        if name not in allowed or allowed[name].get("readonly"):
            raise ValueError(f"欄位 {str(allowed[name]['label'] if name in allowed else name)[:30]} 不能從遠端改")
    before = read_key(ip, key)
    if any(isinstance(v, (dict, list)) for v in before.values()):
        raise ValueError("攝影機回傳格式不對")
    after = dict(before)
    changes = []
    for name, value in values.items():
        after[name] = check_value(allowed[name], value)
        if before.get(name) != after[name]:
            changes.append(f"{allowed[name]['label']}：{before.get(name)!r}→{after[name]!r}")
    if not changes:
        return []
    body = urllib.parse.urlencode({"key": key, **after}).encode("utf-8")
    req = urllib.request.Request(_url(ip, "set.cgi"), data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    with urllib.request.urlopen(req, timeout=10) as r:
        reply = r.read().strip()
    if reply != b"0":
        raise ValueError(f"攝影機沒有接受設定（回覆 {reply[:50]!r}）")
    check = read_key(ip, key)
    wrong = [allowed[n]["label"] for n in values if check.get(n) != after[n]]
    if wrong:
        raise ValueError(f"攝影機回覆成功，但讀回來 {'、'.join(wrong)} 沒有變，請再試一次")
    return changes
