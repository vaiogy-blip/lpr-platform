# -*- coding: utf-8 -*-
"""
PSC 雲平台：主控／客戶公司／場站三層的管理網頁＋現場小主機的連線點（取代 remote/server.py）。

  - 每人一個帳號，權限來自各公司自己定義的「權限群組」；客戶公司只看得到自己的資料。
  - 現場小主機用各自的通行碼連進來（/gw/poll 長輪詢，協定跟 remote/server.py 一樣，舊小主機不用改）。
  - 所有會改東西的動作，以及看照片，都記在資料庫的 audit 表。
  - 只綁 127.0.0.1；給外面連用 cloudflared 通道（HTTPS）。

第一次使用：python psc/server.py init
  建立主控公司與預設管理員 admin。若有舊的 remote/server_config.json，沿用舊密碼與 company 小主機通行碼。
啟動：python psc/server.py
"""

import base64
import binascii
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
OLD_CONFIG = os.path.join(HERE, "..", "remote", "server_config.json")
PORT = int(os.environ.get("PORT", 8600))

POLL_WAIT = 25          # 小主機長輪詢最多等幾秒
CMD_WAIT = 45           # 網頁等指令結果最多幾秒
ONLINE_WITHIN = 60      # 幾秒內有連線就算上線
SESSION_HOURS = 12
MAX_GW_BODY = 4_000_000
MAX_EVENTS_BODY = 12_000_000   # 一次最多 20 筆，每筆照片約 60KB（Base64 後約 80KB）
MAX_EVENT_PHOTO = 400_000
# 進出紀錄與照片保留天數（個資，不能無限累積）。使用者還沒定案，先預設 90 天，可用環境變數 PSC_EVENT_DAYS 改
EVENT_DAYS = int(os.environ.get("PSC_EVENT_DAYS") or 90)
EVENT_PLATE_RE = re.compile(r"^([A-Z0-9]{2,12}|無法辨識)$")
MAX_BODY = 32_000
MAX_QUEUE = 20
MIN_PASSWORD = 10

# 每個小主機指令需要的權限；NO_AUDIT 是只讀、成功不記（失敗一樣記）
CMD_PERM = {
    "wl_list": "wl.view", "wl_add": "wl.edit", "wl_delete": "wl.edit", "wl_batch": "wl.edit",
    "photo_query": "photo.view", "photo_get": "photo.view",
    "param_get": "param.view", "led_get": "param.view", "restart_info": "param.view",
    "param_set": "param.edit", "led_set": "led.edit", "reboot": "camera.reboot",
}
NO_AUDIT = {"restart_info", "led_get", "param_get"}  # 讀白名單也要記：車牌是個資
CMD_LABEL = {"wl_batch": "批次匯入白名單", "wl_add": "新增白名單", "wl_delete": "刪除白名單", "photo_query": "查照片", "photo_get": "看照片",
             "param_set": "改攝影機參數", "led_set": "改字幕機", "reboot": "重開攝影機", "wl_list": "讀白名單",
             "param_get": "讀參數", "led_get": "讀字幕機", "restart_info": "讀重開機排程"}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}


class Fail(Exception):
    """回給網頁的錯誤（訊息是給使用者看的中文）。"""
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


# ---------- 密碼與通行碼 ----------

PBKDF2_ROUNDS = 600_000


