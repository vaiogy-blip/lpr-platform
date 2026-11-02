# -*- coding: utf-8 -*-
"""
PS雲平台（雛形）：管理網頁＋現場小主機的連線點。先在家裡筆電跑，之後搬上雲端。

  - 管理網頁要登入（密碼用 PBKDF2 雜湊存在 server_config.json，不進 git）。
  - 現場小主機用各自的 token 連進來（/gw/poll 長輪詢），平台把指令排隊、等小主機回結果。
  - 每個指令都記在 data/audit_log.txt（誰、何時、哪台小主機／攝影機、什麼指令、結果）。
  - 只綁 127.0.0.1；要給外面連，用 cloudflared 通道（HTTPS）轉進來。

第一次使用：python server.py init     （設定管理密碼、產生小主機 token）
啟動：      python server.py
"""

import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "server_config.json")
DATA = os.path.join(HERE, "data")
PORT = int(os.environ.get("PORT", 8600))

POLL_WAIT = 25          # 小主機長輪詢最多等幾秒
CMD_WAIT = 45           # 管理網頁等指令結果最多幾秒
ONLINE_WITHIN = 60      # 幾秒內有連線就算上線
SESSION_HOURS = 12
MAX_GW_BODY = 4_000_000
MAX_ADMIN_BODY = 16_000
MAX_QUEUE = 20
COMMANDS = {"wl_list", "wl_add", "wl_delete", "restart_info", "reboot", "led_get", "led_set", "photo_query", "photo_get"}
NO_AUDIT = {"wl_list", "restart_info", "led_get"}  # 其他（含看照片＝個資）成功也要記


# ---------- 設定與密碼 ----------

def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000).hex()
    return f"{salt}${digest}"


def check_password(password, stored):
    salt, _ = stored.split("$", 1)
    return hmac.compare_digest(hash_password(password, salt), stored)


def init_config():
    password = os.environ.get("LPR_ADMIN_PASSWORD") or getpass.getpass("設定管理密碼（至少 10 字元）：")
    if len(password) < 10:
        raise SystemExit("密碼太短")
    cfg = {"admin_password": hash_password(password), "gateways": {"company": secrets.token_urlsafe(32)}}
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"已寫入 {CONFIG}")
    print("小主機 company 的 token（貼到公司電腦的 gateway_config.json，不要外流）：")
    print(cfg["gateways"]["company"])


def load_config():
    if not os.path.exists(CONFIG):
        raise SystemExit("還沒設定：先執行 python server.py init")
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def audit(who, gateway, camera, kind, args, result):
    os.makedirs(DATA, exist_ok=True)
    clean = lambda s: " ".join(str(s).split())
    shown = {k: v for k, v in (args or {}).items() if k != "led"}  # 字幕機整包太長，只記有改
    with open(os.path.join(DATA, "audit_log.txt"), "a", encoding="utf-8") as f:
        f.write("\t".join(clean(x) for x in (f"{datetime.now():%Y-%m-%d %H:%M:%S}", who, gateway, camera, kind,
                                              json.dumps(shown, ensure_ascii=False), result)) + "\n")


# ---------- 小主機與指令排隊 ----------

class Gateway:
    def __init__(self, gid):
        self.id, self.last_seen, self.cameras, self.version = gid, 0, [], ""
        self.queue, self.cond = [], threading.Condition()


gateways = {}
waiting = {}            # 指令 id → {"event", "result"}
state_lock = threading.Lock()


def get_gateway(gid):
    with state_lock:
        return gateways.setdefault(gid, Gateway(gid))


def send_command(gid, camera, kind, args):
    gw = get_gateway(gid)
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


# ---------- 登入 ----------

