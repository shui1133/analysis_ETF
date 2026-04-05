"""
台灣股票/ETF資料爬取模組 V4
優先順序：yfinance (完整OHLCV) → GitHub (shui1133/analysis_ETF) → 模擬資料
新增：OHLCV完整資料、技術指標(MACD, KD, RSI, 均線)、法人籌碼估算
"""

import yfinance as yf
import pandas as pd
import requests
import numpy as np
import json
from datetime import datetime, timedelta
import time
import os
import sys
import platform
import re
from io import StringIO

# ── GitHub 持久化快取（選用）────────────────────────────────────
# 需在 Render 環境變數設定：GH_CACHE_TOKEN
# 注意：fetch_data / fetch_stock_analysis 內部仍使用舊版 GitHubCache 介面
#       供快取讀寫；新版 CacheManager 統一入口由 app.py 負責呼叫。
try:
    from github_cache import GitHubCache
    _gh_cache = GitHubCache()
    if _gh_cache.enabled:
        print("[GitHubCache] ✅ 已啟用 GitHub 持久化快取（寫入）")
    else:
        print("[GitHubCache] ℹ️  未設定 GH_CACHE_TOKEN，GitHub 寫入停用（仍可讀取 public repo）")
except ImportError:
    _gh_cache = None
    print("[GitHubCache] ⚠️  github_cache.py 不存在，GitHub 快取停用")


def get_data_dir():
    """
    根據環境自動選擇資料目錄
    Render/Production: 使用 /tmp/data (暫存)
    Windows 開發: C:\\Python\\退休理財規劃分析\\data
    其他: ./data (當前目錄下)
    """
    if os.environ.get('RENDER'):
        data_dir = "/tmp/data"
    elif platform.system() == 'Windows':
        data_dir = r"C:\Python\退休理財規劃分析\data"
    else:
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

    os.makedirs(data_dir, exist_ok=True)
    return data_dir


# GitHub 支援的 ETF 對應名稱
GITHUB_ETF_NAMES = {
    '0056':   '元大高股息',
    '00878':  '國泰永續高股息',
    '00713':  '元大台灣高息低波',
    '00679B': '元大美債20年',
    '00919':  '群益台灣精選高息',
    '00929':  '復華台灣科技優息',
    '006208': '富邦台50',
    '00915':  '凱基優選高股息30'
}

# 熱門台灣股票清單
POPULAR_STOCKS = [
    {'code': '2330', 'name': '台積電'},
    {'code': '2317', 'name': '鴻海'},
    {'code': '2454', 'name': '聯發科'},
    {'code': '2382', 'name': '廣達'},
    {'code': '2308', 'name': '台達電'},
    {'code': '2881', 'name': '富邦金'},
    {'code': '2882', 'name': '國泰金'},
    {'code': '2891', 'name': '中信金'},
    {'code': '2886', 'name': '兆豐金'},
    {'code': '2887', 'name': '台新金'},
    {'code': '2303', 'name': '聯電'},
    {'code': '2412', 'name': '中華電'},
    {'code': '1301', 'name': '台塑'},
    {'code': '1303', 'name': '南亞'},
    {'code': '2002', 'name': '中鋼'},
    {'code': '2357', 'name': '華碩'},
    {'code': '3008', 'name': '大立光'},
    {'code': '2395', 'name': '研華'},
    {'code': '4938', 'name': '和碩'},
    {'code': '00878', 'name': '國泰永續高股息'},
    {'code': '0056',  'name': '元大高股息'},
    {'code': '006208','name': '富邦台50'},
    {'code': '00919', 'name': '群益台灣精選高息'},
    {'code': '00929', 'name': '復華台灣科技優息'},
]


