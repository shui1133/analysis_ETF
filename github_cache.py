# github_cache.py ── 三層快取管理（本機 / GitHub / yfinance）
# 修正紀錄：
#   v2 - GitHub PUT 改為序列化寫入 + SHA 衝突指數退避重試
#   v3 - 補齊 app.py 所需全部介面：GitHubCache / TOP50_STOCKS /
#        _gh_raw_get / _gh_writer / start_scheduler（不含 watchlist_reader 參數）
#        gh_save_price 支援完整 OHLCV 欄位（open/high/low/volume）
# ============================================================

from __future__ import annotations

import os
import csv
import json
import time
import random
import logging
import threading
import io
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Callable
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)

# ── 常數 ────────────────────────────────────────────────────
REPO     = "shui1133/analysis_ETF"
BRANCH   = "main"
API_BASE = f"https://api.github.com/repos/{REPO}/contents"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"
TW_TZ    = ZoneInfo("Asia/Taipei")

TTL_PRICE = timedelta(hours=20)
TTL_DIV   = timedelta(days=7)
TTL_FUND  = timedelta(days=3)

# app.py 的 Warmup 需要這個 set
TOP50_STOCKS: set = {
    '2330','2317','2454','2382','2308','2881','2882','2891',
    '2886','2887','2303','2412','1301','1303','2002','4938',
    '2395','3008','2357','0050','0056','00878','00919',
    '00929','006208','00713',
}

# ── 全域寫入鎖（確保同一 process 內序列寫入 GitHub）────────────
_gh_write_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════
# 低層 GitHub API
# ══════════════════════════════════════════════════════════════

