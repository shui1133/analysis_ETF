"""
github_cache.py - 三層持久化快取模組 v3（增量更新版）
優先順序：本機硬碟 → GitHub Public Repo → yfinance

新功能（v3）
  - incremental_update_price()：讀 GitHub 現有 CSV，找出最後幾筆成交量=0 或
    缺少的日期，只向 yfinance 補抓那幾天，合併後存回本機 + GitHub。
    相較完整下載，速度提升顯著（通常只需補 1~5 筆，而非重抓數年資料）。
  - CacheManager.get_price() 整合增量更新：L1 本機 → L2 GitHub增量補抓 → L3 完整下載
  - CacheManager.force_refresh() price 改用增量，dividend/fundamental 保持完整下載

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
  - 另外：若當天已過 16:00（台灣時間），且時間戳記在 16:00 之前，強制視為過期。

背景排程（APScheduler）
  - 每交易日 16:00（台灣時間）自動更新 watchlist + POPULAR_STOCKS + POPULAR_ETF 各50筆
  - 由 start_scheduler(app, fetcher_factory) 啟動，app.py 在 __main__ 時呼叫
  - 排程內部改用 force_refresh（含增量邏輯），速度大幅提升
  - 更新完成後清空前端 localStorage 快取（透過寫入版本戳記通知前端）
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

# ── 台灣時區（修正 Render 伺服器為 UTC 導致盤後強制過期失效的問題）──
try:
    import pytz as _pytz
    _TW = _pytz.timezone("Asia/Taipei")
except ImportError:
    _TW = None

def _now_tw() -> datetime:
    """回傳台灣本地時間（有 tzinfo）；pytz 未安裝時退回系統時間（本機開發用）"""
    if _TW:
        return datetime.now(_TW)
    return datetime.now()

def _ts_now_tw() -> str:
    """回傳台灣時間的 ISO 字串，用於寫入 meta.json 時間戳記"""
    return _now_tw().isoformat()

def _parse_ts(ts_str: str) -> datetime:
    """
    解析 meta.json 的時間戳記字串，回傳有時區的 datetime（台灣時間）。
    支援帶時區（+08:00 / Z）與不帶時區（舊版寫入）兩種格式。
    """
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        # 舊版無時區時間戳：假設是台灣時間
        if _TW:
            dt = _TW.localize(dt)
    else:
        # 有時區 → 轉換成台灣時間，方便與 _now_tw() 比較
        if _TW:
            dt = dt.astimezone(_TW)
    return dt


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
# ★ 啟動診斷（模組載入時執行一次）
# ──────────────────────────────────────────────────────────────
def _startup_diagnostics():
    """
    模組載入時自動執行，印出 GitHub 快取設定狀態。
    協助開發者快速判斷「GitHub 未儲存」問題的根因。
    """
    token = os.environ.get("GH_CACHE_TOKEN", "")
    sep = "=" * 55
    print(f"\n{sep}")
    print("  [github_cache] 啟動診斷")
    print(sep)
    if token:
        # 遮蔽 token 中段（只顯示前4後4）
        masked = token[:4] + "*" * max(0, len(token) - 8) + token[-4:] if len(token) > 8 else "****"
        print(f"  GH_CACHE_TOKEN : ✅ 已設定（{masked}）")
        # 驗證 token 有效性（呼叫 GitHub API）
        try:
            r = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}",
                headers={"Authorization": f"token {token}",
                         "Accept": "application/vnd.github.v3+json"},
                timeout=8
            )
            if r.status_code == 200:
                print(f"  GitHub Repo    : ✅ 可存取 ({GITHUB_REPO})")
            elif r.status_code == 401:
                print(f"  GitHub Repo    : ❌ Token 無效（401 Unauthorized）→ 請重新產生 PAT")
            elif r.status_code == 403:
                print(f"  GitHub Repo    : ❌ Token 權限不足（403 Forbidden）→ 需要 repo write 權限")
            elif r.status_code == 404:
                print(f"  GitHub Repo    : ❌ Repo 不存在（404）→ 確認 {GITHUB_REPO} 是否正確")
            else:
                print(f"  GitHub Repo    : ⚠️  未預期狀態碼 {r.status_code}")
        except Exception as e:
            print(f"  GitHub Repo    : ⚠️  驗證失敗（網路問題？）: {e}")
    else:
        print(f"  GH_CACHE_TOKEN : ❌ 未設定")
        print(f"  → GitHub 寫入完全停用，資料不會同步到 Repo")
        print(f"  → 修復方式：")
        print(f"     本機：在 .env 加入 GH_CACHE_TOKEN=ghp_xxxxxxxx")
        print(f"     Render：Dashboard → Environment → 新增此環境變數")
    print(sep + "\n")

_startup_diagnostics()


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
    """更新本機 meta 的時間戳記（台灣時間）"""
    meta = _read_local_meta(data_dir, ticker)
    meta[f"{data_type}_at"] = _ts_now_tw()   # ★ 改用台灣時間
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
    另外：若當天已過 16:00（台灣時間），且時間戳記在 16:00 之前，強制視為過期。
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
        updated_dt = _parse_ts(ts_str)   # ★ 改用帶時區解析
    except Exception:
        return False

    now = _now_tw()                       # ★ 改用台灣時間
    elapsed = (now - updated_dt).total_seconds()
    ttl = TTL_MAP.get(data_type, 86400)

    # 超過 TTL → 過期
    if elapsed >= ttl:
        return False

    # 若台灣時間今天已過 16:00 且上次更新在今天 16:00 之前 → 過期（強制當日盤後資料更新）
    market_close_today = now.replace(hour=16, minute=0, second=0, microsecond=0)
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
      - 今日已過 16:00 且上次更新在 16:00 之前 → 強制過期（確保盤後資料當日更新）
    """
    content = _gh_raw_get(f"data/{ticker}/meta.json")
    if not content:
        return False
    try:
        meta = json.loads(content)
        ts_str = meta.get(f"{data_type}_at")
        if not ts_str:
            return False
        updated_dt = _parse_ts(ts_str)   # ★ 改用帶時區解析
        now = _now_tw()                   # ★ 改用台灣時間
        elapsed = (now - updated_dt).total_seconds()

        # TTL 超過 → 過期
        if elapsed >= TTL_MAP.get(data_type, 86400):
            print(f"  [GitHub-R] {ticker}/{data_type} 快取已過期（TTL {elapsed/3600:.1f}h）")
            return False

        # 台灣時間今日已過 16:00 且上次更新在今天 16:00 之前 → 強制視為過期
        market_close_today = now.replace(hour=16, minute=0, second=0, microsecond=0)
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
        else:
            self._headers = {}
            # ★ 明確警告（只在首次初始化時印出）
            print("  [GitHub-W] ⚠️  GH_CACHE_TOKEN 未設定，所有 GitHub 寫入皆停用。")
            print("             設定方式：本機 .env 或 Render Dashboard → Environment")

    def _get_sha(self, path: str) -> str | None:
        if path in self._sha:
            return self._sha[path]
        try:
            r = requests.get(f"{API_BASE}/{path}", headers=self._headers, timeout=10)
            if r.status_code == 200:
                sha = r.json().get("sha")
                self._sha[path] = sha
                return sha
            elif r.status_code == 404:
                return None  # 檔案不存在，這是正常的首次建立
            else:
                print(f"  [GitHub-W] GET SHA 失敗 {path}：HTTP {r.status_code}")
        except Exception as e:
            print(f"  [GitHub-W] GET SHA 例外 {path}：{e}")
        return None

    def put(self, path: str, content_str: str, commit_msg: str = "cache update") -> bool:
        if not self.enabled:
            return False
        payload = {
            "message": commit_msg,
            "content": base64.b64encode(content_str.encode("utf-8")).decode("utf-8"),
            "branch": GITHUB_BRANCH,
        }
        sha = self._get_sha(path)
        if sha:
            payload["sha"] = sha
        try:
            r = requests.put(f"{API_BASE}/{path}", headers=self._headers,
                             json=payload, timeout=20)
            if r.status_code in (200, 201):
                new_sha = r.json()["content"]["sha"]
                self._sha[path] = new_sha
                return True
            else:
                # ★ 詳細的錯誤輸出（幫助診斷 token 權限問題）
                err_body = ""
                try:
                    err_body = r.json().get("message", "")
                except Exception:
                    pass
                print(f"  [GitHub-W] ❌ PUT {path} 失敗：HTTP {r.status_code} {err_body}")
                if r.status_code == 401:
                    print("             → Token 無效，請重新產生 GitHub PAT（Settings → Developer settings → PAT）")
                elif r.status_code == 403:
                    print("             → Token 缺少 repo 寫入權限，請勾選 repo scope")
                elif r.status_code == 422:
                    print("             → SHA 衝突，清除 sha 快取並重試...")
                    if path in self._sha:
                        del self._sha[path]
                return False
        except Exception as e:
            print(f"  [GitHub-W] PUT {path} 例外: {e}")
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
    meta[f"{data_type}_at"] = _ts_now_tw()   # ★ 改用台灣時間
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
            f"price: {ticker} {_now_tw().date()}"   # ★ 台灣時間日期
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
            f"dividend: {ticker} {_now_tw().date()}"   # ★ 台灣時間日期
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
            f"fundamental: {ticker} {_now_tw().date()}"   # ★ 台灣時間日期
        )
        if ok:
            _gh_update_meta(ticker, "fundamental")
            print(f"  [GitHub-W] ✅ {ticker} 基本面已同步")
        return ok
    except Exception as e:
        print(f"  [GitHub-W] ❌ gh_save_fundamental {ticker}: {e}")
        return False


