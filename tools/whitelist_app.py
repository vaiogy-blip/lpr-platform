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

import hx_camera
import hx_led
from hx_whitelist import CMD_ADD, CMD_DELETE, CMD_LIST, log_dir, log_operation, record, send

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


def save_led(ip, view):
    if not isinstance(view, dict):
        raise BadRequest("字幕機資料格式錯誤")
    try:
        before = hx_led.read_raw(ip)
        # 存一份修改前的設定，出事可以對照還原
        with open(os.path.join(log_dir(), f"led_backup_{datetime.now():%Y%m%d-%H%M%S}.json"), "w", encoding="utf-8") as f:
            json.dump(before, f, ensure_ascii=False, indent=1)
        changes = hx_led.write(ip, before, view)
    except OSError:
        log_operation(ip, "網頁改字幕機（連線失敗）", "-")
        raise
    except (ValueError, KeyError, TypeError) as e:
        log_operation(ip, f"網頁改字幕機（失敗：{e}）", "-")
        raise BadRequest(str(e) if isinstance(e, ValueError) else "字幕機資料格式錯誤")
    log_operation(ip, f"網頁改字幕機（成功）{'；'.join(changes) or '沒有變更'}", "-")
    return {"led": hx_led.to_view(hx_led.read_raw(ip))}


def reboot_camera(ip):
    try:
        hx_camera.reboot(ip)
    except OSError:
        log_operation(ip, "網頁重開機（連線失敗）", "-")
        raise
    except (ValueError, KeyError, TypeError) as e:
        log_operation(ip, f"網頁重開機（失敗：{e}）", "-")
        raise BadRequest(str(e) if isinstance(e, ValueError) else "攝影機回傳格式不對")
    log_operation(ip, "網頁重開機（已送出）", "-")


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
        except (ValueError, KeyError, TypeError):
            self.reply_json(502, {"ok": False, "error": "攝影機回傳的資料看不懂，請確認 IP 是不是華夏攝影機"})

    def do_GET(self):
        if not self.from_this_page():
            return self.reply(403, "text/plain; charset=utf-8", b"forbidden")
        url = urlparse(self.path)
        if url.path == "/":
            self.reply(200, "text/html; charset=utf-8", PAGE.replace("{{CAMERA}}", DEFAULT_CAMERA).encode("utf-8"))
        elif url.path == "/api/list":
            ip = parse_qs(url.query).get("ip", [""])[0]
            self.run(lambda: {"items": list_whitelist(check_ip(ip))})
        elif url.path == "/api/led":
            ip = parse_qs(url.query).get("ip", [""])[0]
            self.run(lambda: {"led": hx_led.to_view(hx_led.read_raw(check_ip(ip)))})
        elif url.path == "/api/restart":
            ip = parse_qs(url.query).get("ip", [""])[0]
            self.run(lambda: {"restart": hx_camera.read_restart(check_ip(ip))[0]})
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
        elif path == "/api/led":
            self.run(lambda: save_led(check_ip(body.get("ip", "")), body.get("led")))
        elif path == "/api/reboot":
            self.run(lambda: reboot_camera(check_ip(body.get("ip", ""))))
        else:
            self.reply_json(404, {"ok": False, "error": "not found"})

    def log_message(self, *args):
        pass