def _gh_headers() -> Dict[str, str]:
    token = os.environ.get("GH_CACHE_TOKEN", "")
    h: Dict[str, str] = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _gh_get_file(path: str) -> Optional[Dict]:
    """取得 GitHub 上的檔案 meta（含 sha）；不存在回傳 None。"""
    url = f"{API_BASE}/{path}?ref={BRANCH}"
    try:
        resp = requests.get(url, headers=_gh_headers(), timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.debug("[GitHub-R] GET %s 失敗: %s", path, e)
    return None


def _gh_raw_get(path: str) -> Optional[str]:
    """
    直接從 raw.githubusercontent.com 讀取檔案內容（純文字）。
    供 /api/stock_cache 和 /api/ai_report GET 使用。
    """
    url = f"{RAW_BASE}/{path}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            return resp.text
    except Exception as e:
        logger.debug("[GitHub-R] raw_get %s 失敗: %s", path, e)
    return None


def _gh_put_file_safe(path: str, content_bytes: bytes,
                      commit_msg: str, retries: int = 3) -> bool:
    """
    PUT 檔案至 GitHub，具備 SHA 衝突自動修正。

    修正重點：
    - 持有全域鎖，確保同一 process 不並發寫入（杜絕 422）
    - 每次嘗試前重新取得最新 SHA
    - 指數退避 + 隨機抖動
    """
    token = os.environ.get("GH_CACHE_TOKEN", "")
    if not token:
        logger.debug("[GitHub-W] 無 token，跳過 %s", path)
        return False

    import base64
    encoded = base64.b64encode(content_bytes).decode()
    url     = f"{API_BASE}/{path}"

    with _gh_write_lock:
        for attempt in range(1, retries + 1):
            # 每次重新取最新 SHA（避免使用過期值，這是 422 的根本原因）
            existing = _gh_get_file(path)
            sha      = existing["sha"] if existing else None

            payload: Dict[str, Any] = {
                "message": commit_msg,
                "content": encoded,
                "branch":  BRANCH,
            }
            if sha:
                payload["sha"] = sha

            try:
                resp = requests.put(url, headers=_gh_headers(),
                                    json=payload, timeout=20)
            except Exception as e:
                logger.warning("[GitHub-W] PUT %s 網路錯誤 (attempt %d): %s",
                               path, attempt, e)
                time.sleep(2 ** attempt)
                continue

            if resp.status_code in (200, 201):
                action = "建立" if not sha else "更新"
                logger.info("[GitHub-W] ✓ %s %s", action, path)
                return True

            if resp.status_code == 422:
                wait = (2 ** attempt) + random.random()
                logger.warning(
                    "[GitHub-W] ▲ PUT %s 422 SHA 衝突 (attempt %d)，%.1fs 後重試...",
                    path, attempt, wait
                )
                if attempt < retries:
                    time.sleep(wait)
                continue

            logger.error("[GitHub-W] ✗ PUT %s HTTP %d: %s",
                         path, resp.status_code, resp.text[:200])
            return False

        logger.error("[GitHub-W] ✗ PUT %s %d 次重試均失敗", path, retries)
        return False


# ── _gh_writer：app.py ai_report 使用的輕量 writer 物件 ─────

class _GhWriterClass:
    """app.py 透過 _gh_writer.put(path, text, msg) 寫入 GitHub。"""
    def put(self, path: str, text: str, commit_msg: str = "[bot] update") -> bool:
        return _gh_put_file_safe(path, text.encode("utf-8"), commit_msg)

_gh_writer = _GhWriterClass()


# ══════════════════════════════════════════════════════════════
# TTL 判斷
# ══════════════════════════════════════════════════════════════

def _is_stale(path: str, ttl: timedelta) -> bool:
    """
    雙重 TTL：
    1. 距上次修改超過 ttl
    2. 今日已過 13:30 且上次修改在 13:30 前 → 強制過期
    """
    if not os.path.exists(path):
        return True
    mtime  = datetime.fromtimestamp(os.path.getmtime(path), tz=TW_TZ)
    now    = datetime.now(tz=TW_TZ)
    if now - mtime > ttl:
        return True
    cutoff = now.replace(hour=13, minute=30, second=0, microsecond=0)
    if now >= cutoff and mtime < cutoff:
        return True
    return False


# ══════════════════════════════════════════════════════════════
# 本機存檔（price / dividend / fundamental）
# ══════════════════════════════════════════════════════════════

def _ensure_dir(data_dir: str, ticker: str) -> str:
    d = os.path.join(data_dir, ticker)
    os.makedirs(d, exist_ok=True)
    return d


def local_save_price(data_dir: str, ticker: str, rows: List[Dict]) -> bool:
    """
    存本機 price CSV。
    支援完整 OHLCV（date/open/high/low/close/volume）
    及精簡格式（date/close）。
    """
    if not rows:
        return False
    _ensure_dir(data_dir, ticker)
    path = os.path.join(data_dir, ticker, "price.csv")
    try:
        sample    = rows[0]
        has_ohlcv = any(k in sample for k in ('open', 'Open', 'high', 'High'))
        fieldnames = (['date', 'open', 'high', 'low', 'close', 'volume']
                      if has_ohlcv else ['date', 'close'])

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            for r in rows:
                close = r.get('close') or r.get('Close') or r.get('收盤價', '')
                row: Dict[str, Any] = {
                    'date':  r.get('date') or r.get('日期', ''),
                    'close': close,
                }
                if has_ohlcv:
                    row['open']   = r.get('open')   or r.get('Open',   close)
                    row['high']   = r.get('high')   or r.get('High',   close)
                    row['low']    = r.get('low')    or r.get('Low',    close)
                    row['volume'] = r.get('volume') or r.get('Volume', 0)
                writer.writerow(row)
        return True
    except Exception as e:
        logger.error("local_save_price %s: %s", ticker, e)
        return False


def local_save_dividend(data_dir: str, ticker: str, rows: List[Dict]) -> bool:
    if not rows:
        return False
    _ensure_dir(data_dir, ticker)
    path = os.path.join(data_dir, ticker, "dividend.csv")
    try:
        keys = list(rows[0].keys())
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)
        return True
    except Exception as e:
        logger.error("local_save_dividend %s: %s", ticker, e)
        return False