# ──────────────────────────────────────────────────────────────
# 增量更新：只補抓 GitHub 缺少或成交量=0 的日期
# ──────────────────────────────────────────────────────────────

def _find_missing_dates(rows: list) -> tuple[str | None, list]:
    """
    分析 GitHub 上的 price rows，找出需要補抓的日期範圍。

    規則：
      1. 找出最後一筆成交量 > 0 的日期（anchor_date）
      2. anchor_date 之後的所有列（含 volume=0 的列）皆標記為需補抓
      3. 若最後一筆本身 volume=0，anchor 往前推至第一筆 volume=0 的連續段起點

    回傳：
      (start_date_str, stale_rows)
        start_date_str : 從哪一天開始補抓（yfinance start= 參數，字串 "YYYY-MM-DD"）
                         None 表示資料完整，不需補抓
        stale_rows     : 需被移除/替換的舊資料列（list of dict）
    """
    if not rows:
        return None, []

    # 統一欄位名稱
    date_col   = next((c for c in rows[0] if c in ['date', 'Date', '日期']),   None)
    vol_col    = next((c for c in rows[0] if c in ['volume', 'Volume', '成交量']), None)

    if not date_col:
        return None, []

    # 排序（升序，確保最新在最後）
    try:
        sorted_rows = sorted(rows, key=lambda r: str(r.get(date_col, '')))
    except Exception:
        sorted_rows = rows

    # 找最後一筆 volume > 0 的 index
    last_valid_idx = None
    if vol_col:
        for i in range(len(sorted_rows) - 1, -1, -1):
            try:
                v = float(sorted_rows[i].get(vol_col, 0) or 0)
                if v > 0:
                    last_valid_idx = i
                    break
            except Exception:
                continue

    if last_valid_idx is None:
        # 全部 volume=0（或無 volume 欄位），從最後一筆日期起補
        if sorted_rows:
            return str(sorted_rows[-1].get(date_col, ''))[:10], sorted_rows[-1:]
        return None, []

    # 若最後一筆就是 last_valid_idx（最新資料都有量），不需補抓
    if last_valid_idx == len(sorted_rows) - 1:
        return None, []

    # 最後幾筆 volume=0，從 last_valid_idx+1 那天起補
    stale_rows  = sorted_rows[last_valid_idx + 1:]
    start_date  = str(stale_rows[0].get(date_col, ''))[:10]
    return start_date, stale_rows


