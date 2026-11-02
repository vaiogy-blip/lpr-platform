# -*- coding: utf-8 -*-
"""
華夏攝影機「開閘輸出」試驗程式（⚠️ 會對攝影機下開閘指令）

只能在攝影機沒有接柵欄的桌上測試使用。目的是找出攝影機認得哪一種開閘回覆：
不用等辨識車牌，攝影機每次送來心跳（約 10 秒一次），就用選定的候選格式回覆開閘。
如果格式對了，攝影機應該每 10 秒左右「喀」一聲。

用法（Windows）：
  1. 攝影機後台「入口自動開門」維持不勾。
  2. 在這個資料夾開「命令提示字元」，執行（最後的數字是候選編號）：
         python gate_trial_server.py 1
  3. 攝影機推送網址跟 capture_server.py 一樣（埠 8080，路徑 /api/lpr/notify）。
  4. 等 30 秒左右（約 3 次心跳），注意攝影機有沒有「喀」一聲（繼電器動作）。
  5. 按 Ctrl+C 結束，換下一個編號再試：python gate_trial_server.py 2 ……
  6. 告訴 Claude 每個編號有沒有喀一聲。紀錄會存在 captures_gate_trial 資料夾。
"""

import json
import os
import sys
from datetime import datetime
from http.server import ThreadingHTTPServer

from capture_server import PORT, Handler, local_ip

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures_gate_trial")

# 候選回覆格式：都沒有官方文件，是依同類攝影機的公開資料推測的
CANDIDATES = {
    1: ("華夏格式：gpio_data 讓 io1 動作", "application/json", {
        "error_num": 0,
        "error_str": "noerror",
        "gpio_data": [{"ionum": "io1", "action": "on"}],
    }),
    2: ("華夏格式：gpio_data 讓 io2 動作", "application/json", {
        "error_num": 0,
        "error_str": "noerror",
        "gpio_data": [{"ionum": "io2", "action": "on"}],
    }),
    3: ("臻識格式：Response_AlarmInfoPlate info=ok", "application/json", {
        "Response_AlarmInfoPlate": {"info": "ok"},
    }),
    4: ("臻識心跳格式：Response_Heartbeat info=opendoor", "application/json", {
        "Response_Heartbeat": {"info": "opendoor"},
    }),
    5: ("簡單 JSON：info=ok", "application/json", {"info": "ok"}),
    6: ("純文字 ok", "text/plain", "ok"),
    7: ("華夏格式：gpio_data 讓 io3 動作", "application/json", {
        "error_num": 0,
        "error_str": "noerror",
        "gpio_data": [{"ionum": "io3", "action": "on"}],
    }),
}


def encode(content):
    if isinstance(content, str):
        return content.encode("utf-8")
    return json.dumps(content, ensure_ascii=False).encode("utf-8")


class TrialHandler(Handler):
    out_dir = OUT_DIR
    candidate = None

    def reply(self, parsed):
        # 心跳與車牌辨識都回開閘候選，不必等辨識到車牌
        kind = parsed.get("type") if isinstance(parsed, dict) else None
        if kind not in ("heartbeat", "online"):
            return None, b""

        name, content_type, content = CANDIDATES[self.candidate]
        body = encode(content)
        now = datetime.now().strftime("%H:%M:%S")
        print("=" * 50)
        print(f"  {now} 收到 {kind}：已回覆候選 {self.candidate}（{name}）")
        print("  請注意攝影機有沒有「喀」一聲。")
        print("=" * 50)
        with open(os.path.join(self.out_dir, "trial_log.txt"), "a", encoding="utf-8") as f:
            f.write(f"{now}\t{kind}\t候選{self.candidate}\t{body.decode('utf-8')}\n")
        return content_type, body


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].isdigit() or int(sys.argv[1]) not in CANDIDATES:
        print("請在後面加上候選編號，例如：python gate_trial_server.py 1")
        for n, (name, _, content) in CANDIDATES.items():
            print(f"  {n}：{name}  →  {encode(content).decode('utf-8')}")
        sys.exit(1)

    TrialHandler.candidate = int(sys.argv[1])
    os.makedirs(OUT_DIR, exist_ok=True)
    name = CANDIDATES[TrialHandler.candidate][0]
    print("⚠️ 開閘試驗程式：每次心跳都會回覆開閘指令，只能在沒接柵欄的攝影機上使用。")
    print(f"本次試驗：候選 {TrialHandler.candidate}（{name}）")
    print(f"接收端已啟動：http://{local_ip()}:{PORT}/api/lpr/notify")
    print(f"記錄會存到：{OUT_DIR}")
    print("按 Ctrl+C 結束。")
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), TrialHandler).serve_forever()
    except KeyboardInterrupt:
        print("\n已結束。")
