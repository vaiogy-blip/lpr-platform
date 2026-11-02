"""車牌照片收集程式（第 1 階段）。

24 小時看著攝影機畫面，用兩種方式抓車牌：
  - 動靜：畫面有車經過就連拍幾張
  - 定時：每隔一段時間拍一張（停著的車、機車不會動，要靠這個）；同一個車牌一段時間內只存一次
找出車牌、先猜文字和信心度，存在本機。可同時看多支攝影機（共用一個辨識程序）。
只讀攝影機影像，不下任何指令給攝影機、不開閘、不碰白名單；不連任何雲端服務。

用法：
    python collector.py                    正式收集（讀 config.json 的攝影機）
    python collector.py --test-camera      每支攝影機連一下，各存一張 data/test_frame_名稱.jpg 看角度
    python collector.py --source 影片.mp4  用影片檔測試
    python collector.py --image 照片.jpg   只認一張照片的車牌
停止：在視窗按 Ctrl+C（或直接關掉視窗）。
"""
import argparse
import datetime as dt
import json
import logging
import logging.handlers
import os
import queue
import re
import sys
import threading
import time
from collections import Counter
from urllib.parse import quote

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2  # noqa: E402  （上面的環境變數要在載入 cv2 前設定）
import numpy as np  # noqa: E402

import stats  # noqa: E402
import storage  # noqa: E402
from plate_ocr import PlateReader, light_condition, pad_rect  # noqa: E402

log = logging.getLogger("collector")

VOTE_MIN_CONF = 0.8      # 信心度低於這個的不算票
MOTION_WIDTH = 640       # 偵測動靜時把畫面縮到這個寬度
WARMUP_FRAMES = 30       # 剛開始讓背景模型先學一下，不算動靜
MOTION_FPS_GAP = 0.12    # 偵測動靜每秒看約 8 張就夠
STATUS_EVERY_S = 60
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)),
                    *(f"LPT{i}" for i in range(10))}
CAMERA_KEYS = ("host", "port", "user", "password", "rtsp_path", "roi",
               "motion", "snapshot_interval_s")


# ---------------- 攝影機 ----------------

def camera_list(cfg):
    """把設定整理成攝影機清單。每支沒寫的欄位沿用最外層的 camera_*（錄影主機共用帳密時很方便）。"""
    base = {"host": cfg["camera_host"], "port": cfg["camera_port"], "user": cfg["camera_user"],
            "password": cfg["camera_password"], "rtsp_path": cfg["rtsp_path"], "roi": cfg["roi"],
            "motion": True, "snapshot_interval_s": cfg["snapshot_interval_s"]}
    cams = cfg.get("cameras") or [{"name": "cam1"}]
    out = []
    for i, c in enumerate(cams):
        cam = dict(base)
        cam.update({k: c[k] for k in CAMERA_KEYS if k in c})
        # 名稱會當資料夾名，去掉 Windows 不允許的字元
        name = re.sub(r'[\\/:*?"<>|\s.]', "_", str(c.get("name") or "")).strip("_")
        if not name or name.upper() in WINDOWS_RESERVED:
            name = f"cam{i + 1}"
        cam["name"] = name
        out.append(cam)
    return out


def rtsp_url(cam):
    user = quote(str(cam["user"]), safe="")
    pw = quote(str(cam["password"]), safe="")
    auth = f"{user}:{pw}@" if user else ""
    path = cam["rtsp_path"] if cam["rtsp_path"].startswith("/") else "/" + cam["rtsp_path"]
    return f"rtsp://{auth}{cam['host']}:{cam['port']}{path}"


def where(cam):
    """紀錄、畫面只印攝影機名稱和位址；網址不印（有些品牌的帳密寫在路徑裡）。"""
    return f"{cam['name']}（{cam['host']}:{cam['port']}）"


def open_capture(url):
    params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 10000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000]
    return cv2.VideoCapture(url, cv2.CAP_FFMPEG, params)


