"""考試與成績單（第 3 階段）。

考卷 = 人工確認過、而且車牌被分到「考卷組」的切圖（labels.is_exam_plate），從不拿來訓練。
每次考：
  - 現成模型（RapidOCR，第 1 階段在用的）
  - 我們自己練的模型（目前使用的那版；訓練時另外加考新練好的那版）
分兩種算法：
  1. 單張：每張切圖自己判斷。信心度夠＋格式對 → 給答案；否則 → 認不出來（交給人）。
  2. 連拍投票：同一台車的幾張照片照 voting.Voter 的規則投票（3 票、低信心不算票）。
每種都分白天、晚上，算「全對」「認錯」「認不出來」。認錯比認不出來嚴重。
成績單放 data/reports/scorecard_日期.txt／.json。
"""
import datetime as dt
from collections import defaultdict
from pathlib import Path

import labels
import recognizer as R
import storage
import voting
from plate_ocr import normalize, plate_format

ACCEPT_CONF = 0.9          # 單張：信心度到這個才給答案
MIN_EXAM_VEHICLES = 30     # 考卷少於這麼多台車，成績不穩，不換模型
GOALS = {"day": 0.99, "night": 0.97, "wrong_max": 0.001}   # 計劃第四節的草案（停下來的車）


class BaselineReader:
    """第 1 階段用的現成模型（只用它的認字部分）。"""
    name = "現成模型（RapidOCR）"

    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR
        self.ocr = RapidOCR()

    def read(self, img):
        rec, _ = self.ocr(img, use_det=False, use_cls=False, use_rec=True)
        if not rec:
            return "", 0.0
        return normalize(rec[0][0]), float(rec[0][1])


class OurReader:
    def __init__(self, path, label):
        self.model = R.OnnxRecognizer(path)
        self.name = f"{label}（{Path(path).name}）"

    def read(self, img):
        return self.model.read(img)


def exam_rows(data_dir):
    import train
    return [r for r in train.load_rows(data_dir)
            if r["split"] == "exam" and r["source"] == "human" and r["text"] and r.get("inner")]


def _blank():
    return {"n": 0, "correct": 0, "wrong": 0, "reject": 0, "top1": 0}


def score_reader(reader, data_dir, rows, images):
    """回傳 {'single': {light: 統計}, 'vote': {light: 統計}, 'speed_ms': 平均每張毫秒}。"""
    import time
    single = defaultdict(_blank)
    by_event = defaultdict(list)
    t_total = 0.0
    for row in rows:
        img = images[row["id"]]
        t = time.perf_counter()
        text, conf = reader.read(R.tight_crop(img, row["inner"]))
        t_total += time.perf_counter() - t
        s = single[row["light"]]
        s["n"] += 1
        s["top1"] += text == row["text"]
        if conf >= ACCEPT_CONF and plate_format(text):
            s["correct" if text == row["text"] else "wrong"] += 1
        else:
            s["reject"] += 1
        if row.get("kind", "motion") == "motion":   # 定時拍照只有一張，湊不到 3 票，不算投票成績
            by_event[row["event"]].append((row["order"], text, conf, row))

    vote = defaultdict(_blank)
    for reads in by_event.values():
        reads.sort(key=lambda x: x[0])
        truth, light = reads[0][3]["text"], reads[0][3]["light"]
        voter = voting.Voter()
        decision = None
        for k, (_, text, conf, _) in enumerate(reads):
            decision = voter.feed(k * 0.4, [(text, conf)])   # 收集時大約每 0.4 秒一張
            if decision:
                break
        decision = decision or voter.finish(len(reads) * 0.4)
        s = vote[light]
        s["n"] += 1
        if decision["result"] == "handoff":
            s["reject"] += 1
        else:
            s["correct" if decision["plate"] == truth else "wrong"] += 1
            s["top1"] += decision["plate"] == truth
    return {"single": dict(single), "vote": dict(vote),
            "speed_ms": round(1000 * t_total / max(1, len(rows)), 1)}


def _total(stats):
    t = _blank()
    for s in stats.values():
        for k in t:
            t[k] += s[k]
    return t


def score_value(result):
    """比較兩個模型用：認錯率越低越好，其次全對率越高越好（看連拍投票）。"""
    t = _total(result["vote"])
    n = max(1, t["n"])
    return {"wrong_rate": t["wrong"] / n, "correct_rate": t["correct"] / n, "n": t["n"]}


def better(card):
    """新練的模型要比「目前使用」的好（還沒有目前使用的，就要比現成模型好），用同一份考卷比。"""
    new = card.get("candidate_score")
    old_score = card.get("current_score") or card.get("baseline_score")
    if not new or new["n"] < MIN_EXAM_VEHICLES:
        return False
    if not old_score:
        return True
    if new["wrong_rate"] < old_score["wrong_rate"] - 1e-9:
        return new["correct_rate"] >= old_score["correct_rate"] - 0.02
    return new["wrong_rate"] <= old_score["wrong_rate"] + 1e-9 and new["correct_rate"] > old_score["correct_rate"]


