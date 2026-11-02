# -*- coding: utf-8 -*-
"""
現場小主機「開機自動執行」：用 Windows 工作排程器建一個「開機時用 SYSTEM 執行」的工作，
不用登入、不會有黑色視窗，電腦重開也會自己跑起來。

  - 在小主機視窗輸入 I 再按 Enter → 跳出 Windows 的系統管理員確認 → 建好工作、關掉這個視窗、改由背景執行。
  - 輸入 U 再按 Enter → 移除開機自動執行。
  - 背景執行時看不到視窗，訊息寫在程式旁邊的 gateway_log.txt。
  - 同一台電腦同時只能跑一個小主機（用本機埠 SINGLE_PORT 當鎖），避免背景版和視窗版重複執行。
"""

import ctypes
import os
import socket
import subprocess
import sys
import time

TASK_NAME = "PSC-Gateway"
SINGLE_PORT = 47811
LOG_MAX = 2_000_000
_lock_socket = None


def program():
    """重新啟動自己要用的指令（打包成 exe 時就是 exe 本身）。"""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, os.path.abspath(os.path.join(os.path.dirname(__file__), "gateway.py"))]


def single_instance():
    """搶本機埠當鎖；搶不到代表已經有一個小主機在跑。"""
    global _lock_socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLE_PORT))
    except OSError:
        s.close()
        return False
    _lock_socket = s
    return True


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def run(args):
    r = subprocess.run(args, capture_output=True, text=True, encoding="cp950", errors="replace")
    return r.returncode, (r.stdout + r.stderr).strip()


def installed():
    return run(["schtasks", "/Query", "/TN", TASK_NAME])[0] == 0


def elevate(flag):
    """用系統管理員身分重新開自己（會跳 Windows 確認視窗）。回傳使用者有沒有按「是」。"""
    exe, *rest = program()
    params = subprocess.list2cmdline(rest + [flag, str(os.getpid())])
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1) > 32


def wait_pid_exit(pid, seconds=30):
    """等原本的視窗版關掉（讓出單一執行的鎖）。"""
    for _ in range(seconds * 2):
        r = run(["tasklist", "/FI", f"PID eq {pid}", "/NH"])[1]
        if str(pid) not in r:
            return
        time.sleep(0.5)


def safe_folder():
    """程式所在資料夾。SYSTEM 會執行這裡的程式，所以不能放在一般使用者能改的地方。"""
    folder = os.path.dirname(program()[-1])
    users = os.path.abspath(os.environ.get("SystemDrive", "C:") + "\\Users").lower()
    if os.path.abspath(folder).lower().startswith(users):
        return None, folder
    return folder, folder


def install(caller_pid):
    folder, where = safe_folder()
    if not folder:
        print(f"⚠️ 程式現在放在 {where}（使用者資料夾，例如桌面、下載），不能設成開機自動執行：")
        print("   一般使用者能改這裡的檔案，等於能用系統最高權限執行程式。")
        print("   請把 現場小主機.exe 和 gateway_config.json 搬到 C:\\PSC-Gateway 這種資料夾再設定。")
        input("按 Enter 關閉這個視窗")
        return
    # 資料夾只留 SYSTEM 與系統管理員能改、能讀（裡面有平台通行碼）
    code, out = run(["icacls", folder, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"])
    if code != 0:
        print("設定資料夾權限失敗：", out)
        input("按 Enter 關閉這個視窗")
        return
    cmd = subprocess.list2cmdline(program() + ["--service"])
    code, out = run(["schtasks", "/Create", "/TN", TASK_NAME, "/TR", cmd, "/SC", "ONSTART", "/RU", "SYSTEM", "/RL", "HIGHEST", "/F"])
    if code != 0:
        print("設定失敗：", out)
    else:
        # 工作排程器預設「用電池時不執行」「執行 3 天後停止」，改掉
        ps = (f"$s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 "
              f"-RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1); Set-ScheduledTask -TaskName '{TASK_NAME}' -Settings $s | Out-Null")
        run(["powershell", "-NoProfile", "-Command", ps])
        if caller_pid:
            wait_pid_exit(caller_pid)
        run(["schtasks", "/Run", "/TN", TASK_NAME])
        print("✅ 已設定開機自動執行，現在已改由背景執行（看不到視窗）。")
        print("   電腦重開後會自己跑起來；訊息記在程式旁邊的 gateway_log.txt。")
        print(f"   {folder} 現在只有系統管理員能修改（保護通行碼）。之後要改設定檔請用系統管理員身分。")
    input("按 Enter 關閉這個視窗")


def uninstall():
    run(["schtasks", "/End", "/TN", TASK_NAME])
    code, out = run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    print("✅ 已移除開機自動執行。之後要用時再點兩下程式。" if code == 0 else f"移除失敗：{out}")
    input("按 Enter 關閉這個視窗")


def log_to_file(folder):
    """背景執行沒有視窗：訊息改寫到 gateway_log.txt，超過 2MB 換新檔（舊的留一份 .old）。"""
    path = os.path.join(folder, "gateway_log.txt")
    if os.path.exists(path) and os.path.getsize(path) > LOG_MAX:
        os.replace(path, path + ".old")
    f = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = f


def console_menu(on_quit):
    """視窗版：在背景讀鍵盤，I＝設成開機自動執行，U＝移除。"""
    import threading

    def loop():
        while True:
            try:
                line = input().strip().upper()
            except (EOFError, OSError):
                return
            if line == "I":
                if elevate("--install-service"):
                    print("正在設定開機自動執行，這個視窗會關掉，改由背景執行。")
                    time.sleep(1)
                    on_quit()
                else:
                    print("沒有取得系統管理員權限，沒有設定。")
            elif line == "U":
                if not elevate("--uninstall-service"):
                    print("沒有取得系統管理員權限，沒有移除。")
    threading.Thread(target=loop, daemon=True).start()
