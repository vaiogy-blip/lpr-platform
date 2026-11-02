"""假車牌產生器：用電腦畫出台灣車牌樣式的圖片。

用途：
1. 真照片還不夠多時，先用假車牌把認字模型「預先訓練」一版，之後再用真照片接著練。
2. 產生一份「假的收集資料」，在沒有攝影機時測試標記網頁、匯出、訓練、考試整條流程：
       python synth.py --fake-captures 輸出資料夾 --days 3 --events 120
畫的是隨機字，不是任何真實車牌，不含個資。
"""
import argparse
import datetime as dt
import json
import random
import string
from pathlib import Path

import cv2
import numpy as np

LETTERS = string.ascii_uppercase
DIGITS = string.digits

# (格式, 比重, 產生函式)：比重大略照路上常見程度
FORMATS = [
    ("3英4數", 45, lambda r: _l(r, 3) + "-" + _d(r, 4)),
    ("2碼4數", 15, lambda r: _mix2(r) + "-" + _d(r, 4)),
    ("4數2碼", 10, lambda r: _d(r, 4) + "-" + _mix2(r)),
    ("3英3數", 18, lambda r: _l(r, 3) + "-" + _d(r, 3)),
    ("3數3英", 12, lambda r: _d(r, 3) + "-" + _l(r, 3)),
]

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\bahnschrift.ttf",   # 窄體，跟車牌字型最像
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\consolab.ttf",
    r"C:\Windows\Fonts\verdanab.ttf",
    r"C:\Windows\Fonts\tahomabd.ttf",
]


def _l(r, n):
    return "".join(r.choice(LETTERS) for _ in range(n))


def _d(r, n):
    return "".join(r.choice(DIGITS) for _ in range(n))


def _mix2(r):
    s = [r.choice(LETTERS), r.choice(LETTERS + DIGITS)]
    r.shuffle(s)
    return "".join(s)


def random_plate(r):
    """回傳 (含 - 的顯示字, 去掉 - 的答案)。"""
    total = sum(w for _, w, _ in FORMATS)
    x = r.uniform(0, total)
    for _, w, fn in FORMATS:
        x -= w
        if x <= 0:
            shown = fn(r)
            return shown, shown.replace("-", "")
    shown = FORMATS[0][2](r)
    return shown, shown.replace("-", "")


_fonts = None


def _load_fonts():
    global _fonts
    if _fonts is None:
        from PIL import ImageFont
        _fonts = []
        for path in FONT_CANDIDATES:
            try:
                _fonts.append(ImageFont.truetype(path, 120))
            except OSError:
                pass
    return _fonts


