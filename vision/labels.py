"""標正確答案（第 2 階段）：哪些車牌自動當答案、哪些要人看、存答案、匯出訓練資料。

- 自動答案：有車經過、連拍 3 張以上全部認出同一個字、格式正確、最高信心度 0.9 以上。
  其中約 5% 仍會抽出來給人看（抽查），用來量自動答案的錯誤率。
- 其餘有車牌字樣的都排進「待確認」：格式不符、信心度低、連拍不一致、張數太少、定時拍照（只有一張）。
- 人工答案存在 data/labels/labels.jsonl（只往後加，同一筆以最後一次為準，改錯可以重標）。
- 考卷：依「車牌字」分組，約 20% 的車牌整組留作考卷，永遠不拿來訓練
  （同一台車會天天出現，用車牌分組才不會讓考卷的車出現在訓練資料裡）。
  考卷只放人工確認過的；自動答案若剛好是考卷的車牌，放 holdout（不訓練也不考）。
- 匯出：把答案和切圖複製到 data/dataset/。訓練資料、人工答案的保存天數是 dataset_retention_days
  （預設 90，跟照片一樣，所以預設不會比照片留得久；要留更久練得更好，把它設大，這是個資取捨）。
"""
import csv
import datetime as dt
import hashlib
import json
import os
import shutil
import threading
from pathlib import Path

import stats
import storage
from plate_ocr import normalize, plate_format

AUTO_MIN_VOTES = 3
AUTO_MIN_CONF = 0.9
SPOT_CHECK_EVERY = 20     # 自動答案每 20 筆抽 1 筆給人看
EXAM_EVERY = 5            # 每 5 個車牌留 1 個當考卷
CROP_PAD = 0.25           # 收集程式存切圖時四周多留的比例（collector.Worker.process）

STATUS_OK = "ok"                 # 人工確認了車牌字
STATUS_UNREADABLE = "unreadable"  # 是車牌但看不清楚
STATUS_NOT_PLATE = "not_plate"    # 根本不是車牌（招牌、衣服上的字）

_lock = threading.Lock()


def _hash_mod(text, mod):
    return int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:8], 16) % mod


def is_exam_plate(text):
    return _hash_mod("exam:" + text, EXAM_EVERY) == 0


def event_key(day, event):
    return f"{day}/{event['event_id']}"


def labels_path(data_dir):
    return Path(data_dir) / "labels" / "labels.jsonl"


def load_labels(data_dir):
    """讀人工答案：{key: 最後一次標記}。"""
    out = {}
    path = labels_path(data_dir)
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                out[rec["key"]] = rec
            except (json.JSONDecodeError, KeyError):
                pass  # 斷電留下的半行
    return out


def auto_label(event):
    """符合自動答案條件就回傳車牌字，否則 None。"""
    if event.get("kind", "motion") != "motion":
        return None
    best = event.get("best")
    reads = event.get("reads") or []
    if not best or best["votes"] < AUTO_MIN_VOTES or best["max_conf"] < AUTO_MIN_CONF:
        return None
    if any(r["text"] != best["text"] for r in reads):
        return None  # 有一張認得不一樣 → 可能有錯，給人看
    return best["text"]


def review_reasons(event, key):
    """這筆要給人看的原因；空的代表不用看（自動答案）。"""
    reads = event.get("reads") or []
    if not reads or not any(r.get("crop") for r in reads):
        return []  # 沒有切圖可看
    if auto_label(event):
        return ["抽查自動答案"] if _hash_mod("spot:" + key, SPOT_CHECK_EVERY) == 0 else []
    reasons = []
    if event.get("kind") == "snapshot":
        reasons.append("定時拍照（只有一張）")
    if not any(r.get("format") for r in reads):
        reasons.append("格式不符")
    if max(r["conf"] for r in reads) < AUTO_MIN_CONF:
        reasons.append("信心度低")
    if len({r["text"] for r in reads}) > 1:
        reasons.append("連拍認得不一樣")
    elif event.get("kind", "motion") == "motion":
        best = event.get("best")
        if not best or best["votes"] < AUTO_MIN_VOTES:
            reasons.append("有把握的張數不到 3 張")
    return reasons or ["其他"]


def suggestion(event):
    best = event.get("best")
    if best:
        return best["text"]
    reads = event.get("reads") or []
    return max(reads, key=lambda r: r["conf"])["text"] if reads else ""


def to_item(day, event, key, reasons):
    reads = event.get("reads") or []
    return {
        "key": key,
        "day": day,
        "time": event.get("time", ""),
        "camera": event.get("camera", ""),
        "kind": event.get("kind", "motion"),
        "light": event.get("light", ""),
        "reasons": reasons,
        "suggest": suggestion(event),
        "full": event.get("full_frame"),
        "reads": [{"i": i, "crop": r.get("crop"), "text": r["text"], "conf": r["conf"],
                   "format": r.get("format")}
                  for i, r in enumerate(reads) if r.get("crop")],
    }