def _pct(a, b):
    return f"{a / b:.1%}" if b else "-"


def _section(title, result):
    lines = [f"■ {title}", f"  每張約 {result['speed_ms']} 毫秒（這台電腦）"]
    for mode, name in (("vote", "連拍投票（同一台車幾張一起判斷）"), ("single", "單張判斷")):
        lines.append(f"  【{name}】")
        stats = result[mode]
        for light, label in (("day", "白天"), ("night", "晚上")):
            s = stats.get(light)
            if not s or not s["n"]:
                continue
            goal = GOALS.get(light)
            mark = ""
            if goal and mode == "vote":
                mark = "　✓達標" if s["correct"] / s["n"] >= goal else f"　（目標 {goal:.0%}）"
            lines.append(f"    {label}：{s['n']} {'台' if mode == 'vote' else '張'}，"
                         f"全對 {_pct(s['correct'], s['n'])}、認錯 {_pct(s['wrong'], s['n'])}、"
                         f"認不出來交給人 {_pct(s['reject'], s['n'])}{mark}")
        t = _total(stats)
        if t["n"]:
            wrong_ok = "✓" if t["wrong"] / t["n"] <= GOALS["wrong_max"] else "✗"
            lines.append(f"    合計認錯率 {_pct(t['wrong'], t['n'])}（目標千分之一以下 {wrong_ok}）"
                         + (f"；不管信心度、第一個答案就對的比例 {_pct(t['top1'], t['n'])}" if mode == "single" else ""))
    return lines


def run(cfg, data_dir, candidate=None, train_info=None):
    """考試並寫成績單。candidate：剛練好的模型（訓練時傳進來）。"""
    rows = exam_rows(data_dir)
    stamp = dt.datetime.now().strftime("%Y-%m-%d")
    out_dir = storage.reports_dir(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    card = {"date": stamp, "exam_crops": len(rows), "exam_plates": len({r["text"] for r in rows}),
            "results": {}, "candidate_score": None, "train": train_info}
    lines = [f"車牌辨識成績單 {stamp}", "",
             f"考卷：{card['exam_plates']} 個車牌、{card['exam_crops']} 張切圖（人工確認過，從不拿來訓練）",
             "注意：現在拍的是路上、停車場的車（在動、角度斜），管制車道停下來的車會比較好認；",
             "達標標準是計劃的草案，針對停下來的車，這裡先拿來對照。", ""]
    if not rows:
        lines.append("還沒有考卷（要先在標記網頁確認一些車牌）。")
    else:
        images = {r["id"]: storage.imread(labels.dataset_dir(data_dir) / r["file"]) for r in rows}
        rows = [r for r in rows if images[r["id"]] is not None]
        readers = [BaselineReader()]
        current = storage.read_json(Path(data_dir) / "models" / "current.json", {})
        if current.get("rec_onnx"):
            readers.append(OurReader(Path(data_dir) / "models" / current["rec_onnx"], "我們的模型・目前使用"))
        if candidate:
            readers.append(OurReader(candidate, "我們的模型・這次新練"))
        for reader in readers:
            res = score_reader(reader, data_dir, rows, images)
            card["results"][reader.name] = res
            lines += _section(reader.name, res) + [""]
            kind = ("candidate_score" if "這次新練" in reader.name else
                    "current_score" if "目前使用" in reader.name else "baseline_score")
            card[kind] = score_value(res)
    cand = card.get("candidate_score")
    if cand and cand["n"] < MIN_EXAM_VEHICLES:
        lines += [f"考卷只有 {cand['n']} 台車（要 {MIN_EXAM_VEHICLES} 台以上），成績還不穩，這次不換模型。", ""]
    if train_info:
        info = train_info.get("rec_info", {})
        lines += ["訓練紀錄：",
                  f"  用了真照片 {info.get('real_train', 0)} 張，驗證全對率 {info.get('val_acc', 0):.1%}，"
                  f"練了 {info.get('epochs', 0)} 輪"]
        f = train_info.get("filter_info") or {}
        if f.get("skipped"):
            lines.append(f"  「是不是車牌」過濾器：「不是車牌」的例子只有 {f.get('neg', 0)} 張，還不夠練")
        elif f:
            lines.append(f"  「是不是車牌」過濾器：真車牌留下 {f['keep_plate_rate']:.1%}、"
                         f"非車牌擋掉 {f['drop_nonplate_rate']:.1%}")
    text = "\n".join(lines) + "\n"
    (out_dir / f"scorecard_{stamp}.txt").write_text(text, encoding="utf-8")
    storage.write_json(out_dir / f"scorecard_{stamp}.json", card)
    print(text)
    return card
