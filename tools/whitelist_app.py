# -*- coding: utf-8 -*-
"""
單機白名單管理（試用版）：在瀏覽器裡查看、新增、刪除華夏攝影機的白名單。

只在這台電腦上開（127.0.0.1），區域網路其他人連不進來。
攝影機要跟這台電腦在同一個區域網路（直連攝影機 TCP 8117，見 hx_whitelist.py）。
⚠️ 寫入白名單的車到場會自動開閘，目前只用在沒接柵欄的開發用攝影機。

用法：在這個資料夾開命令提示字元，執行
    python whitelist_app.py
然後用瀏覽器開 http://127.0.0.1:8090
"""

import ipaddress
import json
import os
import re
import sys
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from hx_whitelist import CMD_ADD, CMD_DELETE, CMD_LIST, log_operation, record, send

PORT = int(os.environ.get("PORT", 8090))
DEFAULT_CAMERA = "192.168.1.100"
PLATE_RE = re.compile(r"^[A-Z0-9]{2,10}$")


class BadRequest(Exception):
    pass


def check_ip(ip):
    try:
        addr = ipaddress.ip_address(ip)
        if addr.is_private and not (addr.is_loopback or addr.is_link_local or addr.is_unspecified):
            return ip
    except ValueError:
        pass
    raise BadRequest("攝影機 IP 要是區域網路位址（例如 192.168.1.100）")


def check_plate(plate):
    if not isinstance(plate, str):
        raise BadRequest("車牌只能是英文和數字（不用加 -）")
    plate = plate.replace("-", "").replace(" ", "").upper()
    if not PLATE_RE.match(plate):
        raise BadRequest("車牌只能是英文和數字（不用加 -）")
    return plate


def check_date(text):
    try:
        return datetime.strptime(str(text), "%Y-%m-%d").strftime("%Y/%m/%d")
    except ValueError:
        raise BadRequest(f"日期格式不對：{text}")


def list_whitelist(ip):
    rtype, status, data = send(ip, CMD_LIST)
    if rtype != CMD_LIST + 1 or status != 1:
        raise BadRequest(f"攝影機沒有回應讀取（代碼 {rtype}，狀態 {status}）")
    today = datetime.now().strftime("%Y/%m/%d")
    items = []
    for line in data.split(b"\x00")[0].decode("gb2312", errors="replace").splitlines():
        cols = line.split("\t")
        if len(cols) >= 3 and cols[0].strip():
            begin, end = cols[1], cols[2]
            state = "有效" if begin <= today <= end else ("已過期" if end < today else "還沒開始")
            items.append({"plate": cols[0], "begin": begin, "end": end, "state": state})
    return items


def write_and_log(ip, cmd, body, action, plate):
    """送寫入指令；成功、被拒、連不到都記一筆操作紀錄。"""
    try:
        rtype, status, _ = send(ip, cmd, body)
    except OSError:
        log_operation(ip, f"{action}（連線失敗）", plate)
        raise
    ok = rtype == cmd + 1 and status == 1
    log_operation(ip, f"{action}（{'成功' if ok else f'被拒 代碼{rtype} 狀態{status}'}）", plate)
    if not ok:
        raise BadRequest(f"攝影機沒有接受{action}（代碼 {rtype}，狀態 {status}）")


def add_item(ip, plate, begin, end):
    if begin > end:
        raise BadRequest("開始日期不能晚於結束日期")
    write_and_log(ip, CMD_ADD, record(plate, begin, end, "00:00:00", "23:59:59", "W"), f"網頁新增／更新 {begin}～{end}", plate)


def delete_item(ip, plate):
    write_and_log(ip, CMD_DELETE, record(plate), "網頁刪除", plate)