sessions = {}           # sid → 到期時間
login_fail = {}         # 來源 IP → {"count", "until"}；依 IP 鎖，別人亂打不會把 RYW 也鎖住


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    timeout = 60
    cfg = None

    def send_body(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, code, obj, extra=None):
        self.send_body(code, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode("utf-8"), extra)

    def read_json(self, limit):
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 <= length <= limit or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise ValueError
        body = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(body, dict):
            raise ValueError
        return body

    def is_https(self):
        return self.headers.get("X-Forwarded-Proto") == "https" or "https" in self.headers.get("Cf-Visitor", "")

    def session(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        sid = cookie["sid"].value if "sid" in cookie else ""
        exp = sessions.get(sid)
        if exp and exp > time.time():
            return sid
        sessions.pop(sid, None)
        return None

    def same_origin(self):
        origin = self.headers.get("Origin")
        return origin is None or urlparse(origin).netloc == self.headers.get("Host", "")

    def client_ip(self):
        # 經 cloudflared 進來時對方位址都是 127.0.0.1，真實 IP 在 Cf-Connecting-Ip（只信任本機通道帶的）
        if self.client_address[0] == "127.0.0.1" and self.headers.get("Cf-Connecting-Ip"):
            return self.headers["Cf-Connecting-Ip"][:45]
        return self.client_address[0]

    def host_ok(self):
        """擋 DNS rebinding：只接受本機、Cloudflare 通道網址、或設定檔 allowed_hosts 列的網域。"""
        host = self.headers.get("Host", "").split(":")[0].lower()
        return (host in ("127.0.0.1", "localhost") or host.endswith(".trycloudflare.com")
                or host in self.cfg.get("allowed_hosts", []))

    # --- GET ---
    def do_GET(self):
        if not self.host_ok():
            return self.send_body(403, "text/plain", b"forbidden")
        path = urlparse(self.path).path
        if path == "/":
            return self.send_body(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        if path == "/api/status":
            if not self.session():
                return self.json(401, {"ok": False, "error": "請先登入"})
            now = time.time()
            with state_lock:
                gws = [{"id": g.id, "online": now - g.last_seen < ONLINE_WITHIN, "version": g.version,
                        "last_seen": datetime.fromtimestamp(g.last_seen).strftime("%m-%d %H:%M:%S") if g.last_seen else "從未",
                        "cameras": g.cameras} for g in gateways.values() if g.id in self.cfg["gateways"]]
            return self.json(200, {"ok": True, "gateways": gws})
        self.send_body(404, "text/plain", b"not found")

    # --- POST ---
    def do_POST(self):
        if not self.host_ok():
            return self.json(403, {"ok": False, "error": "forbidden"})
        path = urlparse(self.path).path
        if path == "/gw/poll":
            return self.gateway_poll()
        if not self.same_origin():
            return self.json(403, {"ok": False, "error": "forbidden"})
        try:
            body = self.read_json(MAX_ADMIN_BODY)
        except ValueError:
            return self.json(400, {"ok": False, "error": "資料格式錯誤"})
        if path == "/api/login":
            return self.login(body)
        sid = self.session()
        if not sid:
            return self.json(401, {"ok": False, "error": "請先登入"})
        if path == "/api/logout":
            sessions.pop(sid, None)
            return self.json(200, {"ok": True}, {"Set-Cookie": "sid=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"})
        if path == "/api/cmd":
            return self.admin_command(body)
        self.json(404, {"ok": False, "error": "not found"})

    def login(self, body):
        now, ip = time.time(), self.client_ip()
        fail = login_fail.setdefault(ip, {"count": 0, "until": 0})
        if fail["until"] > now:
            return self.json(429, {"ok": False, "error": "密碼錯太多次，請 5 分鐘後再試"})
        if not isinstance(body.get("password"), str) or not check_password(body["password"], self.cfg["admin_password"]):
            time.sleep(1)
            fail["count"] += 1
            if fail["count"] >= 5:
                fail.update(count=0, until=now + 300)
            audit(f"登入失敗@{ip}", "-", "-", "login", {}, "密碼不對")
            return self.json(401, {"ok": False, "error": "密碼不對"})
        login_fail.pop(ip, None)
        sid = secrets.token_urlsafe(32)
        sessions[sid] = now + SESSION_HOURS * 3600
        secure = "; Secure" if self.is_https() else ""
        audit(f"RYW@{ip}", "-", "-", "login", {}, "成功")
        return self.json(200, {"ok": True},
                         {"Set-Cookie": f"sid={sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_HOURS * 3600}{secure}"})

    def admin_command(self, body):
        gid, camera, kind, args = body.get("gateway"), body.get("camera"), body.get("type"), body.get("args") or {}
        if gid not in self.cfg["gateways"] or kind not in COMMANDS or not isinstance(camera, str) or not isinstance(args, dict):
            return self.json(400, {"ok": False, "error": "指令不對"})
        gw = get_gateway(gid)
        if time.time() - gw.last_seen > ONLINE_WITHIN:
            return self.json(200, {"ok": False, "error": "現場小主機目前離線（最近一分鐘沒有連線），請確認那台電腦和程式有開"})
        if not any(c.get("id") == camera for c in gw.cameras):
            return self.json(400, {"ok": False, "error": "這台小主機沒有這支攝影機"})
        result = send_command(gid, camera, kind, args)
        if kind not in NO_AUDIT or not result.get("ok"):
            audit(f"RYW@{self.client_ip()}", gid, camera, kind, args, "成功" if result.get("ok") else result.get("error"))
        self.json(200, result)

    def gateway_poll(self):
        try:
            body = self.read_json(MAX_GW_BODY)
        except ValueError:
            return self.json(400, {"error": "bad request"})
        gid = body.get("gateway_id")
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        expected = self.cfg["gateways"].get(gid) if isinstance(gid, str) else None
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            time.sleep(1)
            return self.json(401, {"error": "unauthorized"})
        gw = get_gateway(gid)
        gw.last_seen, gw.version = time.time(), str(body.get("version", ""))[:20]
        cams = body.get("cameras")
        if isinstance(cams, list):
            gw.cameras = [{"id": c["id"][:40], "name": str(c.get("name") or c["id"])[:40]}
                          for c in cams if isinstance(c, dict) and isinstance(c.get("id"), str)][:50]
        for res in body.get("results") or []:
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


PAGE = r"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PS雲平台（雛形）</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#1d2129;--muted:#6b7280;--line:#e5e7eb;--accent:#2563eb;--ok:#15803d;--warn:#b45309;--bad:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--card:#1b1e25;--text:#e8eaed;--muted:#9aa0a6;--line:#2c313a;--accent:#60a5fa;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,"Microsoft JhengHei",sans-serif}
main{max-width:860px;margin:0 auto;padding:20px 16px}h1{font-size:22px;margin:0}h2{font-size:18px;margin:22px 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:14px}
label{display:block;font-size:13px;color:var(--muted);margin-bottom:4px}
input,select{width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--text);font-size:16px}
button{padding:9px 16px;border:0;border-radius:6px;background:var(--accent);color:#fff;font-size:15px;cursor:pointer;white-space:nowrap}
button.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}button.del{background:transparent;color:var(--bad);border:1px solid var(--line);padding:5px 10px;font-size:14px}
button:disabled{opacity:.5;cursor:wait}
.row{display:grid;grid-template-columns:1.3fr 1fr 1fr auto;gap:10px;align-items:end}
.bar{display:flex;gap:10px;align-items:end;flex-wrap:wrap}.bar>div{flex:1;min-width:150px}
@media (max-width:600px){.row{grid-template-columns:1fr 1fr}.row button{grid-column:span 2}}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 6px;border-bottom:1px solid var(--line)}th{font-size:13px;color:var(--muted);font-weight:500}
.plate{font-weight:600;letter-spacing:1px}.有效,.on{color:var(--ok)}.已過期,.off{color:var(--bad)}.還沒開始{color:var(--warn)}
.msg{min-height:22px;font-size:14px;margin-top:8px}.err{color:var(--bad)}.okmsg{color:var(--ok)}.muted{color:var(--muted);font-size:14px}
.photos{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px;max-height:260px;overflow:auto}
.photos button{background:var(--bg);color:var(--text);border:1px solid var(--line);font-size:13px;padding:6px;text-align:left;white-space:normal}
#photo{max-width:100%;margin-top:10px;border-radius:6px;display:none}
.hidden{display:none}
</style></head><body><main>
<div class="bar" style="align-items:center;margin-bottom:14px"><div><h1>PS雲平台</h1><div class="muted">雛形：透過現場小主機遠端管理攝影機</div></div>
<button class="ghost hidden" id="logout">登出</button></div>

