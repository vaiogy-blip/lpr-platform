"""訓練（第 3 階段）。在公司電腦上跑，照片不離開這台電腦、不用雲端。

    python train.py pretrain --minutes 40     只用假車牌預先訓練（開發時做一次，結果放 vision/models/）
    python train.py train --minutes 120       用標好的真照片接著練＋練「是不是車牌」過濾器，練完自動考試
（打包版：plate_trainer.exe train）

會先把標好的答案匯出（data/dataset/），只用 train 的照片訓練；考卷（exam）和 holdout 永遠不拿來訓練。
另外從 train 裡依車牌再分出約 10% 當「驗證」，用來挑最好的那一輪（不是考卷）。
結果放 data/models/：rec_日期.onnx（認字）、filter_日期.onnx（過濾器），latest.json（最近一次練的）、current.json（考過試、目前使用的）。
"""
import argparse
import datetime as dt
import json
import os
import random
import sys
import time
from pathlib import Path

import numpy as np

import labels
import recognizer as R
import storage
import synth

BUNDLED = Path(__file__).resolve().parent / "models"
MIN_REAL_FOR_TRAIN = 50       # 真照片少於這個數量就只用假車牌（跑流程用）
MIN_FILTER_SAMPLES = 30       # 「不是車牌」少於這個數量就先不練過濾器


def log(msg):
    print(f"{dt.datetime.now():%H:%M:%S} {msg}", flush=True)