def _fetch_incremental_yfinance(ticker: str, start_date: str) -> list:
    """
    用 yfinance 補抓 ticker 從 start_date 起的 OHLCV 資料。
    回傳 list of dict（欄位：date, open, high, low, close, volume）。
    失敗回傳空 list。
    """
    try:
        import yfinance as yf
        from datetime import date, timedelta

        # 計算 end_date（台灣今天 +1，確保包含當天）
        end_date = (_now_tw().date() + timedelta(days=1)).strftime("%Y-%m-%d")

        new_rows: list = []
        for suffix in ['.TW', '.TWO']:
            yf_ticker = f"{ticker}{suffix}"
            try:
                tk = yf.Ticker(yf_ticker)
                hist = tk.history(start=start_date, end=end_date, timeout=15)
                if hist is None or hist.empty:
                    continue
                for dt, row in hist.iterrows():
                    try:
                        c = float(row['Close'])
                        if c <= 0:
                            continue
                        new_rows.append({
                            'date':   str(dt.date()),
                            'open':   round(float(row.get('Open',  c)), 2),
                            'high':   round(float(row.get('High',  c)), 2),
                            'low':    round(float(row.get('Low',   c)), 2),
                            'close':  round(c, 2),
                            'volume': int(float(row.get('Volume', 0))),
                        })
                    except Exception:
                        continue
                if new_rows:
                    print(f"  [Incremental] ✅ {ticker}({yf_ticker}) 補抓 {len(new_rows)} 筆"
                          f"（{start_date} 起）")
                    return new_rows
            except Exception as e:
                print(f"  [Incremental] {yf_ticker} 失敗: {e}")
                continue
        return []
    except ImportError:
        print("  [Incremental] ⚠️  yfinance 未安裝")
        return []


