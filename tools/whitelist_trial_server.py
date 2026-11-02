# -*- coding: utf-8 -*-
"""
華夏攝影機「平台寫入白名單」試驗程式

目的：試試看能不能在心跳的回覆裡，叫攝影機新增、更新或刪除一筆白名單。
只在前 3 次心跳回覆白名單指令，之後回空白；車牌辨識一律回空白（平台不下開閘）。
⚠️ 白名單寫進去後，攝影機看到這個車牌會依後台設定「自動開閘」，只能在沒接柵欄的桌上攝影機試。

用法（Windows）：
  1. 在這個資料夾開「命令提示字元」，執行（候選編號＋車牌＋動作）：
         python whitelist_trial_server.py 3 S30438 update
     動作：add（新增）、update（更新有效期間）、delete（刪除）
  2. 攝影機推送網址跟 capture_server.py 一樣（埠 8080，路徑 /api/lpr/notify）。
  3. 等畫面顯示「白名單指令送完了」，拿車牌照片對鏡頭，
     看攝影機後台「即時影像」辨識列表的「黑白名單」欄，或這個畫面的「攝影機白名單比對」。
"""

import json
import os
import sys
from datetime import datetime
from http.server import ThreadingHTTPServer

from capture_server import PORT, Handler, local_ip

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures_whitelist_trial")

SEND_TIMES = 3
START, END = "2026-01-01 00:00:00", "2027-12-31 23:59:59"
ACTIONS = ("add", "update", "delete")


# 候選格式：都沒有這台的官方文件
# 1、2：同類攝影機的 white_list_operate（2026-11-02 實測候選 1 的新增、刪除都無效）
# 3：華夏 2018 年「HTTP POST 功能模式說明」的 whitelist_opt_data（https://blog.csdn.net/weixin_34248258/article/details/93819587）
# 4：另一款一體機 HTTP 推送的 addWhiteList／deleteWhiteList（https://www.cnblogs.com/zhouein/p/14347980.html）
def candidate(n, plate, action):
    if n in (1, 2):
        entry = {"plate": plate, "enable": 1}
        if n == 1:
            entry.update(need_alarm=0, enable_time=START, overdue_time=END)
        op = 1 if action == "delete" else 0  # 推測：0 新增、1 刪除
        return {"error_num": 0, "error_str": "noerror",
                "white_list_operate": {"operate_type": op, "white_list_data": [entry]}}
    if n == 3:
        entry = {"Action": action, "PlateNumber": plate}
        if action != "delete":
            entry.update(Type="W", Start=START.replace("-", "/"), End=END.replace("-", "/"))
        return {"PostServer": "OK", "whitelist_opt_data": [entry]}
    if n == 4:
        if action == "delete":
            return {"deleteWhiteList": {"del_data": [{"carnum": plate}]}}
        stamp = lambda t: t.replace("-", "").replace(" ", "").replace(":", "")
        return {"addWhiteList": {"add_data": [{"carnum": plate, "startime": stamp(START), "endtime": stamp(END)}]}}
    return None


CANDIDATE_NAMES = {
    1: "white_list_operate，含有效期間（已試無效）",
    2: "white_list_operate，只帶車牌與啟用",
    3: "華夏 whitelist_opt_data（Action／PlateNumber／Type／Start／End）",
    4: "addWhiteList／deleteWhiteList（carnum／startime／endtime）",
}


class TrialHandler(Handler):
    out_dir = OUT_DIR
    candidate = None
    plate = None
    action = None
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
            body = json.dumps(candidate(self.candidate, self.plate, self.action), ensure_ascii=False).encode("utf-8")
            print(f"  >>> 第 {TrialHandler.sent}/{SEND_TIMES} 次在心跳回覆送出白名單 {self.action}：{body.decode('utf-8')}")
            if TrialHandler.sent == SEND_TIMES:
                print("  >>> 白名單指令送完了，現在可以拿車牌照片對鏡頭。")
            self.log(f"heartbeat\t候選{self.candidate}\t{self.action}\t{body.decode('utf-8')}")
            return "application/json", body

        return None, b""


if __name__ == "__main__":
    args = sys.argv[1:]
    ok = len(args) == 3 and args[0].isdigit() and int(args[0]) in CANDIDATE_NAMES \
        and args[1].isalnum() and args[2] in ACTIONS
    if not ok:
        print("請加上候選編號、車牌（英文數字，不要加 -）、動作，例如：python whitelist_trial_server.py 3 S30438 update")
        print("動作：add（新增）、update（更新有效期間）、delete（刪除）")
        for n, name in CANDIDATE_NAMES.items():
            print(f"  {n}：{name}")
        sys.exit(1)

    TrialHandler.candidate = int(args[0])
    TrialHandler.plate = args[1].upper()
    TrialHandler.action = args[2]
    os.makedirs(OUT_DIR, exist_ok=True)
    print("⚠️ 白名單試驗程式：會叫攝影機修改白名單，只能在沒接柵欄的攝影機上使用。")
    print(f"本次試驗：候選 {TrialHandler.candidate}（{CANDIDATE_NAMES[TrialHandler.candidate]}），{TrialHandler.action} 車牌 {TrialHandler.plate}")
    print(f"接收端已啟動：http://{local_ip()}:{PORT}/api/lpr/notify")
    print(f"記錄會存到：{OUT_DIR}")
    print("按 Ctrl+C 結束。")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), TrialHandler).serve_forever()
    except KeyboardInterrupt:
        print("\n已結束。")