def render_plate(shown, r):
    """畫一張緊貼車牌外框的圖（BGR）。"""
    from PIL import Image, ImageDraw
    fonts = _load_fonts()
    style = r.random()
    if style < 0.75:
        bg, fg = (245, 245, 245), (20, 20, 20)          # 白底黑字（自用車）
    elif style < 0.85:
        bg, fg = (245, 245, 245), (40, 40, 200)         # 白底紅字
    elif style < 0.95:
        bg, fg = (245, 245, 245), (40, 130, 40)         # 白底綠字（營業車）
    else:
        bg, fg = (30, 30, 30), (235, 235, 235)          # 黑底白字
    bg = tuple(int(np.clip(c + r.uniform(-30, 10), 0, 255)) for c in bg)
    w, h = 440, 160
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle([3, 3, w - 4, h - 4], outline=fg, width=r.randint(3, 8))
    if fonts:
        font = r.choice(fonts)
        box = draw.textbbox((0, 0), shown, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
        scale = min((w - 40) / tw, (h - 30) / th)
        layer = Image.new("RGBA", (tw + 10, th + 10), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((5 - box[0], 5 - box[1]), shown, font=font, fill=fg + (255,))
        layer = layer.resize((max(1, int(layer.width * scale * r.uniform(0.85, 1.0))),
                              max(1, int(layer.height * scale))))
        img.paste(layer, ((w - layer.width) // 2, (h - layer.height) // 2 + r.randint(-4, 8)), layer)
        out = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
    else:  # 沒有字型檔時退而求其次
        out = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        cv2.putText(out, shown, (25, 115), cv2.FONT_HERSHEY_DUPLEX, 2.8, fg[::-1], 8)
    # 上方小字列（像「臺灣省」那一排），只畫成雜點塊
    if r.random() < 0.7:
        x0 = r.randint(150, 190)
        cv2.rectangle(out, (x0, 10), (x0 + r.randint(60, 110), 24), fg[::-1], -1)
    return out


def augment(img, r, night=None):
    """模擬攝影機拍到的樣子：斜角、模糊、雜訊、亮暗、紅外線黑白、壓縮、解析度低。"""
    img = cv2.resize(img, (220, 80), interpolation=cv2.INTER_AREA)  # 先縮小，後面每一步都快很多
    h, w = img.shape[:2]
    # 透視變形
    d = 0.12
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[r.uniform(0, d) * w, r.uniform(0, d) * h], [w - r.uniform(0, d) * w, r.uniform(0, d) * h],
                      [w - r.uniform(0, d) * w, h - r.uniform(0, d) * h], [r.uniform(0, d) * w, h - r.uniform(0, d) * h]])
    img = cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (w, h),
                              borderMode=cv2.BORDER_REPLICATE)
    # 解析度低：縮小再放大（遠處車牌字高可能只有 15～30 像素）
    target_h = r.randint(14, 48)  # 車牌外框高度（像素），遠處的車很小
    small = cv2.resize(img, (max(8, int(w * target_h / h)), target_h), interpolation=cv2.INTER_AREA)
    img = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
    if r.random() < 0.5:
        k = r.choice([3, 5])
        img = cv2.GaussianBlur(img, (k, k), 0)
    if r.random() < 0.3:  # 動態模糊
        k = r.randint(3, 9)
        kernel = np.zeros((k, k), np.float32)
        kernel[k // 2, :] = 1.0 / k
        img = cv2.filter2D(img, -1, kernel)
    # 亮暗、對比
    alpha, beta = r.uniform(0.5, 1.3), r.uniform(-60, 40)
    img = np.clip(img.astype(np.float32) * alpha + beta, 0, 255)
    img = img + np.random.default_rng(r.randint(0, 2**31)).normal(0, r.uniform(0, 12), img.shape)
    img = np.clip(img, 0, 255).astype(np.uint8)
    night = r.random() < 0.35 if night is None else night
    if night:  # 紅外線：黑白、反光強
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        g = cv2.convertScaleAbs(g, alpha=r.uniform(1.0, 1.6), beta=r.uniform(-20, 30))
        img = cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, r.randint(35, 95)])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def sample(r, night=None):
    """一張訓練用的假車牌：(BGR 圖, 答案)。"""
    shown, text = random_plate(r)
    return augment(render_plate(shown, r), r, night), text


# ---------------- 假的收集資料（測試整條流程用） ----------------

def _confusable(text, r):
    """模擬模型認錯：換掉一個容易混淆的字。"""
    pairs = {"0": "D", "D": "0", "8": "B", "B": "8", "1": "7", "7": "1", "5": "S", "S": "5",
             "2": "Z", "Z": "2", "6": "G", "G": "6"}
    idx = [i for i, c in enumerate(text) if c in pairs]
    if not idx:
        return text[:-1]
    i = r.choice(idx)
    return text[:i] + pairs[text[i]] + text[i + 1:]