def local_save_fundamental(data_dir: str, ticker: str, info: Dict) -> bool:
    if not info:
        return False
    _ensure_dir(data_dir, ticker)
    path = os.path.join(data_dir, ticker, "fundamental.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error("local_save_fundamental %s: %s", ticker, e)
        return False


# ══════════════════════════════════════════════════════════════
# GitHub 存檔（依序呼叫，不並發）
# ══════════════════════════════════════════════════════════════

def gh_save_price(ticker: str, rows: List[Dict]) -> bool:
    """
    寫入 price 至 GitHub。
    支援完整 OHLCV 與精簡 date/close 兩種格式。
    """
    if not rows:
        return False
    sample    = rows[0]
    has_ohlcv = any(k in sample for k in ('open', 'Open', 'high', 'High'))

    if has_ohlcv:
        lines = ["date,open,high,low,close,volume"]
        for r in rows:
            close = r.get('close') or r.get('Close') or r.get('收盤價', '')
            lines.append(",".join(str(x) for x in [
                r.get('date') or r.get('日期', ''),
                r.get('open')   or r.get('Open',   close),
                r.get('high')   or r.get('High',   close),
                r.get('low')    or r.get('Low',    close),
                close,
                r.get('volume') or r.get('Volume', 0),
            ]))
    else:
        lines = ["date,close"]
        for r in rows:
            lines.append(f"{r.get('date','')},{r.get('close','')}")

    content = "\n".join(lines).encode("utf-8")
    return _gh_put_file_safe(
        f"data/{ticker}/price.csv", content, f"[bot] update {ticker} price"
    )


def gh_save_dividend(ticker: str, rows: List[Dict]) -> bool:
    if not rows:
        return False
    keys  = list(rows[0].keys())
    lines = [",".join(keys)]
    for r in rows:
        lines.append(",".join(str(r.get(k, "")) for k in keys))
    content = "\n".join(lines).encode("utf-8")
    return _gh_put_file_safe(
        f"data/{ticker}/dividend.csv", content, f"[bot] update {ticker} dividend"
    )


def gh_save_fundamental(ticker: str, info: Dict) -> bool:
    if not info:
        return False
    content = json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8")
    return _gh_put_file_safe(
        f"data/{ticker}/fundamental.json", content,
        f"[bot] update {ticker} fundamental"
    )


# ══════════════════════════════════════════════════════════════
# GitHub 讀取
# ══════════════════════════════════════════════════════════════

def _gh_fetch_csv(path: str) -> Optional[List[Dict]]:
    text = _gh_raw_get(path)
    if not text:
        return None
    rows = list(csv.DictReader(io.StringIO(text)))
    return rows if rows else None


def gh_fetch_price(ticker: str) -> Optional[List[Dict]]:
    return _gh_fetch_csv(f"data/{ticker}/price.csv")


def gh_fetch_dividend(ticker: str) -> Optional[List[Dict]]:
    return _gh_fetch_csv(f"data/{ticker}/dividend.csv")


def gh_fetch_fundamental(ticker: str) -> Optional[Dict]:
    text = _gh_raw_get(f"data/{ticker}/fundamental.json")
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
# GitHubCache：app.py Warmup 相容介面
# ══════════════════════════════════════════════════════════════

class GitHubCache:
    """
    Warmup / 舊版相容介面包裝。

    修正紀錄：
    - 補齊 save_price / save_dividend / save_fundamental
      （app.py 部分路徑仍呼叫這些方法，避免 AttributeError）
    """

    def __init__(self) -> None:
        self.enabled = bool(os.environ.get("GH_CACHE_TOKEN", ""))

    def is_fresh(self, ticker: str, kind: str) -> bool:
        ext = {"price": "price.csv",
               "dividend": "dividend.csv",
               "fundamental": "fundamental.json"}.get(kind)
        if not ext:
            return False
        return _gh_raw_get(f"data/{ticker}/{ext}") is not None

    # ── 讀取 ────────────────────────────────────────────────────
    def load_price(self, ticker: str) -> Optional[List[Dict]]:
        return gh_fetch_price(ticker)

    def load_dividend(self, ticker: str) -> Optional[List[Dict]]:
        return gh_fetch_dividend(ticker)

    def load_fundamental(self, ticker: str) -> Optional[Dict]:
        return gh_fetch_fundamental(ticker)

    # ── 寫入（補齊缺失方法，修正 AttributeError）────────────────
    def save_price(self, ticker: str, rows: List[Dict]) -> bool:
        """相容舊呼叫：gh_cache.save_price(ticker, rows)"""
        return gh_save_price(ticker, rows)

    def save_dividend(self, ticker: str, rows: List[Dict]) -> bool:
        """相容舊呼叫：gh_cache.save_dividend(ticker, rows)"""
        return gh_save_dividend(ticker, rows)

    def save_fundamental(self, ticker: str, info: Dict) -> bool:
        """相容舊呼叫：gh_cache.save_fundamental(ticker, info)"""
        return gh_save_fundamental(ticker, info)


# ══════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════
# 輔助：修補精簡格式 price rows（date/close 無 open 欄位）
# ══════════════════════════════════════════════════════════════

def _patch_open_with_prev_close(rows: List[Dict]) -> List[Dict]:
    """
    ★ 修正 Bug：精簡快取格式（date, close）缺少 open/high/low/volume 欄位，
    導致前端 K 棒實體全為 null、日線紅/綠色柱子消失。

    此函數檢查每列是否缺少 open 欄位，若缺值則以前根 close 填補，
    使漲跌方向（close vs open）可被正確判斷。
    不修改已有合法 open 值的列。
    """
    if not rows:
        return rows
    patched = []
    prev_close = None
    for r in rows:
        row = dict(r)  # 避免修改原始 dict
        # 判斷 open 欄位是否存在且有效
        open_val = row.get('open') or row.get('Open') or row.get('開盤價')
        close_val = row.get('close') or row.get('Close') or row.get('收盤價')
        try:
            close_f = float(close_val) if close_val else None
        except (ValueError, TypeError):
            close_f = None

        if close_f is not None:
            # 若 open 缺值，用前根 close（或當根 close）填補
            try:
                open_f = float(open_val) if open_val else None
            except (ValueError, TypeError):
                open_f = None

            if open_f is None:
                row['open'] = prev_close if prev_close is not None else close_f
            prev_close = close_f

        patched.append(row)
    return patched


# CacheManager：三層快取主體
# ══════════════════════════════════════════════════════════════

class CacheManager:

    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir

    def _local_path(self, ticker: str, filename: str) -> str:
        return os.path.join(self.data_dir, ticker, filename)

    def get_price(self, ticker: str,
                  fetcher=None,
                  force_refresh: bool = False) -> Optional[List[Dict]]:
        path = self._local_path(ticker, "price.csv")
        if not force_refresh and not _is_stale(path, TTL_PRICE):
            try:
                with open(path, encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))
                if rows:
                    # ★ 修正：若 CSV 為精簡格式（只有 date/close），補上 open=prev_close
                    #   確保後續 _norm_*_ohlcv 可以正確判斷漲跌方向，日線 K 棒才有顏色
                    rows = _patch_open_with_prev_close(rows)
                    return rows
            except Exception:
                pass

        rows = gh_fetch_price(ticker)
        if rows and len(rows) >= 20:
            rows = _patch_open_with_prev_close(rows)
            local_save_price(self.data_dir, ticker, rows)
            return rows

        if fetcher:
            try:
                rows = fetcher.fetch_price(ticker)
                if rows:
                    local_save_price(self.data_dir, ticker, rows)
                    gh_save_price(ticker, rows)
                    return rows
            except Exception as e:
                logger.error("get_price L3 %s: %s", ticker, e)
        return None

    def get_dividend(self, ticker: str,
                     fetcher=None,
                     force_refresh: bool = False) -> Optional[List[Dict]]:
        path = self._local_path(ticker, "dividend.csv")
        if not force_refresh and not _is_stale(path, TTL_DIV):
            try:
                with open(path, encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))
                if rows:
                    return rows
            except Exception:
                pass

        rows = gh_fetch_dividend(ticker)
        if rows:
            local_save_dividend(self.data_dir, ticker, rows)
            return rows

        if fetcher:
            try:
                rows = fetcher.fetch_dividend(ticker)
                if rows:
                    local_save_dividend(self.data_dir, ticker, rows)
                    gh_save_dividend(ticker, rows)
                    return rows
            except Exception as e:
                logger.error("get_dividend L3 %s: %s", ticker, e)
        return None

    def get_fundamental(self, ticker: str,
                        fetcher=None,
                        force_refresh: bool = False) -> Optional[Dict]:
        path = self._local_path(ticker, "fundamental.json")
        if not force_refresh and not _is_stale(path, TTL_FUND):
            try:
                with open(path, encoding="utf-8") as f:
                    info = json.load(f)
                if info:
                    return info
            except Exception:
                pass

        info = gh_fetch_fundamental(ticker)
        if info:
            local_save_fundamental(self.data_dir, ticker, info)
            return info

        if fetcher:
            try:
                info = fetcher.fetch_fundamental(ticker)
                if info:
                    local_save_fundamental(self.data_dir, ticker, info)
                    gh_save_fundamental(ticker, info)
                    return info
            except Exception as e:
                logger.error("get_fundamental L3 %s: %s", ticker, e)
        return None

    def force_refresh(self, ticker: str, fetcher) -> bool:
        """依序抓取 price → dividend → fundamental，同步更新本機 + GitHub。"""
        ok = True
        # price
        try:
            rows = fetcher.fetch_price(ticker)
            if rows:
                local_save_price(self.data_dir, ticker, rows)
                ok &= gh_save_price(ticker, rows)
        except Exception as e:
            logger.error("force_refresh price %s: %s", ticker, e)
            ok = False
        # dividend
        try:
            rows = fetcher.fetch_dividend(ticker)
            if rows:
                local_save_dividend(self.data_dir, ticker, rows)
                ok &= gh_save_dividend(ticker, rows)
        except Exception as e:
            logger.error("force_refresh dividend %s: %s", ticker, e)
            ok = False
        # fundamental
        try:
            info = fetcher.fetch_fundamental(ticker)
            if info:
                local_save_fundamental(self.data_dir, ticker, info)
                ok &= gh_save_fundamental(ticker, info)
        except Exception as e:
            logger.error("force_refresh fundamental %s: %s", ticker, e)
            ok = False
        return ok


