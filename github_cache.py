"""
github_cache.py - 三層持久化快取模組 v2
優先順序：本機硬碟 → GitHub Public Repo → yfinance

本機硬碟（L1）
  - 路徑：由 get_data_dir() 決定（與 app.py 共用 DATA_DIR）
  - 格式：{ticker}_price.csv / {ticker}_dividend.csv / {ticker}_fundamental.json
  - TTL 判斷：讀 {ticker}_meta.json 內的時間戳記

GitHub（L2，Public Repo 讀取不需 Token）
  - Repo  ：shui1133/analysis_ETF（固定）
  - 路徑  ：data/{ticker}/price.csv 等（與舊版相同）
  - 讀取  ：raw.githubusercontent.com（public，不需 token）
  - 寫入  ：GitHub API（需 GH_CACHE_TOKEN；無 token 時跳過寫入）

TTL（快取有效期）
  - 股價     : 20 小時
  - 配息     : 7 天
  - 基本面   : 3 天

背景排程（APScheduler）
  - 每日 13:30（台灣時間）自動更新 watchlist + POPULAR_STOCKS 查詢過的股票
  - 由 start_scheduler(app, fetcher_factory) 啟動，app.py 在 __main__ 時呼叫
"""

from __future__ import annotations

import os
import json
import base64
import time
import threading
import requests
import pandas as pd

from io import StringIO
from datetime import datetime, timedelta
from pathlib import Path


# ──────────────────────────────────────────────────────────────
# 常數
# ──────────────────────────────────────────────────────────────
GITHUB_REPO   = "shui1133/analysis_ETF"
GITHUB_BRANCH = "master"
RAW_BASE      = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}"
API_BASE      = f"https://api.github.com/repos/{GITHUB_REPO}/contents"

TTL_PRICE       = 60 * 60 * 20        # 20 小時
TTL_DIVIDEND    = 60 * 60 * 24 * 7    # 7 天
TTL_FUNDAMENTAL = 60 * 60 * 24 * 3    # 3 天
TTL_MAP         = {"price": TTL_PRICE, "dividend": TTL_DIVIDEND, "fundamental": TTL_FUNDAMENTAL}


# ──────────────────────────────────────────────────────────────
# 本機路徑輔助
# ──────────────────────────────────────────────────────────────
def _local_price_path(data_dir: str, ticker: str) -> str:
    return os.path.join(data_dir, f"{ticker}_price.csv")

def _local_dividend_path(data_dir: str, ticker: str) -> str:
    return os.path.join(data_dir, f"{ticker}_dividend.csv")

def _local_fundamental_path(data_dir: str, ticker: str) -> str:
    return os.path.join(data_dir, f"{ticker}_fundamental.json")

def _local_meta_path(data_dir: str, ticker: str) -> str:
    return os.path.join(data_dir, f"{ticker}_meta.json")


# ──────────────────────────────────────────────────────────────
# 本機 Meta（時間戳記）
# ──────────────────────────────────────────────────────────────
def _read_local_meta(data_dir: str, ticker: str) -> dict:
    path = _local_meta_path(data_dir, ticker)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def _write_local_meta(data_dir: str, ticker: str, meta: dict):
    path = _local_meta_path(data_dir, ticker)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [LocalCache] meta 寫入失敗 {ticker}: {e}")

def _stamp_local_meta(data_dir: str, ticker: str, data_type: str, extra: dict = None):
    """更新本機 meta 的時間戳記"""
    meta = _read_local_meta(data_dir, ticker)
    meta[f"{data_type}_at"] = datetime.now().isoformat()
    if extra:
        meta.update(extra)
    _write_local_meta(data_dir, ticker, meta)


