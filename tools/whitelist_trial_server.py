# -*- coding: utf-8 -*-
"""
華夏攝影機「平台寫入白名單」試驗程式

目的：試試看能不能在心跳的回覆裡，叫攝影機新增或刪除一筆白名單。
只在前 3 次心跳回覆白名單指令，之後回空白；車牌辨識一律回空白（平台不下開閘）。
⚠️ 白名單寫進去後，攝影機看到這個車牌會依後台設定「自動開閘」，只能在沒接柵欄的桌上攝影機試。

用法（Windows）：
  1. 在這個資料夾開「命令提示字元」，執行（候選編號＋測試車牌）：
         python whitelist_trial_server.py 1 TST0001
     車牌要用攝影機白名單裡「原本沒有」的號碼，才看得出有沒有寫進去。
  2. 攝影機推送網址跟 capture_server.py 一樣（埠 8080，路徑 /api/lpr/notify）。
  3. 等畫面顯示送出 3 次白名單指令後，到攝影機後台的白名單清單，看有沒有多出這個車牌。
     （如果手上有這個車牌的照片，也可以對鏡頭，看「攝影機白名單比對」是不是「是」。）
  4. 試完要刪掉測試車牌：python whitelist_trial_server.py 1 TST0001 delete
     （或到攝影機後台手動刪除）
"""

import json
import os
import sys
from datetime import datetime
from http.server import ThreadingHTTPServer

from capture_server import PORT, Handler, local_ip

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures_whitelist_trial")

SEND_TIMES = 3


# 候選格式：都沒有官方文件，依同類攝影機（與已證實的 gpio_data 同一套）的 white_list_operate 推測
# 2026-11-02 候選 1 送 S30438 沒看到效果，但 S30438 原本就在攝影機白名單裡（逾期），可能是重複新增被忽略
def candidate(n, plate, delete):
    op = 1 if delete else 0  # 推測：0 新增、1 刪除
    if n == 1:
        entry = {
            "plate": plate,
            "enable": 1,
            "need_alarm": 0,
            "enable_time": "2026-01-01 00:00:00",
            "overdue_time": "2027-12-31 23:59:59",
        }
    elif n == 2:
        entry = {"plate": plate, "enable": 1}  # 只帶最少欄位
    else:
        return None
    return {
        "error_num": 0,
        "error_str": "noerror",
        "white_list_operate": {"operate_type": op, "white_list_data": [entry]},
    }


CANDIDATE_NAMES = {
    1: "white_list_operate，含有效期間（2026-01-01～2027-12-31）",
    2: "white_list_operate，只帶車牌與啟用",
}


class TrialHandler(Handler):
    out_dir = OUT_DIR
    candidate = None
    plate = None
    delete = False
    sent = 0

    def log(self, line):
        with open(os.path.join(self.out_dir, "trial_log.txt"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%H:%M:%S}\t{line}\n")

    def reply(self, parsed):
        kind = parsed.get("type") if isinstance(parsed, dict) else None

        if kind == "online":
            hit = "是" if parsed.get("is_whitelist") == "true" else "否"
            print("=" * 50)
            print(f"  車牌 {parsed.get('plate_num')}：攝影機白名單比對＝{hit}")
            print("=" * 50)
            self.log(f"plate\t{parsed.get('plate_num')}\tis_whitelist={parsed.get('is_whitelist')}")
            return None, b""

        if kind == "heartbeat" and TrialHandler.sent < SEND_TIMES:
            TrialHandler.sent += 1
            body = json.dumps(candidate(self.candidate, self.plate, self.delete), ensure_ascii=False).encode("utf-8")
            action = "刪除" if self.delete else "新增"
            print(f"  >>> 第 {TrialHandler.sent}/{SEND_TIMES} 次在心跳回覆送出白名單{action}：{body.decode('utf-8')}")
            if TrialHandler.sent == SEND_TIMES:
                print("  >>> 白名單指令送完了，現在可以去攝影機後台看白名單清單。")
            self.log(f"heartbeat\t候選{self.candidate}\t{action}\t{body.decode('utf-8')}")
            return "application/json", body

        return None, b""


if __name__ == "__main__":
    args = sys.argv[1:]
    ok = len(args) in (2, 3) and args[0].isdigit() and int(args[0]) in CANDIDATE_NAMES \
        and args[1].isalnum() and (len(args) == 2 or args[2] == "delete")
    if not ok:
        print("請加上候選編號和測試車牌（英文數字，不要加 -），例如：python whitelist_trial_server.py 1 TST0001")
        print("刪除測試車牌：python whitelist_trial_server.py 1 TST0001 delete")
        for n, name in CANDIDATE_NAMES.items():
            print(f"  {n}：{name}")
        sys.exit(1)

    TrialHandler.candidate = int(args[0])
    TrialHandler.plate = args[1].upper()
    TrialHandler.delete = len(args) == 3
    os.makedirs(OUT_DIR, exist_ok=True)
    action = "刪除" if TrialHandler.delete else "新增"
    print("⚠️ 白名單試驗程式：會叫攝影機修改白名單，只能在沒接柵欄的攝影機上使用。")
    print(f"本次試驗：候選 {TrialHandler.candidate}（{CANDIDATE_NAMES[TrialHandler.candidate]}），{action}車牌 {TrialHandler.plate}")
    print(f"接收端已啟動：http://{local_ip()}:{PORT}/api/lpr/notify")
    print(f"記錄會存到：{OUT_DIR}")
    print("按 Ctrl+C 結束。")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), TrialHandler).serve_forever()
    except KeyboardInterrupt:
        print("\n已結束。")