def _merge_price_rows(base_rows: list, stale_rows: list, new_rows: list) -> list:
    """
    將 base_rows（舊有完整資料）中，移除 stale_rows（過期列），
    再合併 new_rows（補抓的新資料），去重後按日期升序回傳。

    date 統一識別欄位：date / Date / 日期
    """
    date_col = next((c for c in (base_rows[0] if base_rows else {}) if c in ['date', 'Date', '日期']), 'date')
    stale_dates = {str(r.get(date_col, ''))[:10] for r in stale_rows}
    new_dates   = {str(r.get('date',  ''))[:10] for r in new_rows}

    # 保留舊資料中非過期的部分
    kept = [r for r in base_rows if str(r.get(date_col, ''))[:10] not in stale_dates]

    # 正規化 new_rows 欄位名稱（統一用英文小寫）
    normalized_new = []
    for r in new_rows:
        normalized_new.append({
            'date':   str(r.get('date', ''))[:10],
            'open':   r.get('open',   r.get('close', 0)),
            'high':   r.get('high',   r.get('close', 0)),
            'low':    r.get('low',    r.get('close', 0)),
            'close':  r.get('close',  0),
            'volume': r.get('volume', 0),
        })

    # 若 kept 的欄位名稱是中文，也正規化
    normalized_kept = []
    for r in kept:
        normalized_kept.append({
            'date':   str(r.get('date', r.get('Date', r.get('日期', ''))))[:10],
            'open':   r.get('open',   r.get('Open',   r.get('開盤價', r.get('close', r.get('Close', r.get('收盤價', 0)))))),
            'high':   r.get('high',   r.get('High',   r.get('最高價', r.get('close', r.get('Close', r.get('收盤價', 0)))))),
            'low':    r.get('low',    r.get('Low',    r.get('最低價', r.get('close', r.get('Close', r.get('收盤價', 0)))))),
            'close':  r.get('close',  r.get('Close',  r.get('收盤價', 0))),
            'volume': r.get('volume', r.get('Volume', r.get('成交量',  0))),
        })

    # 合併，以 date 去重（new_rows 優先覆蓋 kept）
    merged: dict = {r['date']: r for r in normalized_kept}
    for r in normalized_new:
        merged[r['date']] = r

    result = sorted(merged.values(), key=lambda r: r['date'])
    return result


