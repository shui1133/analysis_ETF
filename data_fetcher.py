"""
台灣股票/ETF資料爬取模組 V4
優先順序：yfinance (完整OHLCV) → GitHub (shui1133/analysis_ETF) → 模擬資料
新增：OHLCV完整資料、技術指標(MACD, KD, RSI, 均線)、法人籌碼估算
"""

import yfinance as yf
import pandas as pd
import requests
import numpy as np
from datetime import datetime, timedelta
import time
import os
import sys
import platform
import re
from io import StringIO


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
        result = [None] * len(data)
        k = 2 / (period + 1)
        seed = None
        valid_start = 0
        for i, v in enumerate(data):
            if v is None:
                continue
            if seed is None:
                seed = v
                valid_start = i
            else:
                seed = v * k + seed * (1 - k)
            result[i] = round(seed, 4)
        # 前 period-1 筆視為無效
        for i in range(valid_start, min(valid_start + period - 1, len(data))):
            result[i] = None
        return result

    def sma(data, period):
        result = [None] * len(data)
        for i in range(period - 1, len(data)):
            vals = [v for v in data[i - period + 1:i + 1] if v is not None]
            if len(vals) == period:
                result[i] = round(sum(vals) / period, 4)
        return result

    # ── MA ─────────────────────────────────────────────────────
    ma5  = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)

    # ── MACD（EMA12 - EMA26，Signal=EMA9） ────────────────────
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd_line = [
        round(e12 - e26, 4) if (e12 is not None and e26 is not None) else None
        for e12, e26 in zip(ema12, ema26)
    ]
    signal_line = ema(macd_line, 9)
    macd_hist = [
        round((m - s) * 2, 4) if (m is not None and s is not None) else None
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

        # 1. yfinance（優先）
        print("嘗試 yfinance（優先）...")
        yf_data = self._fetch_from_yfinance(ticker)
        if yf_data and yf_data.get('price_data'):
            print(f"✓ yfinance 成功，{len(yf_data['price_data'])} 筆")
            self._save_data(ticker, yf_data)
            return yf_data

        # 2. GitHub（備援，僅 ETF）
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
    def fetch_stock_analysis(self, ticker):
        """
        回傳股票分析所需完整資料：
        {
          ticker, name, ohlcv, price_data, dividend_data,
          indicators, info, source
        }
        """
        print(f"\n[分析模式] 取得 {ticker} 完整資料...")
        self.last_error = ''

        result = None
        for suffix in ['.TW', '.TWO']:
            yf_ticker = f"{ticker}{suffix}"
            try:
                tk = yf.Ticker(yf_ticker)
                hist = tk.history(period='2y', timeout=15)
                if hist is None or hist.empty:
                    continue

                ohlcv = []
                price_data = []
                for date, row in hist.iterrows():
                    try:
                        c = float(row['Close'])
                        o = float(row.get('Open', c))
                        h = float(row.get('High', c))
                        l = float(row.get('Low', c))
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

                # 基本資訊
                info = {}
                try:
                    raw = tk.info or {}
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
                }
                print(f"  ✓ yfinance ({yf_ticker}) 分析資料成功：{len(ohlcv)} 筆")
                self._save_data(ticker, result)
                return result

            except requests.exceptions.ConnectionError:
                self.last_error = f"{yf_ticker}: 網路連線失敗"
                break
            except Exception as e:
                self.last_error = f"{yf_ticker}: {e}"
                continue

        # yfinance 失敗，嘗試 GitHub（ETF）
        if ticker in GITHUB_ETF_NAMES:
            gh_data = self._fetch_from_github(ticker)
            if gh_data and gh_data.get('price_data'):
                # GitHub 只有收盤價，轉為 ohlcv 格式
                ohlcv = [{'date': r['date'], 'open': r['close'], 'high': r['close'],
                          'low': r['close'], 'close': r['close'], 'volume': 0}
                         for r in gh_data['price_data']]
                indicators = calc_technical_indicators(ohlcv)
                gh_data['ohlcv'] = ohlcv
                gh_data['indicators'] = indicators
                gh_data['info'] = {'name': GITHUB_ETF_NAMES.get(ticker, ticker)}
                gh_data['name'] = GITHUB_ETF_NAMES.get(ticker, ticker)
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
                hist = tk.history(period='max', timeout=15)
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

    # ─────────────────────────────────────────────────────────────
    # 儲存資料（與 backtest.py 格式相容）
    # ─────────────────────────────────────────────────────────────
    def _save_data(self, ticker, data):
        try:
            import json
            if data.get('price_data'):
                price_df = pd.DataFrame(data['price_data'])
                for old, new in [('date','日期'),('Date','日期'),
                                  ('close','收盤價'),('Close','收盤價')]:
                    if old in price_df.columns:
                        price_df = price_df.rename(columns={old: new})
                if '日期' not in price_df.columns or '收盤價' not in price_df.columns:
                    if len(price_df.columns) >= 2:
                        price_df.columns = ['日期','收盤價'] + list(price_df.columns[2:])
                    else:
                        return
                price_df = price_df[['日期','收盤價']]
                price_df.to_csv(
                    os.path.join(self.output_dir, f"{ticker}_price.csv"),
                    index=False, encoding='utf-8-sig'
                )

            if data.get('dividend_data'):
                div_df = pd.DataFrame(data['dividend_data'])
                for old, new in [('date','除息日'),('Date','除息日'),
                                  ('dividend','股利'),('Dividend','股利')]:
                    if old in div_df.columns:
                        div_df = div_df.rename(columns={old: new})
                if '除息日' not in div_df.columns or '股利' not in div_df.columns:
                    if len(div_df.columns) >= 2:
                        div_df.columns = ['除息日','股利'] + list(div_df.columns[2:])
                    else:
                        return
                div_df = div_df[['除息日','股利']]
                div_df.to_csv(
                    os.path.join(self.output_dir, f"{ticker}_hist_配息.csv"),
                    index=False, encoding='utf-8-sig'
                )

            with open(os.path.join(self.output_dir, f"{ticker}_data.json"),
                      'w', encoding='utf-8') as f:
                # 排除 ohlcv/indicators（體積太大）只存基本
                save_data = {k: v for k, v in data.items()
                             if k not in ('ohlcv', 'indicators')}
                json.dump(save_data, f, ensure_ascii=False, indent=2)

        except Exception as e:
            print(f"  ❌ 儲存 {ticker} 失敗: {e}")
            import traceback; traceback.print_exc()

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
                hist = tk.history(period='max', timeout=15)
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
