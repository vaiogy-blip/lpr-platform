# -*- coding: utf-8 -*-
"""
PSC 資料庫（SQLite，psc/data/psc.db，不進 git）。

三層：companies（主控 is_root=1／客戶公司）→ sites（社區、停車場）→ gateways（現場小主機）。
帳號 users 屬於某家公司，權限來自 roles（權限群組，由各公司管理員自己定義）；site_ids 是 JSON 陣列，
null 代表「這家公司的全部場站」（主控公司的帳號則是全部公司的場站）。
"""

import json
import os
import sqlite3
import threading
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DB_PATH = os.environ.get("PSC_DB") or os.path.join(DATA, "psc.db")

# 權限（順序就是畫面上的順序）；company.manage 只有主控公司能有
PERMS = [
    ("site.view", "看場站", "看場站、小主機、攝影機的上線狀態"),
    ("wl.view", "看白名單", "讀取白名單"),
    ("wl.edit", "改白名單", "新增、刪除白名單（白名單車會自動開閘）"),
    ("photo.view", "看照片", "查詢、查看 SD 卡照片與進出紀錄的照片（個資，會留紀錄）"),
    ("event.view", "看進出紀錄", "查看車輛進出紀錄（個資，會留紀錄）"),
    ("param.view", "看參數", "讀取攝影機參數、字幕機設定、重開機排程"),
    ("param.edit", "改參數", "修改攝影機參數（名稱、出入口、音量、辨識）"),
    ("gate.rule", "改開閘規則", "修改白名單開閘規則、開閘輸出口（只有主控公司）"),
    ("led.edit", "改字幕機", "修改字幕機顯示內容"),
    ("camera.reboot", "重開攝影機", "遠端重開攝影機"),
    ("site.manage", "管理場站", "新增、修改場站，新增現場小主機"),
    ("user.manage", "管理帳號", "新增帳號、權限群組，重設別人的密碼"),
    ("audit.view", "看操作紀錄", "查看誰在什麼時候做了什麼"),
    ("company.manage", "管理客戶公司", "新增、停用客戶公司（只有主控公司）"),
]
PERM_KEYS = [p[0] for p in PERMS]
ROOT_ONLY = {"company.manage", "gate.rule"}
SITE_KINDS = {"community": "社區", "parking": "停車場", "other": "其他"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, is_root INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sites(
  id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'community', address TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gateways(
  id TEXT PRIMARY KEY, site_id INTEGER NOT NULL REFERENCES sites(id), name TEXT NOT NULL,
  token_hash TEXT NOT NULL, cameras TEXT NOT NULL DEFAULT '[]', created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS roles(
  id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id), name TEXT NOT NULL,
  perms TEXT NOT NULL, is_system INTEGER NOT NULL DEFAULT 0, UNIQUE(company_id, name));
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
  username TEXT NOT NULL UNIQUE COLLATE NOCASE, display_name TEXT NOT NULL, pw_hash TEXT NOT NULL,
  role_id INTEGER NOT NULL REFERENCES roles(id), site_ids TEXT, active INTEGER NOT NULL DEFAULT 1,
  must_change_pw INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, company_id INTEGER, site_id INTEGER, username TEXT,
  ip TEXT, action TEXT NOT NULL, target TEXT, detail TEXT, result TEXT);
CREATE INDEX IF NOT EXISTS audit_company ON audit(company_id, id);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, received TEXT NOT NULL, site_id INTEGER NOT NULL REFERENCES sites(id),
  gateway_id TEXT NOT NULL, camera_id TEXT NOT NULL, camera_name TEXT NOT NULL, plate TEXT NOT NULL,
  direction TEXT NOT NULL, is_whitelist INTEGER NOT NULL, confidence INTEGER, photo BLOB);
CREATE INDEX IF NOT EXISTS events_site_ts ON events(site_id, ts);
"""

_lock = threading.RLock()
_conn = None


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def conn():
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
        _conn.executescript(SCHEMA)
        # 同一支攝影機同一秒同一車牌只留一筆（小主機重送不會重複）；建索引前先清掉舊資料裡的重複，不然會啟動失敗
        if not _conn.execute("SELECT 1 FROM sqlite_master WHERE name='events_dedupe'").fetchone():
            _conn.execute("""DELETE FROM events WHERE id NOT IN
                             (SELECT MIN(id) FROM events GROUP BY site_id, camera_id, ts, plate)""")
            _conn.execute("CREATE UNIQUE INDEX events_dedupe ON events(site_id, camera_id, ts, plate)")
    return _conn


def q(sql, args=()):
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def run(sql, args=()):
    with _lock:
        return conn().execute(sql, args).lastrowid


def tx(fn):
    """在同一個交易裡做好幾件事（例如新增公司＋管理員群組＋管理員帳號），失敗全部還原。"""
    with _lock:
        c = conn()
        c.execute("BEGIN")
        try:
            result = fn()
            c.execute("COMMIT")
            return result
        except BaseException:
            c.execute("ROLLBACK")
            raise


def admin_perms(is_root):
    return [p for p in PERM_KEYS if is_root or p not in ROOT_ONLY]


def create_company(name, is_root=False):
    """新增公司，同時建立不能刪的「管理員」群組。回傳 (company_id, admin_role_id)。"""
    cid = run("INSERT INTO companies(name, is_root, created) VALUES(?,?,?)", (name, 1 if is_root else 0, now()))
    rid = run("INSERT INTO roles(company_id, name, perms, is_system) VALUES(?,?,?,1)",
              (cid, "管理員", json.dumps(admin_perms(is_root))))
    return cid, rid


def audit(user, ip, action, target="", detail="", result="成功", company_id=None, site_id=None):
    clean = lambda s: " ".join(str(s).split())[:2000]
    run("INSERT INTO audit(ts, company_id, site_id, username, ip, action, target, detail, result) VALUES(?,?,?,?,?,?,?,?,?)",
        (now(), company_id if company_id is not None else (user or {}).get("company_id"), site_id,
         (user or {}).get("username", "-"), clean(ip), clean(action), clean(target), clean(detail), clean(result)))