class Handler(BaseHTTPRequestHandler):
    timeout = 10  # 慢速連線不要一直佔著

    def from_this_page(self):
        """擋 DNS rebinding：Host 一定要是本機；POST 若帶 Origin 也要是本機（安全審查 2026-11-02）。"""
        allowed = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        if self.headers.get("Host", "") not in allowed:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in allowed}

    def reply(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def reply_json(self, code, obj):
        self.reply(code, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def run(self, fn):
        try:
            self.reply_json(200, {"ok": True, **(fn() or {})})
        except BadRequest as e:
            self.reply_json(400, {"ok": False, "error": str(e)})
        except OSError as e:
            self.reply_json(502, {"ok": False, "error": f"連不到攝影機：{e}"})

    def do_GET(self):
        if not self.from_this_page():
            return self.reply(403, "text/plain; charset=utf-8", b"forbidden")
        url = urlparse(self.path)
        if url.path == "/":
            self.reply(200, "text/html; charset=utf-8", PAGE.replace("{{CAMERA}}", DEFAULT_CAMERA).encode("utf-8"))
        elif url.path == "/api/list":
            ip = parse_qs(url.query).get("ip", [""])[0]
            self.run(lambda: {"items": list_whitelist(check_ip(ip))})
        else:
            self.reply(404, "text/plain; charset=utf-8", b"not found")

    def do_POST(self):
        if not self.from_this_page():
            return self.reply_json(403, {"ok": False, "error": "forbidden"})
        # 只接受本頁送來的 JSON，擋掉其他網站用表單偷送（CSRF）
        if self.headers.get("Content-Type", "") != "application/json":
            return self.reply_json(415, {"ok": False, "error": "只接受 JSON"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= 4096:
                raise ValueError
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.reply_json(400, {"ok": False, "error": "資料格式錯誤"})
        path = urlparse(self.path).path
        if path == "/api/add":
            self.run(lambda: add_item(check_ip(body.get("ip", "")), check_plate(body.get("plate", "")),
                                      check_date(body.get("begin", "")), check_date(body.get("end", ""))))
        elif path == "/api/delete":
            self.run(lambda: delete_item(check_ip(body.get("ip", "")), check_plate(body.get("plate", ""))))
        else:
            self.reply_json(404, {"ok": False, "error": "not found"})

    def log_message(self, *args):
        pass


PAGE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>白名單管理（單機試用）</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#1d2129;--muted:#6b7280;--line:#e5e7eb;--accent:#2563eb;--ok:#15803d;--warn:#b45309;--bad:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--card:#1b1e25;--text:#e8eaed;--muted:#9aa0a6;--line:#2c313a;--accent:#60a5fa;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 system-ui,"Microsoft JhengHei",sans-serif}
main{max-width:760px;margin:0 auto;padding:20px 16px}h1{font-size:22px;margin:0 0 4px}.sub{color:var(--muted);margin:0 0 16px;font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
label{display:block;font-size:13px;color:var(--muted);margin-bottom:4px}
input{width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--text);font-size:16px}
.row{display:grid;grid-template-columns:1.3fr 1fr 1fr auto;gap:10px;align-items:end}
@media (max-width:600px){.row{grid-template-columns:1fr 1fr}.row button{grid-column:span 2}}
button{padding:9px 16px;border:0;border-radius:6px;background:var(--accent);color:#fff;font-size:15px;cursor:pointer}
button.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}button.del{background:transparent;color:var(--bad);border:1px solid var(--line);padding:5px 10px;font-size:14px}
button:disabled{opacity:.5;cursor:wait}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px 6px;border-bottom:1px solid var(--line)}th{font-size:13px;color:var(--muted);font-weight:500}
.plate{font-weight:600;letter-spacing:1px}.有效{color:var(--ok)}.已過期{color:var(--bad)}.還沒開始{color:var(--warn)}
.top{display:flex;gap:10px;align-items:end}.top div{flex:1}
#msg{min-height:24px;font-size:14px;margin-top:10px}.err{color:var(--bad)}.okmsg{color:var(--ok)}
.empty{color:var(--muted);text-align:center;padding:20px}
</style></head><body><main>
<h1>白名單管理</h1>
<p class="sub">單機試用版：直接改攝影機裡的白名單，改完立刻生效。⚠️ 白名單的車到場會自動開閘。</p>
<div class="card"><div class="top"><div><label for="ip">攝影機 IP</label><input id="ip" value="{{CAMERA}}"></div>
<button class="ghost" id="reload">重新讀取</button></div></div>
<div class="card"><div class="row">
<div><label for="plate">車牌（不用加 -）</label><input id="plate" placeholder="例如 ABC1234" autocomplete="off"></div>
<div><label for="begin">開始日期</label><input id="begin" type="date"></div>
<div><label for="end">結束日期</label><input id="end" type="date"></div>
<button id="add">新增／更新</button></div><div id="msg"></div></div>
<div class="card"><table><thead><tr><th>車牌</th><th>有效期間</th><th>狀態</th><th></th></tr></thead><tbody id="list"><tr><td colspan="4" class="empty">讀取中…</td></tr></tbody></table></div>
</main><script>
const $=id=>document.getElementById(id);
const today=new Date(),pad=n=>String(n).padStart(2,'0'),iso=d=>d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate());
$('begin').value=iso(today);const ny=new Date(today);ny.setFullYear(ny.getFullYear()+1);$('end').value=iso(ny);
function msg(t,ok){$('msg').textContent=t;$('msg').className=ok?'okmsg':'err'}
async function api(path,body){const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});return r.json()}
function esc(s){return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function load(){const tb=$('list');tb.innerHTML='<tr><td colspan="4" class="empty">讀取中…</td></tr>';
 const d=await api('/api/list?ip='+encodeURIComponent($('ip').value.trim()));
 if(!d.ok){tb.innerHTML='<tr><td colspan="4" class="empty err">'+esc(d.error)+'</td></tr>';return}
 if(!d.items.length){tb.innerHTML='<tr><td colspan="4" class="empty">白名單是空的</td></tr>';return}
 tb.innerHTML=d.items.map(i=>'<tr><td class="plate">'+esc(i.plate)+'</td><td>'+esc(i.begin)+' ～ '+esc(i.end)+'</td><td class="'+esc(i.state)+'">'+esc(i.state)+'</td><td><button class="del" data-plate="'+esc(i.plate)+'">刪除</button></td></tr>').join('')}
$('list').addEventListener('click',async e=>{const p=e.target.dataset.plate;if(!p)return;
 if(!confirm('確定要從攝影機刪除 '+p+' 嗎？刪除後這台車不會自動開閘。'))return;
 e.target.disabled=true;const d=await api('/api/delete',{ip:$('ip').value.trim(),plate:p});
 msg(d.ok?'已刪除 '+p:d.error,d.ok);load()});
$('add').onclick=async()=>{const b=$('add');b.disabled=true;
 const d=await api('/api/add',{ip:$('ip').value.trim(),plate:$('plate').value,begin:$('begin').value,end:$('end').value});
 b.disabled=false;if(d.ok){msg('已寫入攝影機：'+$('plate').value.toUpperCase().replace(/[- ]/g,''),true);$('plate').value=''}else msg(d.error,false);load()};
$('plate').addEventListener('keydown',e=>{if(e.key==='Enter')$('add').click()});
$('reload').onclick=load;load();
</script></body></html>
"""


if __name__ == "__main__":
    sys.stdout.reconfigure(errors="replace")  # 輸出被導向檔案時（cp950）印不出 ⚠️，不要因此當掉
    print("白名單管理（單機試用版）已啟動：瀏覽器會自動打開，沒有的話請開 http://127.0.0.1:%d" % PORT)
    print("這個黑色視窗不要關，關掉網頁就不能用了。")
    print("⚠️ 寫入白名單的車到場會自動開閘，只用在沒接柵欄的開發用攝影機。按 Ctrl+C 結束。")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    if "--no-browser" not in sys.argv:
        webbrowser.open(f"http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已結束。")