def hash_password(password, salt=None, rounds=PBKDF2_ROUNDS):
    """新格式 pbkdf2$次數$salt$雜湊；舊平台的 salt$雜湊（20 萬次）也能驗。"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), rounds).hex()
    return f"pbkdf2${rounds}${salt}${digest}"


def check_password(password, stored):
    parts = stored.split("$")
    if len(parts) == 2:  # 舊平台格式
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(parts[0]), 200_000).hex()
        return hmac.compare_digest(digest, parts[1])
    return hmac.compare_digest(hash_password(password, parts[2], int(parts[1])), stored)


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def temp_password():
    return secrets.token_urlsafe(9)  # 12 字元，第一次登入一定要改


# ---------- 小主機與指令排隊（記憶體） ----------

class Gateway:
    def __init__(self, gid):
        self.id, self.last_seen, self.version = gid, 0, ""
        self.camera_status = {}     # 攝影機代號 → 小主機回報時距離上次心跳幾秒
        self.queue, self.cond = [], threading.Condition()


live = {}               # 小主機 id → Gateway
waiting = {}            # 指令 id → {"event", "result", "gateway"}
live_lock = threading.Lock()


def get_live(gid):
    with live_lock:
        return live.setdefault(gid, Gateway(gid))


def gateway_status(gid):
    gw = live.get(gid)
    seen = gw.last_seen if gw else 0
    online = time.time() - seen < ONLINE_WITHIN
    return {"online": online, "version": gw.version if gw else "",
            "last_seen": datetime.fromtimestamp(seen).strftime("%m-%d %H:%M:%S") if seen else "從未連線",
            # 攝影機有沒有在推送（心跳 10 秒一次；超過 60 秒沒收到就當沒在推）；None＝小主機太舊或沒設推送
            "camera_push": None if gw and gw.camera_status is None else
            ({k: v < ONLINE_WITHIN for k, v in gw.camera_status.items()} if gw and online else {})}


def send_command(gid, camera, kind, args):
    gw = get_live(gid)
    cmd_id = secrets.token_hex(12)  # 猜不到，別台小主機也無法冒充回結果
    slot = {"event": threading.Event(), "result": None, "gateway": gid}
    with gw.cond:
        if len(gw.queue) >= MAX_QUEUE:
            return {"ok": False, "error": "這台小主機待執行的指令太多，請稍後再試"}
        waiting[cmd_id] = slot
        gw.queue.append({"id": cmd_id, "camera": camera, "type": kind, "args": args})
        gw.cond.notify_all()
    try:
        if not slot["event"].wait(CMD_WAIT):
            with gw.cond:
                gw.queue = [c for c in gw.queue if c["id"] != cmd_id]  # 還沒送出就收回，避免之後才執行
            return {"ok": False, "error": "現場小主機沒有回應（可能離線或攝影機很慢），指令已取消或結果未知"}
        return slot["result"]
    finally:
        waiting.pop(cmd_id, None)


# ---------- 權限 ----------

def load_user(uid):
    u = db.one("""SELECT u.*, r.perms, r.name AS role_name, c.is_root, c.name AS company_name, c.active AS company_active
                  FROM users u JOIN roles r ON r.id = u.role_id JOIN companies c ON c.id = u.company_id WHERE u.id = ?""", (uid,))
    if not u or not u["active"] or not u["company_active"]:
        return None
    role = db.one("SELECT is_system FROM roles WHERE id=?", (u["role_id"],))
    # 內建管理員群組的權限跟著程式走（之後新增權限時自動有）
    u["perms"] = set(db.admin_perms(u["is_root"]) if role["is_system"] else json.loads(u["perms"]))
    if not u["is_root"]:
        u["perms"] -= db.ROOT_ONLY
    u["site_ids"] = json.loads(u["site_ids"]) if u["site_ids"] else None
    return u


def need(user, perm):
    if perm not in user["perms"]:
        raise Fail("你沒有這個權限", 403)


def visible_sites(user):
    """這個帳號看得到的場站：主控看全部公司，客戶只看自己公司；再套用帳號的場站限制。"""
    if user["is_root"]:
        sites = db.q("SELECT s.*, c.name AS company_name FROM sites s JOIN companies c ON c.id = s.company_id ORDER BY c.is_root DESC, c.name, s.name")
    else:
        sites = db.q("SELECT s.*, c.name AS company_name FROM sites s JOIN companies c ON c.id = s.company_id WHERE s.company_id = ? ORDER BY s.name",
                     (user["company_id"],))
    if user["site_ids"] is not None:
        sites = [s for s in sites if s["id"] in user["site_ids"]]
    return sites


def site_for(user, site_id):
    site = next((s for s in visible_sites(user) if s["id"] == site_id), None)
    if not site:
        raise Fail("找不到這個場站，或你沒有權限", 404)
    return site


def company_for(user, company_id):
    """要管理的公司：沒指定就是自己公司；只有主控能指定別家。"""
    if company_id in (None, "", user["company_id"]):
        company_id = user["company_id"]
    elif not user["is_root"]:
        raise Fail("你只能管理自己的公司", 403)
    elif type(company_id) is not int or not 0 < company_id < 2 ** 62:
        raise Fail("資料格式不對")
    c = db.one("SELECT * FROM companies WHERE id = ?", (company_id,))
    if not c:
        raise Fail("找不到這家公司", 404)
    return c


def text(body, name, label, max_len, required=True):
    v = body.get(name, "")
    if not isinstance(v, str) or any(ord(ch) < 32 for ch in v):
        raise Fail(f"{label}格式不對")
    v = v.strip()
    if required and not v:
        raise Fail(f"{label}不能空白")
    if len(v) > max_len:
        raise Fail(f"{label}太長（最多 {max_len} 字）")
    return v


def int_id(body, name):
    v = body.get(name)
    if type(v) is not int or not 0 < v < 2 ** 62:
        raise Fail("資料格式不對")
    return v


def check_new_password(pw):
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD or len(pw) > 200:
        raise Fail(f"新密碼至少 {MIN_PASSWORD} 個字")
    return pw


# ---------- API ----------

def api_me(h, user, body):
    return {"user": public_user(user), "perm_defs": [{"key": k, "label": l, "desc": d} for k, l, d in db.PERMS
                                                      if user["is_root"] or k not in db.ROOT_ONLY],
            "site_kinds": db.SITE_KINDS}


def public_user(u):
    return {"id": u["id"], "username": u["username"], "display_name": u["display_name"], "company_id": u["company_id"],
            "company_name": u["company_name"], "is_root": bool(u["is_root"]), "role_name": u["role_name"],
            "perms": sorted(u["perms"]), "must_change_pw": bool(u["must_change_pw"]), "site_ids": u["site_ids"]}


def api_change_password(h, user, body):
    t, key = time.time(), f"pw:{user['id']}"
    if locked(key, t):
        raise Fail("密碼錯太多次，請 5 分鐘後再試", 429)
    if not isinstance(body.get("old"), str) or not check_password(body["old"], db.one("SELECT pw_hash FROM users WHERE id=?", (user["id"],))["pw_hash"]):
        time.sleep(1)
        add_fail(key, t)
        db.audit(user, h.client_ip(), "改自己的密碼", result="目前的密碼不對")
        raise Fail("目前的密碼不對")
    login_fail.pop(key, None)
    new = check_new_password(body.get("new"))
    if new == body["old"]:
        raise Fail("新密碼不能跟舊的一樣")
    db.run("UPDATE users SET pw_hash=?, must_change_pw=0 WHERE id=?", (hash_password(new), user["id"]))
    db.run("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user["id"], h.session_hash))  # 其他裝置登出
    db.audit(user, h.client_ip(), "改自己的密碼")
    return {}


def site_rows(user):
    sites = visible_sites(user)
    gws = db.q("SELECT id, site_id, name, cameras FROM gateways ORDER BY name")
    out = []
    for s in sites:
        mine = []
        for g in gws:
            if g["site_id"] == s["id"]:
                mine.append({"id": g["id"], "name": g["name"], "cameras": json.loads(g["cameras"]), **gateway_status(g["id"])})
        out.append({"id": s["id"], "name": s["name"], "kind": s["kind"], "address": s["address"],
                    "company_id": s["company_id"], "company_name": s["company_name"], "gateways": mine,
                    "online": bool(mine) and all(g["online"] for g in mine)})
    return out


def today_counts(site_ids):
    """各場站今天的進出數與非白名單數。"""
    if not site_ids:
        return {}
    marks = ",".join("?" * len(site_ids))
    rows = db.q(f"""SELECT site_id, COUNT(*) AS n, SUM(CASE WHEN is_whitelist=0 THEN 1 ELSE 0 END) AS abnormal
                    FROM events WHERE site_id IN ({marks}) AND ts >= ? GROUP BY site_id""",
                (*site_ids, datetime.now().strftime("%Y-%m-%d 00:00:00")))
    return {r["site_id"]: {"n": r["n"], "abnormal": r["abnormal"] or 0} for r in rows}


def api_dashboard(h, user, body):
    need(user, "site.view")
    sites = site_rows(user)
    today = today_counts([s["id"] for s in sites]) if "event.view" in user["perms"] else {}
    for s in sites:
        s["today"] = today.get(s["id"], {"n": 0, "abnormal": 0}) if "event.view" in user["perms"] else None
    gws = [g for s in sites for g in s["gateways"]]
    companies = {s["company_id"] for s in sites}
    if user["is_root"] and user["site_ids"] is None:
        companies = {c["id"] for c in db.q("SELECT id FROM companies")}
    return {"counts": {"companies": len(companies), "sites": len(sites), "gateways": len(gws),
                       "gateways_online": sum(g["online"] for g in gws),
                       "cameras": sum(len(g["cameras"]) for g in gws),
                       "sites_offline": sum(1 for s in sites if s["gateways"] and not s["online"]),
                       "sites_no_gateway": sum(1 for s in sites if not s["gateways"]),
                       "today_events": sum(t["n"] for t in today.values()) if "event.view" in user["perms"] else None,
                       "today_abnormal": sum(t["abnormal"] for t in today.values()) if "event.view" in user["perms"] else None},
            "sites": sites}


def api_sites(h, user, body):
    need(user, "site.view")
    companies = db.q("SELECT id, name, is_root FROM companies ORDER BY is_root DESC, name") if user["is_root"] else \
        db.q("SELECT id, name, is_root FROM companies WHERE id=?", (user["company_id"],))
    return {"sites": site_rows(user), "companies": companies}


def api_site_save(h, user, body):
    need(user, "site.manage")
    name = text(body, "name", "場站名稱", 40)
    address = text(body, "address", "地址", 100, required=False)
    kind = body.get("kind")
    if not isinstance(kind, str) or kind not in db.SITE_KINDS:
        raise Fail("場站類型不對")
    if body.get("id") is None:
        if user["site_ids"] is not None:
            raise Fail("你的帳號只能管指定的場站，不能新增場站", 403)
        company = company_for(user, body.get("company_id"))
        sid = db.run("INSERT INTO sites(company_id, name, kind, address, created) VALUES(?,?,?,?,?)",
                     (company["id"], name, kind, address, db.now()))
        db.audit(user, h.client_ip(), "新增場站", name, f"公司 {company['name']}", company_id=company["id"], site_id=sid)
        return {"id": sid}
    site = site_for(user, int_id(body, "id"))
    db.run("UPDATE sites SET name=?, kind=?, address=? WHERE id=?", (name, kind, address, site["id"]))
    db.audit(user, h.client_ip(), "修改場站", name, f"原名 {site['name']}", company_id=site["company_id"], site_id=site["id"])
    return {"id": site["id"]}


def gateway_config(h, gid, token):
    return {"server": h.base_url(), "gateway_id": gid, "token": token,
            "cameras": [{"id": "lane1", "name": "入口車道", "ip": "192.168.1.100", "photo_port": 80}]}


def api_gateway_add(h, user, body):
    need(user, "site.manage")
    site = site_for(user, int_id(body, "site_id"))
    name = text(body, "name", "小主機名稱", 40)
    gid, token = "gw-" + secrets.token_hex(4), secrets.token_urlsafe(32)
    db.run("INSERT INTO gateways(id, site_id, name, token_hash, created) VALUES(?,?,?,?,?)",
           (gid, site["id"], name, token_hash(token), db.now()))
    db.audit(user, h.client_ip(), "新增現場小主機", f"{site['name']} / {name}", gid, company_id=site["company_id"], site_id=site["id"])
    return {"config": gateway_config(h, gid, token)}


def gateway_in_scope(user, gid):
    g = db.one("SELECT * FROM gateways WHERE id=?", (gid,)) if isinstance(gid, str) else None
    if not g:
        raise Fail("找不到這台小主機", 404)
    return g, site_for(user, g["site_id"])


def api_gateway_reset(h, user, body):
    need(user, "site.manage")
    g, site = gateway_in_scope(user, body.get("gateway_id"))
    token = secrets.token_urlsafe(32)
    db.run("UPDATE gateways SET token_hash=? WHERE id=?", (token_hash(token), g["id"]))
    db.audit(user, h.client_ip(), "重發小主機通行碼", f"{site['name']} / {g['name']}", g["id"], company_id=site["company_id"], site_id=site["id"])
    cams = json.loads(g["cameras"])
    cfg = gateway_config(h, g["id"], token)
    return {"config": cfg, "note": "舊的通行碼已失效；攝影機清單請沿用現場原本的設定" if cams else ""}


def api_cmd(h, user, body):
    kind, camera, args = body.get("type"), body.get("camera"), body.get("args") or {}
    if not isinstance(kind, str) or kind not in CMD_PERM or not isinstance(camera, str) or not isinstance(args, dict):
        raise Fail("指令不對")
    need(user, CMD_PERM[kind])
    if kind == "param_set" and args.get("key") == "black_whitelist":
        need(user, "gate.rule")  # 開閘規則會影響閘門開不開，只有主控能改
    g, site = gateway_in_scope(user, body.get("gateway"))
    if not db.one("SELECT active FROM companies WHERE id=?", (site["company_id"],))["active"]:
        raise Fail("這家公司已停用")
    if not any(c.get("id") == camera for c in json.loads(g["cameras"])):
        raise Fail("這台小主機沒有這支攝影機")
    if not gateway_status(g["id"])["online"]:
        raise Fail("現場小主機目前離線（最近一分鐘沒有連線），請確認那台電腦和程式有開", 200)
    if kind == "wl_batch":  # 先記下要匯入哪些車牌：就算之後逾時，稽核也查得到
        items = args.get("items") if isinstance(args.get("items"), list) else []
        db.audit(user, h.client_ip(), "批次匯入白名單（送出）", f"{site['name']} / {g['name']} / {camera}",
                 json.dumps({"筆數": len(items), "車牌": [str(i.get("plate"))[:16] for i in items[:200] if isinstance(i, dict)]}, ensure_ascii=False),
                 "送出", company_id=site["company_id"], site_id=site["id"])
    result = send_command(g["id"], camera, kind, args)
    if kind not in NO_AUDIT or not result.get("ok"):
        shown = {k: v for k, v in args.items() if k != "led"}  # 字幕機整包太長，改了什麼小主機會記
        if kind == "param_set" and result.get("ok"):
            shown = {"變更": (result.get("data") or {}).get("changes") or "沒有變更"}
        if kind == "wl_batch":
            rep = (result.get("data") or {}).get("report") or []
            ok_plates = [r.get("plate") for r in rep if isinstance(r, dict) and r.get("ok")]
            shown = {"筆數": len(args.get("items") or []), "成功": len(ok_plates), "車牌": ok_plates[:200]}
        db.audit(user, h.client_ip(), CMD_LABEL.get(kind, kind), f"{site['name']} / {g['name']} / {camera}",
                 json.dumps(shown, ensure_ascii=False), "成功" if result.get("ok") else result.get("error"),
                 company_id=site["company_id"], site_id=site["id"])
    if not result.get("ok"):
        raise Fail(result.get("error") or "失敗", 200)
    return {"data": result.get("data") or {}}


def api_events(h, user, body):
    need(user, "event.view")
    sites = {s["id"]: s for s in visible_sites(user)}
    site_id = body.get("site_id")
    if site_id is not None:
        if type(site_id) is not int or site_id not in sites:
            raise Fail("找不到這個場站，或你沒有權限", 404)
        ids = [site_id]
    else:
        ids = list(sites)
    date = body.get("date") or datetime.now().strftime("%Y-%m-%d")
    try:
        date = datetime.strptime(str(date), "%Y-%m-%d").strftime("%Y-%m-%d")  # 2026-1-3 也轉成 2026-01-03
    except ValueError:
        raise Fail("日期格式不對")
    plate = body.get("plate") or ""
    if not isinstance(plate, str) or len(plate) > 12:
        raise Fail("車牌格式不對")
    plate = re.sub(r"[^A-Z0-9]", "", plate.upper())  # 只留英數：% _ 不會被當萬用字元
    if not ids:
        return {"rows": []}
    marks = ",".join("?" * len(ids))
    sql = f"""SELECT e.id, e.ts, e.site_id, e.camera_name, e.plate, e.direction, e.is_whitelist, e.confidence,
              e.photo IS NOT NULL AS has_photo FROM events e WHERE e.site_id IN ({marks}) AND e.ts >= ? AND e.ts <= ?"""
    args = [*ids, f"{date} 00:00:00", f"{date} 23:59:59"]
    if plate:
        sql += " AND e.plate LIKE ?"
        args.append(f"%{plate}%")
    if body.get("abnormal_only") is True:
        sql += " AND e.is_whitelist = 0"
    rows = db.q(sql + " ORDER BY e.ts DESC, e.id DESC LIMIT 500", args)
    for r in rows:
        r["site_name"] = sites[r["site_id"]]["name"]
    # 進出紀錄是個資：誰查了哪一天、哪個場站要留紀錄（同一畫面每 10 秒的自動更新不重複記，打開／按查詢時才記）
    target = sites[site_id]["name"] if site_id is not None else "全部場站"
    if body.get("auto") is not True:
        db.audit(user, h.client_ip(), "查進出紀錄", target, f"{date} {plate}".strip(), f"{len(rows)} 筆",
                 company_id=sites[site_id]["company_id"] if site_id is not None else None, site_id=site_id)
    return {"rows": rows, "date": date}


def api_event_photo(h, user, body):
    need(user, "event.view")
    need(user, "photo.view")
    e = db.one("SELECT id, site_id, plate, ts, photo FROM events WHERE id=?", (int_id(body, "id"),))
    if not e:
        raise Fail("找不到這筆紀錄", 404)
    site = site_for(user, e["site_id"])
    if not e["photo"]:
        raise Fail("這筆紀錄沒有照片（白名單車不存照片）")
    db.audit(user, h.client_ip(), "看進出照片", site["name"], f"{e['ts']} {e['plate']}", company_id=site["company_id"], site_id=site["id"])
    return {"jpeg_b64": base64.b64encode(e["photo"]).decode("ascii")}


def api_companies(h, user, body):
    need(user, "company.manage")
    rows = db.q("""SELECT c.id, c.name, c.is_root, c.active, c.created,
                   (SELECT COUNT(*) FROM sites s WHERE s.company_id=c.id) AS sites,
                   (SELECT COUNT(*) FROM users u WHERE u.company_id=c.id) AS users
                   FROM companies c ORDER BY c.is_root DESC, c.name""")
    return {"companies": rows}


def api_company_add(h, user, body):
    need(user, "company.manage")
    name = text(body, "name", "公司名稱", 40)
    username = text(body, "admin_username", "管理員帳號", 32)
    if not USERNAME_RE.match(username):
        raise Fail("帳號只能用英文、數字和 _ . -，3～32 字")
    display = text(body, "admin_name", "管理員名稱", 20)
    if db.one("SELECT id FROM companies WHERE name=?", (name,)):
        raise Fail("已經有同名的公司")
    if db.one("SELECT id FROM users WHERE username=?", (username,)):
        raise Fail("這個帳號已經有人用了")
    pw = temp_password()

    def create():
        cid, rid = db.create_company(name)
        db.run("INSERT INTO users(company_id, username, display_name, pw_hash, role_id, must_change_pw, created) VALUES(?,?,?,?,?,1,?)",
               (cid, username, display, hash_password(pw), rid, db.now()))
        return cid
    cid = db.tx(create)
    db.audit(user, h.client_ip(), "新增客戶公司", name, f"管理員 {username}", company_id=cid)
    return {"id": cid, "username": username, "temp_password": pw}


def api_company_active(h, user, body):
    need(user, "company.manage")
    c = company_for(user, int_id(body, "id"))
    if c["is_root"]:
        raise Fail("主控公司不能停用")
    active = 1 if body.get("active") is True else 0
    db.run("UPDATE companies SET active=? WHERE id=?", (active, c["id"]))
    if not active:
        db.run("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE company_id=?)", (c["id"],))
    db.audit(user, h.client_ip(), "啟用客戶公司" if active else "停用客戶公司", c["name"], company_id=c["id"])
    return {}


def role_rows(company_id):
    rows = db.q("""SELECT r.*, (SELECT COUNT(*) FROM users u WHERE u.role_id=r.id) AS users
                   FROM roles r WHERE company_id=? ORDER BY is_system DESC, name""", (company_id,))
    for r in rows:
        r["perms"] = json.loads(r["perms"])
    return rows


def api_roles(h, user, body):
    need(user, "user.manage")
    company = company_for(user, body.get("company_id"))
    return {"roles": role_rows(company["id"]), "company": company}


def check_grant(user, perms, company):
    """只能把自己有的權限給別人；客戶公司不能有主控專用權限。"""
    if not isinstance(perms, list) or any(p not in db.PERM_KEYS for p in perms):
        raise Fail("權限資料不對")
    perms = sorted(set(perms))
    if not company["is_root"] and set(perms) & db.ROOT_ONLY:
        raise Fail("客戶公司不能有「管理客戶公司」權限")
    extra = set(perms) - user["perms"]
    if extra:
        raise Fail("不能給別人你自己沒有的權限：" + "、".join(l for k, l, _ in db.PERMS if k in extra), 403)
    return perms


def api_role_save(h, user, body):
    need(user, "user.manage")
    company = company_for(user, body.get("company_id"))
    name = text(body, "name", "群組名稱", 20)
    perms = check_grant(user, body.get("perms"), company)
    if "site.view" not in perms:
        perms = sorted(perms + ["site.view"])  # 看場站是基本權限
    if body.get("id") is None:
        if db.one("SELECT id FROM roles WHERE company_id=? AND name=?", (company["id"], name)):
            raise Fail("已經有同名的群組")
        rid = db.run("INSERT INTO roles(company_id, name, perms) VALUES(?,?,?)", (company["id"], name, json.dumps(perms)))
        db.audit(user, h.client_ip(), "新增權限群組", name, "、".join(perms), company_id=company["id"])
        return {"id": rid}
    role = db.one("SELECT * FROM roles WHERE id=? AND company_id=?", (int_id(body, "id"), company["id"]))
    if not role:
        raise Fail("找不到這個群組", 404)
    if role["is_system"]:
        raise Fail("內建的「管理員」群組不能修改")
    if set(json.loads(role["perms"])) - user["perms"]:
        raise Fail("這個群組有你沒有的權限，你不能修改它", 403)
    if db.one("SELECT id FROM roles WHERE company_id=? AND name=? AND id<>?", (company["id"], name, role["id"])):
        raise Fail("已經有同名的群組")
    db.run("UPDATE roles SET name=?, perms=? WHERE id=?", (name, json.dumps(perms), role["id"]))
    db.audit(user, h.client_ip(), "修改權限群組", name, f"{role['perms']} → {json.dumps(perms)}", company_id=company["id"])
    return {"id": role["id"]}


def api_role_delete(h, user, body):
    need(user, "user.manage")
    company = company_for(user, body.get("company_id"))
    role = db.one("SELECT * FROM roles WHERE id=? AND company_id=?", (int_id(body, "id"), company["id"]))
    if not role:
        raise Fail("找不到這個群組", 404)
    if role["is_system"]:
        raise Fail("內建的「管理員」群組不能刪除")
    if set(json.loads(role["perms"])) - user["perms"]:
        raise Fail("這個群組有你沒有的權限，你不能刪除它", 403)
    if db.one("SELECT id FROM users WHERE role_id=?", (role["id"],)):
        raise Fail("還有帳號在用這個群組，先把那些帳號換到別的群組")
    db.run("DELETE FROM roles WHERE id=?", (role["id"],))
    db.audit(user, h.client_ip(), "刪除權限群組", role["name"], company_id=company["id"])
    return {}


def api_users(h, user, body):
    need(user, "user.manage")
    company = company_for(user, body.get("company_id"))
    rows = db.q("""SELECT u.id, u.username, u.display_name, u.role_id, r.name AS role_name, u.site_ids, u.active,
                   u.must_change_pw, u.created FROM users u JOIN roles r ON r.id=u.role_id WHERE u.company_id=? ORDER BY u.username""",
                (company["id"],))
    for r in rows:
        r["site_ids"] = json.loads(r["site_ids"]) if r["site_ids"] else None
    sites = db.q("SELECT id, name FROM sites WHERE company_id=? ORDER BY name", (company["id"],))
    if user["site_ids"] is not None:  # 只管部分場站的人，只看得到範圍內的場站與帳號
        sites = [x for x in sites if x["id"] in user["site_ids"]]
        rows = [r for r in rows if r["site_ids"] is not None and set(r["site_ids"]) <= set(user["site_ids"])]
    return {"users": rows, "roles": role_rows(company["id"]), "sites": sites, "company": company}


def check_target(user, target):
    """不能動權限比自己大的人（例如一般管理帳號的人去重設管理員密碼）。"""
    role = db.one("SELECT perms FROM roles WHERE id=?", (target["role_id"],))
    if set(json.loads(role["perms"])) - user["perms"]:
        raise Fail("這個帳號的權限比你大，你不能修改它", 403)
    if user["site_ids"] is not None:
        t = json.loads(target["site_ids"]) if target["site_ids"] else None
        if t is None or set(t) - set(user["site_ids"]):
            raise Fail("這個帳號管的場站超出你的範圍", 403)


def api_user_save(h, user, body):
    need(user, "user.manage")
    company = company_for(user, body.get("company_id"))
    display = text(body, "display_name", "名稱", 20)
    role = db.one("SELECT * FROM roles WHERE id=? AND company_id=?", (int_id(body, "role_id"), company["id"]))
    if not role:
        raise Fail("權限群組不對")
    check_grant(user, json.loads(role["perms"]), company)
    site_ids = body.get("site_ids")
    if site_ids is not None:
        valid = {s["id"] for s in db.q("SELECT id FROM sites WHERE company_id=?", (company["id"],))}
        if not isinstance(site_ids, list) or not site_ids or any(type(i) is not int or i not in valid for i in site_ids):
            raise Fail("場站範圍不對（至少選一個場站，或選全部場站）")
        site_ids = sorted(set(site_ids))
    if user["site_ids"] is not None and (site_ids is None or set(site_ids) - set(user["site_ids"])):
        raise Fail("你只能把自己管得到的場站給別人", 403)
    active = 0 if body.get("active") is False else 1
    sites_json = json.dumps(site_ids) if site_ids is not None else None
    if body.get("id") is None:
        username = text(body, "username", "帳號", 32)
        if not USERNAME_RE.match(username):
            raise Fail("帳號只能用英文、數字和 _ . -，3～32 字")
        if db.one("SELECT id FROM users WHERE username=?", (username,)):
            raise Fail("這個帳號已經有人用了")
        pw = temp_password()
        uid = db.run("""INSERT INTO users(company_id, username, display_name, pw_hash, role_id, site_ids, active, must_change_pw, created)
                        VALUES(?,?,?,?,?,?,?,1,?)""", (company["id"], username, display, hash_password(pw), role["id"], sites_json, active, db.now()))
        db.audit(user, h.client_ip(), "新增帳號", username, f"群組 {role['name']}", company_id=company["id"])
        return {"id": uid, "username": username, "temp_password": pw}
    target = db.one("SELECT * FROM users WHERE id=? AND company_id=?", (int_id(body, "id"), company["id"]))
    if not target:
        raise Fail("找不到這個帳號", 404)
    check_target(user, target)
    if target["id"] == user["id"] and (not active or role["id"] != target["role_id"] or sites_json != target["site_ids"]):
        raise Fail("不能停用或降低自己的權限，請請另一位管理員處理")
    db.run("UPDATE users SET display_name=?, role_id=?, site_ids=?, active=? WHERE id=?",
           (display, role["id"], sites_json, active, target["id"]))
    if not active:
        db.run("DELETE FROM sessions WHERE user_id=?", (target["id"],))
    old_role = db.one("SELECT name FROM roles WHERE id=?", (target["role_id"],))["name"]
    db.audit(user, h.client_ip(), "修改帳號", target["username"],
             f"群組 {old_role}→{role['name']}、{'啟用' if target['active'] else '停用'}→{'啟用' if active else '停用'}、"
             f"場站 {target['site_ids'] or '全部'}→{site_ids or '全部'}", company_id=company["id"])
    return {"id": target["id"]}


def api_user_reset_password(h, user, body):
    need(user, "user.manage")
    company = company_for(user, body.get("company_id"))
    target = db.one("SELECT * FROM users WHERE id=? AND company_id=?", (int_id(body, "id"), company["id"]))
    if not target:
        raise Fail("找不到這個帳號", 404)
    if target["id"] == user["id"]:
        raise Fail("自己的密碼請到「我的帳號」修改")
    check_target(user, target)
    pw = temp_password()
    db.run("UPDATE users SET pw_hash=?, must_change_pw=1 WHERE id=?", (hash_password(pw), target["id"]))
    db.run("DELETE FROM sessions WHERE user_id=?", (target["id"],))
    db.audit(user, h.client_ip(), "重設密碼", target["username"], company_id=company["id"])
    return {"username": target["username"], "temp_password": pw}


def api_audit(h, user, body):
    need(user, "audit.view")
    limit = body.get("limit") if type(body.get("limit")) is int and 0 < body.get("limit") <= 500 else 200
    if user["is_root"] and body.get("company_id") in (None, ""):
        rows = db.q("""SELECT a.*, c.name AS company_name FROM audit a LEFT JOIN companies c ON c.id=a.company_id
                       ORDER BY a.id DESC LIMIT ?""", (limit,))
    else:
        company = company_for(user, body.get("company_id"))
        rows = db.q("""SELECT a.*, c.name AS company_name FROM audit a LEFT JOIN companies c ON c.id=a.company_id
                       WHERE a.company_id=? ORDER BY a.id DESC LIMIT ?""", (company["id"], limit))
    if user["site_ids"] is not None:
        rows = [r for r in rows if r["site_id"] in user["site_ids"]]
    return {"rows": rows}


API = {
    "me": api_me, "change_password": api_change_password, "dashboard": api_dashboard, "sites": api_sites,
    "site_save": api_site_save, "gateway_add": api_gateway_add, "gateway_reset": api_gateway_reset, "cmd": api_cmd,
    "companies": api_companies, "company_add": api_company_add, "company_active": api_company_active,
    "roles": api_roles, "role_save": api_role_save, "role_delete": api_role_delete,
    "users": api_users, "user_save": api_user_save, "user_reset_password": api_user_reset_password, "audit": api_audit,
    "events": api_events, "event_photo": api_event_photo,
}
ALLOWED_BEFORE_PW_CHANGE = {"me", "change_password"}


# ---------- 登入 ----------

login_fail = {}         # "ip:來源" 或 "user:帳號" → {"count", "until"}；IP 和帳號都鎖，換 IP 也猜不完
LOCK_AFTER, LOCK_SECONDS = 5, 300


def locked(key, t):
    f = login_fail.get(key)
    return bool(f and f["until"] > t)


def add_fail(key, t):
    if len(login_fail) > 10000:  # 不讓這張表無限長大
        login_fail.clear()
    f = login_fail.setdefault(key, {"count": 0, "until": 0})
    f["count"] += 1
    if f["count"] >= LOCK_AFTER:
        f.update(count=0, until=t + LOCK_SECONDS)


def login(h, body):
    t, ip = time.time(), h.client_ip()
    username, password = body.get("username"), body.get("password")
    ukey = "user:" + username.lower()[:64] if isinstance(username, str) else "user:-"
    if locked("ip:" + ip, t) or locked(ukey, t):
        db.audit(None, ip, "登入被擋", result="嘗試太多次，鎖定中")
        raise Fail("密碼錯太多次，請 5 分鐘後再試", 429)
    row = db.one("SELECT id, username, company_id, pw_hash FROM users WHERE username=?", (username,)) if isinstance(username, str) else None
    ok = isinstance(password, str) and len(password) <= 200 and check_password(password, row["pw_hash"] if row else hash_password("x"))
    user = load_user(row["id"]) if row and ok else None
    if not user:
        time.sleep(1)
        add_fail("ip:" + ip, t)
        add_fail(ukey, t)
        # 只記存在的帳號名稱；不存在的不記原文（有人會把密碼打在帳號欄）
        db.audit({"username": row["username"], "company_id": row["company_id"]} if row else {"username": "（不存在的帳號）"}, ip, "登入失敗",
                 result="帳號或公司已停用" if row and ok else "帳號或密碼不對")
        raise Fail("帳號或密碼不對", 401)
    login_fail.pop("ip:" + ip, None)
    login_fail.pop(ukey, None)
    sid = secrets.token_urlsafe(32)
    db.run("DELETE FROM sessions WHERE expires < ?", (t,))
    db.run("INSERT INTO sessions(token_hash, user_id, expires) VALUES(?,?,?)", (token_hash(sid), user["id"], t + SESSION_HOURS * 3600))
    db.audit(user, ip, "登入")
    secure = "; Secure" if h.is_https() else ""
    return {"user": public_user(user)}, {"Set-Cookie": f"sid={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_HOURS * 3600}{secure}"}


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    timeout = 60
    # HTTP/1.1（連線可重複使用）：實測 HTTP/1.0 每次回應後關連線，在這台 Windows 上大一點的回應（照片）偶爾會被截斷
    protocol_version = "HTTP/1.1"
    session_hash = ""

    body_read = False

    def send_body(self, code, ctype, body, extra=None):
        if not self.body_read and (self.headers.get("Content-Length") not in (None, "", "0") or self.headers.get("Transfer-Encoding")):
            self.close_connection = True  # 沒讀內容就回應（例如被擋）：這條連線不能再用，不然剩下的內容會被當成下一個請求
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError as e:
            print(time.strftime("%H:%M:%S"), f"回應送到一半失敗（{len(body)} bytes）：{e!r}", file=sys.stderr, flush=True)
            raise

    def json(self, code, obj, extra=None):
        self.send_body(code, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra)

    def read_json(self, limit):
        if self.headers.get("Transfer-Encoding"):  # 只接受 Content-Length，避免連線重複使用時內容被誤判（request smuggling）
            raise ValueError
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError
        if not 0 <= length <= limit or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise ValueError
        raw = self.rfile.read(length)
        self.body_read = True
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            raise ValueError
        try:
            json.dumps(body, ensure_ascii=False).encode("utf-8")  # 擋 \ud800 這種轉不成 UTF-8 的字
        except UnicodeEncodeError:
            raise ValueError
        return body

    def is_https(self):
        return self.headers.get("X-Forwarded-Proto") == "https" or "https" in self.headers.get("Cf-Visitor", "")

    def base_url(self):
        return ("https://" if self.is_https() else "http://") + self.headers.get("Host", f"127.0.0.1:{PORT}")

    def client_ip(self):
        # 經 cloudflared 進來時對方位址都是 127.0.0.1，真實 IP 在 Cf-Connecting-Ip（只信任本機通道帶的）
        if self.client_address[0] == "127.0.0.1" and self.headers.get("Cf-Connecting-Ip"):
            return self.headers["Cf-Connecting-Ip"][:45]
        return self.client_address[0]

    def host_ok(self):
        """擋 DNS rebinding：只接受本機、Cloudflare 通道網址、或環境變數 PSC_HOSTS 列的網域。"""
        host = self.headers.get("Host", "").split(":")[0].lower()
        extra = [x.strip().lower() for x in os.environ.get("PSC_HOSTS", "").split(",") if x.strip()]
        return host in ("127.0.0.1", "localhost") or host.endswith(".trycloudflare.com") or host in extra

    def same_origin(self):
        origin = self.headers.get("Origin")
        return origin is None or urlparse(origin).netloc == self.headers.get("Host", "")

    def current_user(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        sid = cookie["sid"].value if "sid" in cookie else ""
        if not sid:
            return None
        row = db.one("SELECT user_id, expires FROM sessions WHERE token_hash=?", (token_hash(sid),))
        if not row or row["expires"] < time.time():
            return None
        self.session_hash = token_hash(sid)
        return load_user(row["user_id"])

    def do_GET(self):
        self.body_read = False  # 同一條連線會處理好幾個請求，每次重算
        if not self.host_ok():
            return self.send_body(403, "text/plain", b"forbidden")
        path = urlparse(self.path).path
        if path in STATIC:
            name, ctype = STATIC[path]
            with open(os.path.join(WEB, name), "rb") as f:
                return self.send_body(200, ctype, f.read())
        self.send_body(404, "text/plain", b"not found")

    def do_POST(self):
        self.body_read = False  # 同一條連線會處理好幾個請求，每次重算
        if not self.host_ok():
            return self.json(403, {"ok": False, "error": "forbidden"})
        path = urlparse(self.path).path
        if path == "/gw/poll":
            return self.gateway_poll()
        if path == "/gw/events":
            return self.gateway_events()
        if not self.same_origin() or not path.startswith("/api/"):
            return self.json(403, {"ok": False, "error": "forbidden"})
        try:
            body = self.read_json(MAX_BODY)
        except ValueError:
            return self.json(400, {"ok": False, "error": "資料格式錯誤"})
        name = path[len("/api/"):]
        try:
            if name == "login":
                data, extra = login(self, body)
                return self.json(200, {"ok": True, **data}, extra)
            user = self.current_user()
            if not user:
                return self.json(401, {"ok": False, "error": "請先登入", "login": True})
            if name == "logout":
                db.run("DELETE FROM sessions WHERE token_hash=?", (self.session_hash,))
                db.audit(user, self.client_ip(), "登出")
                return self.json(200, {"ok": True}, {"Set-Cookie": "sid=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"})
            fn = API.get(name)
            if not fn:
                return self.json(404, {"ok": False, "error": "not found"})
            if user["must_change_pw"] and name not in ALLOWED_BEFORE_PW_CHANGE:
                return self.json(403, {"ok": False, "error": "請先修改密碼", "must_change_pw": True})
            try:
                return self.json(200, {"ok": True, **(fn(self, user, body) or {})})
            except Fail as e:
                if e.code == 403:  # 越權嘗試要留紀錄
                    db.audit(user, self.client_ip(), "權限不足被擋", name, json.dumps(body, ensure_ascii=False)[:300], str(e))
                raise
        except Fail as e:
            return self.json(e.code, {"ok": False, "error": str(e)})
        except Exception as e:  # 沒想到的錯誤也回中文，不讓連線直接斷掉
            print(time.strftime("%H:%M:%S"), f"/api/{name} 發生錯誤：{type(e).__name__}: {e}", file=sys.stderr)
            return self.json(500, {"ok": False, "error": "平台處理時發生錯誤，請稍後再試"})

    def gateway_auth(self, limit):
        """先用通行碼找到小主機再讀內容：沒有正確通行碼的人不能丟大封包耗資源（資料庫只存雜湊，用雜湊查）。
        成功回 (row, body)，失敗已回應並回 (None, None)。"""
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        row = db.one("""SELECT g.id, g.site_id, g.cameras, c.active FROM gateways g JOIN sites s ON s.id=g.site_id
                        JOIN companies c ON c.id=s.company_id WHERE g.token_hash=?""", (token_hash(token),)) if len(token) >= 24 else None
        if not row or not row["active"]:
            if row is None:
                db.audit(None, self.client_ip(), "小主機連線被拒", result="通行碼不對")
            time.sleep(1)
            self.json(401, {"error": "unauthorized"})
            return None, None
        try:
            body = self.read_json(limit)
        except ValueError:
            self.json(400, {"error": "bad request"})
            return None, None
        if body.get("gateway_id") != row["id"]:
            self.json(401, {"error": "unauthorized"})
            return None, None
        return row, body

    def gateway_events(self):
        """小主機送來攝影機認到的車牌；照片只有非白名單車才有。"""
        row, body = self.gateway_auth(MAX_EVENTS_BODY)
        if not row:
            return
        names = {c["id"]: c["name"] for c in json.loads(row["cameras"])}
        events = body.get("events")
        saved = 0
        for e in (events if isinstance(events, list) else [])[:50]:
            if not isinstance(e, dict) or e.get("camera") not in names:
                continue
            plate, ts = e.get("plate"), e.get("time")
            if not isinstance(plate, str) or not EVENT_PLATE_RE.match(plate) or not isinstance(ts, str):
                continue
            try:
                ts = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            photo = None
            if isinstance(e.get("photo_b64"), str) and e.get("is_whitelist") is not True:
                try:
                    photo = base64.b64decode(e["photo_b64"], validate=True)
                except (binascii.Error, ValueError):
                    photo = None
                if photo and (not photo.startswith(b"\xff\xd8") or len(photo) > MAX_EVENT_PHOTO):
                    photo = None
            conf = e.get("confidence")
            db.run("""INSERT OR IGNORE INTO events(ts, received, site_id, gateway_id, camera_id, camera_name, plate, direction, is_whitelist, confidence, photo)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                   (ts, db.now(), row["site_id"], row["id"], e["camera"], names[e["camera"]], plate,
                    "out" if e.get("direction") == "out" else "in", 1 if e.get("is_whitelist") is True else 0,
                    conf if type(conf) is int and 0 <= conf <= 1000 else None, photo))
            saved += 1
        get_live(row["id"]).last_seen = time.time()
        self.json(200, {"saved": saved})

    def gateway_poll(self):
        row, body = self.gateway_auth(MAX_GW_BODY)
        if not row:
            return
        gid = row["id"]
        gw = get_live(gid)
        gw.last_seen, gw.version = time.time(), str(body.get("version", ""))[:20]
        status = body.get("camera_status")
        # 0.4 以前的小主機不回報推送狀態：記成 None，畫面才不會誤以為攝影機沒在推
        gw.camera_status = {str(k)[:40]: v for k, v in status.items() if type(v) is int} if isinstance(status, dict) else None
        cams = body.get("cameras")
        if isinstance(cams, list):
            cams = [{"id": c["id"][:40], "name": str(c.get("name") or c["id"])[:40]}
                    for c in cams if isinstance(c, dict) and isinstance(c.get("id"), str)][:50]
            if json.dumps(cams, ensure_ascii=False) != row["cameras"]:
                db.run("UPDATE gateways SET cameras=? WHERE id=?", (json.dumps(cams, ensure_ascii=False), gid))
        results = body.get("results")
        for res in results if isinstance(results, list) else []:
            slot = waiting.get(res["id"]) if isinstance(res, dict) and isinstance(res.get("id"), str) else None
            if slot and slot["gateway"] == gid and not slot["event"].is_set():  # 只收派給自己的指令的結果
                slot["result"] = {"ok": bool(res.get("ok")), "data": res.get("data") or {}, "error": str(res.get("error") or "")}
                slot["event"].set()
        with gw.cond:
            if not gw.queue:
                gw.cond.wait(POLL_WAIT)
            commands, gw.queue = gw.queue, []
        gw.last_seen = time.time()
        self.json(200, {"commands": commands})

    def log_message(self, *args):
        pass

    def log_error(self, fmt, *args):  # 連線逾時、送資料失敗等，印出來方便查
        print(time.strftime("%H:%M:%S"), "連線錯誤：", fmt % args, file=sys.stderr, flush=True)


