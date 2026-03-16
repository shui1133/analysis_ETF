"""
github_cache.py - GitHub CSV 持久化快取模組
儲存股價、配息、基本面到您的私有 GitHub Repo
讀取時優先使用快取，避免重複呼叫 yfinance
"""
import os, json, base64, time
import requests
import pandas as pd
from io import StringIO
from datetime import datetime, timedelta


class GitHubCache:
    """
    GitHub 私有 Repo 作為持久化 CSV 資料庫
    
    目錄結構（在您的 GitHub Repo 內）：
        data/2330/price.csv         ← OHLCV 股價
        data/2330/dividend.csv      ← 配息記錄
        data/2330/fundamental.json  ← 基本面
        meta/2330.json              ← 更新時間戳記
    """

    # 快取有效期（秒）
    TTL_PRICE       = 60 * 60 * 20   # 股價：20小時（每交易日更新一次即可）
    TTL_DIVIDEND    = 60 * 60 * 24 * 7   # 配息：7天
    TTL_FUNDAMENTAL = 60 * 60 * 24 * 3   # 基本面：3天

    def __init__(self):
        self.token = os.environ.get("GH_CACHE_TOKEN", "")
        self.repo  = os.environ.get("GH_CACHE_REPO", "")   # 格式: username/repo-name
        self._sha  = {}   # 暫存 SHA，避免重複 GET

        if not self.token or not self.repo:
            self.enabled = False
        else:
            self.enabled = True
            self._api = f"https://api.github.com/repos/{self.repo}/contents"
            self._headers = {
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            }

    # ── 低層 API ────────────────────────────────────────────────

    def _get(self, path):
        """讀取 GitHub 檔案，回傳 (text, sha)，不存在回傳 (None, None)"""
        r = requests.get(f"{self._api}/{path}", headers=self._headers, timeout=10)
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        d = r.json()
        content = base64.b64decode(d["content"]).decode("utf-8")
        sha = d["sha"]
        self._sha[path] = sha
        return content, sha

    def _put(self, path, content_str, commit_msg="cache update"):
        """寫入/更新 GitHub 檔案"""
        payload = {
            "message": commit_msg,
            "content": base64.b64encode(content_str.encode("utf-8")).decode("utf-8"),
        }
        # 若已有 SHA（更新既有檔案必須帶 SHA）
        sha = self._sha.get(path)
        if not sha:
            # 嘗試取得 SHA
            _, sha = self._get(path)
        if sha:
            payload["sha"] = sha

        r = requests.put(f"{self._api}/{path}", headers=self._headers,
                         json=payload, timeout=20)
        r.raise_for_status()
        self._sha[path] = r.json()["content"]["sha"]

    # ── Metadata（時間戳記）────────────────────────────────────

    def _read_meta(self, ticker):
        content, sha = self._get(f"meta/{ticker}.json")
        if content:
            self._sha[f"meta/{ticker}.json"] = sha
            return json.loads(content)
        return {}

    def _write_meta(self, ticker, meta):
        self._put(f"meta/{ticker}.json",
                  json.dumps(meta, ensure_ascii=False, indent=2),
                  f"meta: update {ticker}")

    def is_fresh(self, ticker, data_type):
        """
        data_type: 'price' | 'dividend' | 'fundamental'
        回傳 True 表示快取仍有效，不需重新抓取
        """
        if not self.enabled:
            return False
        try:
            meta = self._read_meta(ticker)
            ts_str = meta.get(f"{data_type}_at")
            if not ts_str:
                return False
            elapsed = time.time() - datetime.fromisoformat(ts_str).timestamp()
            ttl = {
                "price":       self.TTL_PRICE,
                "dividend":    self.TTL_DIVIDEND,
                "fundamental": self.TTL_FUNDAMENTAL,
            }.get(data_type, 86400)
            fresh = elapsed < ttl
            print(f"  [GitHubCache] {ticker}/{data_type} 快取 {'有效' if fresh else '已過期'}"
                  f"（{elapsed/3600:.1f}h 前更新）")
            return fresh
        except Exception as e:
            print(f"  [GitHubCache] is_fresh 錯誤: {e}")
            return False

    # ── 股價（OHLCV）──────────────────────────────────────────

    def save_price(self, ticker, ohlcv: list):
        """
        ohlcv: [{date, open, high, low, close, volume}, ...]
        也接受只有 {date, close} 的簡化格式
        """
        if not self.enabled or not ohlcv:
            return
        try:
            df = pd.DataFrame(ohlcv)
            path = f"data/{ticker}/price.csv"
            self._put(path, df.to_csv(index=False),
                      f"price: {ticker} {datetime.now().date()}")
            meta = self._read_meta(ticker)
            meta["price_at"] = datetime.now().isoformat()
            meta["price_rows"] = len(ohlcv)
            self._write_meta(ticker, meta)
            print(f"  [GitHubCache] ✅ {ticker} 股價已儲存（{len(ohlcv)} 筆）")
        except Exception as e:
            print(f"  [GitHubCache] ❌ save_price 失敗: {e}")

    def load_price(self, ticker):
        """回傳 list of dict（含 date/close，或完整 OHLCV），失敗回傳 None"""
        if not self.enabled:
            return None
        try:
            content, _ = self._get(f"data/{ticker}/price.csv")
            if not content:
                return None
            df = pd.read_csv(StringIO(content))
            return df.to_dict("records")
        except Exception as e:
            print(f"  [GitHubCache] load_price 失敗: {e}")
            return None

    # ── 配息 ───────────────────────────────────────────────────

    def save_dividend(self, ticker, dividend_data: list):
        """
        dividend_data: [{date, dividend}, ...]
        """
        if not self.enabled or not dividend_data:
            return
        try:
            df = pd.DataFrame(dividend_data)
            path = f"data/{ticker}/dividend.csv"
            self._put(path, df.to_csv(index=False),
                      f"dividend: {ticker} {datetime.now().date()}")
            meta = self._read_meta(ticker)
            meta["dividend_at"] = datetime.now().isoformat()
            self._write_meta(ticker, meta)
            print(f"  [GitHubCache] ✅ {ticker} 配息已儲存（{len(dividend_data)} 筆）")
        except Exception as e:
            print(f"  [GitHubCache] ❌ save_dividend 失敗: {e}")

    def load_dividend(self, ticker):
        if not self.enabled:
            return None
        try:
            content, _ = self._get(f"data/{ticker}/dividend.csv")
            if not content:
                return None
            df = pd.read_csv(StringIO(content))
            return df.to_dict("records")
        except Exception as e:
            print(f"  [GitHubCache] load_dividend 失敗: {e}")
            return None

    # ── 基本面（JSON）─────────────────────────────────────────

    def save_fundamental(self, ticker, info: dict):
        """info: yfinance tk.info 處理後的 dict"""
        if not self.enabled or not info:
            return
        try:
            path = f"data/{ticker}/fundamental.json"
            self._put(path, json.dumps(info, ensure_ascii=False, indent=2),
                      f"fundamental: {ticker} {datetime.now().date()}")
            meta = self._read_meta(ticker)
            meta["fundamental_at"] = datetime.now().isoformat()
            self._write_meta(ticker, meta)
            print(f"  [GitHubCache] ✅ {ticker} 基本面已儲存")
        except Exception as e:
            print(f"  [GitHubCache] ❌ save_fundamental 失敗: {e}")

    def load_fundamental(self, ticker):
        if not self.enabled:
            return None
        try:
            content, _ = self._get(f"data/{ticker}/fundamental.json")
            return json.loads(content) if content else None
        except Exception as e:
            print(f"  [GitHubCache] load_fundamental 失敗: {e}")
            return None