<div class="card hidden" id="loginbox"><label for="pw">管理密碼</label>
<div class="bar"><div><input id="pw" type="password" autocomplete="current-password"></div><button id="loginbtn">登入</button></div><div class="msg" id="loginmsg"></div></div>

<div id="app" class="hidden">
<div class="card"><div class="bar">
<div><label for="cam">攝影機</label><select id="cam"></select></div>
<button class="ghost" id="refresh">重新整理</button></div><div class="msg muted" id="gwinfo"></div></div>

<h2>白名單</h2>
<div class="card"><div class="row">
<div><label for="plate">車牌（不用加 -）</label><input id="plate" placeholder="例如 ABC1234" autocomplete="off"></div>
<div><label for="begin">開始日期</label><input id="begin" type="date"></div>
<div><label for="end">結束日期</label><input id="end" type="date"></div>
<button id="add">新增／更新</button></div><div class="msg" id="wlmsg"></div>
<table><thead><tr><th>車牌</th><th>有效期間</th><th>狀態</th><th></th></tr></thead><tbody id="list"></tbody></table></div>

<h2>SD 卡照片</h2>
<div class="card"><div class="bar">
<div><label for="pplate">車牌（空白＝全部）</label><input id="pplate" autocomplete="off"></div>
<div><label for="pstart">從</label><input id="pstart" type="datetime-local"></div>
<div><label for="pend">到</label><input id="pend" type="datetime-local"></div>
<button id="pq">查詢</button></div><div class="msg" id="pmsg"></div>
<div class="photos" id="plist"></div><img id="photo" alt="照片"></div>