# ──────────────────────────────────────────────────────────────
# TTL 判斷（通用）
# ──────────────────────────────────────────────────────────────
def is_local_fresh(data_dir: str, ticker: str, data_type: str) -> bool:
    """
    判斷本機快取是否仍在有效期內。
    同時滿足兩個條件才算有效：
      1. 時間戳記在 TTL 內
      2. 檔案實際存在且非空
    另外：若當天已過 13:30（台灣時間），且時間戳記在 13:30 之前，強制視為過期。
    """
    # --- 先確認檔案存在 ---
    file_map = {
        "price":       _local_price_path(data_dir, ticker),
        "dividend":    _local_dividend_path(data_dir, ticker),
        "fundamental": _local_fundamental_path(data_dir, ticker),
    }
    fpath = file_map.get(data_type, "")
    if not fpath or not os.path.exists(fpath) or os.path.getsize(fpath) == 0:
        return False

    # --- 讀時間戳記 ---
    meta = _read_local_meta(data_dir, ticker)
    ts_str = meta.get(f"{data_type}_at")
    if not ts_str:
        return False

    try:
        updated_dt = datetime.fromisoformat(ts_str)
    except Exception:
        return False

    now = datetime.now()
    elapsed = (now - updated_dt).total_seconds()
    ttl = TTL_MAP.get(data_type, 86400)

    # 超過 TTL → 過期
    if elapsed >= ttl:
        return False

    # 若今天已過 13:30 且上次更新在今天 13:30 之前 → 過期（強制當日盤後更新）
    market_close_today = now.replace(hour=13, minute=30, second=0, microsecond=0)
    if now >= market_close_today and updated_dt < market_close_today:
        return False

    return True


# ──────────────────────────────────────────────────────────────
# 本機快取 - 讀取
# ──────────────────────────────────────────────────────────────
def local_load_price(data_dir: str, ticker: str) -> list | None:
    """讀本機股價 CSV，回傳 list of dict 或 None"""
    path = _local_price_path(data_dir, ticker)
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
        if df.empty or len(df) < 5:
            return None
        return df.to_dict("records")
    except Exception as e:
        print(f"  [LocalCache] load_price 失敗 {ticker}: {e}")
        return None

def local_load_dividend(data_dir: str, ticker: str) -> list | None:
    path = _local_dividend_path(data_dir, ticker)
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, encoding="utf-8-sig")
        return df.to_dict("records") if not df.empty else None
    except Exception as e:
        print(f"  [LocalCache] load_dividend 失敗 {ticker}: {e}")
        return None