def calc_technical_indicators(ohlcv: list) -> dict:
    """
    計算技術指標：MACD、KD、RSI、MA5/10/20/60
    輸入: ohlcv = [{date, open, high, low, close, volume}, ...]（按日期升序）
    輸出: dict，各指標 list，與 ohlcv 等長（前段不足為 None）
    """
    if not ohlcv or len(ohlcv) < 5:
        return {}

    closes = [r['close'] for r in ohlcv]
    highs  = [r['high']  for r in ohlcv]
    lows   = [r['low']   for r in ohlcv]
    n = len(closes)

    def ema(data, period):
        """
        EMA 計算：前 period 筆用 SMA 作為初始種子（與 Goodinfo/TradingView 一致）
        前 period-1 筆回傳 None（資料不足）
        """
        result = [None] * len(data)
        valid_indices = [i for i, v in enumerate(data) if v is not None]
        if len(valid_indices) < period:
            return result
        first = valid_indices[0]
        if first + period > len(data):
            return result
        # 用前 period 筆 SMA 當種子
        seed_vals = [data[i] for i in range(first, first + period) if data[i] is not None]
        if len(seed_vals) < period:
            return result
        seed = sum(seed_vals) / period
        result[first + period - 1] = round(seed, 6)
        k = 2 / (period + 1)
        for i in range(first + period, len(data)):
            if data[i] is None:
                continue
            seed = data[i] * k + seed * (1 - k)
            result[i] = round(seed, 6)
        return result

    def sma(data, period):
        result = [None] * len(data)
        for i in range(period - 1, len(data)):
            vals = [v for v in data[i - period + 1:i + 1] if v is not None]
            if len(vals) == period:
                result[i] = round(sum(vals) / period, 4)
        return result

    # ── MA ─────────────────────────────────────────────────────
    ma5   = sma(closes, 5)
    ma10  = sma(closes, 10)
    ma20  = sma(closes, 20)
    ma60  = sma(closes, 60)
    ma120 = sma(closes, 120)
    ma200 = sma(closes, 200)

    # ── MACD（EMA12 - EMA26，Signal=EMA9） ────────────────────
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd_line = [
        round(e12 - e26, 6) if (e12 is not None and e26 is not None) else None
        for e12, e26 in zip(ema12, ema26)
    ]
    signal_line = ema(macd_line, 9)
    macd_hist = [
        round(m - s, 6) if (m is not None and s is not None) else None
        for m, s in zip(macd_line, signal_line)
    ]

    # ── RSI(14) ───────────────────────────────────────────────
    rsi = [None] * n
    if n > 14:
        gains, losses = [], []
        for i in range(1, n):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))
        avg_gain = sum(gains[:14]) / 14
        avg_loss = sum(losses[:14]) / 14
        for i in range(14, n):
            avg_gain = (avg_gain * 13 + gains[i - 1]) / 14
            avg_loss = (avg_loss * 13 + losses[i - 1]) / 14
            if avg_loss == 0:
                rsi[i] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi[i] = round(100 - 100 / (1 + rs), 2)

    # ── KD（Stochastic，9日）──────────────────────────────────
    kd_period = 9
    k_vals = [None] * n
    d_vals = [None] * n
    raw_k = []
    for i in range(n):
        if i < kd_period - 1:
            raw_k.append(None)
            continue
        period_highs = highs[i - kd_period + 1:i + 1]
        period_lows  = lows[i - kd_period + 1:i + 1]
        hh = max(period_highs)
        ll = min(period_lows)
        if hh == ll:
            raw_k.append(50.0)
        else:
            raw_k.append(round((closes[i] - ll) / (hh - ll) * 100, 2))
    # 使用 EMA3 平滑
    k_smooth = 50.0
    d_smooth = 50.0
    for i, rk in enumerate(raw_k):
        if rk is None:
            continue
        k_smooth = round(rk * (1/3) + k_smooth * (2/3), 2)
        d_smooth = round(k_smooth * (1/3) + d_smooth * (2/3), 2)
        k_vals[i] = k_smooth
        d_vals[i] = d_smooth

    return {
        'ma5':        ma5,
        'ma10':       ma10,
        'ma20':       ma20,
        'ma60':       ma60,
        'ma120':      ma120,
        'ma200':      ma200,
        'macd':       macd_line,
        'macd_signal':signal_line,
        'macd_hist':  macd_hist,
        'rsi':        rsi,
        'k':          k_vals,
        'd':          d_vals,
    }