_cache = {}


def day_events(data_dir, day):
    """讀某天的紀錄；檔案沒變就用上次讀的（網頁每按一下都要查，90 天的資料不能每次重讀）。"""
    path = storage.captures_dir(data_dir) / day / "events.jsonl"
    try:
        st = path.stat()
    except OSError:
        return []
    sig = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    events = [e for e in stats.load_events(path.parent) if "event_id" in e]
    _cache[path] = (sig, events)
    return events


def iter_events(data_dir):
    for day in reversed(storage.list_days(data_dir)):
        for event in day_events(data_dir, day):
            yield day, event


def pending(data_dir, limit=30):
    """待確認清單：新的日子先，同一天照時間。回傳 (清單, 總數)。"""
    labels = load_labels(data_dir)
    items, total = [], 0
    for day in reversed(storage.list_days(data_dir)):
        day_items = []
        for event in day_events(data_dir, day):
            key = event_key(day, event)
            if key in labels:
                continue
            reasons = review_reasons(event, key)
            if reasons:
                day_items.append((day, event, key, reasons))
        total += len(day_items)
        for day_, event, key, reasons in day_items:
            if len(items) < limit:
                items.append(to_item(day_, event, key, reasons))
    return items, total


def find_item(data_dir, key):
    day, _, event_id = key.partition("/")
    if not storage.DAY_RE.match(day):
        return None
    for event in day_events(data_dir, day):
        if event.get("event_id") == event_id:
            return to_item(day, event, key, review_reasons(event, key) or ["自動答案"])
    return None


def save_label(data_dir, key, status, text="", include=None):
    """存一筆人工答案。include：要套用這個字的切圖編號（沒勾的代表是別台車或不是車牌）。"""
    day, _, event_id = key.partition("/")
    if not storage.DAY_RE.match(day) or not event_id:
        raise ValueError("編號不對")
    if status not in (STATUS_OK, STATUS_UNREADABLE, STATUS_NOT_PLATE):
        raise ValueError("狀態不對")
    text = normalize(text)
    if status == STATUS_OK and not (2 <= len(text) <= 8):
        raise ValueError("車牌字要 2～8 個英文或數字")
    if find_item(data_dir, key) is None:
        raise ValueError("找不到這筆")
    rec = {
        "key": key,
        "status": status,
        "text": text if status == STATUS_OK else "",
        "include": sorted({int(i) for i in include}) if include is not None else None,
        "time": dt.datetime.now().isoformat(timespec="seconds"),
    }
    path = labels_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with open(path, "a+b") as f:
            if f.tell() > 0:
                f.seek(-1, 2)
                if f.read(1) != b"\n":
                    f.write(b"\n")
            f.write(json.dumps(rec, ensure_ascii=False).encode("utf-8") + b"\n")
    return rec