<h2>攝影機維護</h2>
<div class="card"><div class="bar" style="align-items:center"><button class="ghost" id="reboot" style="color:var(--bad)">攝影機重開機</button>
<span class="muted" id="rinfo"></span></div><div class="msg" id="rmsg"></div></div>
</div>
</main><script>
const $=id=>document.getElementById(id);
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function say(id,t,ok){$(id).textContent=t;$(id).className='msg '+(ok?'okmsg':'err')}
async function api(path,body){const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
 const d=await r.json().catch(()=>({ok:false,error:'平台沒有回應'}));if(r.status==401&&path!='/api/login'){show(false)}return d}
let gw=null;
function sel(){const [g,c]=($('cam').value||'|').split('|');return {gateway:g,camera:c}}
async function cmd(type,args){return api('/api/cmd',{...sel(),type,args:args||{}})}
function show(logged){$('loginbox').classList.toggle('hidden',logged);$('app').classList.toggle('hidden',!logged);$('logout').classList.toggle('hidden',!logged)}
async function status(){const d=await api('/api/status');if(!d.ok){show(false);return false}show(true);
 const cur=$('cam').value;$('cam').innerHTML=d.gateways.flatMap(g=>g.cameras.map(c=>'<option value="'+esc(g.id+'|'+c.id)+'">'+esc(c.name)+'（小主機 '+esc(g.id)+'）</option>')).join('')||'<option value="">（還沒有小主機連上來）</option>';
 if(cur)$('cam').value=cur;
 $('gwinfo').innerHTML=d.gateways.map(g=>'小主機 '+esc(g.id)+'：<span class="'+(g.online?'on':'off')+'">'+(g.online?'線上':'離線')+'</span>（最後連線 '+esc(g.last_seen)+'）').join('　')||'還沒有小主機連上來';
 return true}
async function loadWl(){const tb=$('list');tb.innerHTML='<tr><td colspan="4" class="muted">讀取中…</td></tr>';const d=await cmd('wl_list');
 if(!d.ok){tb.innerHTML='<tr><td colspan="4" class="err">'+esc(d.error)+'</td></tr>';return}
 const items=d.data.items;tb.innerHTML=items.length?items.map(i=>'<tr><td class="plate">'+esc(i.plate)+'</td><td>'+esc(i.begin)+' ～ '+esc(i.end)+'</td><td class="'+esc(i.state)+'">'+esc(i.state)+'</td><td><button class="del" data-plate="'+esc(i.plate)+'">刪除</button></td></tr>').join(''):'<tr><td colspan="4" class="muted">白名單是空的</td></tr>'}