class ETFDataFetcher:
    """台灣股票/ETF資料爬取器 V4（yfinance優先）"""

    def __init__(self, output_dir=None):
        if output_dir is None:
            self.output_dir = get_data_dir()
        else:
            self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
        }
        self.last_error = ''

    # ─────────────────────────────────────────────────────────────
    # 主要入口（供回測使用，回傳 {price_data, dividend_data}）
    # 優先順序：yfinance → GitHub → 模擬
    # ─────────────────────────────────────────────────────────────
    def fetch_data(self, ticker):
        print(f"\n開始爬取 {ticker} 的資料...")
        self.last_error = ''

        # ── L2：GitHub 持久化快取（跨重啟有效）──────────────
        if _gh_cache and _gh_cache.enabled and _gh_cache.is_fresh(ticker, 'price'):
            rows = _gh_cache.load_price(ticker)
            if rows:
                div_rows = _gh_cache.load_dividend(ticker) or []
                price_data = [
                    {'date': str(r.get('date', r.get('日期', ''))),
                     'close': float(r.get('close', r.get('收盤價', 0)))}
                    for r in rows
                ]
                dividend_data = [
                    {'date': str(r.get('date', r.get('除息日', ''))),
                     'dividend': float(r.get('dividend', r.get('股利', 0)))}
                    for r in div_rows
                ]
                print(f"  [L2 GitHub] {ticker} 回測資料命中快取（{len(price_data)} 筆）")
                return {'ticker': ticker, 'price_data': price_data,
                        'dividend_data': dividend_data,
                        'is_simulated': False, 'source': 'github_cache'}

        # ── L3：yfinance 重新抓取 ─────────────────────────────
        print("嘗試 yfinance（優先）...")
        yf_data = self._fetch_from_yfinance(ticker)
        if yf_data and yf_data.get('price_data'):
            print(f"✓ yfinance 成功，{len(yf_data['price_data'])} 筆")
            self._save_data(ticker, yf_data)
            # ★ GitHub 快取寫入改背景，不阻塞回測請求
            if _gh_cache and _gh_cache.enabled:
                _p = yf_data['price_data']
                _d = yf_data.get('dividend_data', [])
                _t = ticker
                def _bg_w(_tk=_t, _pd=_p, _dd=_d):
                    try:
                        _gh_cache.save_price(_tk, _pd)
                        _gh_cache.save_dividend(_tk, _dd)
                    except Exception as _e:
                        print(f"  [GitHubCache] {_tk} 背景寫入失敗: {_e}")
                import threading as _th
                _th.Thread(target=_bg_w, daemon=True).start()
            return yf_data

        # 2. GitHub ETF 備援（原有邏輯）
        if ticker in GITHUB_ETF_NAMES:
            print("嘗試 GitHub 備援...")
            gh_data = self._fetch_from_github(ticker)
            if gh_data and gh_data.get('price_data'):
                print(f"✓ GitHub 成功")
                self._save_data(ticker, gh_data)
                return gh_data

        # 3. 模擬資料
        print(f"⚠ 所有來源失敗，使用模擬資料")
        sim_data = self._generate_simulated_data(ticker)
        self._save_data(ticker, sim_data)
        return sim_data

    # ─────────────────────────────────────────────────────────────
    # 股票分析專用：取得含 OHLCV + 技術指標 的完整資料
    # ─────────────────────────────────────────────────────────────
    def fetch_stock_analysis(self, ticker, force_refresh: bool = False):
        """
        回傳股票分析所需完整資料（四層快取）：
        L1 記憶體(1hr) → L1.5 磁碟(盤後) → L2 GitHub(20hr) → L3 yfinance
        {
          ticker, name, ohlcv, price_data, dividend_data,
          indicators, info, source
        }

        force_refresh=True：跳過所有快取，強制從 yfinance 重新抓取。
        """
        print(f"\n[分析模式] 取得 {ticker} 完整資料... (force={force_refresh})")
        self.last_error = ''

        # ── L1：記憶體快取（最快，同一進程內，1小時有效）────
        import sys
        _app = sys.modules.get('__main__') or sys.modules.get('app')
        mem_cache = getattr(_app, 'analysis_cache', {})
        if not force_refresh:
            cached = mem_cache.get(ticker)
            if isinstance(cached, dict) and (time.time() - cached.get('_ts', 0)) < 3600:
                print(f"  [L1 記憶體] {ticker} 命中快取")
                return cached

        # ── L1.5：本地磁碟快取（雙重TTL：同日 + 13:30盤後更新）──
        # ★ 修正：路徑統一為子資料夾格式 data/{ticker}/price.csv
        ticker_dir      = os.path.join(self.output_dir, ticker)
        disk_json_path  = os.path.join(ticker_dir, "meta.json")
        disk_ohlcv_path = os.path.join(ticker_dir, "price.csv")
        # 同時支援舊版平面路徑（向下相容讀取）
        _legacy_json  = os.path.join(self.output_dir, f"{ticker}_data.json")
        _legacy_ohlcv = os.path.join(self.output_dir, f"{ticker}_price.csv")
        if not os.path.exists(disk_json_path) and os.path.exists(_legacy_json):
            disk_json_path  = _legacy_json
        if not os.path.exists(disk_ohlcv_path) and os.path.exists(_legacy_ohlcv):
            disk_ohlcv_path = _legacy_ohlcv

        if not force_refresh and os.path.exists(disk_json_path) and os.path.exists(disk_ohlcv_path):
            try:
                from github_cache import _is_stale, TTL_PRICE
                _stale = _is_stale(disk_json_path, TTL_PRICE)
            except Exception:
                import datetime as _dt
                mtime_date = pd.Timestamp.fromtimestamp(os.path.getmtime(disk_json_path)).date()
                _stale = (mtime_date != pd.Timestamp.now().date())

            if not _stale:
                try:
                    with open(disk_json_path, 'r', encoding='utf-8') as f:
                        saved = json.load(f)
                    price_df = pd.read_csv(disk_ohlcv_path, encoding='utf-8-sig')

                    date_col  = next((c for c in price_df.columns if c in ['日期','date','Date']), None)
                    close_col = next((c for c in price_df.columns if c in ['收盤價','close','Close']), None)
                    if date_col and close_col and len(price_df) >= 5:
                        ohlcv = []
                        for _, row in price_df.iterrows():
                            c = float(row[close_col])
                            ohlcv.append({
                                'date':   str(row[date_col]),
                                'open':   float(row.get('open', row.get('開盤價', c))),
                                'high':   float(row.get('high', row.get('最高價', c))),
                                'low':    float(row.get('low',  row.get('最低價', c))),
                                'close':  c,
                                'volume': int(float(row.get('volume', row.get('成交量', 0)))),
                            })
                        # ★ 修正：配息路徑也改為子資料夾格式，同時向下相容舊路徑
                        div_csv = os.path.join(ticker_dir, "dividend.csv")
                        if not os.path.exists(div_csv):
                            div_csv = os.path.join(self.output_dir, f"{ticker}_hist_配息.csv")
                        dividend_data = []
                        if os.path.exists(div_csv):
                            try:
                                div_df = pd.read_csv(div_csv, encoding='utf-8-sig')
                                dc = next((c for c in div_df.columns if c in ['除息日','date','Date']), None)
                                dv = next((c for c in div_df.columns if c in ['股利','dividend','Dividend']), None)
                                if dc and dv:
                                    dividend_data = [
                                        {'date': str(r[dc]), 'dividend': float(r[dv])}
                                        for _, r in div_df.iterrows()
                                    ]
                            except Exception:
                                pass
                        indicators = calc_technical_indicators(ohlcv)
                        info = saved.get('info', {'name': ticker})
                        result = {
                            'ticker':        ticker,
                            'name':          info.get('name', ticker),
                            'ohlcv':         ohlcv,
                            'price_data':    [{'date': r['date'], 'close': r['close']} for r in ohlcv],
                            'dividend_data': dividend_data,
                            'indicators':    indicators,
                            'info':          info,
                            'source':        'disk_cache',
                            'is_simulated':  saved.get('is_simulated', False),
                            '_ts':           time.time(),
                        }
                        mem_cache[ticker] = result
                        print(f"  [L1.5 磁碟] {ticker} 快取有效（{len(ohlcv)} 筆），略過重新抓取")
                        return result
                except Exception as e:
                    print(f"  [L1.5 磁碟] 讀取失敗（非致命）: {e}")

        # ── L2：GitHub 持久化快取（跨重啟有效，force_refresh 時跳過）──
        if not force_refresh and _gh_cache and _gh_cache.enabled and _gh_cache.is_fresh(ticker, 'price'):
            rows = _gh_cache.load_price(ticker)
            if rows:
                div_rows  = _gh_cache.load_dividend(ticker)    or []
                info      = _gh_cache.load_fundamental(ticker) or {'name': ticker}

                ohlcv = []
                for r in rows:
                    c = float(r.get('close', r.get('收盤價', 0)))
                    ohlcv.append({
                        'date':   str(r.get('date', r.get('日期', ''))),
                        'open':   float(r.get('open',   c)),
                        'high':   float(r.get('high',   c)),
                        'low':    float(r.get('low',    c)),
                        'close':  c,
                        'volume': int(r.get('volume', 0)),
                    })

                dividend_data = [
                    {'date':     str(r.get('date', r.get('除息日', ''))),
                     'dividend': float(r.get('dividend', r.get('股利', 0)))}
                    for r in div_rows
                ]
                indicators = calc_technical_indicators(ohlcv)
                result = {
                    'ticker':        ticker,
                    'name':          info.get('name', ticker),
                    'ohlcv':         ohlcv,
                    'price_data':    [{'date': r['date'], 'close': r['close']} for r in ohlcv],
                    'dividend_data': dividend_data,
                    'indicators':    indicators,
                    'info':          info,
                    'source':        'github_cache',
                    'is_simulated':  False,
                    '_ts':           time.time(),
                }
                mem_cache[ticker] = result  # 回填 L1
                print(f"  [L2 GitHub] {ticker} 命中快取（{len(ohlcv)} 筆）")
                return result

        # ── L3：yfinance 重新抓取 ────────────────────────────
        # ★ 最多抓近 5 年（約 1250 筆），避免 period='max' 造成 timeout / 502
        # ★ 增量邏輯：若本機已有快取，依落差天數縮短抓取範圍，再合併去重
        # ★ timeout=60s：留足夠緩衝，防止 signal timed out
        print(f"  [L3 yfinance] {ticker} 重新抓取...")
        result = None

        # ── 讀取本機舊快取（供增量合併）──────────────────────
        _existing_rows: list = []
        _existing_path = os.path.join(self.output_dir, ticker, "price.csv")
        if not os.path.exists(_existing_path):
            _existing_path = os.path.join(self.output_dir, f"{ticker}_price.csv")
        if os.path.exists(_existing_path):
            try:
                _ex_df = pd.read_csv(_existing_path, encoding='utf-8-sig')
                _dc = next((c for c in _ex_df.columns if c in ['日期','date','Date']), None)
                _cc = next((c for c in _ex_df.columns if c in ['收盤價','close','Close']), None)
                if _dc and _cc:
                    for _, _r in _ex_df.iterrows():
                        _cv = float(_r[_cc])
                        _existing_rows.append({
                            'date':   str(_r[_dc]),
                            'open':   float(_r.get('open', _r.get('開盤價', _cv))),
                            'high':   float(_r.get('high', _r.get('最高價', _cv))),
                            'low':    float(_r.get('low',  _r.get('最低價', _cv))),
                            'close':  _cv,
                            'volume': int(float(_r.get('volume', _r.get('成交量', 0)))),
                        })
            except Exception:
                _existing_rows = []

        # 依落差天數決定抓取範圍（最多 5 年）
        _yf_period = '5y'   # ← 首次或快取過舊時，上限固定 5 年
        if _existing_rows:
            _last_date  = _existing_rows[-1]['date']
            # ★ 修正：統一使用 tz-naive 比較，避免 "Cannot subtract tz-naive and tz-aware" 錯誤
            #   pd.Timestamp(_last_date) 若字串含時區會是 tz-aware，.normalize() 後仍可能帶tz
            #   改用 .date() 轉為純 date 物件後再比較，完全迴避 tz 問題
            try:
                _last_ts = pd.Timestamp(_last_date)
                if _last_ts.tzinfo is not None:
                    _last_ts = _last_ts.tz_convert(None)  # 轉為 tz-naive（UTC）
                _now_ts = pd.Timestamp.now()              # tz-naive
                _days_behind = (_now_ts - _last_ts).days
            except Exception:
                # 終極保底：純字串比較計算天數差
                import datetime as _dt_mod
                try:
                    _last_d = _dt_mod.date.fromisoformat(str(_last_date)[:10])
                    _days_behind = (_dt_mod.date.today() - _last_d).days
                except Exception:
                    _days_behind = 999  # 視為過舊，觸發全量更新
            if _days_behind <= 30:
                _yf_period = '6mo'
                print(f"  [L3] 已有快取至 {_last_date}（差 {_days_behind} 天），改抓近 6 個月（增量）")
            elif _days_behind <= 180:
                _yf_period = '2y'
                print(f"  [L3] 已有快取至 {_last_date}（差 {_days_behind} 天），改抓近 2 年（增量）")
            else:
                print(f"  [L3] 已有快取至 {_last_date}（差 {_days_behind} 天），改抓近 5 年（全量更新）")

        for suffix in ['.TW', '.TWO']:
            yf_ticker = f"{ticker}{suffix}"
            try:
                tk   = yf.Ticker(yf_ticker)
                hist = tk.history(period=_yf_period, timeout=60)
                if hist is None or hist.empty:
                    continue

                ohlcv = []
                price_data = []
                for date, row in hist.iterrows():
                    try:
                        c = float(row['Close'])
                        o = float(row.get('Open', c))
                        h = float(row.get('High', c))
                        l = float(row.get('Low',  c))
                        v = float(row.get('Volume', 0))
                        if c > 0:
                            d = str(date.date())
                            ohlcv.append({'date': d, 'open': round(o,2),
                                          'high': round(h,2), 'low': round(l,2),
                                          'close': round(c,2), 'volume': int(v)})
                            price_data.append({'date': d, 'close': round(c,2)})
                    except Exception:
                        continue

                if not ohlcv:
                    continue

                # ── 增量合併：新資料優先，以 date 去重後按日期排序 ──
                if _existing_rows and _yf_period != '5y':
                    existing_dates = {r['date'] for r in ohlcv}
                    merged = [r for r in _existing_rows if r['date'] not in existing_dates]
                    merged.extend(ohlcv)
                    merged.sort(key=lambda r: r['date'])
                    ohlcv      = merged
                    price_data = [{'date': r['date'], 'close': r['close']} for r in ohlcv]
                    print(f"  [L3] 增量合併完成：共 {len(ohlcv)} 筆")
                # 配息
                dividend_data = []
                try:
                    divs = tk.dividends
                    if divs is not None and not divs.empty:
                        for date, amount in divs.items():
                            if float(amount) > 0:
                                dividend_data.append({
                                    'date': str(date.date()),
                                    'dividend': round(float(amount), 4)
                                })
                except Exception:
                    pass

                # 基本資訊（加 futures timeout 保護，避免 tk.info 卡住 worker）
                info = {}
                try:
                    from concurrent.futures import ThreadPoolExecutor as _TPE, TimeoutError as _TE
                    def _get_info():
                        return tk.info or {}
                    with _TPE(max_workers=1) as _ex:
                        try:
                            raw = _ex.submit(_get_info).result(timeout=10)
                        except (_TE, Exception):
                            raw = {}
                    info = {
                        'name':           raw.get('longName') or raw.get('shortName', ticker),
                        'sector':         raw.get('sector', ''),
                        'industry':       raw.get('industry', ''),
                        'market_cap':     raw.get('marketCap', 0),
                        'pe_ratio':       raw.get('trailingPE') or raw.get('forwardPE'),
                        'pb_ratio':       raw.get('priceToBook'),
                        'dividend_yield': raw.get('dividendYield'),
                        'eps':            raw.get('trailingEps'),
                        'revenue':        raw.get('totalRevenue'),
                        'profit_margin':  raw.get('profitMargins'),
                        'roe':            raw.get('returnOnEquity'),
                        'debt_ratio':     raw.get('debtToEquity'),
                        '52w_high':       raw.get('fiftyTwoWeekHigh'),
                        '52w_low':        raw.get('fiftyTwoWeekLow'),
                        'avg_volume':     raw.get('averageVolume'),
                        'description':    raw.get('longBusinessSummary', ''),
                    }
                except Exception as e:
                    print(f"  取得 info 失敗（非致命）: {e}")
                    info = {'name': ticker}

                # 計算技術指標
                indicators = calc_technical_indicators(ohlcv)

                result = {
                    'ticker':        ticker,
                    'yf_ticker':     yf_ticker,
                    'name':          info.get('name', ticker),
                    'ohlcv':         ohlcv,
                    'price_data':    price_data,
                    'dividend_data': dividend_data,
                    'indicators':    indicators,
                    'info':          info,
                    'source':        'yfinance',
                    'is_simulated':  False,
                    '_ts':           time.time(),
                }
                print(f"  ✓ yfinance ({yf_ticker}) 分析資料成功：{len(ohlcv)} 筆")
                self._save_data(ticker, result)

                # ★ GitHub 快取寫入改為背景執行緒，不阻塞 HTTP response
                if _gh_cache and _gh_cache.enabled:
                    _ohlcv_snap  = ohlcv
                    _div_snap    = dividend_data
                    _info_snap   = info
                    _ticker_snap = ticker
                    def _bg_gh_write(_t=_ticker_snap, _o=_ohlcv_snap,
                                     _d=_div_snap, _i=_info_snap):
                        try:
                            _gh_cache.save_price(_t, _o)
                            _gh_cache.save_dividend(_t, _d)
                            _gh_cache.save_fundamental(_t, _i)
                            print(f"  [GitHubCache] {_t} 背景寫入完成")
                        except Exception as _eg:
                            print(f"  [GitHubCache] {_t} 背景寫入失敗（非致命）: {_eg}")
                    import threading as _th
                    _th.Thread(target=_bg_gh_write, daemon=True).start()

                # 回填 L1 記憶體快取
                mem_cache[ticker] = result
                return result

            except requests.exceptions.ConnectionError:
                self.last_error = f"{yf_ticker}: 網路連線失敗"
                break
            except Exception as e:
                self.last_error = f"{yf_ticker}: {e}"
                continue

        # yfinance 失敗，嘗試 GitHub ETF 備援（原有邏輯保留）
        if ticker in GITHUB_ETF_NAMES:
            gh_data = self._fetch_from_github(ticker)
            if gh_data and gh_data.get('price_data'):
                ohlcv = [{'date': r['date'], 'open': r['close'], 'high': r['close'],
                          'low': r['close'], 'close': r['close'], 'volume': 0}
                         for r in gh_data['price_data']]
                indicators = calc_technical_indicators(ohlcv)
                gh_data['ohlcv']      = ohlcv
                gh_data['indicators'] = indicators
                _etf_name = GITHUB_ETF_NAMES.get(ticker, ticker)
                gh_data['info']       = {
                    'name':     _etf_name,
                    'sector':   '金融',
                    'industry': 'ETF',
                }
                gh_data['name']       = _etf_name
                return gh_data

        print(f"  ✗ {ticker} 分析資料取得失敗")
        return None

    # ─────────────────────────────────────────────────────────────
    # yfinance 爬取（回傳僅 price_data + dividend_data，供回測用）
    # ─────────────────────────────────────────────────────────────
    def _fetch_from_yfinance(self, ticker):
        for suffix in ['.TW', '.TWO']:
            yf_ticker = f"{ticker}{suffix}"
            try:
                tk = yf.Ticker(yf_ticker)
                hist = tk.history(period='5y', timeout=60)
                if hist is None or hist.empty:
                    continue

                price_data = []
                for date, row in hist.iterrows():
                    try:
                        c = float(row['Close'])
                        if c > 0:
                            price_data.append({
                                'date':  str(date.date()),
                                'close': round(c, 2)
                            })
                    except Exception:
                        continue

                if not price_data:
                    continue

                dividend_data = []
                try:
                    divs = tk.dividends
                    if divs is not None and not divs.empty:
                        for date, amount in divs.items():
                            if float(amount) > 0:
                                dividend_data.append({
                                    'date':     str(date.date()),
                                    'dividend': round(float(amount), 4)
                                })
                except Exception:
                    pass

                return {
                    'ticker':        ticker,
                    'yf_ticker':     yf_ticker,
                    'price_data':    price_data,
                    'dividend_data': dividend_data,
                    'is_simulated':  False,
                    'source':        'yfinance',
                }

            except requests.exceptions.ConnectionError:
                self.last_error = f"{yf_ticker}: 網路連線失敗（伺服器無法連接 Yahoo Finance）"
                break
            except Exception as e:
                self.last_error = f"{yf_ticker}: {e}"
                continue

        return None

    # ─────────────────────────────────────────────────────────────
    # GitHub 備援（僅支援預設 ETF 清單）
    # ─────────────────────────────────────────────────────────────
    def _fetch_from_github(self, ticker):
        if ticker not in GITHUB_ETF_NAMES:
            return None
        try:
            base_url  = "https://raw.githubusercontent.com/shui1133/analysis_ETF/main/data"
            etf_name  = GITHUB_ETF_NAMES[ticker]
            price_url = f"{base_url}/{ticker}_{etf_name}.csv"
            div_url   = f"{base_url}/{ticker}_{etf_name}_配息.csv"

            r = requests.get(price_url, timeout=10)
            if r.status_code != 200:
                return None

            price_df = pd.read_csv(StringIO(r.text))
            # 欄位標準化
            col_map = {}
            for col in price_df.columns:
                cl = col.lower().strip()
                if cl in ['date', '日期', 'datetime']:
                    col_map[col] = '日期'
                elif cl in ['close', '收盤價', 'price', 'closing']:
                    col_map[col] = '收盤價'
            if col_map:
                price_df = price_df.rename(columns=col_map)
            if '日期' not in price_df.columns or '收盤價' not in price_df.columns:
                if len(price_df.columns) >= 2:
                    price_df.columns = ['日期', '收盤價'] + list(price_df.columns[2:])
                else:
                    return None

            price_data = []
            for _, row in price_df.iterrows():
                try:
                    price_data.append({
                        'date':  str(row['日期']),
                        'close': float(row['收盤價'])
                    })
                except Exception:
                    continue

            if not price_data:
                return None

            dividend_data = []
            try:
                dr = requests.get(div_url, timeout=10)
                if dr.status_code == 200:
                    div_df = pd.read_csv(StringIO(dr.text))
                    dcol = next((c for c in div_df.columns
                                 if c.lower() in ['date','除息日','日期']), None)
                    vcol = next((c for c in div_df.columns
                                 if c.lower() in ['dividend','股利','amount']), None)
                    if dcol and vcol:
                        for _, row in div_df.iterrows():
                            try:
                                dividend_data.append({
                                    'date':     str(row[dcol]),
                                    'dividend': float(row[vcol])
                                })
                            except Exception:
                                continue
            except Exception:
                pass

            return {
                'ticker':        ticker,
                'price_data':    price_data,
                'dividend_data': dividend_data,
                'is_simulated':  False,
                'source':        'GitHub',
            }
        except Exception as e:
            print(f"  GitHub 錯誤: {e}")
            return None

    # ─────────────────────────────────────────────────────────────
    # 模擬資料（最後備援）
    # ─────────────────────────────────────────────────────────────
    def _generate_simulated_data(self, ticker):
        print(f"生成 {ticker} 模擬資料...")
        etf_params = {
            '0056':   {'start_price': 25, 'vol': 0.15, 'div_yield': 0.05},
            '00878':  {'start_price': 18, 'vol': 0.12, 'div_yield': 0.06},
            '00713':  {'start_price': 38, 'vol': 0.10, 'div_yield': 0.04},
            '00679B': {'start_price': 40, 'vol': 0.05, 'div_yield': 0.03},
            '00919':  {'start_price': 16, 'vol': 0.13, 'div_yield': 0.055},
            '00929':  {'start_price': 19, 'vol': 0.14, 'div_yield': 0.052},
            '006208': {'start_price': 80, 'vol': 0.18, 'div_yield': 0.035},
            '00915':  {'start_price': 18, 'vol': 0.16, 'div_yield': 0.048},
        }
        params = etf_params.get(ticker, {'start_price': 30, 'vol': 0.15, 'div_yield': 0.04})

        end_date   = datetime.now()
        start_date = end_date - timedelta(days=5 * 365)
        np.random.seed(hash(ticker) % 10000)

        price_data = []
        current    = params['start_price']
        cur_date   = start_date
        while cur_date <= end_date:
            if cur_date.weekday() < 5:
                daily_ret = np.random.normal(0.0003, params['vol'] / np.sqrt(252))
                current  *= (1 + daily_ret)
                price_data.append({'date': cur_date.strftime('%Y-%m-%d'),
                                   'close': round(current, 2)})
            cur_date += timedelta(days=1)

        dividend_data = []
        for yr in range(start_date.year, end_date.year + 1):
            for mo in [3, 6, 9, 12]:
                try:
                    div_date = datetime(yr, mo, 15)
                    if start_date <= div_date <= end_date:
                        avg_p = np.mean([p['close'] for p in price_data
                                         if p['date'][:7] == div_date.strftime('%Y-%m')] or [current])
                        dividend_data.append({
                            'date':     div_date.strftime('%Y-%m-%d'),
                            'dividend': round(avg_p * params['div_yield'] / 4, 4)
                        })
                except Exception:
                    pass

        return {
            'ticker':        ticker,
            'price_data':    price_data,
            'dividend_data': dividend_data,
            'is_simulated':  True,
            'source':        'simulated',
        }

    def _save_data(self, ticker, data):
        """
        儲存股票資料到本機磁碟。
        ★ 修正：路徑統一為子資料夾格式 data/{ticker}/price.csv
                 與 github_cache.local_save_price 完全一致，消除路徑雙軌問題。
        """
        try:
            # ── 建立子資料夾 ────────────────────────────────────
            ticker_dir = os.path.join(self.output_dir, ticker)
            os.makedirs(ticker_dir, exist_ok=True)

            # ── 存 price.csv（OHLCV 完整格式，僅保留近 5 年）──
            # ★ 截留近 5 年：避免 CSV 無限膨脹，技術分析用不到更早的資料
            _cutoff_5y = (pd.Timestamp.now() - pd.DateOffset(years=5)).strftime('%Y-%m-%d')

            if data.get('ohlcv'):
                ohlcv_df = pd.DataFrame(data['ohlcv'])
                # 欄位映射：統一輸出英文小寫欄位名（與 github_cache 格式一致）
                col_map = {'date': 'date', 'open': 'open', 'high': 'high',
                           'low': 'low', 'close': 'close', 'volume': 'volume',
                           '日期': 'date', '收盤價': 'close', '開盤價': 'open',
                           '最高價': 'high', '最低價': 'low', '成交量': 'volume'}
                ohlcv_df = ohlcv_df.rename(columns={k: v for k, v in col_map.items()
                                                      if k in ohlcv_df.columns})
                keep_cols = [c for c in ['date','open','high','low','close','volume']
                             if c in ohlcv_df.columns]
                if 'date' in keep_cols and 'close' in keep_cols:
                    ohlcv_df = ohlcv_df[ohlcv_df['date'] >= _cutoff_5y]
                    ohlcv_df[keep_cols].to_csv(
                        os.path.join(ticker_dir, "price.csv"),
                        index=False, encoding='utf-8-sig'
                    )
            elif data.get('price_data'):
                price_df = pd.DataFrame(data['price_data'])
                for old, new in [('date','date'),('Date','date'),('日期','date'),
                                  ('close','close'),('Close','close'),('收盤價','close')]:
                    if old in price_df.columns and new not in price_df.columns:
                        price_df = price_df.rename(columns={old: new})
                if 'date' in price_df.columns and 'close' in price_df.columns:
                    price_df = price_df[price_df['date'] >= _cutoff_5y]
                    price_df[['date','close']].to_csv(
                        os.path.join(ticker_dir, "price.csv"),
                        index=False, encoding='utf-8-sig'
                    )

            # ── 存 dividend.csv ─────────────────────────────────
            if data.get('dividend_data'):
                div_df = pd.DataFrame(data['dividend_data'])
                for old, new in [('date','date'),('Date','date'),('除息日','date'),
                                  ('dividend','dividend'),('Dividend','dividend'),('股利','dividend')]:
                    if old in div_df.columns and new not in div_df.columns:
                        div_df = div_df.rename(columns={old: new})
                if 'date' in div_df.columns and 'dividend' in div_df.columns:
                    div_df[['date','dividend']].to_csv(
                        os.path.join(ticker_dir, "dividend.csv"),
                        index=False, encoding='utf-8-sig'
                    )

            # ── 存 meta.json（基本資訊 + 元資料）──────────────
            # ★ 修正：同步記錄 price_at / dividend_at，供 github_cache._is_stale()
            #   使用時間戳判斷（而非退化成不可靠的檔案 mtime）
            import datetime as _dt
            _now_iso = _dt.datetime.now().isoformat()
            save_data = {k: v for k, v in data.items()
                         if k not in ('ohlcv', 'indicators')}
            save_data['price_at']    = _now_iso
            save_data['dividend_at'] = _now_iso
            save_data.setdefault('fundamental_at', _now_iso)
            with open(os.path.join(ticker_dir, "meta.json"),
                      'w', encoding='utf-8') as f:
                json.dump(save_data, f, ensure_ascii=False, indent=2)

            # ── 同步更新舊版平面路徑（向下相容，讓舊版 app.py 仍可讀）──
            # 注意：只做「指向性」複製，下次整合完成後可移除此段
            try:
                import shutil
                shutil.copy2(os.path.join(ticker_dir, "price.csv"),
                             os.path.join(self.output_dir, f"{ticker}_price.csv"))
                shutil.copy2(os.path.join(ticker_dir, "meta.json"),
                             os.path.join(self.output_dir, f"{ticker}_data.json"))
            except Exception:
                pass

        except Exception as e:
            print(f"  ❌ 儲存 {ticker} 失敗: {e}")
            import traceback; traceback.print_exc()

    # ─────────────────────────────────────────────────────────────
    # CacheManager 所需的三個獨立 fetch 方法
    # （github_cache.py 的 CacheManager.get_price/dividend/fundamental
    #   在 L3 網路抓取時會呼叫這三個方法）
    # ─────────────────────────────────────────────────────────────
    def fetch_price(self, ticker: str, force_refresh: bool = False) -> list | None:
        """
        取得股價資料，回傳 [{date, open, high, low, close, volume}, ...] 或 None。
        委派至 fetch_stock_analysis 以重用已有的 yfinance 邏輯。
        """
        raw = self.fetch_stock_analysis(ticker, force_refresh=force_refresh)
        if raw and raw.get('ohlcv'):
            return raw['ohlcv']
        # 降級：僅取 price_data（date + close）
        if raw and raw.get('price_data'):
            return raw['price_data']
        return None

    def fetch_dividend(self, ticker: str) -> list | None:
        """
        取得配息資料，回傳 [{date, dividend}, ...] 或 None。
        """
        raw = self.fetch_stock_analysis(ticker)
        if raw:
            return raw.get('dividend_data') or None
        return None

    def fetch_fundamental(self, ticker: str) -> dict | None:
        """
        取得基本面資料，回傳 info dict 或 None。
        """
        raw = self.fetch_stock_analysis(ticker)
        if raw:
            return raw.get('info') or None
        return None

    # ─────────────────────────────────────────────────────────────
    # 批量取得（供回測使用）
    # ─────────────────────────────────────────────────────────────
    def fetch_all_etfs(self, etf_list):
        results = {}
        for i, ticker in enumerate(etf_list, 1):
            print(f"\n[{i}/{len(etf_list)}] 處理 {ticker}")
            results[ticker] = self.fetch_data(ticker)
            if i < len(etf_list):
                time.sleep(1)
        success = sum(1 for r in results.values() if r is not None)
        print(f"\n完成！成功 {success}/{len(etf_list)} 支")
        return results

    # ─────────────────────────────────────────────────────────────
    # 自訂股票查詢（回測用）
    # ─────────────────────────────────────────────────────────────
    def fetch_custom_stock(self, ticker):
        self.last_error = ''
        errors = []
        for suffix in ['.TW', '.TWO']:
            yf_ticker = f"{ticker}{suffix}"
            try:
                tk   = yf.Ticker(yf_ticker)
                hist = tk.history(period='5y', timeout=60)
                if hist is None or hist.empty:
                    errors.append(f"{yf_ticker}: 無歷史資料")
                    continue

                price_data = []
                for date, row in hist.iterrows():
                    try:
                        c = float(row['Close'])
                        if c > 0:
                            price_data.append({'date': str(date.date()), 'close': round(c,2)})
                    except Exception:
                        continue

                if not price_data:
                    errors.append(f"{yf_ticker}: 資料筆數為0")
                    continue

                dividend_data = []
                try:
                    divs = tk.dividends
                    if divs is not None and not divs.empty:
                        for date, amount in divs.items():
                            if float(amount) > 0:
                                dividend_data.append({
                                    'date':     str(date.date()),
                                    'dividend': round(float(amount), 4)
                                })
                except Exception:
                    pass

                result = {
                    'ticker':        ticker,
                    'yf_ticker':     yf_ticker,
                    'price_data':    price_data,
                    'dividend_data': dividend_data,
                    'is_simulated':  False,
                }
                self._save_data(ticker, result)
                return result

            except requests.exceptions.ConnectionError:
                msg = f"{yf_ticker}: 網路連線失敗（伺服器無法連接 Yahoo Finance）"
                errors.append(msg)
                break
            except Exception as e:
                errors.append(f"{yf_ticker}: {e}")
                continue

        self.last_error = '；'.join(errors)
        return None


