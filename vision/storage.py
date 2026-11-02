"""設定檔、資料夾、存檔、到期刪除。

資料夾結構（data_dir 預設是程式旁邊的 data/，不進 git）：
    data/captures/2026-11-05/events.jsonl      每次動靜一行（時間、白天晚上、認出的字、信心度）
    data/captures/2026-11-05/counters.json     略過次數、斷線秒數
    data/captures/2026-11-05/顧車/153012_0007/ 車牌切圖＋一張完整畫面（依攝影機分資料夾）
    data/reports/2026-11-05.json / .txt        每日統計（只有數字）
    data/collector.log                         執行紀錄（不寫車牌文字）
"""
import datetime as dt
import json
import os
import re
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MIN_RETENTION_DAYS = 7

DEFAULTS = {
    "camera_host": "",
    "camera_port": 554,
    "camera_user": "",
    "camera_password": "",
    "rtsp_path": "/user={user}&password={password}&channel={channel}&stream=0.sdp?real_stream",
    "data_dir": "data",
    "retention_days": 90,
    "min_free_gb": 20,
    "roi": [0.0, 0.0, 1.0, 0.9],
    "motion_threshold": 0.015,
    "sample_interval_s": 0.4,
    "max_frames_per_event": 5,
    "event_gap_s": 2.0,
    "max_event_s": 20,
    "min_plate_height": 12,
    "save_full_frame": True,
    "snapshot_interval_s": 60,
    "repeat_save_s": 3600,
    "cameras": [],
    "label_port": 8790,
    "dataset_retention_days": 90,
}


def base_dir():
    """程式所在資料夾；打包成 exe 時是 exe 所在資料夾。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def load_config(path=None):
    explicit = path is not None
    path = Path(path) if path else base_dir() / "config.json"
    if explicit and not path.exists():
        raise SystemExit(f"找不到設定檔：{path}")
    cfg = dict(DEFAULTS)
    if path.exists():
        with open(path, encoding="utf-8-sig") as f:
            cfg.update({k: v for k, v in json.load(f).items() if not k.startswith("_")})
    # data_dir 寫相對路徑時，以設定檔所在的資料夾為準（訓練程式借用收集程式的設定檔時才找得到資料）
    cfg["_config_dir"] = str(path.resolve().parent)
    try:
        cfg["retention_days"] = int(cfg["retention_days"])
    except (TypeError, ValueError):
        raise SystemExit("config.json 的 retention_days 要填天數（數字）。")
    if cfg["retention_days"] < MIN_RETENTION_DAYS:
        raise SystemExit(f"config.json 的 retention_days 至少要 {MIN_RETENTION_DAYS} 天（填太小會刪掉剛收集的照片）。")
    return cfg


def resolve_data_dir(cfg):
    p = Path(cfg["data_dir"])
    return (p if p.is_absolute() else Path(cfg.get("_config_dir") or base_dir()) / p).resolve()


def captures_dir(data_dir):
    return Path(data_dir) / "captures"


def reports_dir(data_dir):
    return Path(data_dir) / "reports"


def today():
    return dt.date.today().isoformat()


def list_days(data_dir):
    d = captures_dir(data_dir)
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and DAY_RE.match(p.name))


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, obj):
    """先寫暫存檔再換名，避免寫到一半斷電留下壞檔。"""
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def imwrite(path, img, quality=90):
    """cv2.imwrite 在中文路徑會失敗，改用 imencode＋tofile。"""
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if ok:
        buf.tofile(str(path))
    return ok


def imread(path):
    return cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)


def free_gb(data_dir):
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(data_dir).free / 1024 ** 3


def delete_expired(data_dir, retention_days, now=None):
    """刪除超過保存期限的日期資料夾（照片＋紀錄）。只動 captures/ 底下名字是日期的資料夾。"""
    now = now or dt.date.today()
    retention_days = max(int(retention_days), MIN_RETENTION_DAYS)
    cutoff = (now - dt.timedelta(days=int(retention_days))).isoformat()
    root = captures_dir(data_dir).resolve()
    removed = []
    for day in list_days(data_dir):
        if day >= cutoff:
            continue
        target = (root / day).resolve()
        if target.parent != root:  # 防呆：絕不刪 captures/ 以外的東西
            continue
        shutil.rmtree(target, ignore_errors=True)
        if target.exists():
            continue  # 刪不掉（檔案被開著），下次再試
        removed.append(day)
    return removed
