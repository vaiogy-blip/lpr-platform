"""模擬管制車道測試（第 4 階段）。**只判斷、只記錄，不送任何開閘指令。**

用法（打包版把 python lane_test.py 換成 plate_collector.exe --lane-test）：
    python lane_test.py                     看 config.json 第一支攝影機
    python lane_test.py --camera 金爐        指定攝影機
    python lane_test.py --source 影片.mp4    用錄好的影片（例如手機拍公司車開過來停下）
    python lane_test.py --model rapidocr    指定用現成模型（預設：有練好的就用我們的）

做法：畫面有動靜（車來了）開始計時 → 每張畫面認車牌 → 照 voting.Voter 投票（3 票、低信心不算、
3 秒湊不到交給人）→ 比對測試白名單 data/lane_whitelist.txt（一行一個車牌，只用來測試）。
每次結果顯示在畫面、記在 data/captures/日期/lane_test.jsonl，最好的那張切圖存在 lane_test/ 資料夾
（跟其他照片一樣 90 天後刪除）。重點數字是「從車出現到確定」花幾秒。
"""
import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import collector
import storage
import voting
from plate_ocr import PlateReader, light_condition, normalize, pad_rect

CLEAR_S = 5.0     # 確定之後，這個車牌連續幾秒沒出現才算車走了、準備下一台


def load_whitelist(data_dir):
    path = Path(data_dir) / "lane_whitelist.txt"
    if not path.exists():
        return set()
    out = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        plate = normalize(line.split("#")[0])
        if plate:
            out.add(plate)
    return out


def model_paths(data_dir, choice):
    """回傳 (認字模型, 過濾器, 說明)。"""
    if choice == "rapidocr":
        return None, None, "現成模型（RapidOCR）"
    current = storage.read_json(Path(data_dir) / "models" / "current.json", {})
    if current.get("rec_onnx"):
        mdir = Path(data_dir) / "models"
        flt = mdir / current["filter_onnx"] if current.get("filter_onnx") else None
        return mdir / current["rec_onnx"], flt, f"我們的模型（{current['rec_onnx']}）"
    if choice == "ours":
        raise SystemExit("還沒有練好的模型（data/models/current.json），先跑訓練。")
    return None, None, "現成模型（RapidOCR，還沒有練好的模型）"


class LaneLog:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.seq = 0

    def write(self, decision, best, frame):
        now = dt.datetime.now()
        day_dir = storage.captures_dir(self.data_dir) / now.date().isoformat()
        (day_dir / "lane_test").mkdir(parents=True, exist_ok=True)
        self.seq += 1
        rec = dict(decision, time=now.isoformat(timespec="seconds"), light=light_condition(frame)[0])
        if best is not None:
            name = f"lane_test/{now:%H%M%S}_{self.seq:03d}.jpg"
            x1, y1, x2, y2 = pad_rect(best["box"], frame.shape, 0.25)
            storage.imwrite(day_dir / name, frame[y1:y2, x1:x2], 95)
            rec["crop"] = name
        collector.append_line(day_dir / "lane_test.jsonl", json.dumps(rec, ensure_ascii=False))