def local_load_fundamental(data_dir: str, ticker: str) -> dict | None:
    path = _local_fundamental_path(data_dir, ticker)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if data else None
    except Exception as e:
        print(f"  [LocalCache] load_fundamental 失敗 {ticker}: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# 本機快取 - 寫入
# ──────────────────────────────────────────────────────────────
def local_save_price(data_dir: str, ticker: str, ohlcv: list):
    """
    ohlcv: [{date, open, high, low, close, volume}, ...]
    或簡化格式 [{date, close}, ...]
    欄位名稱統一用英文小寫存檔
    """
    if not ohlcv:
        return
    try:
        df = pd.DataFrame(ohlcv)
        path = _local_price_path(data_dir, ticker)
        df.to_csv(path, index=False, encoding="utf-8-sig")
        _stamp_local_meta(data_dir, ticker, "price", {"price_rows": len(ohlcv)})
        print(f"  [LocalCache] ✅ {ticker} 股價存檔 ({len(ohlcv)} 筆) → {path}")
    except Exception as e:
        print(f"  [LocalCache] ❌ save_price 失敗 {ticker}: {e}")

def local_save_dividend(data_dir: str, ticker: str, dividend_data: list):
    if not dividend_data:
        return
    try:
        df = pd.DataFrame(dividend_data)
        path = _local_dividend_path(data_dir, ticker)
        df.to_csv(path, index=False, encoding="utf-8-sig")
        _stamp_local_meta(data_dir, ticker, "dividend")
        print(f"  [LocalCache] ✅ {ticker} 配息存檔 ({len(dividend_data)} 筆)")
    except Exception as e:
        print(f"  [LocalCache] ❌ save_dividend 失敗 {ticker}: {e}")

def local_save_fundamental(data_dir: str, ticker: str, info: dict):
    if not info:
        return
    try:
        path = _local_fundamental_path(data_dir, ticker)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
        _stamp_local_meta(data_dir, ticker, "fundamental")
        print(f"  [LocalCache] ✅ {ticker} 基本面存檔")
    except Exception as e:
        print(f"  [LocalCache] ❌ save_fundamental 失敗 {ticker}: {e}")


# ──────────────────────────────────────────────────────────────
# GitHub 讀取（Public Repo，不需 Token）
# ──────────────────────────────────────────────────────────────
def _gh_raw_get(path: str, timeout: int = 10) -> str | None:
    """從 raw.githubusercontent.com 讀取文字檔，失敗回傳 None"""
    url = f"{RAW_BASE}/{path}"
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            return r.text
        return None
    except Exception as e:
        print(f"  [GitHub-R] GET {path} 失敗: {e}")
        return None

def _gh_meta_fresh(ticker: str, data_type: str) -> bool:
    """
    判斷 GitHub 端的 meta 時間戳記是否仍在 TTL 內。
    與 is_local_fresh() 行為一致：
      - TTL 超過 → 過期
      - 今日已過 13:30 且上次更新在 13:30 之前 → 強制過期（確保盤後資料當日更新）
    """
    content = _gh_raw_get(f"data/{ticker}/meta.json")
    if not content:
        return False
    try:
        meta = json.loads(content)
        ts_str = meta.get(f"{data_type}_at")
        if not ts_str:
            return False
        updated_dt = datetime.fromisoformat(ts_str)
        now = datetime.now()
        elapsed = (now - updated_dt).total_seconds()

        # TTL 超過 → 過期
        if elapsed >= TTL_MAP.get(data_type, 86400):
            print(f"  [GitHub-R] {ticker}/{data_type} 快取已過期（TTL {elapsed/3600:.1f}h）")
            return False

        # 今日已過 13:30 且上次更新在今天 13:30 之前 → 強制視為過期
        market_close_today = now.replace(hour=13, minute=30, second=0, microsecond=0)
        if now >= market_close_today and updated_dt < market_close_today:
            print(f"  [GitHub-R] {ticker}/{data_type} 盤後強制過期（更新於 {ts_str[:16]}）")
            return False

        print(f"  [GitHub-R] {ticker}/{data_type} 快取有效（{elapsed/3600:.1f}h 前更新）")
        return True
    except Exception as e:
        print(f"  [GitHub-R] meta 解析失敗: {e}")
        return False

def gh_load_price(ticker: str) -> list | None:
    content = _gh_raw_get(f"data/{ticker}/price.csv")
    if not content:
        return None
    try:
        df = pd.read_csv(StringIO(content))
        return df.to_dict("records") if not df.empty else None
    except Exception as e:
        print(f"  [GitHub-R] load_price 失敗 {ticker}: {e}")
        return None

def gh_load_dividend(ticker: str) -> list | None:
    content = _gh_raw_get(f"data/{ticker}/dividend.csv")
    if not content:
        return None
    try:
        df = pd.read_csv(StringIO(content))
        return df.to_dict("records") if not df.empty else None
    except Exception as e:
        print(f"  [GitHub-R] load_dividend 失敗 {ticker}: {e}")
        return None

def gh_load_fundamental(ticker: str) -> dict | None:
    content = _gh_raw_get(f"data/{ticker}/fundamental.json")
    if not content:
        return None
    try:
        return json.loads(content)
    except Exception as e:
        print(f"  [GitHub-R] load_fundamental 失敗 {ticker}: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# GitHub 寫入（需 GH_CACHE_TOKEN；無 token 時 silent skip）
# ──────────────────────────────────────────────────────────────
class _GitHubWriter:
    """
    封裝 GitHub API 寫入，共用 token / headers / sha 快取。
    token 從環境變數 GH_CACHE_TOKEN 讀取；不存在時 enabled=False。
    """
    def __init__(self):
        self.token = os.environ.get("GH_CACHE_TOKEN", "")
        self.enabled = bool(self.token)
        self._sha: dict[str, str] = {}
        if self.enabled:
            self._headers = {
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            }

    def _get_sha(self, path: str) -> str | None:
        if path in self._sha:
            return self._sha[path]
        try:
            r = requests.get(f"{API_BASE}/{path}", headers=self._headers, timeout=10)
            if r.status_code == 200:
                sha = r.json().get("sha")
                self._sha[path] = sha
                return sha
        except Exception:
            pass
        return None

    def put(self, path: str, content_str: str, commit_msg: str = "cache update") -> bool:
        if not self.enabled:
            return False
        payload = {
            "message": commit_msg,
            "content": base64.b64encode(content_str.encode("utf-8")).decode("utf-8"),
        }
        sha = self._get_sha(path)
        if sha:
            payload["sha"] = sha
        try:
            r = requests.put(f"{API_BASE}/{path}", headers=self._headers,
                             json=payload, timeout=20)
            r.raise_for_status()
            new_sha = r.json()["content"]["sha"]
            self._sha[path] = new_sha
            return True
        except Exception as e:
            print(f"  [GitHub-W] PUT {path} 失敗: {e}")
            return False


# 模組層級單例（避免重複建立 headers）
_gh_writer = _GitHubWriter()


def _gh_update_meta(ticker: str, data_type: str, extra: dict = None):
    """更新 GitHub 端 meta.json 的時間戳記（寫入失敗不影響主流程）"""
    meta_path = f"data/{ticker}/meta.json"
    # 嘗試讀現有內容
    try:
        content = _gh_raw_get(meta_path)
        meta = json.loads(content) if content else {}
    except Exception:
        meta = {}
    meta[f"{data_type}_at"] = datetime.now().isoformat()
    if extra:
        meta.update(extra)
    _gh_writer.put(meta_path, json.dumps(meta, ensure_ascii=False, indent=2),
                   f"meta: update {ticker}/{data_type}")


def gh_save_price(ticker: str, ohlcv: list) -> bool:
    if not _gh_writer.enabled or not ohlcv:
        return False
    try:
        df = pd.DataFrame(ohlcv)
        ok = _gh_writer.put(
            f"data/{ticker}/price.csv",
            df.to_csv(index=False),
            f"price: {ticker} {datetime.now().date()}"
        )
        if ok:
            _gh_update_meta(ticker, "price", {"price_rows": len(ohlcv)})
            print(f"  [GitHub-W] ✅ {ticker} 股價已同步 ({len(ohlcv)} 筆)")
        return ok
    except Exception as e:
        print(f"  [GitHub-W] ❌ gh_save_price {ticker}: {e}")
        return False

def gh_save_dividend(ticker: str, dividend_data: list) -> bool:
    if not _gh_writer.enabled or not dividend_data:
        return False
    try:
        df = pd.DataFrame(dividend_data)
        ok = _gh_writer.put(
            f"data/{ticker}/dividend.csv",
            df.to_csv(index=False),
            f"dividend: {ticker} {datetime.now().date()}"
        )
        if ok:
            _gh_update_meta(ticker, "dividend")
            print(f"  [GitHub-W] ✅ {ticker} 配息已同步")
        return ok
    except Exception as e:
        print(f"  [GitHub-W] ❌ gh_save_dividend {ticker}: {e}")
        return False

def gh_save_fundamental(ticker: str, info: dict) -> bool:
    if not _gh_writer.enabled or not info:
        return False
    try:
        ok = _gh_writer.put(
            f"data/{ticker}/fundamental.json",
            json.dumps(info, ensure_ascii=False, indent=2),
            f"fundamental: {ticker} {datetime.now().date()}"
        )
        if ok:
            _gh_update_meta(ticker, "fundamental")
            print(f"  [GitHub-W] ✅ {ticker} 基本面已同步")
        return ok
    except Exception as e:
        print(f"  [GitHub-W] ❌ gh_save_fundamental {ticker}: {e}")
        return False


# ──────────────────────────────────────────────────────────────
# 主要公開介面：三層快取統一入口
# ──────────────────────────────────────────────────────────────
class CacheManager:
    """
    三層快取管理器（本機 → GitHub → yfinance）

    使用方式（在 app.py 中）：
        from github_cache import CacheManager
        cache = CacheManager(data_dir=DATA_DIR)

        # 取得股價（自動走三層邏輯）
        price_rows = cache.get_price(ticker, fetcher)

        # 取得配息
        div_rows = cache.get_dividend(ticker, fetcher)

        # 取得基本面
        info = cache.get_fundamental(ticker, fetcher)
    """

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

    # ── 股價 ──────────────────────────────────────────────────

    def get_price(self, ticker: str, fetcher=None) -> list | None:
        """
        三層取得股價：
          L1 本機快取（有效期內直接回傳）
          L2 GitHub（有效期內回傳並回填本機）
          L3 fetcher.fetch_price(ticker)（最後手段，結果同步兩層）
        fetcher: 具有 fetch_price(ticker) → list of dict 方法的物件
        """
        tag = f"[get_price/{ticker}]"

        # L1: 本機
        if is_local_fresh(self.data_dir, ticker, "price"):
            rows = local_load_price(self.data_dir, ticker)
            if rows:
                print(f"  {tag} ✅ L1 本機命中 ({len(rows)} 筆)")
                return rows

        # L2: GitHub
        if _gh_meta_fresh(ticker, "price"):
            rows = gh_load_price(ticker)
            if rows and len(rows) >= 5:
                print(f"  {tag} ✅ L2 GitHub 命中 ({len(rows)} 筆)，回填本機")
                local_save_price(self.data_dir, ticker, rows)
                return rows

        # L3: 網路抓取
        if fetcher is None:
            print(f"  {tag} ⚠️ 無 fetcher，無法從網路取得")
            return None
        print(f"  {tag} 🌐 L3 從網路抓取...")
        try:
            rows = fetcher.fetch_price(ticker)
            if rows:
                local_save_price(self.data_dir, ticker, rows)
                gh_save_price(ticker, rows)
                return rows
        except Exception as e:
            print(f"  {tag} ❌ 網路抓取失敗: {e}")
        return None

    # ── 配息 ──────────────────────────────────────────────────

    def get_dividend(self, ticker: str, fetcher=None) -> list | None:
        tag = f"[get_dividend/{ticker}]"

        if is_local_fresh(self.data_dir, ticker, "dividend"):
            rows = local_load_dividend(self.data_dir, ticker)
            if rows:
                print(f"  {tag} ✅ L1 本機命中")
                return rows

        if _gh_meta_fresh(ticker, "dividend"):
            rows = gh_load_dividend(ticker)
            if rows:
                print(f"  {tag} ✅ L2 GitHub 命中，回填本機")
                local_save_dividend(self.data_dir, ticker, rows)
                return rows

        if fetcher is None:
            return None
        print(f"  {tag} 🌐 L3 從網路抓取...")
        try:
            rows = fetcher.fetch_dividend(ticker)
            if rows:
                local_save_dividend(self.data_dir, ticker, rows)
                gh_save_dividend(ticker, rows)
            return rows
        except Exception as e:
            print(f"  {tag} ❌ 網路抓取失敗: {e}")
            return None

    # ── 基本面 ────────────────────────────────────────────────

    def get_fundamental(self, ticker: str, fetcher=None) -> dict | None:
        tag = f"[get_fundamental/{ticker}]"

        if is_local_fresh(self.data_dir, ticker, "fundamental"):
            info = local_load_fundamental(self.data_dir, ticker)
            if info:
                print(f"  {tag} ✅ L1 本機命中")
                return info

        if _gh_meta_fresh(ticker, "fundamental"):
            info = gh_load_fundamental(ticker)
            if info:
                print(f"  {tag} ✅ L2 GitHub 命中，回填本機")
                local_save_fundamental(self.data_dir, ticker, info)
                return info

        if fetcher is None:
            return None
        print(f"  {tag} 🌐 L3 從網路抓取...")
        try:
            info = fetcher.fetch_fundamental(ticker)
            if info:
                local_save_fundamental(self.data_dir, ticker, info)
                gh_save_fundamental(ticker, info)
            return info
        except Exception as e:
            print(f"  {tag} ❌ 網路抓取失敗: {e}")
            return None

    # ── 強制刷新（排程器呼叫）────────────────────────────────

    def force_refresh(self, ticker: str, fetcher, data_types: list = None):
        """
        強制忽略快取，重新從網路抓取並存檔。
        data_types: ['price', 'dividend', 'fundamental']，None 表示全部
        """
        if data_types is None:
            data_types = ["price", "dividend", "fundamental"]
        print(f"  [force_refresh] 開始刷新 {ticker}：{data_types}")
        for dt in data_types:
            try:
                if dt == "price":
                    rows = fetcher.fetch_price(ticker)
                    if rows:
                        local_save_price(self.data_dir, ticker, rows)
                        gh_save_price(ticker, rows)
                elif dt == "dividend":
                    rows = fetcher.fetch_dividend(ticker)
                    if rows:
                        local_save_dividend(self.data_dir, ticker, rows)
                        gh_save_dividend(ticker, rows)
                elif dt == "fundamental":
                    info = fetcher.fetch_fundamental(ticker)
                    if info:
                        local_save_fundamental(self.data_dir, ticker, info)
                        gh_save_fundamental(ticker, info)
            except Exception as e:
                print(f"  [force_refresh] {ticker}/{dt} 失敗: {e}")


# ──────────────────────────────────────────────────────────────
# 背景排程：每日 13:30（台灣時間）自動更新
# ──────────────────────────────────────────────────────────────

def start_scheduler(cache: CacheManager, fetcher_factory, watchlist_reader=None,
                    popular_stocks: list = None):
    """
    啟動背景排程執行緒（daemon thread）。

    Parameters
    ----------
    cache : CacheManager
        已初始化的 CacheManager 實例
    fetcher_factory : callable
        呼叫後回傳具有 fetch_price / fetch_dividend / fetch_fundamental 的物件
        例如：lambda: ETFDataFetcher(output_dir=DATA_DIR)
    watchlist_reader : callable | None
        呼叫後回傳自選股代碼 list，例如：_wl_read
        None 時不更新自選股
    popular_stocks : list | None
        常駐更新的股票代碼清單，例如 POPULAR_STOCKS

    說明
    ----
    排程邏輯：
      - 每 60 秒醒來一次，檢查現在是否為「今日 13:30–13:35」且今日尚未執行
      - 執行時合併 watchlist + popular_stocks，去重後逐一 force_refresh
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        import pytz
        _use_apscheduler = True
    except ImportError:
        _use_apscheduler = False
        print("  [Scheduler] APScheduler 未安裝，改用內建 threading 排程")

    if _use_apscheduler:
        _start_apscheduler(cache, fetcher_factory, watchlist_reader, popular_stocks)
    else:
        _start_thread_scheduler(cache, fetcher_factory, watchlist_reader, popular_stocks)


def _collect_tickers(watchlist_reader, popular_stocks) -> list:
    tickers = list(popular_stocks or [])
    if watchlist_reader:
        try:
            wl = watchlist_reader() or []
            for t in wl:
                if t not in tickers:
                    tickers.append(t)
        except Exception as e:
            print(f"  [Scheduler] watchlist_reader 失敗: {e}")
    return tickers


def _run_daily_refresh(cache: CacheManager, fetcher_factory, watchlist_reader, popular_stocks):
    """實際執行每日更新的函式（被排程器呼叫）"""
    tickers = _collect_tickers(watchlist_reader, popular_stocks)
    print(f"\n{'='*55}")
    print(f"  [Scheduler] 每日 13:30 自動更新，共 {len(tickers)} 支股票")
    print(f"{'='*55}")
    fetcher = fetcher_factory()
    for tk in tickers:
        try:
            cache.force_refresh(tk, fetcher, data_types=["price", "dividend", "fundamental"])
        except Exception as e:
            print(f"  [Scheduler] {tk} 更新失敗（跳過）: {e}")
    print(f"  [Scheduler] ✅ 每日更新完成\n")


def _start_apscheduler(cache, fetcher_factory, watchlist_reader, popular_stocks):
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        import pytz
        tz = pytz.timezone("Asia/Taipei")
        scheduler = BackgroundScheduler(timezone=tz)
        scheduler.add_job(
            func=_run_daily_refresh,
            trigger="cron",
            hour=13, minute=30,
            kwargs={
                "cache": cache,
                "fetcher_factory": fetcher_factory,
                "watchlist_reader": watchlist_reader,
                "popular_stocks": popular_stocks,
            },
            id="daily_cache_refresh",
            replace_existing=True,
        )
        scheduler.start()
        print("  [Scheduler] ✅ APScheduler 已啟動，每日 13:30 (Asia/Taipei) 自動更新")
    except Exception as e:
        print(f"  [Scheduler] APScheduler 啟動失敗: {e}")


def _start_thread_scheduler(cache, fetcher_factory, watchlist_reader, popular_stocks):
    """fallback：用 threading 做輪詢排程（每 60 秒檢查一次）"""
    _last_run_date = [None]   # mutable container 供 closure 修改

    def _loop():
        while True:
            try:
                now = datetime.now()
                today = now.date()
                # 13:30 ~ 13:35 視窗內且今日尚未執行
                if (now.hour == 13 and 30 <= now.minute < 35
                        and _last_run_date[0] != today):
                    _last_run_date[0] = today
                    _run_daily_refresh(cache, fetcher_factory, watchlist_reader, popular_stocks)
            except Exception as e:
                print(f"  [Scheduler] loop 例外: {e}")
            time.sleep(60)

    t = threading.Thread(target=_loop, daemon=True, name="cache-scheduler")
    t.start()
    print("  [Scheduler] ✅ threading 排程已啟動（每日 13:30–13:35 自動更新）")


# ──────────────────────────────────────────────────────────────
# 向下相容：保留舊版 GitHubCache 類別（避免 app.py import 錯誤）
# ──────────────────────────────────────────────────────────────
class GitHubCache:
    """
    向下相容層 - 保留舊介面，內部委派至新函式。
    新程式碼請直接使用 CacheManager。

    原有的 GH_CACHE_TOKEN / GH_CACHE_REPO 環境變數繼續生效：
      - GH_CACHE_TOKEN  : 有值時啟用 GitHub 寫入
      - GH_CACHE_REPO   : 舊版用，現已固定為 shui1133/analysis_ETF，可忽略
    """
    TTL_PRICE       = TTL_PRICE
    TTL_DIVIDEND    = TTL_DIVIDEND
    TTL_FUNDAMENTAL = TTL_FUNDAMENTAL

    def __init__(self):
        self.token = os.environ.get("GH_CACHE_TOKEN", "")
        self.repo  = GITHUB_REPO
        self.enabled = True          # 讀取永遠啟用（public repo）
        self._api  = API_BASE
        self._headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        }
        self._sha: dict[str, str] = {}

    # 舊 app.py 用到的低層方法（watchlist 備份用）
    def _get(self, path: str):
        content = _gh_raw_get(path)
        return (content, None) if content else (None, None)

    def _put(self, path: str, content_str: str, commit_msg: str = "cache update"):
        _gh_writer.put(path, content_str, commit_msg)

    def is_fresh(self, ticker: str, data_type: str) -> bool:
        return _gh_meta_fresh(ticker, data_type)

    def save_price(self, ticker: str, ohlcv: list):
        gh_save_price(ticker, ohlcv)

    def load_price(self, ticker: str) -> list | None:
        return gh_load_price(ticker)

    def save_dividend(self, ticker: str, dividend_data: list):
        gh_save_dividend(ticker, dividend_data)

    def load_dividend(self, ticker: str) -> list | None:
        return gh_load_dividend(ticker)

    def save_fundamental(self, ticker: str, info: dict):
        gh_save_fundamental(ticker, info)

    def load_fundamental(self, ticker: str) -> dict | None:
        return gh_load_fundamental(ticker)
