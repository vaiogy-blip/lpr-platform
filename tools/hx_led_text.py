# -*- coding: utf-8 -*-
"""
直接送文字到字幕機（經攝影機 485 透傳，不靠攝影機自己的字幕機設定）

協議從老闆停車場軟體讀出（2026-11-02，只讀分析）：
  - 字幕機指令（com.yszb.sounds.impl.KFVoiceIMPL，系統預設的 kf 字幕機）：
      00 64 FF FF ＋ 指令 ＋ 資料長度 ＋ 資料 ＋ CRC16（Modbus，初值 FFFF，低位元組在前）
      顯示一行文字＝指令 0x62：17 位元組設定（行號、進出場特效、顏色…）＋ 文字長度（2 位元組，低在前）＋ GBK 文字
  - 經攝影機轉送（ice_ipcsdk.jar）：TCP 8117，指令 4151＝485-1 透傳、4163＝485-2（後台的 rs232_* 欄位）透傳

用法：
  python hx_led_text.py 192.168.1.100 2 1 測試123   （485-2、第 1 行）
"""

import struct
import sys

from hx_whitelist import send

PORTS = {1: 4151, 2: 4163}
COLORS = {"red": (0xFF, 0, 0, 0), "green": (0, 0xFF, 0, 0), "yellow": (0xFF, 0xFF, 0, 0)}


def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def frame(cmd, data):
    body = bytes([0x00, 0x64, 0xFF, 0xFF, cmd, len(data)]) + data
    return body + struct.pack("<H", crc16(body))


def show_text(line, text, color="green", stay=3, repeat=10):
    """照 KFVoiceIMPL.getDisplayTag：行號、進場特效 21、速度 1、0、停留、出場特效 21、1、3、重複次數、文字色 RGBA、背景色。"""
    tag = bytes([line - 1, 21, 1, 0, stay, 21, 1, 3, repeat]) + bytes(COLORS[color]) + bytes(4)
    raw = text.encode("gbk")  # 用 GBK 才能送繁體字（字幕機本來就顯示繁體「招財進寶」）
    return frame(0x62, tag + struct.pack("<H", len(raw)) + raw)


def send_to_led(ip, port_no, packet):
    rtype, status, _ = send(ip, PORTS[port_no], packet)
    return rtype == PORTS[port_no] + 1 and status == 1, rtype, status


if __name__ == "__main__":
    ip, port_no, line, text = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    color = sys.argv[5] if len(sys.argv) > 5 else "green"
    pkt = show_text(line, text, color)
    print("送出：", pkt.hex(" "))
    print("攝影機回覆：", send_to_led(ip, port_no, pkt))
