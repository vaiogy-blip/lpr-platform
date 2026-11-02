# -*- coding: utf-8 -*-
"""
現場小主機程式（社區／公司那台電腦跑）：主動連到 PS雲平台，拿指令、在攝影機上執行、回報結果。

  - 只往外連（HTTPS 長輪詢），社區網路不用開任何埠。
  - 平台只能用「攝影機代號」指定攝影機；代號對應的 IP 寫在本機設定檔，平台無法叫小主機去連別的設備。
  - 只接受固定幾種指令，參數在這裡再檢查一次（平台被入侵也不能亂下指令）。
  - 每個會改東西的動作都記在 captures_hx_whitelist/operation_log.txt。

設定檔 gateway_config.json（放在程式旁邊，不進 git），範例見 gateway_config.example.json。
用法：python gateway.py
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))

import hx_camera  # noqa: E402
import hx_led  # noqa: E402
from hx_whitelist import log_operation  # noqa: E402
from whitelist_app import (BadRequest, add_item, check_date, check_plate, delete_item,  # noqa: E402
                           list_whitelist, reboot_camera, save_led)

VERSION = "0.2"
POLL_TIMEOUT = 40  # 平台最多讓長輪詢等 25 秒


def load_config():
    path = os.path.join(HERE, "gateway_config.json")
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
        "results": results,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(cfg["server"].rstrip("/") + "/gw/poll", data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + cfg["token"]})
    with urllib.request.urlopen(req, timeout=POLL_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8")).get("commands", [])


def main():
    sys.stdout.reconfigure(errors="replace")
    cfg = load_config()
    print(f"現場小主機 {cfg['gateway_id']} 啟動，連線到 {cfg['server']}，攝影機 {len(cfg['cameras'])} 支。這個視窗不要關。")
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