def incremental_update_price(
    data_dir: str,
    ticker: str,
    force: bool = False,
) -> list | None:
    """
    增量更新股價快取的統一入口。

    流程：
      1. 從 GitHub 讀取現有 price.csv
      2. 分析最後幾筆是否 volume=0 或缺失日期
      3. 只向 yfinance 補抓缺少的日期段
      4. 合併後存回本機 + GitHub
      5. 回傳最終完整資料列（list of dict）

    Parameters
    ----------
    data_dir : str
        本機快取目錄
    ticker : str
        股票代碼（不含 .TW）
    force : bool
        True 時跳過 is_local_fresh 判斷，強制執行增量檢查

    Returns
    -------
    list | None
        合併後的完整 price rows，失敗時 None
    """
    tag = f"[IncrementalUpdate/{ticker}]"

    # ── 先嘗試讀 GitHub 上的現有資料 ──────────────────────────
    print(f"  {tag} 讀取 GitHub 現有資料...")
    existing_rows = gh_load_price(ticker)

    if not existing_rows:
        print(f"  {tag} GitHub 無資料，需完整下載")
        return None  # 由上層 CacheManager.get_price L3 處理完整下載

    # ── 分析需補抓的起始日期 ───────────────────────────────────
    start_date, stale_rows = _find_missing_dates(existing_rows)

    if start_date is None:
        print(f"  {tag} ✅ 資料完整（無 volume=0 缺口），無需補抓")
        # 仍回填本機（若本機沒有）
        local_save_price(data_dir, ticker, existing_rows)
        return existing_rows

    print(f"  {tag} 發現 {len(stale_rows)} 筆缺漏/volume=0，從 {start_date} 起補抓...")

    # ── 向 yfinance 補抓 ───────────────────────────────────────
    new_rows = _fetch_incremental_yfinance(ticker, start_date)

    if not new_rows:
        print(f"  {tag} ⚠️  yfinance 無回傳，保留現有資料")
        local_save_price(data_dir, ticker, existing_rows)
        return existing_rows

    # ── 合併 ──────────────────────────────────────────────────
    merged = _merge_price_rows(existing_rows, stale_rows, new_rows)
    print(f"  {tag} 合併完成：{len(existing_rows)} + {len(new_rows)} → {len(merged)} 筆")

    # ── 存本機 + GitHub ───────────────────────────────────────
    local_save_price(data_dir, ticker, merged)
    gh_save_price(ticker, merged)

    return merged


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
        三層取得股價（含增量更新）：
          L1 本機快取（有效期內直接回傳）
          L2 GitHub（先做增量補抓 volume=0/缺失筆數，再回填本機）
          L3 fetcher.fetch_price(ticker)（GitHub 無資料時完整下載）
        fetcher: 具有 fetch_price(ticker) → list of dict 方法的物件
        """
        tag = f"[get_price/{ticker}]"

        # L1: 本機有效快取 → 直接回傳（最快路徑）
        if is_local_fresh(self.data_dir, ticker, "price"):
            rows = local_load_price(self.data_dir, ticker)
            if rows:
                print(f"  {tag} ✅ L1 本機命中 ({len(rows)} 筆)")
                return rows

        # L2: GitHub 增量更新路徑
        # 先嘗試增量補抓（只下載 volume=0 或缺少的日期），速度遠快於完整下載
        print(f"  {tag} 🔄 嘗試 L2 GitHub 增量更新...")
        rows = incremental_update_price(self.data_dir, ticker)
        if rows and len(rows) >= 5:
            print(f"  {tag} ✅ L2 增量更新完成 ({len(rows)} 筆)")
            return rows

        # L3: GitHub 無資料 → 向 yfinance 完整下載
        if fetcher is None:
            # 嘗試讀取 GitHub 原始資料（即使未做增量，至少回傳現有的）
            gh_rows = gh_load_price(ticker)
            if gh_rows and len(gh_rows) >= 5:
                print(f"  {tag} ✅ L2 GitHub 原始資料 ({len(gh_rows)} 筆，無 fetcher 跳過增量)")
                local_save_price(self.data_dir, ticker, gh_rows)
                return gh_rows
            print(f"  {tag} ⚠️ 無 fetcher，無法從網路取得")
            return None

        print(f"  {tag} 🌐 L3 GitHub 無資料，從 yfinance 完整下載...")
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
        強制刷新快取。
        - price    : 優先使用增量更新（只補 volume=0/缺少的日期），GitHub 無資料才完整下載
        - dividend : 完整下載（配息資料量少，不做增量）
        - fundamental : 完整下載
        data_types: ['price', 'dividend', 'fundamental']，None 表示全部
        """
        if data_types is None:
            data_types = ["price", "dividend", "fundamental"]
        print(f"  [force_refresh] 開始刷新 {ticker}：{data_types}")
        for dt in data_types:
            try:
                if dt == "price":
                    # ── 優先嘗試增量更新 ──────────────────────
                    rows = incremental_update_price(self.data_dir, ticker, force=True)
                    if rows:
                        print(f"  [force_refresh] {ticker}/price 增量完成 ({len(rows)} 筆)")
                    else:
                        # GitHub 無資料 → 完整下載
                        print(f"  [force_refresh] {ticker}/price 無 GitHub 基底，改為完整下載")
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
# 背景排程：每交易日 16:00（台灣時間）自動更新
# ──────────────────────────────────────────────────────────────