def run(cfg, data_dir, source, roi, reader, whitelist, max_seconds=None):
    detector = collector.MotionDetector(roi, cfg["motion_threshold"])
    voter = voting.Voter(whitelist=whitelist)
    lane_log = LaneLog(data_dir)
    t_start = None            # 這台車開始的時間（第一次有動靜）
    best = None               # 這台車信心度最高的那一筆（存切圖用）
    last_seen = 0.0           # 已確定的車牌最後一次出現的時間
    last_ts = 0.0
    times = []
    end = time.time() + max_seconds if max_seconds else None
    print("車道測試開始（只判斷、不開閘）。按 Ctrl+C 停止。")
    try:
        while not getattr(source, "ended", False):
            if end and time.time() > end:
                break
            frame, ts = source.read(last_ts)
            if frame is None:
                time.sleep(0.02)
                continue
            last_ts = ts
            moving = detector.update(frame)
            if voter.decision:
                # 已經確定了：等這台車離開（車牌消失一陣子）
                reads = reader.read(frame, roi)
                if any(r["text"] == voter.decision["plate"] for r in reads):
                    last_seen = ts
                if ts - last_seen > CLEAR_S and not moving:
                    voter.reset()
                    t_start, best = None, None
                    print("（車走了，等下一台）")
                continue
            if t_start is None and not moving:
                continue
            if t_start is None:
                t_start = ts
            reads = reader.read(frame, roi)
            for r in reads:
                if best is None or r["conf"] > best["conf"]:
                    best = dict(r)
                    best_frame = frame
            # Voter 的 3 秒從第一次認到車牌字算；另外記「從車出現（有動靜）到確定」的總時間
            decision = voter.feed(ts, [(r["text"], r["conf"]) for r in reads])
            if not decision and voter.t0 is None and ts - t_start > voter.timeout_s * 3:
                t_start = None   # 有動靜但一直沒看到車牌（路人、樹影），當作沒車
                continue
            if decision:
                decision["total_s"] = round(ts - t_start, 2)
                last_seen = ts
                times.append(decision["total_s"])
                name = voting.RESULT_NAMES[decision["result"]]
                plate = decision["plate"] or "-"
                ref = f"（白名單裡的 {decision['whitelist_ref']}）" if decision["result"] == "near" else ""
                print(f"[{dt.datetime.now():%H:%M:%S}] {plate}：{name}{ref}，"
                      f"從車出現到確定 {decision['total_s']} 秒（看到車牌後 {decision['elapsed_s']} 秒）、"
                      f"看了 {decision['frames']} 張")
                lane_log.write(decision, best, best_frame if best else frame)
                # 交給人之後也一樣：等畫面靜下來 5 秒、下一台有動靜才重新開始，不會同一台一直重報
    except KeyboardInterrupt:
        pass
    finally:
        if t_start is not None and not voter.decision:
            d = voter.finish(last_ts)
            if d:
                d["total_s"] = round(last_ts - t_start, 2)
                times.append(d["total_s"])
                lane_log.write(d, best, best_frame if best else frame)
        source.close()
    if times:
        times.sort()
        print(f"\n共 {len(times)} 台；從車出現到確定：中位數 {times[len(times) // 2]} 秒、"
              f"最慢 {times[-1]} 秒（目標 2 秒以內）")
    return times


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="模擬管制車道測試（只判斷、不開閘）")
    ap.add_argument("--config")
    ap.add_argument("--camera", help="攝影機名稱（config.json 的 cameras.name）")
    ap.add_argument("--source", help="用影片檔")
    ap.add_argument("--model", choices=["auto", "ours", "rapidocr"], default="auto")
    ap.add_argument("--max-seconds", type=float)
    args = ap.parse_args(argv)
    cfg = storage.load_config(args.config)
    data_dir = storage.resolve_data_dir(cfg)
    data_dir.mkdir(parents=True, exist_ok=True)
    cams = collector.camera_list(cfg)
    cam = cams[0]
    if args.camera:
        match = [c for c in cams if c["name"] == args.camera]
        if not match:
            raise SystemExit(f"找不到攝影機「{args.camera}」，有：{'、'.join(c['name'] for c in cams)}")
        cam = match[0]
    rec, flt, desc = model_paths(data_dir, args.model)
    # 車道上車很近，找字時把畫面縮到長邊 960 就夠，速度快很多（lane_det_max_side 可改，0 = 不縮）
    reader = PlateReader(cfg["min_plate_height"], rec, flt, cfg.get("lane_det_max_side", 960) or None)
    whitelist = load_whitelist(data_dir)
    print(f"認字：{desc}；測試白名單 {len(whitelist)} 筆（data/lane_whitelist.txt）")
    if args.source:
        source = collector.FileSource(args.source, paced=True)
    else:
        if not cam["host"] or cam["host"].endswith("xxx"):
            raise SystemExit("config.json 還沒填攝影機 IP。")
        source = collector.RtspSource(cam, collector.Counters(data_dir))
    run(cfg, data_dir, source, cam["roi"], reader, whitelist, args.max_seconds)


if __name__ == "__main__":
    main()
