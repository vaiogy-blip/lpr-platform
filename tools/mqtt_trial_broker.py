# -*- coding: utf-8 -*-
"""
華夏攝影機 MQTT 試驗用「迷你伺服器」（不用另外安裝任何軟體）

目的：讓攝影機用 MQTT 連進這台電腦，記錄它
  1. 用什麼帳號、用戶端 ID 連線；
  2. 訂閱哪些主題（＝平台之後要把白名單指令送到哪裡）；
  3. 發佈什麼內容（心跳、車牌辨識的格式）。
預設只記錄、不送任何指令給攝影機。

之後要試白名單指令時，由 Claude 在 captures_mqtt_trial\\outbox 放一個 .txt 檔：
  第一行是主題，第二行以後是內容。這個程式會送給有訂閱那個主題的攝影機，送完改名成 .sent。
⚠️ 寫入白名單後攝影機會依後台設定自動開閘，送指令只能在沒接柵欄的桌上攝影機試。

用法（Windows）：
  1. 在這個資料夾開「命令提示字元」，執行：
         python mqtt_trial_broker.py
     第一次執行若跳出 Windows 防火牆提示，請選「允許」。
  2. 攝影機後台 MQTT 設定：伺服器填畫面顯示的 IP、PORT 1884、選「自建服務器」。
  3. 看畫面有沒有出現「攝影機連線」「訂閱主題」。按 Ctrl+C 結束。
"""

import os
import socket
import struct
import threading
import time
from datetime import datetime

from capture_server import local_ip, shorten

PORT = 1884  # 這台筆電的 1883 已經有 Mosquitto 在用（2026-11-02 發現），所以改用 1884
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures_mqtt_trial")
OUTBOX = os.path.join(OUT_DIR, "outbox")

PACKET_NAMES = {1: "CONNECT", 3: "PUBLISH", 6: "PUBREL", 8: "SUBSCRIBE", 10: "UNSUBSCRIBE", 12: "PINGREQ", 14: "DISCONNECT"}

clients = []  # 目前連線中的攝影機
clients_lock = threading.Lock()


def log(line):
    print(line)
    with open(os.path.join(OUT_DIR, "mqtt_log.txt"), "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}\t{line}\n")


def varint(n):
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def mqtt_str(s):
    b = s.encode("utf-8")
    return struct.pack("!H", len(b)) + b


def topic_matches(pattern, topic):
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)


class Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, n):
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def byte(self):
        return self.take(1)[0]

    def u16(self):
        return struct.unpack("!H", self.take(2))[0]

    def binary(self):
        return self.take(self.u16())

    def string(self):
        return self.binary().decode("utf-8", errors="replace")

    def varint(self):
        n, mult = 0, 1
        while True:
            b = self.byte()
            n += (b & 0x7F) * mult
            if not b & 0x80:
                return n
            mult *= 128

    def skip_props(self):
        self.take(self.varint())

    def rest(self):
        return self.data[self.pos:]