if __name__ == "__main__":
    fetcher = ETFDataFetcher()
    data = fetcher.fetch_stock_analysis("2330")
    if data:
        print(f"股票: {data['name']}")
        print(f"最新收盤: {data['ohlcv'][-1]['close'] if data['ohlcv'] else 'N/A'}")
        ind = data.get('indicators', {})
        if ind.get('macd'):
            last_macd = next((v for v in reversed(ind['macd']) if v is not None), None)
            print(f"MACD: {last_macd}")


# ─────────────────────────────────────────────────────────────
# 後端中文名稱對照表（供 app.py efficient_frontier API 使用）
# ─────────────────────────────────────────────────────────────
STOCK_NAMES_ZH_BACKEND = {
    '2330':'台積電','2317':'鴻海','2454':'聯發科','2382':'廣達',
    '2308':'台達電','2881':'富邦金','2882':'國泰金','2891':'中信金',
    '2886':'兆豐金','2303':'聯電','2412':'中華電','1301':'台塑',
    '1303':'南亞','2002':'中鋼','2357':'華碩','3008':'大立光',
    '2395':'研華','4938':'和碩','2603':'長榮','2615':'萬海',
    '2609':'陽明','2880':'華南金','2884':'玉山金','2885':'元大金',
    '2887':'台新金','2888':'新光金','2890':'永豐金','3045':'台灣大',
    '4904':'遠傳','2376':'技嘉','3711':'日月光投控','2379':'瑞昱',
    '2409':'友達','2408':'南亞科','6505':'台塑化','1326':'台化',
    '2207':'和泰車','2474':'可成','3481':'群創','2345':'智邦',
    '5880':'合庫金','2892':'第一金','2883':'開發金','2834':'臺企銀',
    '5871':'中租-KY','6669':'緯穎','3034':'聯詠','2301':'光寶科',
    '2317':'鴻海','2542':'興富發','2881':'富邦金','2324':'仁寶',
    '2535':'達欣工','5880':'合庫金','1101':'台泥','2330':'台積電',
    '2301':'光寶科',
    '0050':'元大台灣50','0056':'元大高股息',
    '006208':'富邦台50','00878':'國泰永續高股息',
    '00713':'元大台灣高息低波','00679B':'元大美債20年',
    '00919':'群益台灣精選高息','00929':'復華台灣科技優息',
    '00915':'凱基優選高股息30',
}