def fake_captures(out_dir, days, events_per_day, seed=0):
    """產生跟收集程式一樣格式的 captures/日期/events.jsonl＋切圖。"""
    import storage
    from plate_ocr import pad_rect, plate_format
    r = random.Random(seed)
    root = storage.captures_dir(out_dir)
    today = dt.date.today()
    regulars = [random_plate(r) for _ in range(15)]  # 天天出現的車（測試考卷用車牌分組）
    for d in range(days, 0, -1):
        day = (today - dt.timedelta(days=d)).isoformat()
        day_dir = root / day
        day_dir.mkdir(parents=True, exist_ok=True)
        lines = []
        for n in range(events_per_day):
            shown, text = r.choice(regulars) if r.random() < 0.3 else random_plate(r)
            night = r.random() < 0.35
            kind = "snapshot" if r.random() < 0.2 else "motion"
            camera = r.choice(["顧車", "金爐", "巷弄機車"])
            t = dt.datetime.fromisoformat(day) + dt.timedelta(seconds=int(86400 * n / events_per_day))
            event_id = f"{t:%H%M%S}_{n + 1:04d}"
            rel = f"{camera}/{event_id}"
            (day_dir / rel).mkdir(parents=True, exist_ok=True)
            plate = render_plate(shown, r)
            frames = 1 if kind == "snapshot" else r.randint(2, 5)
            reads = []
            for f in range(frames):
                img = augment(plate, r, night)
                ph = r.randint(28, 70)
                pw = int(img.shape[1] * ph / img.shape[0])
                frame = np.full((720, 1280, 3), 90 if not night else 40, np.uint8)
                frame = cv2.add(frame, np.random.default_rng(n * 10 + f).integers(0, 30, frame.shape, dtype=np.uint8))
                x1, y1 = r.randint(50, 1280 - pw - 50), r.randint(200, 720 - ph - 50)
                frame[y1:y1 + ph, x1:x1 + pw] = cv2.resize(img, (pw, ph))
                box = [x1, y1, x1 + pw, y1 + ph]
                hard = r.random()
                ocr = text if hard < 0.75 else _confusable(text, r)
                conf = round(r.uniform(0.9, 0.999) if ocr == text and hard < 0.6 else r.uniform(0.4, 0.92), 3)
                crop_rel = f"{rel}/f{f}_{f}.jpg"
                cx1, cy1, cx2, cy2 = pad_rect(box, frame.shape, 0.25)
                storage.imwrite(day_dir / crop_rel, frame[cy1:cy2, cx1:cx2], 95)
                if f == 0:
                    storage.imwrite(day_dir / rel / "full.jpg", frame, 85)
                reads.append({"box": box, "text": ocr, "raw": ocr, "conf": conf,
                              "format": plate_format(ocr), "frame": f, "crop": crop_rel})
            if r.random() < 0.04:  # 偶爾混一個不是車牌的字（招牌）
                reads[0].update(text="SALE88", raw="SALE88", format=None, conf=0.7)
            from collector import vote
            lines.append({"event_id": event_id, "camera": camera, "kind": kind,
                          "time": t.isoformat(timespec="seconds"),
                          "light": "night" if night else "day", "saturation": 0, "brightness": 0,
                          "frames": frames, "reads": reads, "best": vote(reads),
                          "full_frame": f"{rel}/full.jpg", "full_frame_index": 0,
                          "_truth": text})  # 只有假資料有正確答案，方便測試
        with open(day_dir / "events.jsonl", "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return root


def main():
    ap = argparse.ArgumentParser(description="假車牌產生器")
    ap.add_argument("--fake-captures", metavar="資料夾", help="產生一份假的收集資料（data_dir 格式）")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--events", type=int, default=120)
    ap.add_argument("--preview", metavar="檔名", help="畫 24 張假車牌拼成一張圖看看")
    args = ap.parse_args()
    if args.fake_captures:
        print(fake_captures(args.fake_captures, args.days, args.events))
    if args.preview:
        import storage
        r = random.Random(1)
        tiles = [cv2.resize(sample(r)[0], (220, 80)) for _ in range(24)]
        rows = [np.hstack(tiles[i:i + 4]) for i in range(0, 24, 4)]
        storage.imwrite(args.preview, np.vstack(rows))


if __name__ == "__main__":
    main()
