# -*- coding: utf-8 -*-
"""
華夏攝影機白名單工具（區域網路直連，攝影機 TCP 8117 埠）

協議是從老闆停車場軟體裡的華夏官方 SDK（ice_ipcsdk.jar）讀出來的（2026-11-02，只讀分析）：
  每個指令開一條新的 TCP 連線，送「32 位元組表頭＋內容」：
    表頭 0～3：總長度（32＋內容長度），4～7：指令代碼，其餘補 0；數字都是小端序（little-endian）。
  攝影機回一樣格式的表頭，4～7 是回覆代碼、8～11 是狀態（1＝成功），表頭後面是資料。
  白名單一筆 135 位元組（GB2312）：0 車牌、16 開始日期、27 結束日期、38 開始時間、49 結束時間、60 類型（W）。
  日期 yyyy/MM/dd、時間 HH:mm:ss。

⚠️ 寫入白名單後攝影機會依後台設定自動開閘，只能在沒接柵欄的桌上攝影機試。

用法（在這個資料夾開命令提示字元）：
  python hx_whitelist.py 192.168.1.100 list
  python hx_whitelist.py 192.168.1.100 find S30438
  python hx_whitelist.py 192.168.1.100 add S30438 2026/01/01 2027/12/31
  python hx_whitelist.py 192.168.1.100 delete S30438
"""

import os
import socket
import struct
import sys
from datetime import datetime

PORT = 8117
HEADER = 32
RECORD = 135

CMD_LIST = 20741    # 回 20742：整份白名單（文字）
CMD_ADD = 20753     # 回 20754
CMD_DELETE = 20755  # 回 20756
CMD_FIND = 20759    # 回 20760


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("攝影機提早斷線")
        buf += chunk
    return buf


def send(ip, cmd, body=b""):
    with socket.create_connection((ip, PORT), timeout=3) as s:
        s.settimeout(5)
        header = struct.pack("<II", HEADER + len(body), cmd) + bytes(HEADER - 8)
        s.sendall(header + body)
        reply = recv_exact(s, HEADER)
        total, rtype, status = struct.unpack("<III", reply[:12])
        data = recv_exact(s, total - HEADER) if total > HEADER else b""
    return rtype, status, data


def record(plate, date_begin="", date_end="", time_begin="", time_end="", kind=""):
    buf = bytearray(RECORD)
    for offset, text in ((0, plate), (16, date_begin), (27, date_end), (38, time_begin), (49, time_end), (60, kind)):
        b = text.encode("gb2312")
        buf[offset:offset + len(b)] = b
    return bytes(buf)


def field(data, offset, size):
    return data[offset:offset + size].split(b"\x00")[0].decode("gb2312", errors="replace")


def show(cmd_name, rtype, status, expect):
    ok = rtype == expect and status == 1
    print(f"{cmd_name}：{'成功' if ok else '失敗'}（回覆代碼 {rtype}，狀態 {status}）")
    return ok


def confirm_write(ip, action, plate):
    """寫入前確認（安全審查 2026-11-02 建議）：防止打錯 IP 改到有接柵欄的攝影機。加 --yes 可略過。"""
    if "--yes" not in sys.argv:
        print(f"⚠️ 即將對攝影機 {ip} {action} 白名單 {plate}。寫入白名單的車到場會自動開閘。")
        if input("確定是沒接柵欄的測試攝影機嗎？輸入 yes 繼續：").strip().lower() != "yes":
            print("已取消。")
            sys.exit(1)
    log_operation(ip, action, plate)


def log_operation(ip, action, plate):
    # 打包成 .exe 時 __file__ 在暫存資料夾，關掉就消失，所以改放在 .exe 旁邊
    base = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
    log_dir = os.path.join(base, "captures_hx_whitelist")
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "operation_log.txt"), "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{ip}\t{action}\t{plate}\n")


def main():
    args_all = [a for a in sys.argv[1:] if a != "--yes"]
    if len(args_all) < 2:
        print(__doc__)
        sys.exit(1)
    ip, action, args = args_all[0], args_all[1], args_all[2:]

    if action == "list":
        rtype, status, data = send(ip, CMD_LIST)
        if show("讀取白名單", rtype, status, CMD_LIST + 1):
            text = data.split(b"\x00")[0].decode("gb2312", errors="replace")
            print(text if text.strip() else "（白名單是空的）")
    elif action == "find" and len(args) == 1:
        rtype, status, data = send(ip, CMD_FIND, record(args[0].upper()))
        if show(f"查詢 {args[0]}", rtype, status, CMD_FIND + 1):
            print(f"  車牌 {field(data, 0, 16)}，日期 {field(data, 16, 11)}～{field(data, 27, 11)}，"
                  f"時間 {field(data, 38, 11)}～{field(data, 49, 11)}，類型 {field(data, 60, 11)}")
    elif action == "add" and len(args) == 3:
        plate, begin, end = args[0].upper(), args[1], args[2]
        confirm_write(ip, "新增／更新", plate)
        rtype, status, _ = send(ip, CMD_ADD, record(plate, begin, end, "00:00:00", "23:59:59", "W"))
        show(f"新增／更新 {plate}（{begin}～{end}）", rtype, status, CMD_ADD + 1)
    elif action == "delete" and len(args) == 1:
        confirm_write(ip, "刪除", args[0].upper())
        rtype, status, _ = send(ip, CMD_DELETE, record(args[0].upper()))
        show(f"刪除 {args[0]}", rtype, status, CMD_DELETE + 1)
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