class Client:
    def __init__(self, sock, addr):
        self.sock, self.addr = sock, addr
        self.level = 4
        self.name = addr[0]
        self.subs = []
        self.send_lock = threading.Lock()

    def send(self, packet_type, flags, body):
        with self.send_lock:
            self.sock.sendall(bytes([(packet_type << 4) | flags]) + varint(len(body)) + body)

    def recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    def read_packet(self):
        header = self.recv_exact(1)[0]
        length, mult = 0, 1
        while True:
            b = self.recv_exact(1)[0]
            length += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        return header >> 4, header & 0x0F, self.recv_exact(length) if length else b""

    def v5(self):
        return self.level == 5

    def publish(self, topic, payload):
        body = mqtt_str(topic) + (b"\x00" if self.v5() else b"") + payload
        self.send(3, 0, body)

    def on_connect(self, r):
        proto = r.string()
        self.level = r.byte()
        flags = r.byte()
        keepalive = r.u16()
        if self.v5():
            r.skip_props()
        client_id = r.string()
        will = ""
        if flags & 0x04:
            if self.v5():
                r.skip_props()
            will = f"，遺囑主題 {r.string()}，遺囑內容 {r.binary().decode('utf-8', 'replace')}"
        user = r.string() if flags & 0x80 else "（無）"
        pw = r.binary() if flags & 0x40 else b""
        self.name = f"{client_id}@{self.addr[0]}"
        log("=" * 50)
        log(f"攝影機連線：用戶端ID {client_id}，帳號 {user}，密碼 {len(pw)} 字元，"
            f"協議 {proto} 版本 {self.level}，keepalive {keepalive} 秒{will}")
        log("=" * 50)
        self.send(2, 0, b"\x00\x00\x00" if self.v5() else b"\x00\x00")

    def on_subscribe(self, r):
        pid = r.u16()
        if self.v5():
            r.skip_props()
        codes = bytearray()
        while r.pos < len(r.data):
            topic = r.string()
            qos = r.byte() & 0x03
            self.subs.append(topic)
            codes.append(min(qos, 1))
            log(f"*** 訂閱主題：{topic}（QoS {qos}）  ← 平台送指令給攝影機就是用這個主題")
        self.send(9, 0, struct.pack("!H", pid) + (b"\x00" if self.v5() else b"") + bytes(codes))

    def on_unsubscribe(self, r):
        pid = r.u16()
        if self.v5():
            r.skip_props()
        count = 0
        while r.pos < len(r.data):
            topic = r.string()
            count += 1
            if topic in self.subs:
                self.subs.remove(topic)
            log(f"取消訂閱：{topic}")
        self.send(11, 0, struct.pack("!H", pid) + (b"\x00" + b"\x00" * count if self.v5() else b""))

    def on_publish(self, flags, r):
        qos = (flags >> 1) & 0x03
        topic = r.string()
        pid = r.u16() if qos else None
        if self.v5():
            r.skip_props()
        payload = r.rest()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        with open(os.path.join(OUT_DIR, f"{stamp}_publish.txt"), "wb") as f:
            f.write(f"topic: {topic}\r\nqos: {qos}\r\nfrom: {self.name}\r\n\r\n".encode("utf-8"))
            f.write(payload)
        text = payload.decode("utf-8", errors="replace")
        log(f"攝影機發佈 → 主題 {topic}（QoS {qos}，{len(payload)} bytes）：{shorten(text) if len(text) > 200 else text}")
        if qos == 1:
            self.send(4, 0, struct.pack("!H", pid))
        elif qos == 2:
            self.send(5, 0, struct.pack("!H", pid))

    def run(self):
        try:
            while True:
                ptype, flags, body = self.read_packet()
                r = Reader(body)
                if ptype == 1:
                    self.on_connect(r)
                elif ptype == 3:
                    self.on_publish(flags, r)
                elif ptype == 6:
                    self.send(7, 0, body[:2])
                elif ptype == 8:
                    self.on_subscribe(r)
                elif ptype == 10:
                    self.on_unsubscribe(r)
                elif ptype == 12:
                    self.send(13, 0, b"")
                elif ptype == 14:
                    log(f"攝影機主動斷線：{self.name}")
                    break
                elif ptype not in (4, 5, 7):
                    log(f"收到未處理的封包類型 {ptype}（{len(body)} bytes）")
        except (ConnectionError, OSError, IndexError, struct.error) as e:
            log(f"連線結束：{self.name}（{type(e).__name__}）")
        finally:
            with clients_lock:
                if self in clients:
                    clients.remove(self)
            self.sock.close()


def watch_outbox():
    while True:
        time.sleep(1)
        for name in sorted(os.listdir(OUTBOX)):
            if not name.endswith(".txt"):
                continue
            path = os.path.join(OUTBOX, name)
            with open(path, "rb") as f:
                topic, _, payload = f.read().partition(b"\n")
            topic = topic.decode("utf-8").strip()
            payload = payload.strip()
            with clients_lock:
                targets = [c for c in clients if any(topic_matches(s, topic) for s in c.subs)]
            if not targets:
                continue  # 攝影機還沒連上或還沒訂閱，先留著，連上後再送
            for c in targets:
                c.publish(topic, payload)
            log(f">>> 送出 {name} → 主題 {topic}，送給 {len(targets)} 台：{payload.decode('utf-8', 'replace')}")
            os.replace(path, path[:-4] + ".sent")


if __name__ == "__main__":
    os.makedirs(OUTBOX, exist_ok=True)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("0.0.0.0", PORT))
    server.listen(5)
    server.settimeout(1)
    threading.Thread(target=watch_outbox, daemon=True).start()
    print("MQTT 試驗伺服器：只記錄；只有 outbox 資料夾有檔案時才會送指令給攝影機。")
    print(f"攝影機 MQTT 伺服器請填：{local_ip()}，PORT {PORT}")
    print(f"記錄會存到：{OUT_DIR}")
    print("按 Ctrl+C 結束。")
    try:
        while True:
            try:
                sock, addr = server.accept()
            except socket.timeout:
                continue
            log(f"有裝置連進來：{addr[0]}:{addr[1]}")
            c = Client(sock, addr)
            with clients_lock:
                clients.append(c)
            threading.Thread(target=c.run, daemon=True).start()
    except KeyboardInterrupt:
        print("\n已結束。")
