"""每日統計：今天抓到幾台車、白天／晚上、信心度分布。

收集程式每天換日時會自動跑；也可以手動：
    python stats.py               （補齊所有還沒產生的日子）
    python stats.py 2026-11-05    （重新產生某一天）
報表放在 data/reports/，只有數字、不含車牌文字和照片。
"""
import json
import sys
from collections import Counter

import storage

CONF_BUCKETS = [(0.0, "0.5 以下"), (0.5, "0.5～0.7"), (0.7, "0.7～0.8"),
                (0.8, "0.8～0.9"), (0.9, "0.9～0.95"), (0.95, "0.95 以上")]


def _bucket(conf):
    label = CONF_BUCKETS[0][1]
    for low, name in CONF_BUCKETS:
        if conf >= low:
            label = name
    return label


def load_events(day_dir):
    path = day_dir / "events.jsonl"
    events = []
    if not path.exists():
        return events
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # 程式中途被關掉時，最後一行可能不完整
    return events


def build_report(data_dir, day):
    day_dir = storage.captures_dir(data_dir) / day
    events = load_events(day_dir)
    counters = storage.read_json(day_dir / "counters.json", {})

    light = Counter()
    light_plate = Counter()
    conf_dist = Counter()
    hours = Counter()
    cameras = {}
    snapshot_new = 0
    format_ok = 0
    voted3 = 0
    for e in events:
        cam = cameras.setdefault(e.get("camera", "-"), Counter())
        if e.get("kind", "motion") == "snapshot":
            snapshot_new += 1
            cam["snapshot_new"] += 1
        else:
            light[e.get("light", "day")] += 1
            hours[str(e.get("time", ""))[11:13] or "??"] += 1
            cam["motion"] += 1
            if e.get("reads"):
                light_plate[e.get("light", "day")] += 1
                cam["motion_with_plate"] += 1
        if not e.get("reads"):
            continue
        conf_dist[_bucket(max(r.get("conf", 0) for r in e["reads"]))] += 1
        best = e.get("best")
        if best:
            format_ok += 1
            if best["votes"] >= 3:
                voted3 += 1

    with_plate = sum(light_plate.values())
    return {
        "date": day,
        "events": sum(light.values()),
        "events_with_plate": with_plate,
        "day": {"events": light["day"], "with_plate": light_plate["day"]},
        "night": {"events": light["night"], "with_plate": light_plate["night"]},
        "snapshot_new": snapshot_new,
        "format_ok": format_ok,
        "three_votes": voted3,
        "conf_distribution": {name: conf_dist[name] for _, name in CONF_BUCKETS},
        "by_hour": dict(sorted(hours.items())),
        "cameras": {k: dict(v) for k, v in sorted(cameras.items())},
        "dropped_events": counters.get("dropped_events", 0),
        "offline_minutes": round(counters.get("offline_seconds", 0) / 60),
    }


def report_text(r):
    def pct(n, d):
        return f"{n * 100 // d}%" if d else "-"

    with_reads = r["events_with_plate"] + r.get("snapshot_new", 0)
    lines = [
        f"車牌收集日報 {r['date']}",
        "",
        "【有車經過（畫面有動靜）】",
        f"有動靜：{r['events']} 次",
        f"其中找到車牌字樣（約等於經過的車數）：{r['events_with_plate']} 台",
        f"  白天 {r['day']['with_plate']} 台（動靜 {r['day']['events']} 次）",
        f"  晚上 {r['night']['with_plate']} 台（動靜 {r['night']['events']} 次）",
        "",
        "【定時拍照（停著的車、機車）】",
        f"拍到新的車牌字樣：{r.get('snapshot_new', 0)} 次（同一台一小時內只算一次）",
        "",
        "【認字品質（以上全部）】",
        f"符合台灣車牌格式、且信心度 0.8 以上：{r['format_ok']} 次（{pct(r['format_ok'], with_reads)}）",
        f"連拍 3 張以上認出一樣的字：{r['three_votes']} 台（{pct(r['three_votes'], r['events_with_plate'])}）",
        "最高信心度分布：",
    ]
    for name, n in r["conf_distribution"].items():
        lines.append(f"  {name:<10} {n:>5}  {'█' * min(n, 60)}")
    lines += ["", "各攝影機："]
    for cam, c in r.get("cameras", {}).items():
        lines.append(f"  {cam}：動靜 {c.get('motion', 0)} 次（有車牌 {c.get('motion_with_plate', 0)}）、"
                     f"定時新車牌 {c.get('snapshot_new', 0)} 次")
    lines += ["", "每小時動靜次數："]
    for hour, n in r["by_hour"].items():
        lines.append(f"  {hour} 時 {n:>5}")
    lines += [
        "",
        f"來不及處理而略過：{r['dropped_events']} 次",
        f"攝影機斷線（各支加總）：約 {r['offline_minutes']} 分鐘",
    ]
    return "\n".join(lines) + "\n"


def write_report(data_dir, day):
    r = build_report(data_dir, day)
    out = storage.reports_dir(data_dir)
    out.mkdir(parents=True, exist_ok=True)
    storage.write_json(out / f"{day}.json", r)
    (out / f"{day}.txt").write_text(report_text(r), encoding="utf-8")
    return r


def fill_missing_reports(data_dir, today):
    """把今天以前、還沒有報表的日子補產生。"""
    done = []
    for day in storage.list_days(data_dir):
        if day < today and not (storage.reports_dir(data_dir) / f"{day}.json").exists():
            write_report(data_dir, day)
            done.append(day)
    return done


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = storage.load_config()
    data_dir = storage.resolve_data_dir(cfg)
    if len(sys.argv) > 1:
        r = write_report(data_dir, sys.argv[1])
        print(report_text(r))
    else:
        done = fill_missing_reports(data_dir, storage.today())
        print("已產生：" + ("、".join(done) if done else "沒有缺的"))


if __name__ == "__main__":
    main()