class RtspSource:
    """背景一直讀串流、只留最新一張（處理不及時就丟舊的），斷線自動重連。"""
    realtime = True

    def __init__(self, cam, counters):
        self.name = where(cam)
        self.url = rtsp_url(cam)
        self.counters = counters
        self.lock = threading.Lock()
        self.frame, self.ts = None, 0.0
        self.connected = False
        self.stopped = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while not self.stopped:
            try:
                self._loop()
            except Exception:
                # 任何意外都不能讓這一路悄悄停掉
                self.connected = False
                log.exception("[%s] 讀取攝影機時出錯，10 秒後重來", self.name)
                time.sleep(10)

    def _loop(self):
        # 斷線時間由主程式每分鐘依 connected 累計，這裡只管連線
        wait = 5
        while not self.stopped:
            cap = open_capture(self.url)
            got_frame = False
            if cap.isOpened():
                while not self.stopped:
                    ok, frame = cap.read()
                    if not ok:
                        break
                    with self.lock:
                        self.frame, self.ts = frame, time.time()
                    if not got_frame:
                        # 真的拿到畫面才算連上（帳號對但路徑錯時會開得起來卻讀不到）
                        log.info("[%s] 攝影機已連線", self.name)
                        self.connected, got_frame, wait = True, True, 5
            cap.release()
            self.connected = False
            if self.stopped:
                break
            if got_frame:
                log.warning("[%s] 攝影機斷線，重新連線", self.name)
                time.sleep(2)
            else:
                log.warning("[%s] 連不上攝影機或拿不到畫面，%d 秒後重試", self.name, wait)
                time.sleep(wait)
                wait = min(wait * 2, 300)

    def read(self, last_ts):
        """有比上次新的畫面就給，沒有就回 None（不等待，多支攝影機輪流看）。"""
        with self.lock:
            if self.frame is not None and self.ts >= last_ts + MOTION_FPS_GAP:
                return self.frame, self.ts
        return None, last_ts

    def close(self):
        self.stopped = True


class FileSource:
    """影片檔：一張一張照順序讀，時間用影格數換算。"""
    realtime = False

    def __init__(self, path):
        self.name = "test"
        self.cap = cv2.VideoCapture(str(path))
        if not self.cap.isOpened():
            raise SystemExit(f"打不開影片：{path}")
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 25
        self.index = 0
        self.connected = True
        self.ended = False

    def read(self, last_ts):
        # 影片每秒 25 張，偵測動靜用 1/4 就夠（跟即時模式差不多）
        frame = None
        for _ in range(4):
            ok, f = self.cap.read()
            if not ok:
                self.ended = True
                break
            frame = f
            self.index += 1
        return frame, self.index / self.fps

    def close(self):
        self.cap.release()


# ---------------- 每天的計數（略過、斷線） ----------------

class Counters:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.lock = threading.Lock()
        self.days = {}

    def _day(self, day):
        if day not in self.days:
            path = storage.captures_dir(self.data_dir) / day / "counters.json"
            self.days[day] = Counter(storage.read_json(path, {}))
        return self.days[day]

    def add_dropped(self):
        with self.lock:
            self._day(storage.today())["dropped_events"] += 1

    def add_offline(self, seconds):
        with self.lock:
            self._day(storage.today())["offline_seconds"] += int(seconds)

    def flush(self):
        with self.lock:
            for day, c in list(self.days.items()):
                d = storage.captures_dir(self.data_dir) / day
                d.mkdir(parents=True, exist_ok=True)
                storage.write_json(d / "counters.json", dict(c))
            # 只留今天的在記憶體
            self.days = {k: v for k, v in self.days.items() if k == storage.today()}


# ---------------- 處理每次動靜／定時拍照 ----------------

def iou(a, b):
    """兩個框重疊的比例（0～1）。"""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def append_line(path, line):
    """寫一行紀錄。上次被強制關掉、最後一行沒寫完時，先補換行，免得連累這一筆。"""
    with open(path, "a+b") as f:
        if f.tell() > 0:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                f.write(b"\n")
        f.write(line.encode("utf-8") + b"\n")


def vote(reads):
    """同一個字出現幾次（只算格式正確、信心度夠的）。"""
    tally = Counter()
    best_conf = {}
    for r in reads:
        if r["format"] and r["conf"] >= VOTE_MIN_CONF:
            tally[r["text"]] += 1
            best_conf[r["text"]] = max(best_conf.get(r["text"], 0), r["conf"])
    if not tally:
        return None
    text = max(tally, key=lambda t: (tally[t], best_conf[t]))
    return {"text": text, "votes": tally[text], "max_conf": best_conf[text]}