def start_scheduler(cache: CacheManager, fetcher_factory, watchlist_reader=None,
                    popular_stocks: list = None, popular_etfs: list = None):
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
        熱門股票代碼清單（最多50筆），例如 POPULAR_STOCKS
    popular_etfs : list | None
        熱門ETF代碼清單（最多50筆），例如 POPULAR_ETF_CODES

    說明
    ----
    排程邏輯：
      - 每 60 秒醒來一次，檢查現在是否為「今日 16:00–16:05」且今日尚未執行
      - 執行時合併 watchlist + popular_stocks + popular_etfs，去重後逐一 force_refresh
      - 更新後寫入版本戳記（data/cache_version.json），供前端偵測並清空 localStorage
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        import pytz
        _use_apscheduler = True
    except ImportError:
        _use_apscheduler = False
        print("  [Scheduler] APScheduler 未安裝，改用內建 threading 排程")

    if _use_apscheduler:
        _start_apscheduler(cache, fetcher_factory, watchlist_reader, popular_stocks, popular_etfs)
    else:
        _start_thread_scheduler(cache, fetcher_factory, watchlist_reader, popular_stocks, popular_etfs)


def _collect_tickers(watchlist_reader, popular_stocks, popular_etfs=None) -> list:
    """合併自選股 + 熱門股票(50筆) + 熱門ETF(50筆)，去重後回傳"""
    seen = set()
    tickers = []

    def _add(code):
        c = str(code).strip()
        if c and c not in seen:
            seen.add(c)
            tickers.append(c)

    # 熱門股票（前50筆）
    stocks = list(popular_stocks or [])[:50]
    for s in stocks:
        _add(s['code'] if isinstance(s, dict) else s)

    # 熱門ETF（前50筆）
    etfs = list(popular_etfs or [])[:50]
    for e in etfs:
        _add(e['code'] if isinstance(e, dict) else e)

    # 自選股
    if watchlist_reader:
        try:
            wl = watchlist_reader() or []
            for t in wl:
                _add(t)
        except Exception as e:
            print(f"  [Scheduler] watchlist_reader 失敗: {e}")

    return tickers


