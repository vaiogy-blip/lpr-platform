"""連拍投票（第 4 階段，計劃第一節的規則）。只做判斷、不開閘。

規則（計劃草案，數字都可以改）：
1. 每張畫面認出一個車牌字和信心度。
2. 信心度低於 min_conf 的不算票；不符合台灣車牌格式的不算票。
3. 同一個字先拿到 votes_needed 票的就算確定。
4. 從第一次認到車牌字開始 timeout_s 秒內湊不到 → 交給人判斷。
5. 確定之後比對白名單：完全一樣 → 「符合」；只差一個字 → 「差一字，不自動放行，提示人工」；
   其他 → 「不在名單」。
這裡的「符合」只是判斷結果；這條線的程式不會送出任何開閘指令。
"""
from collections import Counter

from plate_ocr import plate_format

RESULT_NAMES = {
    "match": "符合白名單（模擬放行，未開閘）",
    "near": "只差一個字，不自動放行，提示人工",
    "not_listed": "不在白名單",
    "handoff": "湊不到票，交給人判斷",
}


def edit_distance(a, b):
    """兩個字串差幾個字（插入、刪除、替換都算一個）。"""
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def check_whitelist(plate, whitelist):
    if plate in whitelist:
        return "match", plate
    near = sorted(w for w in whitelist if edit_distance(plate, w) == 1)
    if near:
        return "near", near[0]
    return "not_listed", None


class Voter:
    def __init__(self, votes_needed=3, min_conf=0.8, timeout_s=3.0, whitelist=()):
        self.votes_needed = votes_needed
        self.min_conf = min_conf
        self.timeout_s = timeout_s
        self.whitelist = set(whitelist)
        self.reset()

    def reset(self):
        self.t0 = None
        self.tally = Counter()
        self.reads = 0
        self.decision = None

    def feed(self, t, reads):
        """t：這張畫面的時間（秒）；reads：這張畫面認到的 [(字, 信心度)]（可能 0 個或好幾個）。
        決定了就回傳 decision dict，否則 None。決定之後再餵也只回傳同一個結果。"""
        if self.decision:
            return self.decision
        if self.t0 is None:
            if not reads:
                return None          # 還沒認到任何車牌字，計時還沒開始
            self.t0 = t
        if t - self.t0 > self.timeout_s:
            return self._handoff(t)
        self.reads += 1
        seen = set()
        for text, conf in reads:
            if text in seen:
                continue          # 同一張畫面同一個字只算一票
            seen.add(text)
            if conf >= self.min_conf and plate_format(text):
                self.tally[text] += 1
                if self.tally[text] >= self.votes_needed:
                    result, ref = check_whitelist(text, self.whitelist)
                    self.decision = {"result": result, "plate": text, "whitelist_ref": ref,
                                     "votes": dict(self.tally), "frames": self.reads,
                                     "elapsed_s": round(t - self.t0, 2)}
                    return self.decision
        return None

    def _handoff(self, t):
        self.decision = {"result": "handoff", "plate": None, "whitelist_ref": None,
                         "votes": dict(self.tally), "frames": self.reads,
                         "elapsed_s": round(t - self.t0, 2)}
        return self.decision

    def finish(self, t):
        """畫面結束（車走了、影片完了）還沒決定 → 交給人。"""
        if not self.decision and self.t0 is not None:
            return self._handoff(t)
        return self.decision