class Worker:
    """背景認字、存檔。佇列滿了就略過該次（並記數），不讓攝影機那邊卡住。"""

    def __init__(self, cfg, data_dir, counters):
        self.cfg = cfg
        self.data_dir = data_dir
        self.counters = counters
        self.queue = queue.Queue(maxsize=40)  # 每個最多 5 張 JPEG，40 個約占數百 MB 以內
        self.reader = PlateReader(cfg["min_plate_height"])
        self.low_disk = False
        self.today = Counter()
        self.today_day = None
        self.seq = 0
        # 最近存過的車牌 [(攝影機, 字, 框, 時間)]，定時拍照用來避免一直存同一台停著的車
        self.recent = []
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, event):
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            self.counters.add_dropped()

    def _run(self):
        while True:
            event = self.queue.get()
            try:
                self.process(event)
            except Exception:
                log.exception("處理一次拍照時出錯（略過，程式繼續）")
            finally:
                self.queue.task_done()

    def _seen(self, camera, r):
        """同一支攝影機最近存過同樣的字，或幾乎同一個位置（停著的車字認得略不同時）。"""
        return any(c == camera and (t == r["text"] or iou(b, r["box"]) > 0.5)
                   for c, t, b, _ in self.recent)

    def _remember(self, camera, reads, now):
        window = self.cfg["repeat_save_s"]
        self.recent = [x for x in self.recent if now - x[3] < window]
        self.recent += [(camera, r["text"], r["box"], now) for r in reads]

    def _drop_repeats(self, camera, reads, now):
        """定時拍照：repeat_save_s 內存過的車就不再存。"""
        self._remember(camera, [], now)
        fresh = [r for r in reads if not self._seen(camera, r)]
        self._remember(camera, fresh, now)
        return fresh

    def process(self, event):
        start, camera, kind = event["start"], event["camera"], event["kind"]
        frames = [cv2.imdecode(np.frombuffer(j, np.uint8), cv2.IMREAD_COLOR) for j in event["jpegs"]]
        reads = []
        for i, frame in enumerate(frames):
            for j, r in enumerate(self.reader.read(frame, event["roi"])):
                r["frame"] = i
                reads.append(r)
        if kind == "snapshot":
            reads = self._drop_repeats(camera, reads, time.time())
            if not reads:
                return  # 都是剛剛存過的車（或沒有車），不用記
        else:
            self._remember(camera, reads, time.time())

        day = start.date().isoformat()
        self.seq += 1
        event_id = f"{start:%H%M%S}_{self.seq:04d}"
        day_dir = storage.captures_dir(self.data_dir) / day
        rel = f"{camera}/{event_id}"
        light, sat, bright = light_condition(frames[0])
        record = {
            "event_id": event_id,
            "camera": camera,
            "kind": kind,
            "time": start.isoformat(timespec="seconds"),
            "light": light, "saturation": sat, "brightness": bright,
            "frames": len(frames),
            "reads": reads,
            "best": vote(reads),
            "full_frame": None,
        }

        # 沒找到車牌就不存任何照片（路人、樹影只留一筆「有動靜」的紀錄）
        if reads and not self.low_disk:
            ev_dir = day_dir / rel
            ev_dir.mkdir(parents=True, exist_ok=True)
            for j, r in enumerate(reads):
                r["crop"] = f"{rel}/f{r['frame']}_{j}.jpg"
                x1, y1, x2, y2 = pad_rect(r["box"], frames[r["frame"]].shape, 0.25)
                storage.imwrite(day_dir / r["crop"], frames[r["frame"]][y1:y2, x1:x2], 95)
            # 完整畫面可能拍到路人：只有確定像車牌（格式正確、信心度夠）時才存
            sure = [r for r in reads if r["format"] and r["conf"] >= VOTE_MIN_CONF]
            if self.cfg["save_full_frame"] and sure:
                best = max(sure, key=lambda r: r["conf"])
                storage.imwrite(ev_dir / "full.jpg", frames[best["frame"]], 85)
                record["full_frame"] = f"{rel}/full.jpg"
                record["full_frame_index"] = best["frame"]
        else:
            for r in reads:
                r["crop"] = None

        day_dir.mkdir(parents=True, exist_ok=True)
        append_line(day_dir / "events.jsonl", json.dumps(record, ensure_ascii=False))

        if day != self.today_day:
            self.today, self.today_day = Counter(), day
        self.today[kind] += 1
        if reads and kind == "motion":
            self.today["motion_with_plate"] += 1

    def drain(self, timeout):
        end = time.time() + timeout
        while self.queue.unfinished_tasks and time.time() < end:
            time.sleep(0.2)