# ══════════════════════════════════════════════════════════════
# 背景排程
# ══════════════════════════════════════════════════════════════

def start_scheduler(cache: "CacheManager",
                    fetcher_factory: Callable,
                    popular_stocks: List[str],
                    watchlist_reader: Optional[Callable] = None):
    """
    每日 13:30（台灣時間）自動更新所有股票快取。
    依序（非並發）處理，避免 GitHub API 競爭。

    參數：
      cache            - CacheManager 實例
      fetcher_factory  - 回傳 ETFDataFetcher 的 callable
      popular_stocks   - 熱門股票代碼清單
      watchlist_reader - （選用）回傳自選股清單的 callable
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning(
            "apscheduler 未安裝，排程停用。"
            "請執行：pip install apscheduler pytz"
        )
        return None

    def _job():
        logger.info("[排程] 開始每日 13:30 快取更新...")
        fetcher   = fetcher_factory()
        watchlist: List[str] = []
        if callable(watchlist_reader):
            try:
                watchlist = list(watchlist_reader())
            except Exception:
                pass
        tickers = list(set(list(popular_stocks) + watchlist))

        for tk in tickers:
            logger.info("[排程] force_refresh: %s", tk)
            try:
                cache.force_refresh(tk, fetcher)
            except Exception as e:
                logger.error("[排程] %s 失敗: %s", tk, e)
            time.sleep(0.5)  # 降低 GitHub API 壓力

        logger.info("[排程] 完成，共 %d 支", len(tickers))

    scheduler = BackgroundScheduler(timezone="Asia/Taipei")
    scheduler.add_job(
        _job,
        trigger=CronTrigger(hour=13, minute=30, timezone="Asia/Taipei"),
        id="daily_cache_refresh",
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.start()
    logger.info("[排程] 已啟動，每日 13:30 (TW) 自動更新")
    return scheduler
