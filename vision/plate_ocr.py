"""找車牌、認字（第 1 階段：用現成開源模型先猜）。

用 RapidOCR（Apache-2.0，內建 PaddleOCR PP-OCRv4 模型，Apache-2.0）在本機跑：
1. 文字偵測找出畫面上所有「像文字的框」
2. 把同一行、靠很近的框合併（車牌中間的 - 常讓它被拆成兩塊）
3. 只留形狀像車牌的框，再逐一認字
4. 正規化成大寫英數字，檢查是否符合台灣車牌格式

不連網、不上傳，照片只在這台電腦處理。
"""
import re

import cv2
import numpy as np

# 台灣車牌格式（去掉 - 之後比對）。這是收集階段的草案，第 2、3 階段會依實際資料調整。
PLATE_FORMATS = [
    ("3英4數", re.compile(r"^[A-Z]{3}\d{4}$")),        # 新式汽車、重型機車 ABC-1234
    ("3英3數", re.compile(r"^[A-Z]{3}\d{3}$")),        # 機車 ABC-123
    ("3數3英", re.compile(r"^\d{3}[A-Z]{3}$")),        # 機車 123-ABC
    ("2碼4數", re.compile(r"^(?=.*[A-Z])[A-Z0-9]{2}\d{4}$")),  # 舊式汽車 AB-1234、S3-0438
    ("4數2碼", re.compile(r"^\d{4}(?=.*[A-Z])[A-Z0-9]{2}$")),  # 舊式汽車 1234-AB
]

MIN_CHARS, MAX_CHARS = 5, 8          # 正規化後字數在這範圍內才當作車牌候選
MIN_ASPECT, MAX_ASPECT = 1.2, 6.5    # 合併後框的寬高比
TIMESTAMP_CHARS = re.compile(r"[:/]")  # 攝影機疊印的日期時間，不是車牌


def normalize(text):
    """轉大寫、只留英數字。"""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def plate_format(plate):
    """回傳符合的格式名稱，不符合回傳 None。"""
    for name, pattern in PLATE_FORMATS:
        if pattern.match(plate):
            return name
    return None


def _box_rect(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return [int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))]


def merge_boxes(rects):
    """把同一行、水平距離很近、高度相近的框合併成一個。"""
    rects = sorted(rects, key=lambda r: r[0])
    merged = []
    for r in rects:
        for m in merged:
            mh, rh = m[3] - m[1], r[3] - r[1]
            overlap = min(m[3], r[3]) - max(m[1], r[1])
            gap = r[0] - m[2]
            if (overlap > 0.6 * min(mh, rh)
                    and 0.6 < rh / max(mh, 1) < 1.6
                    and gap < 0.8 * max(mh, rh)):
                m[0], m[1] = min(m[0], r[0]), min(m[1], r[1])
                m[2], m[3] = max(m[2], r[2]), max(m[3], r[3])
                break
        else:
            merged.append(list(r))
    return merged


def pad_rect(rect, shape, ratio):
    """往外擴一點，留一些車牌周圍的畫面（之後標記、訓練找車牌時用得到）。"""
    h, w = shape[:2]
    x1, y1, x2, y2 = rect
    px, py = int((x2 - x1) * ratio), int((y2 - y1) * ratio)
    return [max(0, x1 - px), max(0, y1 - py), min(w, x2 + px), min(h, y2 + py)]


class PlateReader:
    def __init__(self, min_plate_height=12, rec_model=None, filter_model=None, det_max_side=None):
        """det_max_side：找字前把畫面長邊縮到這麼大（車道測試用，車很近、要快）；None = 原尺寸（收集用，遠處小車牌才找得到）。"""
        # 匯入很慢（載入模型），放在這裡才不會拖慢 --help
        from rapidocr_onnxruntime import RapidOCR
        kw = {"det_limit_side_len": det_max_side, "det_limit_type": "max"} if det_max_side else {}
        self.ocr = RapidOCR(**kw)
        self.min_plate_height = min_plate_height
        # 第 3 階段練好的模型（可選）：認字換成我們的、加上「是不是車牌」過濾器
        self.rec = self.filter = None
        if rec_model:
            from recognizer import OnnxRecognizer
            self.rec = OnnxRecognizer(rec_model)
        if filter_model:
            from recognizer import OnnxPlateFilter
            self.filter = OnnxPlateFilter(filter_model)

    def read(self, frame, roi=None):
        """在一張畫面裡找車牌。

        roi：[x1, y1, x2, y2]（0～1 的比例），只看這個範圍（例如排除攝影機疊印的時間字）。
        回傳 list of dict：box（原圖座標）、text（正規化後）、raw（模型原始輸出）、conf、format。
        """
        h, w = frame.shape[:2]
        ox, oy = 0, 0
        view = frame
        if roi:
            ox, oy = int(roi[0] * w), int(roi[1] * h)
            view = frame[oy:int(roi[3] * h), ox:int(roi[2] * w)]
        if view.size == 0:
            return []

        boxes, _ = self.ocr(view, use_det=True, use_cls=False, use_rec=False)
        if not boxes:
            return []
        rects = merge_boxes([_box_rect(b) for b in boxes])

        results = []
        for x1, y1, x2, y2 in rects:
            bw, bh = x2 - x1, y2 - y1
            if bh < self.min_plate_height or not (MIN_ASPECT <= bw / max(bh, 1) <= MAX_ASPECT):
                continue
            # 認字時四周多留一點，模型比較穩
            cx1, cy1, cx2, cy2 = pad_rect([x1, y1, x2, y2], view.shape, 0.08)
            crop = view[cy1:cy2, cx1:cx2]
            if self.filter and self.filter.is_plate(crop) < 0.5:
                continue  # 過濾器判斷不是車牌（招牌、衣服上的字）
            if self.rec:
                raw, conf = self.rec.read(crop)
                if not raw:
                    continue
            else:
                rec, _ = self.ocr(crop, use_det=False, use_cls=False, use_rec=True)
                if not rec:
                    continue
                raw, conf = rec[0][0], float(rec[0][1])
            if TIMESTAMP_CHARS.search(raw):
                continue
            text = normalize(raw)
            if not (MIN_CHARS <= len(text) <= MAX_CHARS):
                continue
            results.append({
                "box": [x1 + ox, y1 + oy, x2 + ox, y2 + oy],
                "text": text,
                "raw": raw,
                "conf": round(conf, 3),
                "format": plate_format(text),
            })
        return results


def light_condition(frame):
    """判斷白天／晚上：紅外線夜間模式畫面幾乎沒有顏色，或整體很暗就算晚上。"""
    small = cv2.resize(frame, (160, 90))
    hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
    saturation = float(np.mean(hsv[:, :, 1]))
    brightness = float(np.mean(hsv[:, :, 2]))
    night = saturation < 12 or brightness < 50
    return ("night" if night else "day"), round(saturation, 1), round(brightness, 1)