def models_dir(data_dir):
    d = Path(data_dir) / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _setup_torch():
    import torch
    # 留一半 CPU 給收集程式
    torch.set_num_threads(max(1, (os.cpu_count() or 4) // 2))
    torch.manual_seed(0)
    return torch


# ---------------- 資料 ----------------

def load_rows(data_dir):
    path = labels.dataset_dir(data_dir) / "index.jsonl"
    rows = []
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


def load_image(data_dir, row):
    return storage.imread(labels.dataset_dir(data_dir) / row["file"])


def is_val_plate(text):
    return labels._hash_mod("val:" + text, 10) == 0


class SynthPool:
    """先產生一批假車牌放記憶體，每輪換掉一部分（產生圖比訓練慢，不能每張都現做）。"""

    def __init__(self, size, seed=0):
        self.r = random.Random(seed)
        self.items = [self._one() for _ in range(size)]

    def _one(self):
        img, text = synth.sample(self.r)
        return R.prep(img, R.REC_H, R.REC_W), text

    def refresh(self, fraction):
        for _ in range(int(len(self.items) * fraction)):
            self.items[self.r.randrange(len(self.items))] = self._one()


def real_sample(img, row, r):
    crop = R.tight_crop(img, row["inner"], jitter=r)
    if r.random() < 0.3:
        import cv2
        crop = cv2.GaussianBlur(crop, (3, 3), 0)
    x = R.prep(crop, R.REC_H, R.REC_W)
    if r.random() < 0.5:
        x = np.clip(x * r.uniform(0.7, 1.2) + r.uniform(-0.15, 0.15), 0, 1).astype(np.float32)
    return x


# ---------------- 認字模型 ----------------

def _batches(items, bs, r):
    idx = list(range(len(items)))
    r.shuffle(idx)
    for i in range(0, len(idx), bs):
        yield [items[j] for j in idx[i:i + bs]]


def _ctc_step(torch, model, opt, ctc, batch):
    x = torch.from_numpy(np.stack([b[0] for b in batch]))
    targets = [R.encode(b[1]) for b in batch]
    flat = torch.tensor([c for t in targets for c in t], dtype=torch.long)
    tlen = torch.tensor([len(t) for t in targets], dtype=torch.long)
    logits = model(x)                                  # N×T×C
    logp = logits.log_softmax(2).permute(1, 0, 2)      # T×N×C
    ilen = torch.full((len(batch),), logp.shape[0], dtype=torch.long)
    loss = ctc(logp, flat, ilen, tlen)
    opt.zero_grad()
    loss.backward()
    torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
    opt.step()
    return float(loss)


def _accuracy(torch, model, items):
    if not items:
        return 0.0
    model.eval()
    ok = 0
    with torch.no_grad():
        for i in range(0, len(items), 256):
            chunk = items[i:i + 256]
            x = torch.from_numpy(np.stack([c[0] for c in chunk]))
            probs = model(x).softmax(2).numpy()
            ok += sum(R.ctc_decode(p)[0] == c[1] for p, c in zip(probs, chunk))
    model.train()
    return ok / len(items)


def train_recognizer(data_dir, out_dir, minutes, init=None, synth_only=False, seed=0, pool_size=None):
    torch = _setup_torch()
    r = random.Random(seed)
    model = R.build_crnn()
    if init and Path(init).exists():
        model.load_state_dict(torch.load(init, map_location="cpu", weights_only=True))
        log(f"從 {Path(init).name} 接著練")

    # 真照片
    real_train, real_val = [], []
    if not synth_only:
        for row in load_rows(data_dir):
            if row["split"] != "train" or not row["text"] or not row.get("inner"):
                continue
            img = load_image(data_dir, row)
            if img is None:
                continue
            (real_val if is_val_plate(row["text"]) else real_train).append((img, row))
        log(f"真照片：訓練 {len(real_train)} 張、驗證 {len(real_val)} 張")
        if len(real_train) < MIN_REAL_FOR_TRAIN:
            log(f"真照片少於 {MIN_REAL_FOR_TRAIN} 張，這次以假車牌為主")

    pool = SynthPool(pool_size or (6000 if real_train else 15000), seed)
    synth_val = SynthPool(min(1500, pool_size or 1500), seed + 99).items
    val_items = [(R.prep(R.tight_crop(img, row["inner"]), R.REC_H, R.REC_W), row["text"])
                 for img, row in real_val] or synth_val

    lr = 3e-4 if init else 1e-3
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    ctc = torch.nn.CTCLoss(blank=R.BLANK, zero_infinity=True)
    best_acc, best_state = -1.0, None
    end = time.time() + minutes * 60
    epoch = 0
    model.train()
    while time.time() < end:
        epoch += 1
        items = list(pool.items)
        # 真照片每輪重新隨機偏移；真照片多時佔大部分
        # 真照片每張每輪做 3 種不同的隨機變化
        items += [(real_sample(img, row, r), row["text"]) for _ in range(3) for img, row in real_train]
        losses = []
        for batch in _batches(items, 64, r):
            losses.append(_ctc_step(torch, model, opt, ctc, batch))
            if time.time() >= end:
                break
        acc = _accuracy(torch, model, val_items)
        log(f"第 {epoch} 輪：loss {np.mean(losses):.3f}，驗證全對率 {acc:.1%}")
        if acc > best_acc:
            best_acc = acc
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
        pool.refresh(0.3)
    if best_state is not None:
        model.load_state_dict(best_state)
    synth_acc = _accuracy(torch, model, synth_val)
    return model, {"epochs": epoch, "val_acc": round(max(best_acc, 0.0), 4), "synth_val_acc": round(synth_acc, 4),
                   "real_train": len(real_train), "real_val": len(real_val)}


# ---------------- 「是不是車牌」過濾器 ----------------

def train_filter(data_dir, minutes, seed=0):
    torch = _setup_torch()
    r = random.Random(seed)
    pos, neg = [], []
    for row in load_rows(data_dir):
        if row.get("source") != "human" or not row.get("inner"):
            continue
        if row["split"] == "train" and row["text"]:   # 考卷永遠不拿來訓練
            target = pos
        elif row["split"] == "not_plate":
            target = neg
        else:
            continue
        img = load_image(data_dir, row)
        if img is not None:
            target.append((img, row))
    if len(neg) < MIN_FILTER_SAMPLES or len(pos) < MIN_FILTER_SAMPLES:
        log(f"「不是車牌」{len(neg)} 張、車牌 {len(pos)} 張（各要 {MIN_FILTER_SAMPLES} 張），這次先不練過濾器")
        return None, {"skipped": True, "neg": len(neg), "pos": len(pos)}

    def split(items):
        tr, va = [], []
        for it in items:
            # 依車牌（不是車牌的依事件）分驗證組，同一台車不會同時在訓練和驗證
            group = it[1]["text"] or it[1]["event"]
            (va if labels._hash_mod("fval:" + group, 5) == 0 else tr).append(it)
        return tr, va

    pos_tr, pos_va = split(pos)
    neg_tr, neg_va = split(neg)
    model = R.build_filter()
    opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)
    weight = torch.tensor([len(pos_tr) / max(1, len(neg_tr))])
    lossf = torch.nn.BCEWithLogitsLoss(pos_weight=1 / weight)
    end = time.time() + minutes * 60

    def make(items, y, jitter):
        return [(R.prep(R.tight_crop(img, row["inner"], jitter=r if jitter else None), R.CLS_H, R.CLS_W), y)
                for img, row in items]

    va = make(pos_va, 1.0, False) + make(neg_va, 0.0, False)
    model.train()
    while time.time() < end:
        tr = make(pos_tr, 1.0, True) + make(neg_tr, 0.0, True)
        for batch in _batches(tr, 64, r):
            x = torch.from_numpy(np.stack([b[0] for b in batch]))
            y = torch.tensor([[b[1]] for b in batch])
            loss = lossf(model(x), y)
            opt.zero_grad()
            loss.backward()
            opt.step()
    model.eval()
    with torch.no_grad():
        p = torch.sigmoid(model(torch.from_numpy(np.stack([v[0] for v in va])))).numpy().reshape(-1) if va else []
    ys = [v[1] for v in va]
    tp = sum(1 for pi, y in zip(p, ys) if pi >= 0.5 and y == 1)
    fn = sum(1 for pi, y in zip(p, ys) if pi < 0.5 and y == 1)
    tn = sum(1 for pi, y in zip(p, ys) if pi < 0.5 and y == 0)
    fp = sum(1 for pi, y in zip(p, ys) if pi >= 0.5 and y == 0)
    stats = {"pos": len(pos), "neg": len(neg),
             "keep_plate_rate": round(tp / max(1, tp + fn), 4),     # 真的車牌被留下的比例
             "drop_nonplate_rate": round(tn / max(1, tn + fp), 4)}  # 不是車牌被擋掉的比例
    log(f"過濾器：車牌留下 {stats['keep_plate_rate']:.1%}，非車牌擋掉 {stats['drop_nonplate_rate']:.1%}")
    return model, stats


# ---------------- 指令 ----------------

def cmd_pretrain(args):
    out = BUNDLED
    out.mkdir(exist_ok=True)
    model, info = train_recognizer(None, out, args.minutes, synth_only=True)
    import torch
    torch.save(model.state_dict(), out / "rec_synth.pt")
    R.export_onnx(model, out / "rec_synth.onnx", R.REC_H, R.REC_W)
    (out / "rec_synth.json").write_text(json.dumps(
        {**info, "trained": dt.datetime.now().isoformat(timespec="seconds"),
         "note": "只用假車牌訓練，不含任何真實照片"}, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"完成：{out / 'rec_synth.onnx'}（假車牌驗證全對率 {info['synth_val_acc']:.1%}）")


def cmd_train(args):
    cfg = storage.load_config(args.config)
    data_dir = storage.resolve_data_dir(cfg)
    log("先匯出標好的答案…")
    r = labels.export_dataset(data_dir, cfg["dataset_retention_days"])
    log(f"資料集：共 {r['total']} 張 {r['by_split']}")
    mdir = models_dir(data_dir)
    # 從「目前使用」（考過試、比較好的那版）接著練；還沒有就從假車牌預先訓練版開始
    current = storage.read_json(mdir / "current.json", {})
    init = mdir / current["rec_pt"] if current.get("rec_pt") else BUNDLED / "rec_synth.pt"
    if not Path(init).exists():
        log(f"找不到 {Path(init).name}，改從假車牌預先訓練版開始")
        init = BUNDLED / "rec_synth.pt"
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")

    import torch
    model, info = train_recognizer(data_dir, mdir, args.minutes, init=init)
    torch.save(model.state_dict(), mdir / f"rec_{stamp}.pt")
    R.export_onnx(model, mdir / f"rec_{stamp}.onnx", R.REC_H, R.REC_W)
    record = {"trained": stamp, "rec_pt": f"rec_{stamp}.pt", "rec_onnx": f"rec_{stamp}.onnx",
              "rec_info": info, "filter_onnx": current.get("filter_onnx")}

    fmodel, finfo = train_filter(data_dir, max(3, args.minutes // 6))
    if fmodel is not None:
        R.export_onnx(fmodel, mdir / f"filter_{stamp}.onnx", R.CLS_H, R.CLS_W)
        record["filter_onnx"] = f"filter_{stamp}.onnx"
    record["filter_info"] = finfo
    storage.write_json(mdir / f"train_{stamp}.json", record)

    # 考試：新模型要考得比上一版好（認錯更少、全對更多）才換成「目前使用」
    import evaluate
    card = evaluate.run(cfg, data_dir, candidate=mdir / record["rec_onnx"], train_info=record)
    if evaluate.better(card):
        record["score"] = card["candidate_score"]
        storage.write_json(mdir / "current.json", record)
        log(f"新模型考得比較好，已設為目前使用：{record['rec_onnx']}")
    else:
        log("新模型沒有比目前使用的好，先不換（檔案留著）")
    storage.write_json(mdir / "latest.json", record)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="車牌認字模型訓練（只在本機跑）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pretrain", help="只用假車牌預先訓練")
    p.add_argument("--minutes", type=float, default=40)
    p = sub.add_parser("train", help="用標好的真照片訓練並考試")
    p.add_argument("--minutes", type=float, default=120)
    p.add_argument("--config")
    p = sub.add_parser("evaluate", help="只考試、產生成績單")
    p.add_argument("--config")
    args = ap.parse_args()
    if args.cmd == "pretrain":
        cmd_pretrain(args)
    elif args.cmd == "train":
        cmd_train(args)
    else:
        import evaluate
        cfg = storage.load_config(args.config)
        data_dir = storage.resolve_data_dir(cfg)
        labels.export_dataset(data_dir, cfg["dataset_retention_days"])
        evaluate.run(cfg, data_dir)


if __name__ == "__main__":
    main()