# ---------------- 偵測動靜 ----------------

class MotionDetector:
    def __init__(self, roi, threshold):
        self.roi = roi
        self.threshold = threshold
        self.bg = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=25, detectShadows=True)
        self.kernel = np.ones((3, 3), np.uint8)
        self.mask = None
        self.frames = 0

    def update(self, frame):
        h, w = frame.shape[:2]
        small = cv2.resize(frame, (MOTION_WIDTH, int(h * MOTION_WIDTH / w)))
        # 用彩色比對：有些車的灰階亮度跟路面差不多，只看灰階會漏掉
        small = cv2.GaussianBlur(small, (5, 5), 0)
        if self.mask is None or self.mask.shape != small.shape[:2]:
            sh, sw = small.shape[:2]
            self.mask = np.zeros((sh, sw), np.uint8)
            x1, y1, x2, y2 = self.roi
            self.mask[int(y1 * sh):int(y2 * sh), int(x1 * sw):int(x2 * sw)] = 255
            self.area = max(int(np.count_nonzero(self.mask)), 1)
        fg = self.bg.apply(small)
        fg = cv2.morphologyEx((fg > 200).astype(np.uint8), cv2.MORPH_OPEN, self.kernel)
        self.frames += 1
        ratio = np.count_nonzero(fg & (self.mask > 0)) / self.area
        return self.frames > WARMUP_FRAMES and ratio >= self.threshold


class CameraState:
    """一支攝影機的動靜狀態與定時拍照時間。"""

    def __init__(self, cam, source, cfg):
        self.cam = cam
        self.source = source
        self.cfg = cfg
        self.detector = MotionDetector(cam["roi"], cfg["motion_threshold"]) if cam["motion"] else None
        self.event = None
        self.last_ts = 0.0
        self.last_motion = 0.0
        self.last_snapshot = 0.0

    def _new_event(self, kind):
        return {"start": dt.datetime.now(), "camera": self.cam["name"], "kind": kind,
                "roi": self.cam["roi"], "t0": self.last_ts, "last_sample": -1e9, "jpegs": []}

    def step(self, worker):
        """看一張新畫面；回傳 True 表示有處理到新畫面。"""
        frame, ts = self.source.read(self.last_ts)
        if frame is None:
            return False
        self.last_ts = ts
        cfg = self.cfg

        interval = self.cam["snapshot_interval_s"]
        if interval and ts - self.last_snapshot >= interval:
            self.last_snapshot = ts
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
            if ok:
                snap = self._new_event("snapshot")
                snap["jpegs"].append(buf.tobytes())
                worker.submit(snap)

        if not self.detector:
            return True
        moving = self.detector.update(frame)
        if moving:
            self.last_motion = ts
            if self.event is None:
                self.event = self._new_event("motion")
        if self.event is not None:
            ev = self.event
            if (moving and len(ev["jpegs"]) < cfg["max_frames_per_event"]
                    and ts - ev["last_sample"] >= cfg["sample_interval_s"]):
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
                if ok:
                    ev["jpegs"].append(buf.tobytes())
                    ev["last_sample"] = ts
            if ts - self.last_motion > cfg["event_gap_s"] or ts - ev["t0"] > cfg["max_event_s"]:
                if ev["jpegs"]:
                    worker.submit(ev)
                self.event = None
        return True

    def finish(self, worker):
        if self.event and self.event["jpegs"]:
            worker.submit(self.event)
        self.event = None


# ---------------- 主程式 ----------------

