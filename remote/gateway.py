# -*- coding: utf-8 -*-
"""
現場小主機程式（社區／公司那台電腦跑）：主動連到 PS雲平台，拿指令、在攝影機上執行、回報結果。

  - 只往外連（HTTPS 長輪詢），社區網路不用開任何埠。
  - 平台只能用「攝影機代號」指定攝影機；代號對應的 IP 寫在本機設定檔，平台無法叫小主機去連別的設備。
  - 只接受固定幾種指令，參數在這裡再檢查一次（平台被入侵也不能亂下指令）。
  - 每個會改東西的動作都記在 captures_hx_whitelist/operation_log.txt。
  - 接收攝影機的辨識推送（push_receiver.py，預設 8080 埠，只在社區內網），把進出紀錄送到 PSC。

設定檔 gateway_config.json（放在程式旁邊，不進 git），範例見 gateway_config.example.json。
用法：python gateway.py
"""

import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

import hx_camera  # noqa: E402
import hx_led  # noqa: E402
import hx_params  # noqa: E402
from hx_whitelist import log_operation  # noqa: E402
import autostart  # noqa: E402
import push_receiver  # noqa: E402
from whitelist_app import (BadRequest, add_item, check_date, check_plate, delete_item,  # noqa: E402
                           list_whitelist, reboot_camera, save_led)

VERSION = "0.4"
POLL_TIMEOUT = 40  # 平台最多讓長輪詢等 25 秒


def load_config():
    path = os.environ.get("GATEWAY_CONFIG") or os.path.join(HERE, "gateway_config.json")  # 模擬時可指定別的設定檔
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("token") or len(cfg["token"]) < 24:
        raise SystemExit("gateway_config.json 的 token 太短或沒填（至少 24 字元，要跟平台設定的一樣）")
    server = urllib.parse.urlparse(cfg.get("server", ""))
    if server.scheme != "https" and server.hostname not in ("127.0.0.1", "localhost"):
        raise SystemExit("平台網址一定要是 https://（不然 token 會用明文送出）")
    return cfg


def check_time(text):
    """照片查詢時間：網頁送 yyyy-mm-ddTHH:MM，轉成攝影機要的 yyyy-MM-dd HH:mm:ss。"""
    from datetime import datetime
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(text), fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    raise BadRequest(f"時間格式不對：{text}")


def run_command(cfg, cmd):
    cams = {c["id"]: c for c in cfg["cameras"]}
    cam = cams.get(cmd.get("camera"))
    if not cam:
        raise BadRequest("這台小主機沒有這支攝影機")
    ip, args, kind = cam["ip"], cmd.get("args") or {}, cmd.get("type")
    if not isinstance(args, dict):
        raise BadRequest("參數格式不對")

    if kind == "wl_list":
        return {"items": list_whitelist(ip)}
    if kind == "wl_add":
        add_item(ip, check_plate(args.get("plate", "")), check_date(args.get("begin", "")), check_date(args.get("end", ""), end=True))
        return {}
    if kind == "wl_batch":
        # 批次匯入：一次最多 200 筆，每筆各自檢查、各自回報；有錯的跳過不影響其他筆
        items = args.get("items")
        if not isinstance(items, list) or not 0 < len(items) <= 200:
            raise BadRequest("批次匯入一次 1～200 筆")
        report = []
        for it in items:
            plate = it.get("plate", "") if isinstance(it, dict) else ""
            try:
                p = check_plate(plate)
                add_item(ip, p, check_date(it.get("begin", "")), check_date(it.get("end", ""), end=True))
                report.append({"plate": p, "ok": True})
            except BadRequest as e:
                report.append({"plate": str(plate)[:16], "ok": False, "error": str(e)})
            except OSError as e:  # 攝影機連不到：這筆記失敗，繼續下一筆（每筆都要有結果，稽核才查得到）
                report.append({"plate": str(plate)[:16], "ok": False, "error": f"連不到攝影機：{e}"})
        return {"report": report}
    if kind == "wl_delete":
        delete_item(ip, check_plate(args.get("plate", "")))
        return {}
    if kind == "restart_info":
        return {"restart": hx_camera.read_restart(ip)[0]}
    if kind == "reboot":
        reboot_camera(ip)
        return {}
    if kind == "led_get":
        return {"led": hx_led.to_view(hx_led.read_raw(ip))}
    if kind == "led_set":
        return save_led(ip, args.get("led"))
    if kind == "param_get":
        return hx_params.read_all(ip)
    if kind == "param_set":
        key = str(args.get("key", ""))[:40]
        try:
            changes = hx_params.write(ip, key, args.get("values"))
        except ValueError as e:
            log_operation(ip, f"遠端改參數 {key}（失敗：{e}）", "-")
            raise BadRequest(str(e))
        log_operation(ip, f"遠端改參數 {key}（成功）{'；'.join(changes) or '沒有變更'}", "-")
        return {"changes": changes, **hx_params.read_all(ip)}
    if kind == "photo_query":
        plate = args.get("plate", "")
        plate = check_plate(plate) if plate else ""
        return {"days": hx_camera.query_photos(ip, plate, check_time(args.get("start")), check_time(args.get("end")))}
    if kind == "photo_get":
        path = str(args.get("path", ""))
        if not hx_camera.PHOTO_PATH.match(path) or ".." in path:
            raise BadRequest("照片路徑不對")
        data = hx_camera.get_photo(ip, path, cam.get("photo_port", 80))
        return {"jpeg_b64": base64.b64encode(data).decode("ascii")}
    raise BadRequest(f"不認得的指令：{kind}")


