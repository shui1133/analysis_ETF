"""
github_cache.py - 三層持久化快取模組 v3
優先順序：本機硬碟 → GitHub Public Repo → yfinance

════════════════════════════════════════════════════════════
必要環境變數（本機：.env；Render：Dashboard → Environment）
════════════════════════════════════════════════════════════

  GH_CACHE_TOKEN = ghp_xxxxxxxxxxxxxxxxxxxxxxxx
    ├─ 用途：GitHub API 寫入權限（PUT/存檔至 Repo）
    ├─ 來源：GitHub → Settings → Developer settings
    │        → Personal access tokens (classic)
    │        → 勾選 repo（或 public_repo 若為公開 Repo）
    ├─ 未設定時：程式仍可【讀取】GitHub Public Repo 資料，
    │            但【不會】將更新推送回 GitHub
    └─ 注意：請勿將實際 Token 值寫入程式碼或 HTML，
             只能透過環境變數傳入

════════════════════════════════════════════════════════════

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
  - 每日 16:00（台灣時間，盤後 30 分鐘）自動更新 TOP50 熱門股票 + ETF
  - 同時儲存至本機 Storage 及同步推送至 GitHub Repo
  - 由 start_scheduler(cache, fetcher_factory) 啟動，app.py 在 __main__ 時呼叫
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
# TOP 50 熱門股票 + ETF（每日 16:00 自動更新）
# 修改此清單即可調整每日排程更新範圍
# ──────────────────────────────────────────────────────────────
TOP50_STOCKS = [
    # ── 半導體 / 科技 ──────────────────────────────────────────
    '2330', '2454', '2303', '2308', '2382', '2357', '3711', '2379',
    '3034', '2337', '6770', '3714', '2344', '2408', '2409',
    # ── 電子代工 / 組裝 ────────────────────────────────────────
    '2317', '4938', '2395', '2356', '2353',
    # ── 金融 ──────────────────────────────────────────────────
    '2881', '2882', '2891', '2886', '2887', '2884', '2885', '2890',
    # ── 傳產 / 電信 ───────────────────────────────────────────
    '2412', '1301', '1303', '2002', '1326',
    # ── 熱門 ETF（前 15 大）────────────────────────────────────
    '00878', '0056', '006208', '00919', '00929', '00713', '00631L',
    '00679B', '00772B', '00720B', '0050', '00915', '00646', '00864B', '00905',
]


# ──────────────────────────────────────────────────────────────
# ★ 啟動診斷（模組載入時執行一次）
# ──────────────────────────────────────────────────────────────
def _startup_diagnostics():
    """
    模組載入時自動執行，印出 GitHub 快取設定狀態。
    GitHub API 驗證改為背景執行緒，避免阻塞 Render/gunicorn 啟動健康檢查。
    """
    token = os.environ.get("GH_CACHE_TOKEN", "")
    sep = "=" * 55
    print(f"\n{sep}")
    print("  [github_cache] 啟動診斷")
    print(sep)
    if token:
        masked = token[:4] + "*" * max(0, len(token) - 8) + token[-4:] if len(token) > 8 else "****"
        print(f"  GH_CACHE_TOKEN : ✅ 已設定（{masked}）")
        print(f"  GitHub Repo    : 🔄 驗證中（背景執行緒，不阻塞啟動）")
        # ★ 關鍵修正：GitHub API 驗證移入背景執行緒，不阻塞主程序啟動
        def _check_repo():
            try:
                r = requests.get(
                    f"https://api.github.com/repos/{GITHUB_REPO}",
                    headers={"Authorization": f"token {token}",
                             "Accept": "application/vnd.github.v3+json"},
                    timeout=8
                )
                if r.status_code == 200:
                    print(f"  [github_cache] GitHub Repo : ✅ 可存取 ({GITHUB_REPO})")
                elif r.status_code == 401:
                    print(f"  [github_cache] GitHub Repo : ❌ Token 無效（401）→ 請重新產生 PAT")
                elif r.status_code == 403:
                    print(f"  [github_cache] GitHub Repo : ❌ Token 權限不足（403）")
                elif r.status_code == 404:
                    print(f"  [github_cache] GitHub Repo : ❌ Repo 不存在（404）")
                else:
                    print(f"  [github_cache] GitHub Repo : ⚠️  狀態碼 {r.status_code}")
            except Exception as e:
                print(f"  [github_cache] GitHub Repo : ⚠️  驗證失敗: {e}")
        import threading as _threading
        _threading.Thread(target=_check_repo, daemon=True, name="gh-diag").start()
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

    # 若今天已過 16:00 且上次更新在今天 16:00 之前 → 過期（強制當日盤後更新）
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

        # 今日已過 16:00 且上次更新在今天 16:00 之前 → 強制視為過期
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

        encoded = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")

        for attempt in range(1, 3):   # 最多重試 2 次（處理 SHA 衝突）
            # ── 每次都重新取 SHA（確保最新）──────────────────────
            if path in self._sha:
                sha = self._sha[path]
            else:
                sha = self._get_sha(path)

            payload = {
                "message": commit_msg,
                "content": encoded,
                "branch":  GITHUB_BRANCH,
            }
            if sha:
                payload["sha"] = sha

            try:
                r = requests.put(f"{API_BASE}/{path}", headers=self._headers,
                                 json=payload, timeout=20)
                if r.status_code in (200, 201):
                    new_sha = r.json()["content"]["sha"]
                    self._sha[path] = new_sha
                    return True

                err_body = ""
                try:
                    err_body = r.json().get("message", "")
                except Exception:
                    pass

                if r.status_code == 422:
                    # SHA 過期或不符：清除快取，強制重新 GET，然後重試
                    print(f"  [GitHub-W] ⚠️  PUT {path} 422 SHA 衝突（attempt {attempt}），重新取 SHA 後重試...")
                    if path in self._sha:
                        del self._sha[path]
                    # 直接向 API 重取 SHA，不走快取
                    try:
                        rg = requests.get(f"{API_BASE}/{path}", headers=self._headers, timeout=10)
                        if rg.status_code == 200:
                            fresh_sha = rg.json().get("sha")
                            if fresh_sha:
                                self._sha[path] = fresh_sha
                                print(f"             → 取得最新 SHA: {fresh_sha[:8]}...")
                        elif rg.status_code == 404:
                            # 檔案不存在，下次不帶 SHA 重試
                            print("             → 檔案不存在（首次建立）")
                    except Exception as e_sha:
                        print(f"             → 重取 SHA 失敗: {e_sha}")
                    continue   # 重試

                # 其他錯誤直接放棄
                print(f"  [GitHub-W] ❌ PUT {path} 失敗：HTTP {r.status_code} {err_body}")
                if r.status_code == 401:
                    print("             → Token 無效，請重新產生 GitHub PAT")
                elif r.status_code == 403:
                    print("             → Token 缺少 repo 寫入權限，請勾選 repo scope")
                return False

            except Exception as e:
                print(f"  [GitHub-W] PUT {path} 例外 (attempt {attempt}): {e}")
                if attempt < 2:
                    continue
                return False

        print(f"  [GitHub-W] ❌ PUT {path} 兩次重試均失敗")
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
        （已廢棄，保留向下相容，傳入無效果）
        更新清單現由內建 TOP50_STOCKS 定義，不再讀取自選股
    popular_stocks : list | None
        額外補充的股票代碼清單，合併至 TOP50_STOCKS 一起更新
        None 時僅更新 TOP50_STOCKS

    說明
    ----
    排程邏輯（每日 16:00 台灣時間，盤後 30 分鐘）：
      - 以 TOP50_STOCKS ∪ popular_stocks 為更新清單（去重）
      - 逐支 force_refresh（忽略 TTL，強制重抓）
      - 結果同時儲存至【本機 Storage】及【GitHub Repo】
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
    """合併 TOP50_STOCKS + popular_stocks（watchlist_reader 已廢棄，不再使用）"""
    seen = set()
    tickers = []
    for t in TOP50_STOCKS:
        if t not in seen:
            seen.add(t)
            tickers.append(t)
    for t in (popular_stocks or []):
        if t not in seen:
            seen.add(t)
            tickers.append(t)
    return tickers


def _run_daily_refresh(cache: CacheManager, fetcher_factory, watchlist_reader, popular_stocks):
    """實際執行每日 16:00 更新的函式（被排程器呼叫）"""
    tickers = _collect_tickers(watchlist_reader, popular_stocks)
    print(f"\n{'='*55}")
    print(f"  [Scheduler] 每日 16:00 自動更新，共 {len(tickers)} 支股票")
    print(f"  [Scheduler] 更新範圍：TOP50_STOCKS + 額外清單")
    print(f"  [Scheduler] 儲存目標：本機 Storage ＋ GitHub Repo")
    print(f"{'='*55}")
    fetcher = fetcher_factory()
    for tk in tickers:
        try:
            cache.force_refresh(tk, fetcher, data_types=["price", "dividend", "fundamental"])
        except Exception as e:
            print(f"  [Scheduler] {tk} 更新失敗（跳過）: {e}")
    print(f"  [Scheduler] ✅ 每日 16:00 更新完成\n")


def _start_apscheduler(cache, fetcher_factory, watchlist_reader, popular_stocks):
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
                "cache": cache,
                "fetcher_factory": fetcher_factory,
                "watchlist_reader": watchlist_reader,
                "popular_stocks": popular_stocks,
            },
            id="daily_cache_refresh",
            replace_existing=True,
        )
        scheduler.start()
        print("  [Scheduler] ✅ APScheduler 已啟動，每日 16:00 (Asia/Taipei) 自動更新")
        print("  [Scheduler]    更新範圍：TOP50_STOCKS（50 支熱門股票 + ETF）")
        print("  [Scheduler]    儲存目標：本機 Storage ＋ GitHub Repo")
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
                # 16:00 ~ 16:05 視窗內且今日尚未執行
                if (now.hour == 16 and 0 <= now.minute < 5
                        and _last_run_date[0] != today):
                    _last_run_date[0] = today
                    _run_daily_refresh(cache, fetcher_factory, watchlist_reader, popular_stocks)
            except Exception as e:
                print(f"  [Scheduler] loop 例外: {e}")
            time.sleep(60)

    t = threading.Thread(target=_loop, daemon=True, name="cache-scheduler")
    t.start()
    print("  [Scheduler] ✅ threading 排程已啟動（每日 16:00–16:05 自動更新）")


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