PAGE = """<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>車道管理（單機試用）</title>
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
h2{font-size:18px;margin:24px 0 8px}h3{font-size:15px;margin:14px 0 6px}
.ledline{display:grid;grid-template-columns:52px 1fr 1.4fr 70px;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid var(--line)}
.ledline:last-child{border-bottom:0}.ln{color:var(--muted);font-size:13px}
.chk{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:14px}.chk label{display:inline-flex;align-items:center;gap:4px;margin:0;color:var(--text);font-size:14px}
.chk input,.opt input{width:auto}select{padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--text);font-size:15px}
.opt{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;font-size:14px}.opt label{display:inline-flex;align-items:center;gap:6px;margin:0;color:var(--text);font-size:14px}
@media (max-width:600px){.ledline{grid-template-columns:1fr 1fr}.ln{grid-column:span 2}}
</style></head><body><main>
<h1>車道管理</h1>
<p class="sub">單機試用版：直接改攝影機裡的白名單和字幕機，改完立刻生效。⚠️ 白名單的車到場會自動開閘。</p>
<div class="card"><div class="top"><div><label for="ip">攝影機 IP</label><input id="ip" value="{{CAMERA}}"></div>
<button class="ghost" id="reload">重新讀取</button></div></div>
<div class="card"><div class="row">
<div><label for="plate">車牌（不用加 -）</label><input id="plate" placeholder="例如 ABC1234" autocomplete="off"></div>
<div><label for="begin">開始日期</label><input id="begin" type="date"></div>
<div><label for="end">結束日期</label><input id="end" type="date"></div>
<button id="add">新增／更新</button></div><div id="msg"></div></div>
<div class="card"><table><thead><tr><th>車牌</th><th>有效期間</th><th>狀態</th><th></th></tr></thead><tbody id="list"><tr><td colspan="4" class="empty">讀取中…</td></tr></tbody></table></div>
<h2>字幕機（入口）</h2>
<div class="card" id="ledcard"><div class="empty">讀取中…</div></div>
<h2>攝影機維護</h2>
<div class="card"><div class="opt"><button class="ghost" id="reboot" style="color:var(--bad)">攝影機重開機</button><span class="ln" id="restartinfo"></span></div><div id="rbmsg"></div></div>
</main><script>
const $=id=>document.getElementById(id);
const today=new Date(),pad=n=>String(n).padStart(2,'0'),iso=d=>d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate());
$('begin').value=iso(today);const ny=new Date(today);ny.setFullYear(ny.getFullYear()+1);$('end').value=iso(ny);
function msg(t,ok){$('msg').textContent=t;$('msg').className=ok?'okmsg':'err'}
async function api(path,body){const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});return r.json()}
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
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
const COLORS=['紅','綠','黃'],MODES={0:'離線時才顯示',1:'一律顯示',2:'不使用'};
const IDLE=[['time','時間'],['date','日期'],['idle','自訂文字']],BUSY=[['plate','車牌'],['type','車輛類型'],['days','月租剩餘天數'],['busy','自訂文字']];
let led=null;
function colorSel(v,cls,i){return '<select class="'+cls+'" data-i="'+i+'">'+COLORS.map((c,k)=>'<option value="'+k+'"'+(k==v?' selected':'')+'>'+c+'</option>').join('')+'</select>'}
function lineRows(kind,flags){return led.lines.map((l,i)=>'<div class="ledline"><span class="ln">第'+(i+1)+'行</span><div class="chk">'+
 flags.map(([f,n])=>'<label><input type="checkbox" class="'+kind+'" data-i="'+i+'" data-f="'+f+'"'+(l[kind][f]?' checked':'')+'>'+n+'</label>').join('')+
 '</div><input class="'+kind+'_text" data-i="'+i+'" value="'+esc(l[kind+'_text'])+'" placeholder="自訂文字">'+colorSel(l[kind+'_color'],kind+'_color',i)+'</div>').join('')}
function renderLed(){const c=$('ledcard');
 c.innerHTML='<div class="opt"><label>顯示方式 <select id="ledmode">'+Object.entries(MODES).map(([k,n])=>'<option value="'+k+'"'+(k==led.mode?' selected':'')+'>'+n+'</option>').join('')+'</select></label>'+
 '<span class="ln">協定 '+esc(led.protocol)+'</span></div>'+
 '<h3>沒有車的時候</h3>'+lineRows('idle',IDLE)+'<h3>車子來的時候</h3>'+lineRows('busy',BUSY)+
 '<h3>語音</h3><div class="opt"><label><input type="checkbox" id="vplate"'+(led.voice_plate?' checked':'')+'>播報車牌</label>'+
 '<label><input type="checkbox" id="vwel"'+(led.voice_welcome_on?' checked':'')+'>歡迎語</label><select id="vweltext">'+
 [...new Set([led.voice_welcome,'歡迎光臨','歡迎回家'])].map(w=>'<option'+(w==led.voice_welcome?' selected':'')+'>'+esc(w)+'</option>').join('')+'</select></div>'+
 '<div class="opt" style="margin-top:14px"><button id="ledsave">儲存到攝影機</button><button class="ghost" id="ledreload">重新讀取</button></div><div id="ledmsg"></div>';
 $('ledsave').onclick=saveLed;$('ledreload').onclick=loadLed}
function ledMsg(t,ok){$('ledmsg').textContent=t;$('ledmsg').className=ok?'okmsg':'err'}
async function loadLed(){$('ledcard').innerHTML='<div class="empty">讀取中…</div>';
 const d=await api('/api/led?ip='+encodeURIComponent($('ip').value.trim()));
 if(!d.ok){$('ledcard').innerHTML='<div class="empty err">'+esc(d.error)+'</div>';return}led=d.led;renderLed()}
async function saveLed(){const v=JSON.parse(JSON.stringify(led)),q=s=>document.querySelectorAll('#ledcard '+s);
 v.mode=+$('ledmode').value;v.voice_plate=$('vplate').checked;v.voice_welcome_on=$('vwel').checked;v.voice_welcome=$('vweltext').value;
 for(const k of ['idle','busy']){q('input.'+k).forEach(e=>v.lines[e.dataset.i][k][e.dataset.f]=e.checked);
  q('.'+k+'_text').forEach(e=>v.lines[e.dataset.i][k+'_text']=e.value);q('.'+k+'_color').forEach(e=>v.lines[e.dataset.i][k+'_color']=+e.value)}
 $('ledsave').disabled=true;const d=await api('/api/led',{ip:$('ip').value.trim(),led:v});
 if(d.ok){led=d.led;renderLed();ledMsg('已存到攝影機，字幕機會馬上更新',true)}else{$('ledsave').disabled=false;ledMsg(d.error,false)}}
async function loadRestart(){const d=await api('/api/restart?ip='+encodeURIComponent($('ip').value.trim()));
 $('restartinfo').textContent=d.ok?(d.restart.auto?'自動重開：每 '+d.restart.every_days+' 天 '+d.restart.time:'自動重開：沒有開'):''}
$('reboot').onclick=async()=>{const ip=$('ip').value.trim();
 if(!confirm('確定要讓攝影機 '+ip+' 重開機嗎？\\n重開大約 1～2 分鐘，這段時間不會辨識、不會自動開閘。'))return;
 $('reboot').disabled=true;const d=await api('/api/reboot',{ip});
 $('rbmsg').textContent=d.ok?'已送出重開機，約 1～2 分鐘後按「重新讀取」確認恢復':d.error;$('rbmsg').className=d.ok?'okmsg':'err';
 setTimeout(()=>$('reboot').disabled=false,60000)};
$('reload').onclick=()=>{load();loadLed();loadRestart()};load();loadLed();loadRestart();
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