def setup_logging(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(data_dir / "collector.log", maxBytes=5_000_000,
                                              backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    log.setLevel(logging.INFO)


def single_instance(data_dir):
    """同一個資料夾只能跑一份（不小心點兩次時）。"""
    import msvcrt
    f = open(data_dir / "collector.lock", "w")
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        raise SystemExit("收集程式已經在跑了（同一個資料夾只能開一個）。")
    return f


def daily_maintenance(cfg, data_dir):
    """產生昨天以前的日報、刪除過期照片。"""
    # 每一步各自保護：日報失敗不能擋住刪除，也不能讓程式停掉
    try:
        for day in stats.fill_missing_reports(data_dir, storage.today()):
            log.info("已產生日報 %s", day)
    except Exception:
        log.exception("產生日報時出錯（略過，程式繼續）")
    try:
        for f in data_dir.glob("test_frame_*.jpg"):
            if time.time() - f.stat().st_mtime > 86400:
                f.unlink(missing_ok=True)  # 測試畫面可能拍到人，留一天就刪
        removed = storage.delete_expired(data_dir, cfg["retention_days"])
        if removed:
            log.info("已刪除超過 %s 天的資料：%s", cfg["retention_days"], "、".join(removed))
    except Exception:
        log.exception("刪除過期資料時出錯（明天再試，程式繼續）")


def write_status(path, states, worker, started):
    free = storage.free_gb(worker.data_dir)
    worker.low_disk = free < worker.cfg["min_free_gb"]
    today = worker.today if worker.today_day == storage.today() else Counter()
    status = {
        "started": started,
        "updated": dt.datetime.now().isoformat(timespec="seconds"),
        "cameras": {s.cam["name"]: s.source.connected for s in states},
        "today_motion": today["motion"],
        "today_motion_with_plate": today["motion_with_plate"],
        "today_snapshot_new": today["snapshot"],
        "queue": worker.queue.qsize(),
        "free_gb": round(free, 1),
        "low_disk": worker.low_disk,
    }
    storage.write_json(path, status)
    if worker.low_disk:
        log.warning("硬碟剩 %.1f GB，低於 %s GB，暫停存照片（紀錄照寫）", free, worker.cfg["min_free_gb"])
    online = "、".join(f"{n}{'✓' if ok else '✗斷線'}" for n, ok in status["cameras"].items())
    log.info("狀態：%s｜今天有車經過 %d 次（找到車牌 %d）、定時拍到新車牌 %d 次｜待處理 %d｜硬碟剩 %.0f GB",
             online, status["today_motion"], status["today_motion_with_plate"],
             status["today_snapshot_new"], status["queue"], free)


def run(cfg, sources, data_dir, max_seconds=None):
    """sources：[(攝影機設定, 來源)]。"""
    counters = Counters(data_dir)
    worker = Worker(cfg, data_dir, counters)
    states = [CameraState(cam, src, cfg) for cam, src in sources]
    for s in states:
        if isinstance(s.source, str):
            s.source = RtspSource(s.cam, counters)
    realtime = all(s.source.realtime for s in states)
    started = dt.datetime.now().isoformat(timespec="seconds")
    status_path = data_dir / "status.json"
    daily_maintenance(cfg, data_dir)
    write_status(status_path, states, worker, started)

    last_status = time.time()
    current_day = storage.today()
    run_until = time.time() + max_seconds if max_seconds else None
    log.info("開始收集（%d 支攝影機）。按 Ctrl+C 停止。", len(states))
    try:
        while not all(getattr(s.source, "ended", False) for s in states):
            if run_until and time.time() > run_until:
                break
            busy = False
            for s in states:
                try:
                    busy = s.step(worker) or busy
                except Exception:
                    # 一支攝影機的一張畫面出錯，不能讓整支程式停掉
                    log.exception("[%s] 處理畫面時出錯（略過，程式繼續）", s.cam["name"])
                    time.sleep(1)
            if not busy:
                time.sleep(0.03)

            now = time.time()
            if now - last_status >= STATUS_EVERY_S:
                for s in states:
                    if not s.source.connected:
                        counters.add_offline(now - last_status)
                last_status = now
                try:
                    counters.flush()
                    write_status(status_path, states, worker, started)
                except Exception:
                    log.exception("寫狀態時出錯（可能硬碟滿了），程式繼續")
                if storage.today() != current_day:
                    current_day = storage.today()
                    worker.drain(30)  # 讓昨天最後幾筆先寫完，再產生昨天的日報
                    daily_maintenance(cfg, data_dir)
            if not realtime and worker.queue.qsize() > 30:
                worker.drain(5)  # 測試影片：等一下處理，不要略過
    except KeyboardInterrupt:
        log.info("收到停止指令，收尾中…")
    finally:
        for s in states:
            s.finish(worker)
            s.source.close()
        worker.drain(30 if realtime else 120)
        try:
            counters.flush()
            write_status(status_path, states, worker, started)
        except Exception:
            log.exception("收尾寫檔時出錯")
        log.info("已停止。")


def test_cameras(cfg, data_dir):
    reader = None
    for cam in camera_list(cfg):
        print(f"\n[{where(cam)}] 連線中…")
        cap = open_capture(rtsp_url(cam))
        if not cap.isOpened():
            print("  連不上。請檢查 IP、帳號密碼、rtsp_path，以及有沒有開 RTSP。")
            continue
        t0, n, frame = time.time(), 0, None
        while time.time() - t0 < 5:
            ok, f = cap.read()
            if ok:
                frame, n = f, n + 1
        cap.release()
        if frame is None:
            print("  有連上但拿不到畫面。")
            continue
        out = data_dir / f"test_frame_{cam['name']}.jpg"
        storage.imwrite(out, frame)
        h, w = frame.shape[:2]
        light = light_condition(frame)
        print(f"  成功：解析度 {w}x{h}，約每秒 {n / 5:.0f} 張，目前判斷為{'晚上' if light[0] == 'night' else '白天'}")
        print(f"  畫面存在 {out}（收集程式會在一天後自動刪除）")
        reader = reader or PlateReader(cfg["min_plate_height"])
        for r in reader.read(frame, cam["roi"]):
            print(f"  畫面中的車牌字樣：{r['text']}  信心度 {r['conf']}  高 {r['box'][3] - r['box'][1]} 像素")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="車牌照片收集程式（只記錄、不開閘）")
    ap.add_argument("--config", help="設定檔（預設是程式旁的 config.json）")
    ap.add_argument("--test-camera", action="store_true", help="測試每支攝影機連線並各存一張畫面")
    ap.add_argument("--source", help="用影片檔代替攝影機（測試用）")
    ap.add_argument("--image", help="只認一張照片的車牌")
    ap.add_argument("--max-seconds", type=float, help="跑幾秒就停（測試用）")
    ap.add_argument("--report", metavar="日期", help="重新產生某一天的日報，例如 2026-11-05")
    args = ap.parse_args()

    cfg = storage.load_config(args.config)
    data_dir = storage.resolve_data_dir(cfg)
    data_dir.mkdir(parents=True, exist_ok=True)

    if args.report:
        if not storage.DAY_RE.match(args.report):
            raise SystemExit("日期格式要像 2026-11-05")
        print(stats.report_text(stats.write_report(data_dir, args.report)))
        return
    if args.image:
        frame = storage.imread(args.image)
        if frame is None:
            raise SystemExit(f"讀不到照片：{args.image}")
        print("白天/晚上：", light_condition(frame))
        for r in PlateReader(cfg["min_plate_height"]).read(frame, cfg["roi"]):
            print(r)
        return

    cams = camera_list(cfg)
    if not args.source and any(not c["host"] or c["host"].endswith("xxx") for c in cams):
        raise SystemExit("config.json 還沒填攝影機 IP（複製 config.example.json 成 config.json 再填）。")
    if args.test_camera:
        test_cameras(cfg, data_dir)
        return

    _lock = single_instance(data_dir)  # noqa: F841  （程式結束前都要握著）
    setup_logging(data_dir)
    if args.source:
        run(cfg, [(cams[0], FileSource(args.source))], data_dir, args.max_seconds)
    else:
        run(cfg, [(cam, "rtsp") for cam in cams], data_dir, args.max_seconds)


if __name__ == "__main__":
    main()
