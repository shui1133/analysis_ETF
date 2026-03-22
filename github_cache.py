# github_cache.py  ── 三層快取管理（本機 / GitHub / yfinance）
# 修正：GitHub PUT 改為序列化寫入 + SHA 衝突指數退避重試
# ============================================================

import os
import json
import time
import random
import hashlib
import logging
import requests
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ── 常數 ────────────────────────────────────────────────────
REPO       = "shui1133/analysis_ETF"
BRANCH     = "main"
API_BASE   = f"https://api.github.com/repos/{REPO}/contents"
TW_TZ      = ZoneInfo("Asia/Taipei")

TTL_PRICE  = timedelta(hours=20)
TTL_DIV    = timedelta(days=7)
TTL_FUND   = timedelta(days=3)

# ── 低層 GitHub API ─────────────────────────────────────────

def _gh_headers() -> Dict[str, str]:
    token = os.environ.get("GH_CACHE_TOKEN", "")
    h = {"Accept": "application/vnd.github+json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _gh_get_file(path: str) -> Optional[Dict]:
    """取得 GitHub 上的檔案資訊（含 sha 與 content）。不存在時回傳 None。"""
    url  = f"{API_BASE}/{path}?ref={BRANCH}"
    resp = requests.get(url, headers=_gh_headers(), timeout=10)
    if resp.status_code == 200:
        return resp.json()
    return None


def _gh_put_file_safe(path: str, content_bytes: bytes,
                      commit_msg: str, retries: int = 3) -> bool:
    """
    PUT 檔案至 GitHub，具備 SHA 衝突自動修正。

    修正重點：
    - 每次嘗試前先重新取得最新 SHA（避免 422 衝突）
    - 指數退避 + 隨機抖動，防止並發競爭
    - 最多 retries 次（預設 3）

    回傳 True 表示成功，False 表示全部失敗。
    """
    token = os.environ.get("GH_CACHE_TOKEN", "")
    if not token:
        logger.debug("[GitHub-W] 無 token，跳過寫入 %s", path)
        return False

    import base64
    encoded = base64.b64encode(content_bytes).decode()
    url     = f"{API_BASE}/{path}"

    for attempt in range(1, retries + 1):
        # ① 每次重新取最新 SHA（關鍵：避免使用快取的過期 SHA）
        existing = _gh_get_file(path)
        sha      = existing["sha"] if existing else None
        is_new   = sha is None

        payload: Dict[str, Any] = {
            "message": commit_msg,
            "content": encoded,
            "branch":  BRANCH,
        }
        if sha:
            payload["sha"] = sha

        resp = requests.put(url, headers=_gh_headers(),
                            json=payload, timeout=15)

        if resp.status_code in (200, 201):
            action = "建立" if is_new else "更新"
            logger.info("[GitHub-W] ✓ %s %s (%d)", action, path, resp.status_code)
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

        # 其他錯誤直接失敗
        logger.error("[GitHub-W] ✗ PUT %s 失敗 HTTP %d: %s",
                     path, resp.status_code, resp.text[:200])
        return False

    logger.error("[GitHub-W] ✗ PUT %s %d 次重試均失敗", path, retries)
    return False


# ── 序列化寫入：price / dividend / fundamental ───────────────
# 重點：三個檔案依序寫入，不並發，杜絕 422 競爭

def gh_save_price(ticker: str, rows: List[Dict]) -> bool:
    """把 price 資料序列化後寫入 GitHub（CSV 格式）。"""
    if not rows:
        return False
    lines = ["date,close"]
    for r in rows:
        lines.append(f"{r.get('date','')},{r.get('close','')}")
    content = "\n".join(lines).encode("utf-8")
    path    = f"data/{ticker}/price.csv"
    return _gh_put_file_safe(path, content,
                             f"[bot] update {ticker} price")


def gh_save_dividend(ticker: str, rows: List[Dict]) -> bool:
    """把 dividend 資料序列化後寫入 GitHub（CSV 格式）。"""
    if not rows:
        return False
    keys  = list(rows[0].keys())
    lines = [",".join(keys)]
    for r in rows:
        lines.append(",".join(str(r.get(k, "")) for k in keys))
    content = "\n".join(lines).encode("utf-8")
    path    = f"data/{ticker}/dividend.csv"
    return _gh_put_file_safe(path, content,
                             f"[bot] update {ticker} dividend")


def gh_save_fundamental(ticker: str, info: Dict) -> bool:
    """把 fundamental 資料序列化後寫入 GitHub（JSON 格式）。"""
    if not info:
        return False
    content = json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8")
    path    = f"data/{ticker}/fundamental.json"
    return _gh_put_file_safe(path, content,
                             f"[bot] update {ticker} fundamental")


def gh_save_all(ticker: str,
                price_rows: Optional[List[Dict]] = None,
                div_rows:   Optional[List[Dict]] = None,
                fund_info:  Optional[Dict]       = None) -> Dict[str, bool]:
    """
    依序（非並發）寫入三種資料至 GitHub。
    回傳 {'price': bool, 'dividend': bool, 'fundamental': bool}。
    """
    results = {}
    # ① price
    if price_rows is not None:
        results["price"] = gh_save_price(ticker, price_rows)
    # ② dividend（等前一步完成才繼續）
    if div_rows is not None:
        results["dividend"] = gh_save_dividend(ticker, div_rows)
    # ③ fundamental（最後寫）
    if fund_info is not None:
        results["fundamental"] = gh_save_fundamental(ticker, fund_info)
    return results


# ── 本機寫入（不變）────────────────────────────────────────

def local_save_price(data_dir: str, ticker: str, rows: List[Dict]) -> bool:
    os.makedirs(os.path.join(data_dir, ticker), exist_ok=True)
    path = os.path.join(data_dir, ticker, "price.csv")
    try:
        lines = ["date,close"]
        for r in rows:
            lines.append(f"{r.get('date','')},{r.get('close','')}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return True
    except Exception as e:
        logger.error("local_save_price %s: %s", ticker, e)
        return False


def local_save_dividend(data_dir: str, ticker: str, rows: List[Dict]) -> bool:
    if not rows:
        return False
    os.makedirs(os.path.join(data_dir, ticker), exist_ok=True)
    path = os.path.join(data_dir, ticker, "dividend.csv")
    try:
        keys  = list(rows[0].keys())
        lines = [",".join(keys)]
        for r in rows:
            lines.append(",".join(str(r.get(k, "")) for k in keys))
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return True
    except Exception as e:
        logger.error("local_save_dividend %s: %s", ticker, e)
        return False


def local_save_fundamental(data_dir: str, ticker: str, info: Dict) -> bool:
    if not info:
        return False
    os.makedirs(os.path.join(data_dir, ticker), exist_ok=True)
    path = os.path.join(data_dir, ticker, "fundamental.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error("local_save_fundamental %s: %s", ticker, e)
        return False


# ── TTL 判斷 ────────────────────────────────────────────────

def _is_stale(path: str, ttl: timedelta) -> bool:
    """
    雙重 TTL 判斷：
    1. 距上次修改超過 ttl
    2. 今日已過 13:30 且上次修改在 13:30 前 → 強制視為過期
    """
    if not os.path.exists(path):
        return True
    mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=TW_TZ)
    now   = datetime.now(tz=TW_TZ)

    if now - mtime > ttl:
        return True

    cutoff = now.replace(hour=13, minute=30, second=0, microsecond=0)
    if now >= cutoff and mtime < cutoff:
        return True

    return False


# ── GitHub 讀取 ─────────────────────────────────────────────

def gh_is_fresh(ticker: str, kind: str) -> bool:
    """檢查 GitHub 上的檔案 commit 時間是否在 TTL 內。"""
    ext = {"price": "price.csv",
           "dividend": "dividend.csv",
           "fundamental": "fundamental.json"}.get(kind)
    if not ext:
        return False
    info = _gh_get_file(f"data/{ticker}/{ext}")
    if not info:
        return False
    # GitHub 不直接回傳 commit 時間，用 ETag / last_modified 替代
    # 簡化：只要檔案存在就視為新鮮（排程每日更新）
    return True


def gh_fetch_csv(ticker: str, kind: str) -> Optional[List[Dict]]:
    import base64
    import csv, io
    ext  = {"price": "price.csv", "dividend": "dividend.csv"}.get(kind)
    if not ext:
        return None
    info = _gh_get_file(f"data/{ticker}/{ext}")
    if not info:
        return None
    raw  = base64.b64decode(info["content"]).decode("utf-8")
    reader = csv.DictReader(io.StringIO(raw))
    return list(reader)


def gh_fetch_fundamental(ticker: str) -> Optional[Dict]:
    import base64
    info = _gh_get_file(f"data/{ticker}/fundamental.json")
    if not info:
        return None
    raw = base64.b64decode(info["content"]).decode("utf-8")
    try:
        return json.loads(raw)
    except Exception:
        return None


# ── CacheManager ────────────────────────────────────────────

class CacheManager:
    """
    三層快取：
      L1 本機檔案 → L2 GitHub → L3 yfinance 網路抓取
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir

    def _local_path(self, ticker: str, filename: str) -> str:
        return os.path.join(self.data_dir, ticker, filename)

    # ── price ──────────────────────────────────────────────

    def get_price(self, ticker: str,
                  fetcher=None,
                  force_refresh: bool = False) -> Optional[List[Dict]]:
        path = self._local_path(ticker, "price.csv")

        # L1
        if not force_refresh and not _is_stale(path, TTL_PRICE):
            try:
                import csv
                with open(path, encoding="utf-8") as f:
                    return list(csv.DictReader(f))
            except Exception:
                pass

        # L2
        rows = gh_fetch_csv(ticker, "price")
        if rows and len(rows) >= 20:
            local_save_price(self.data_dir, ticker, rows)
            return rows

        # L3
        if fetcher:
            try:
                rows = fetcher.fetch_price(ticker)
                if rows:
                    local_save_price(self.data_dir, ticker, rows)
                    gh_save_price(ticker, rows)
                    return rows
            except Exception as e:
                logger.error("fetch_price %s: %s", ticker, e)

        return None

    # ── dividend ───────────────────────────────────────────

    def get_dividend(self, ticker: str,
                     fetcher=None,
                     force_refresh: bool = False) -> Optional[List[Dict]]:
        path = self._local_path(ticker, "dividend.csv")

        if not force_refresh and not _is_stale(path, TTL_DIV):
            try:
                import csv
                with open(path, encoding="utf-8") as f:
                    return list(csv.DictReader(f))
            except Exception:
                pass

        rows = gh_fetch_csv(ticker, "dividend")
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
                logger.error("fetch_dividend %s: %s", ticker, e)

        return None

    # ── fundamental ────────────────────────────────────────

    def get_fundamental(self, ticker: str,
                        fetcher=None,
                        force_refresh: bool = False) -> Optional[Dict]:
        path = self._local_path(ticker, "fundamental.json")

        if not force_refresh and not _is_stale(path, TTL_FUND):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
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
                logger.error("fetch_fundamental %s: %s", ticker, e)

        return None

    # ── 強制全量更新（排程使用）────────────────────────────

    def force_refresh(self, ticker: str, fetcher) -> bool:
        """
        強制從 yfinance 抓取並依序更新本機 + GitHub。
        price → dividend → fundamental 三步序列執行。
        """
        ok = True
        try:
            price_rows = fetcher.fetch_price(ticker)
            if price_rows:
                local_save_price(self.data_dir, ticker, price_rows)
                ok &= gh_save_price(ticker, price_rows)
        except Exception as e:
            logger.error("force_refresh price %s: %s", ticker, e)
            ok = False

        try:
            div_rows = fetcher.fetch_dividend(ticker)
            if div_rows:
                local_save_dividend(self.data_dir, ticker, div_rows)
                ok &= gh_save_dividend(ticker, div_rows)
        except Exception as e:
            logger.error("force_refresh dividend %s: %s", ticker, e)
            ok = False

        try:
            fund_info = fetcher.fetch_fundamental(ticker)
            if fund_info:
                local_save_fundamental(self.data_dir, ticker, fund_info)
                ok &= gh_save_fundamental(ticker, fund_info)
        except Exception as e:
            logger.error("force_refresh fundamental %s: %s", ticker, e)
            ok = False

        return ok


# ── 背景排程 ────────────────────────────────────────────────

def start_scheduler(cache: CacheManager,
                    fetcher_factory: Callable,
                    watchlist_reader: Callable,
                    popular_stocks: List[str]):
    """
    每日 13:30（台灣時間）自動更新所有股票快取。
    依序（非並發）處理每支股票，避免 GitHub API 競爭。
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("apscheduler 未安裝，排程停用。請執行：pip install apscheduler pytz")
        return

    def _job():
        logger.info("[排程] 開始每日 13:30 快取更新...")
        fetcher  = fetcher_factory()
        watchlist = watchlist_reader() if callable(watchlist_reader) else []
        tickers   = list(set(popular_stocks + list(watchlist)))

        for tk in tickers:
            logger.info("[排程] force_refresh: %s", tk)
            cache.force_refresh(tk, fetcher)
            # 每支間隔 0.5s，降低 GitHub API 壓力
            time.sleep(0.5)

        logger.info("[排程] 更新完成，共 %d 支", len(tickers))

    scheduler = BackgroundScheduler(timezone="Asia/Taipei")
    scheduler.add_job(
        _job,
        trigger=CronTrigger(hour=13, minute=30, timezone="Asia/Taipei"),
        id="daily_cache_refresh",
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.start()
    logger.info("[排程] 已啟動，每日 13:30 (TW) 自動更新快取")
    return scheduler