def execute(cfg, cmd):
    try:
        return {"id": cmd.get("id"), "ok": True, "data": run_command(cfg, cmd)}
    except BadRequest as e:
        return {"id": cmd.get("id"), "ok": False, "error": str(e)}
    except OSError as e:
        return {"id": cmd.get("id"), "ok": False, "error": f"連不到攝影機：{e}"}
    except Exception as e:  # 攝影機回半截資料（http.client.HTTPException 等）也不能讓小主機掛掉
        return {"id": cmd.get("id"), "ok": False, "error": f"攝影機回傳的資料看不懂（{type(e).__name__}）"}


def poll(cfg, results):
    body = json.dumps({
        "gateway_id": cfg["gateway_id"], "version": VERSION,
        "cameras": [{"id": c["id"], "name": c.get("name", c["id"])} for c in cfg["cameras"]],
        "results": results, "camera_status": push_receiver.camera_status(),
    }, ensure_ascii=False).encode("utf-8")
    return post(cfg, "/gw/poll", body).get("commands", [])


def post(cfg, path, body):
    req = urllib.request.Request(cfg["server"].rstrip("/") + path, data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + cfg["token"]})
    with urllib.request.urlopen(req, timeout=POLL_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def send_events(cfg):
    """另一條執行緒：攝影機推來的進出紀錄盡快送到平台；送不出去就留著，網路恢復再補送。"""
    backoff = 2
    while True:
        batch = push_receiver.take()
        if not batch:
            continue
        try:
            body = json.dumps({"gateway_id": cfg["gateway_id"], "events": batch}, ensure_ascii=False).encode("utf-8")
            post(cfg, "/gw/events", body)
            backoff = 2
        except Exception:  # 平台連不到：放回去，等一下再送
            push_receiver.give_back(batch)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def start_push(cfg, service=False):
    port = cfg.get("push_port", 8080)
    if not port:
        return
    # 背景版的記錄檔不寫車牌（個資）；視窗版才顯示，方便現場確認
    show = None if service else (lambda e: print(
        time.strftime("%H:%M:%S"), f"攝影機 {e['camera']} 認到 {e['plate']}（{'白名單' if e['is_whitelist'] else '非白名單'}）"))
    try:
        push_receiver.start(cfg["cameras"], cfg.get("push_host", "0.0.0.0"), int(port), on_event=show)
    except OSError as e:
        print(f"⚠️ 接收攝影機推送的 {port} 埠開不起來（{e}），進出紀錄收不到；其他功能照常。")
        return
    threading.Thread(target=send_events, args=(cfg,), daemon=True).start()
    print(f"接收攝影機推送：這台電腦的 {port} 埠（攝影機後台 HTTP POST 填這台電腦的 IP:{port}）")


def main():
    if "--install-service" in sys.argv:  # 系統管理員身分重開自己時才會帶這些參數
        pid = sys.argv[-1]
        return autostart.install(int(pid) if pid.isdigit() else None)
    if "--uninstall-service" in sys.argv:
        return autostart.uninstall()
    service = "--service" in sys.argv     # 開機自動執行（背景、沒有視窗）
    if service:
        autostart.log_to_file(HERE)
    else:
        sys.stdout.reconfigure(errors="replace")
    if os.environ.get("GATEWAY_ALLOW_MULTI") != "1" and not autostart.single_instance():  # 模擬多台時才允許
        print(time.strftime("%Y-%m-%d %H:%M:%S"), "這台電腦已經有一個現場小主機在跑（可能是開機自動執行的背景版），不用再開。")
        if not service:
            print("要取消開機自動執行並停止背景版：輸入 U 再按 Enter；直接按 Enter 關閉。")
            try:
                answer = input().strip().upper()
            except (EOFError, OSError):
                answer = ""
            if answer == "U" and not autostart.elevate("--uninstall-service"):
                input("沒有取得系統管理員權限，沒有移除。按 Enter 關閉")
        return
    cfg = load_config()
    print(time.strftime("%Y-%m-%d %H:%M:%S"), f"現場小主機 {cfg['gateway_id']} {VERSION} 啟動，連線到 {cfg['server']}，攝影機 {len(cfg['cameras'])} 支。")
    if not service:
        print("這個視窗不要關。" + ("（已設定開機自動執行）" if autostart.installed() else
                                   "想要開機自動執行、不用開這個視窗：輸入 I 再按 Enter。"))
        autostart.console_menu(lambda: os._exit(0))
    start_push(cfg, service)
    results, backoff, online = [], 2, False
    while True:
        try:
            commands = poll(cfg, results)
            results = []
            if not online:
                print(time.strftime("%H:%M:%S"), "已連上平台")
                online, backoff = True, 2
            for cmd in commands if isinstance(commands, list) else []:
                if not isinstance(cmd, dict):
                    continue
                res = execute(cfg, cmd)
                print(time.strftime("%H:%M:%S"), f"指令 {cmd.get('type')} → {'成功' if res['ok'] else res['error']}")
                results.append(res)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                log_operation(cfg["server"], "平台拒絕連線（token 不對）", cfg["gateway_id"])
                print("平台拒絕連線：token 不對，請檢查 gateway_config.json。60 秒後再試。")
                time.sleep(60)
                continue
            online = False
            print(time.strftime("%H:%M:%S"), f"平台回應錯誤 {e.code}，{backoff} 秒後重試")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as e:  # 任何錯誤都只重連，不讓程式結束（不然要人到現場重開）
            if online:
                print(time.strftime("%H:%M:%S"), f"跟平台斷線（{type(e).__name__}），重新連線中…")
            online = False
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