def _bump_cache_version(data_dir: str):
    """更新快取版本戳記（前端輪詢此值，有變化時清空 localStorage）"""
    try:
        version_file = os.path.join(data_dir, "cache_version.json")
        now_tw = _now_tw()
        version_str = now_tw.strftime("%Y%m%d%H%M%S")
        with open(version_file, "w", encoding="utf-8") as f:
            json.dump({"version": version_str, "updated_at": now_tw.isoformat()}, f)
        print(f"  [Scheduler] 快取版本戳記已更新: {version_str}")
    except Exception as e:
        print(f"  [Scheduler] 版本戳記寫入失敗（非致命）: {e}")


def _run_daily_refresh(cache: CacheManager, fetcher_factory, watchlist_reader,
                       popular_stocks, popular_etfs=None, data_dir: str = None):
    """實際執行每日 16:00 更新的函式（被排程器呼叫）"""
    tickers = _collect_tickers(watchlist_reader, popular_stocks, popular_etfs)
    stocks_count = len(list(popular_stocks or [])[:50])
    etfs_count   = len(list(popular_etfs   or [])[:50])
    print(f"\n{'='*55}")
    print(f"  [Scheduler] 每交易日 16:00 自動更新")
    print(f"  熱門股票: {stocks_count} 支 / 熱門ETF: {etfs_count} 支 / 合計: {len(tickers)} 支")
    print(f"{'='*55}")
    fetcher = fetcher_factory()
    ok, fail = 0, 0
    for tk in tickers:
        try:
            cache.force_refresh(tk, fetcher, data_types=["price", "dividend", "fundamental"])
            ok += 1
        except Exception as e:
            fail += 1
            print(f"  [Scheduler] {tk} 更新失敗（跳過）: {e}")
    print(f"  [Scheduler] ✅ 每日更新完成 — 成功 {ok} / 失敗 {fail}\n")
    # 寫入版本戳記，通知前端清空 localStorage
    if data_dir:
        _bump_cache_version(data_dir)


def _start_apscheduler(cache, fetcher_factory, watchlist_reader, popular_stocks,
                       popular_etfs=None, data_dir: str = None):
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        import pytz
        tz = pytz.timezone("Asia/Taipei")
        scheduler = BackgroundScheduler(timezone=tz)
        scheduler.add_job(
            func=_run_daily_refresh,
            trigger="cron",
            hour=16, minute=0,
            kwargs={
                "cache":            cache,
                "fetcher_factory":  fetcher_factory,
                "watchlist_reader": watchlist_reader,
                "popular_stocks":   popular_stocks,
                "popular_etfs":     popular_etfs,
                "data_dir":         data_dir,
            },
            id="daily_cache_refresh",
            replace_existing=True,
        )
        scheduler.start()
        print("  [Scheduler] ✅ APScheduler 已啟動，每交易日 16:00 (Asia/Taipei) 自動更新")
    except Exception as e:
        print(f"  [Scheduler] APScheduler 啟動失敗: {e}")


def _start_thread_scheduler(cache, fetcher_factory, watchlist_reader, popular_stocks,
                             popular_etfs=None, data_dir: str = None):
    """fallback：用 threading 做輪詢排程（每 60 秒檢查一次）"""
    _last_run_date = [None]   # mutable container 供 closure 修改

    def _loop():
        while True:
            try:
                now = _now_tw()          # ★ 改用台灣時間
                today = now.date()
                # 台灣時間 16:00 ~ 16:05 視窗內且今日尚未執行
                if (now.hour == 16 and 0 <= now.minute < 5
                        and _last_run_date[0] != today):
                    _last_run_date[0] = today
                    _run_daily_refresh(cache, fetcher_factory, watchlist_reader,
                                       popular_stocks, popular_etfs, data_dir)
            except Exception as e:
                print(f"  [Scheduler] loop 例外: {e}")
            time.sleep(60)

    t = threading.Thread(target=_loop, daemon=True, name="cache-scheduler")
    t.start()
    print("  [Scheduler] ✅ threading 排程已啟動（每交易日 16:00–16:05 自動更新）")


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