def prune_labels(data_dir, cutoff):
    """人工答案裡有車牌字（個資）：拍攝日早於 cutoff 的一起刪掉，跟訓練資料同一個期限。"""
    path = labels_path(data_dir)
    if not path.exists():
        return 0
    with _lock:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        keep = [ln for ln in lines if _line_day(ln) >= cutoff]
        if len(keep) == len(lines):
            return 0
        tmp = path.with_name(f"labels.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(ln if ln.endswith("\n") else ln + "\n" for ln in keep)
        tmp.replace(path)
    return len(lines) - len(keep)


def _line_day(line):
    try:
        return json.loads(line)["key"][:10]
    except (json.JSONDecodeError, KeyError, TypeError):
        return ""  # 壞掉的半行也一起清掉


def summary(data_dir):
    labels = load_labels(data_dir)
    today = storage.today()
    human = labels
    auto = 0
    for day, event in iter_events(data_dir):
        key = event_key(day, event)
        if key not in labels and auto_label(event):
            auto += 1
    return {
        "human_total": len(human),
        "human_today": sum(1 for v in human.values() if v["time"].startswith(today)),
        "human_ok": sum(1 for v in human.values() if v["status"] == STATUS_OK),
        "auto_total": auto,
        "exam_plates": len({v["text"] for v in human.values()
                            if v["status"] == STATUS_OK and is_exam_plate(v["text"])}),
    }


# ---------------- 匯出訓練資料 ----------------

def dataset_dir(data_dir):
    return Path(data_dir) / "dataset"


def _rows_for_event(day, event, label):
    """一筆動靜／定時拍照 → 匯出的切圖列。label 是人工答案（可能是 None）。"""
    key = event_key(day, event)
    reads = event.get("reads") or []
    if label:
        source = "human"
        include = label["include"]
        if label["status"] == STATUS_OK:
            text = label["text"]
            split = "exam" if is_exam_plate(text) else "train"
        else:
            text, split = "", label["status"]  # 看不清楚／不是車牌：留著當難例或反例
    else:
        text = auto_label(event)
        if not text:
            return []
        source, include = "auto", None
        split = "holdout" if is_exam_plate(text) else "train"
    rows = []
    for i, r in enumerate(reads):
        if not r.get("crop") or (include is not None and i not in include):
            continue
        rows.append({
            "id": f"{key.replace('/', '_')}_{i}",
            "src": r["crop"], "day": day,
            "text": text, "ocr_text": r["text"], "ocr_conf": r["conf"],
            "format": plate_format(text) if text else None,
            "source": source, "split": split,
            "camera": event.get("camera", ""), "light": event.get("light", ""),
            "time": event.get("time", ""),
            "event": key, "order": i, "kind": event.get("kind", "motion"),
            "inner": inner_box(r["box"]),
        })
    return rows


def inner_box(box, ratio=CROP_PAD):
    """切圖存檔時四周多留了 25%（碰到畫面邊緣會被截掉）；算回車牌在切圖裡的位置。
    跟 plate_ocr.pad_rect 的算法一致：左上被截的量剛好就是 x1、y1 本身。"""
    x1, y1, x2, y2 = box
    px, py = int((x2 - x1) * ratio), int((y2 - y1) * ratio)
    ox, oy = min(px, x1), min(py, y1)
    return [ox, oy, ox + (x2 - x1), oy + (y2 - y1)]


def export_dataset(data_dir, dataset_retention_days):
    """把答案（人工＋自動）的切圖複製到 data/dataset/，並重寫 index.jsonl 與 labels.csv。
    已匯出的切圖不會因照片 90 天刪除而消失；資料集本身照 dataset_retention_days 刪舊的。"""
    data_dir = Path(data_dir)
    out = dataset_dir(data_dir)
    img_dir = out / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    index_path = out / "index.jsonl"

    index = {}
    if index_path.exists():
        with open(index_path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                    index[row["id"]] = row
                except (json.JSONDecodeError, KeyError):
                    pass

    # 資料集保存期限（先算好：過期的日子不用再複製，免得每天刪了又抄）
    cutoff = (dt.date.today() - dt.timedelta(
        days=max(int(dataset_retention_days), storage.MIN_RETENTION_DAYS))).isoformat()

    by_event = {}  # 事件 → 已匯出的列編號（先分好組，不然資料多了會很慢）
    for rid, row in index.items():
        by_event.setdefault(row.get("event"), []).append(rid)

    labels = load_labels(data_dir)
    copied = 0
    for day, event in iter_events(data_dir):
        if day < cutoff:
            continue
        key = event_key(day, event)
        rows = _rows_for_event(day, event, labels.get(key))
        # 重標過（例如改了字、取消勾選）：先清掉這筆舊的列
        new_ids = {r["id"] for r in rows}
        for old_id in by_event.get(key, []):
            if old_id not in new_ids:
                index.pop(old_id, None)
                (img_dir / f"{old_id}.jpg").unlink(missing_ok=True)
        for row in rows:
            dst = img_dir / f"{row['id']}.jpg"
            if not dst.exists():
                src = storage.captures_dir(data_dir) / day / row["src"]
                if not src.exists():
                    continue
                shutil.copyfile(src, dst)
                copied += 1
            row["file"] = f"images/{row['id']}.jpg"
            index[row["id"]] = row

    for rid in [k for k, v in index.items() if str(v.get("day", "")) < cutoff]:
        index.pop(rid)
        (img_dir / f"{rid}.jpg").unlink(missing_ok=True)
    prune_labels(data_dir, cutoff)

    rows = sorted(index.values(), key=lambda r: r["id"])
    # 暫存檔名帶程式編號：收集程式、網頁、訓練同時匯出時不會互相蓋到
    tmp = index_path.with_name(f"index.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(index_path)

    # labels.csv 只是方便用 Excel 看；被開著寫不進去也不算匯出失敗
    cols = ["file", "text", "split", "source", "light", "camera", "day", "ocr_text", "ocr_conf"]
    try:
        with open(out / "labels.csv", "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in rows:
                w.writerow([r.get(c, "") for c in cols])
    except OSError:
        pass

    counts = {}
    for r in rows:
        counts[r["split"]] = counts.get(r["split"], 0) + 1
    return {"copied": copied, "total": len(rows), "by_split": counts}