async function loadRestart(){const d=await cmd('restart_info');$('rinfo').textContent=d.ok?(d.data.restart.auto?'自動重開：每 '+d.data.restart.every_days+' 天 '+d.data.restart.time:'自動重開：沒有開'):''}
async function loadAll(){if(!await status())return;if(!sel().camera)return;loadWl();loadRestart()}
$('loginbtn').onclick=async()=>{const d=await api('/api/login',{password:$('pw').value});$('pw').value='';if(d.ok){say('loginmsg','',true);loadAll()}else say('loginmsg',d.error,false)};
$('pw').addEventListener('keydown',e=>{if(e.key==='Enter')$('loginbtn').click()});
$('logout').onclick=async()=>{await api('/api/logout',{});show(false)};
$('refresh').onclick=loadAll;$('cam').onchange=()=>{loadWl();loadRestart()};
const pad=n=>String(n).padStart(2,'0'),iso=d=>d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate());
const t=new Date();$('begin').value=iso(t);const ny=new Date(t);ny.setFullYear(ny.getFullYear()+1);$('end').value=iso(ny);
$('pstart').value=iso(t)+'T00:00';$('pend').value=iso(t)+'T23:59';
$('add').onclick=async()=>{$('add').disabled=true;const p=$('plate').value;const d=await cmd('wl_add',{plate:p,begin:$('begin').value,end:$('end').value});
 $('add').disabled=false;if(d.ok){say('wlmsg','已寫入攝影機：'+p.toUpperCase().replace(/[- ]/g,''),true);$('plate').value=''}else say('wlmsg',d.error,false);loadWl()};
$('plate').addEventListener('keydown',e=>{if(e.key==='Enter')$('add').click()});
$('list').addEventListener('click',async e=>{const p=e.target.dataset.plate;if(!p)return;if(!confirm('確定要從攝影機刪除 '+p+' 嗎？'))return;
 e.target.disabled=true;const d=await cmd('wl_delete',{plate:p});say('wlmsg',d.ok?'已刪除 '+p:d.error,d.ok);loadWl()});
$('pq').onclick=async()=>{$('pq').disabled=true;say('pmsg','查詢中…',true);$('plist').innerHTML='';$('photo').style.display='none';
 const d=await cmd('photo_query',{plate:$('pplate').value.trim(),start:$('pstart').value,end:$('pend').value});$('pq').disabled=false;
 if(!d.ok)return say('pmsg',d.error,false);const paths=d.data.days.flatMap(x=>x.paths);say('pmsg','找到 '+paths.length+' 張，點一下看照片',true);
 $('plist').innerHTML=paths.map(p=>'<button data-path="'+esc(p)+'">'+esc(p.split('/').pop())+'</button>').join('')};
$('plist').addEventListener('click',async e=>{const p=e.target.dataset.path;if(!p)return;say('pmsg','從攝影機抓照片中…',true);
 const d=await cmd('photo_get',{path:p});if(!d.ok)return say('pmsg',d.error,false);$('photo').src='data:image/jpeg;base64,'+d.data.jpeg_b64;$('photo').style.display='block';say('pmsg',p.split('/').pop(),true)});
$('reboot').onclick=async()=>{if(!confirm('確定要讓攝影機重開機嗎？\n大約 1～2 分鐘不會辨識、不會自動開閘。'))return;$('reboot').disabled=true;
 const d=await cmd('reboot');say('rmsg',d.ok?'已送出重開機，約 1～2 分鐘後恢復':d.error,d.ok);setTimeout(()=>$('reboot').disabled=false,60000)};
loadAll();setInterval(status,20000);
</script></body></html>
"""


def main():
    if sys.argv[1:] == ["init"]:
        return init_config()
    sys.stdout.reconfigure(errors="replace")
    Handler.cfg = load_config()
    os.makedirs(DATA, exist_ok=True)
    print(f"PS雲平台（雛形）啟動：http://127.0.0.1:{PORT}　小主機 {len(Handler.cfg['gateways'])} 台。按 Ctrl+C 結束。")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
