"""我們自己的認字模型（第 3 階段）。

- 模型：小型 CRNN（卷積＋雙向 LSTM＋CTC），專門認台灣車牌的英文和數字，約 1～2 MB。
- 訓練用 PyTorch（BSD 授權，免費）；訓練完匯出成 ONNX，收集程式和車道測試只用 onnxruntime 跑，
  不需要裝 PyTorch。
- 「是不是車牌」過濾器：同一套小卷積網路，判斷文字框是不是車牌（擋招牌、衣服上的字）。

輸入都是灰階、固定大小：認字 32×128，過濾器 32×96。
"""
from pathlib import Path

import cv2
import numpy as np

CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
BLANK = len(CHARSET)          # CTC 的空白放最後一個
REC_H, REC_W = 32, 128
CLS_H, CLS_W = 32, 96
TIGHT_PAD = 0.08              # 認字時框外多留的比例（跟 plate_ocr.PlateReader 一致）


def tight_crop(img, inner, pad=TIGHT_PAD, jitter=None):
    """從存檔的切圖（四周多留 25%）切回車牌本身，外加一點邊。jitter 是訓練時的隨機偏移。"""
    h, w = img.shape[:2]
    x1, y1, x2, y2 = inner
    bw, bh = x2 - x1, y2 - y1
    p = [pad, pad, pad, pad]
    if jitter is not None:
        p = [pad + jitter.uniform(-0.06, 0.12) for _ in range(4)]
    a = max(0, int(x1 - bw * p[0]))
    b = max(0, int(y1 - bh * p[1]))
    c = min(w, int(x2 + bw * p[2]))
    d = min(h, int(y2 + bh * p[3]))
    if c - a < 4 or d - b < 4:
        return img
    return img[b:d, a:c]


def prep(img, h, w):
    """BGR 或灰階 → 1×h×w 的 0～1 浮點數。"""
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA if img.shape[1] > w else cv2.INTER_LINEAR)
    img = img.astype(np.float32) / 255.0
    # 每張自己拉亮度對比，白天晚上都差不多
    lo, hi = np.percentile(img, 2), np.percentile(img, 98)
    if hi - lo > 0.05:
        img = np.clip((img - lo) / (hi - lo), 0, 1)
    return img[None, :, :]


def encode(text):
    return [CHARSET.index(c) for c in text if c in CHARSET]


def ctc_decode(probs):
    """probs：T×C（已 softmax）。回傳 (文字, 信心度)；信心度取每個字最低的那個，寧可保守。"""
    best = probs.argmax(axis=1)
    out, confs, prev = [], [], BLANK
    for t, k in enumerate(best):
        if k != BLANK and k != prev:
            out.append(CHARSET[k])
            confs.append(float(probs[t, k]))
        prev = k
    if not out:
        return "", 0.0
    return "".join(out), float(min(confs))


# ---------------- 推論（只需要 onnxruntime） ----------------

class OnnxRecognizer:
    def __init__(self, path):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        self.sess = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
        self.name = Path(path).name

    def read(self, plate_img):
        """plate_img：已切到車牌（含一點邊）的圖。回傳 (文字, 信心度)。"""
        x = prep(plate_img, REC_H, REC_W)[None]
        logits = self.sess.run(None, {"image": x})[0][0]      # T×C
        e = np.exp(logits - logits.max(axis=1, keepdims=True))
        return ctc_decode(e / e.sum(axis=1, keepdims=True))


class OnnxPlateFilter:
    def __init__(self, path):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        self.sess = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])

    def is_plate(self, img):
        """回傳是車牌的機率 0～1。"""
        x = prep(img, CLS_H, CLS_W)[None]
        logit = float(self.sess.run(None, {"image": x})[0].reshape(-1)[0])
        return 1.0 / (1.0 + np.exp(-logit))


# ---------------- 模型定義（訓練時才用，需要 PyTorch） ----------------

def build_crnn():
    import torch.nn as nn

    class CRNN(nn.Module):
        def __init__(self):
            super().__init__()

            def block(i, o, pool):
                layers = [nn.Conv2d(i, o, 3, padding=1), nn.BatchNorm2d(o), nn.ReLU(inplace=True)]
                if pool:
                    layers.append(nn.MaxPool2d(pool))
                return layers

            self.cnn = nn.Sequential(
                *block(1, 32, (2, 2)),      # 16×64
                *block(32, 64, (2, 2)),     # 8×32
                *block(64, 128, None),
                *block(128, 128, (2, 1)),   # 4×32
                *block(128, 192, None),
                *block(192, 192, (2, 1)),   # 2×32
                nn.Conv2d(192, 256, (2, 1)), nn.BatchNorm2d(256), nn.ReLU(inplace=True),  # 1×32
            )
            self.rnn = nn.LSTM(256, 128, num_layers=2, bidirectional=True, batch_first=True, dropout=0.1)
            self.fc = nn.Linear(256, len(CHARSET) + 1)

        def forward(self, x):                       # x: N×1×32×128
            f = self.cnn(x).squeeze(2).permute(0, 2, 1)   # N×32×256
            f, _ = self.rnn(f)
            return self.fc(f)                       # N×T×C（logits）

    return CRNN()


def build_filter():
    import torch.nn as nn

    class PlateFilter(nn.Module):
        def __init__(self):
            super().__init__()
            self.net = nn.Sequential(
                nn.Conv2d(1, 16, 3, padding=1), nn.BatchNorm2d(16), nn.ReLU(inplace=True), nn.MaxPool2d(2),
                nn.Conv2d(16, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(inplace=True), nn.MaxPool2d(2),
                nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(inplace=True), nn.MaxPool2d(2),
                nn.Conv2d(64, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(inplace=True),
                nn.AdaptiveAvgPool2d(1), nn.Flatten(), nn.Dropout(0.2), nn.Linear(64, 1),
            )

        def forward(self, x):
            return self.net(x)

    return PlateFilter()


def export_onnx(model, path, h, w):
    import torch
    model.eval()
    dummy = torch.zeros(1, 1, h, w)
    torch.onnx.export(model, dummy, str(path), input_names=["image"], output_names=["out"],
                      dynamic_axes={"image": {0: "n"}, "out": {0: "n"}}, opset_version=17,
                      dynamo=False)
