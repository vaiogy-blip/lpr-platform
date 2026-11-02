# -*- coding: utf-8 -*-
"""
在家模擬現場（開發用）：照 psc/data/sim/*.json（小主機設定檔）每一份開一台模擬小主機，
並在設定檔裡的攝影機 IP（必須是 127.x.x.x）開假攝影機。按 Ctrl+C 全部關掉。

設定檔怎麼來：在 PSC 網頁「場站」→ 新增小主機 → 下載設定檔，存到 psc/data/sim/，
把 cameras 的 ip 改成 127.0.0.2、127.0.0.3…（每支不同），photo_port 改成 8081。
用法：python psc/sim.py
"""

import glob
import json
import os
import subprocess
import sys
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SIM = os.path.join(ROOT, "psc", "data", "sim")


def main():
    configs = sorted(glob.glob(os.path.join(SIM, "*.json")))
    configs = [c for c in configs if os.path.basename(c) != "state.json"]
    if not configs:
        raise SystemExit(f"{SIM} 裡沒有小主機設定檔，先在 PSC 網頁新增小主機並下載設定檔放進去")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    procs, hosts = [], set()
    for path in configs:
        cfg = json.load(open(path, encoding="utf-8"))
        for cam in cfg.get("cameras", []):
            ip = cam.get("ip", "")
            if not ip.startswith("127.") or ip in hosts:
                continue
            hosts.add(ip)
            procs.append(subprocess.Popen([sys.executable, os.path.join(ROOT, "tools", "fake_camera.py"), "--host", ip], env=env))
    time.sleep(1)
    for path in configs:
        procs.append(subprocess.Popen([sys.executable, "-u", os.path.join(ROOT, "remote", "gateway.py")],
                                      env={**env, "GATEWAY_CONFIG": path}))
    print(f"模擬中：假攝影機 {len(hosts)} 支、小主機 {len(configs)} 台。按 Ctrl+C 結束。")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