# ---------- 初始化 ----------

def init():
    if db.one("SELECT id FROM companies WHERE is_root=1"):
        raise SystemExit(f"已經初始化過了（{db.DB_PATH}）。要重來請先把那個檔案移走。")
    old = None
    if os.path.exists(OLD_CONFIG):
        with open(OLD_CONFIG, encoding="utf-8") as f:
            old = json.load(f)
    password = os.environ.get("PSC_ADMIN_PASSWORD")
    if password is None and not (old and old.get("admin_password")):
        password = getpass.getpass(f"設定管理員 admin 的密碼（至少 {MIN_PASSWORD} 字元）：")
    if password is not None and len(password) < MIN_PASSWORD:
        raise SystemExit("密碼太短")
    pw_hash = hash_password(password) if password is not None else old["admin_password"]

    def create():
        cid, rid = db.create_company("PS（主控）", is_root=True)
        db.run("INSERT INTO users(company_id, username, display_name, pw_hash, role_id, created) VALUES(?,?,?,?,?,?)",
               (cid, "admin", "系統管理員", pw_hash, rid, db.now()))
        if old and old.get("gateways", {}).get("company"):
            sid = db.run("INSERT INTO sites(company_id, name, kind, address, created) VALUES(?,?,?,?,?)",
                         (cid, "公司測試場站", "other", "", db.now()))
            db.run("INSERT INTO gateways(id, site_id, name, token_hash, created) VALUES(?,?,?,?,?)",
                   ("company", sid, "公司電腦", token_hash(old["gateways"]["company"]), db.now()))
    db.tx(create)
    print(f"已建立 {db.DB_PATH}")
    print("主控公司：PS（主控），管理員帳號：admin" + ("（密碼沿用舊平台的密碼）" if password is None else ""))
    if old and old.get("gateways", {}).get("company"):
        print("已沿用舊的 company 小主機通行碼（場站：公司測試場站），公司那台小主機不用重設。")


def purge_loop():
    """每天清一次超過保留天數的進出紀錄（含照片）。"""
    while True:
        cutoff = (datetime.now() - timedelta(days=EVENT_DAYS)).strftime("%Y-%m-%d 00:00:00")
        n = db.one("SELECT COUNT(*) AS n FROM events WHERE ts < ?", (cutoff,))["n"]
        if n:
            db.run("DELETE FROM events WHERE ts < ?", (cutoff,))
            db.audit(None, "-", "清除過期進出紀錄", f"{EVENT_DAYS} 天前", f"{n} 筆", company_id=None)
        time.sleep(24 * 3600)


def main():
    if sys.argv[1:] == ["init"]:
        return init()
    sys.stdout.reconfigure(errors="replace")
    if not db.one("SELECT id FROM companies WHERE is_root=1"):
        raise SystemExit("還沒初始化：先執行 python psc/server.py init")
    threading.Thread(target=purge_loop, daemon=True).start()
    print(f"PSC 雲平台啟動：http://127.0.0.1:{PORT}　進出紀錄保留 {EVENT_DAYS} 天。按 Ctrl+C 結束。")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
