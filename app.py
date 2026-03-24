"""
Flask Web應用主程式 V4
台灣ETF/股票投資分析系統
新增：股票分析頁 API（OHLCV、技術指標、投資建議）
調整：蒙地卡羅/情境分析資料仍由後端計算，由新頁面 analysis.html 呈現

════════════════════════════════════════════════════════════
必要環境變數（本機：.env；Render：Dashboard → Environment）
════════════════════════════════════════════════════════════

  GH_CACHE_TOKEN = ghp_xxxxxxxxxxxxxxxxxxxxxxxx
    ├─ 用途：GitHub API 寫入（每日 16:00 自動 push 快取至 Repo）
    ├─ 來源：GitHub → Settings → Developer settings
    │        → Personal access tokens (classic)
    │        → 勾選 repo scope（或 public_repo 若為公開 Repo）
    ├─ 未設定時：程式仍可【讀取】GitHub Public Repo，
    │            但每日排程不會將資料 push 回 GitHub
    └─ 安全注意：Token 僅可透過環境變數傳入，不得寫入程式碼或 HTML

  ANTHROPIC_API_KEY = sk-ant-xxxxxxxxxxxxxxxxxxxxxxxx
    ├─ 用途：AI 財務健診報告（呼叫 Claude API）
    └─ 未設定時：/api/ai_report POST 端點會回傳 500 錯誤

════════════════════════════════════════════════════════════
"""

from flask import Flask, render_template, request, jsonify, send_file
from flask.json.provider import DefaultJSONProvider
import pandas as pd
import json
import os
import platform
import numpy as np
from data_fetcher import ETFDataFetcher, get_data_dir, POPULAR_STOCKS, calc_technical_indicators
from backtest import PortfolioBacktestV3
from github_cache import CacheManager, start_scheduler
import io
import requests as _req
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── 強化版移動平均線分析模組 (V2) ──────────────────────────────────────────
from ma_analysis_enhanced import (
    analyze_ma,
    calc_granville_signals,
    estimate_cross_days,
    calc_bias,
    bias_warning,
    enhanced_calc_trend,
    enhanced_generate_recommendation,
)

# ── 載入 .env（本機開發用；Render 環境直接讀系統環境變數）──
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass   # 若未安裝 python-dotenv，忽略（Render 不需要）

app = Flask(__name__)


# 修復 numpy int32/float32/ndarray 無法 JSON 序列化的問題（Flask 3.x）
class NumpyJSONProvider(DefaultJSONProvider):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


app.json_provider_class = NumpyJSONProvider
app.json = NumpyJSONProvider(app)

# 設定資料目錄
DATA_DIR = get_data_dir()
print(f"資料目錄: {DATA_DIR}")
os.makedirs(DATA_DIR, exist_ok=True)

# 三層快取管理器（本機 Storage → GitHub → yfinance）
# ★ 保護：若 CacheManager.__init__ 內有網路呼叫，加 try/except 防止阻塞 worker
try:
    cache_mgr = CacheManager(data_dir=DATA_DIR)
except Exception as _cm_err:
    print(f"  [WARNING] CacheManager 初始化失敗（不影響啟動）: {_cm_err}")
    class _DummyCache:
        def __getattr__(self, name):
            return lambda *a, **kw: None
    cache_mgr = _DummyCache()

# 全域快取
cached_results    = {}
etf_memory_cache  = {}
analysis_cache    = {}   # 股票分析快取（key=ticker）

# ★ 新增：啟動時背景預熱快取（從 GitHub 拉取 → 存本機）
def _background_warmup():
    """
    Render 部署後快取為空，這個背景執行緒在啟動後立即從 GitHub
    把最近一次的快取拉回本機，讓第一次前端請求直接命中 L1/L2。
    不阻塞 gunicorn 健康檢查，失敗也不影響正常服務。
    """
    import time as _t
    _t.sleep(20)   # ★ 延長等待：給 gunicorn health check 充足時間通過
    try:
        from github_cache import (GitHubCache, local_save_price,
                                  local_save_dividend, local_save_fundamental,
                                  TOP50_STOCKS)
        gh = GitHubCache()
        if not getattr(gh, 'enabled', False):
            print("  [Warmup] GitHub 未設定，略過預熱")
            return
        tickers = list(TOP50_STOCKS)
        if isinstance(POPULAR_STOCKS, dict):
            tickers += [k for k in POPULAR_STOCKS.keys() if k not in tickers]
        elif isinstance(POPULAR_STOCKS, list):
            for s in POPULAR_STOCKS:
                code = s.get('code') or s.get('ticker') if isinstance(s, dict) else s
                if code and code not in tickers:
                    tickers.append(code)
        print(f"  [Warmup] 開始預熱 {len(tickers)} 支股票快取...")
        hit = 0
        for tk in tickers:
            try:
                rows = gh.load_price(tk)
                if rows:
                    local_save_price(DATA_DIR, tk, rows)
                    hit += 1
                rows_d = gh.load_dividend(tk)
                if rows_d:
                    local_save_dividend(DATA_DIR, tk, rows_d)
                info = gh.load_fundamental(tk)
                if info:
                    local_save_fundamental(DATA_DIR, tk, info)
                _t.sleep(0.05)   # ★ 每次請求間隔，避免 rate limit
            except Exception:
                pass
        print(f"  [Warmup] ✅ 預熱完成，命中 {hit}/{len(tickers)} 支")
    except Exception as e:
        print(f"  [Warmup] 預熱失敗（不影響服務）: {e}")

import threading as _threading
_threading.Thread(target=_background_warmup, daemon=True).start()


# ═══════════════════════════════════════════════════════════════
# 頁面路由
# ═══════════════════════════════════════════════════════════════

@app.route('/')
def index():
    """主頁（退休規劃回測）"""
    return render_template('index.html')


@app.route('/analysis')
def analysis():
    """股票分析頁"""
    return render_template('analysis.html')


# ═══════════════════════════════════════════════════════════════
# 原有 API（回測系統）
# ═══════════════════════════════════════════════════════════════

@app.route('/api/fetch_data', methods=['POST'])
def fetch_data():
    """爬取ETF資料API"""
    try:
        data         = request.json
        portfolio_type = data.get('portfolio_type', 'conservative')

        portfolio_etfs = {
            'conservative': ['00878', '00713', '00679B'],
            'balanced':     ['00919', '00929', '0056'],
            'aggressive':   ['006208', '00929', '00915']
        }
        etf_list = portfolio_etfs.get(portfolio_type, [])
        fetcher  = ETFDataFetcher(output_dir=DATA_DIR)
        results  = fetcher.fetch_all_etfs(etf_list)

        for code, etf_data in results.items():
            if etf_data is not None:
                etf_memory_cache[code] = etf_data

        success_count = sum(1 for r in results.values() if r is not None)
        return jsonify({
            'status':  'success',
            'message': f'成功爬取 {success_count}/{len(etf_list)} 支ETF資料',
            'results': {k: (v is not None) for k, v in results.items()}
        })
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/fetch_custom', methods=['POST'])
def fetch_custom():
    """爬取自訂台灣上市股票/ETF資料"""
    try:
        data        = request.get_json(force=True) or {}
        tickers_raw = data.get('tickers', [])

        if not tickers_raw or len(tickers_raw) < 2:
            return jsonify({'status': 'error', 'message': '請至少輸入2支股票代碼'}), 400
        if len(tickers_raw) > 5:
            return jsonify({'status': 'error', 'message': '最多輸入5支股票代碼'}), 400

        tickers = [t.strip().upper() for t in tickers_raw if t.strip()]
        if len(tickers) != len(set(tickers)):
            return jsonify({'status': 'error', 'message': '股票代碼不能重複'}), 400

        fetcher = ETFDataFetcher(output_dir=DATA_DIR)

        try:
            batch_results = fetcher.fetch_all_etfs(tickers)
        except Exception:
            batch_results = {}
            for ticker in tickers:
                try:
                    r = fetcher.fetch_custom_stock(ticker)
                    batch_results[ticker] = r if (r and r.get('price_data')) else None
                except Exception:
                    batch_results[ticker] = None

        results = {}
        failed  = []
        for ticker in tickers:
            result = batch_results.get(ticker)
            if result and result.get('price_data'):
                results[ticker] = result
                etf_memory_cache[ticker] = result
            else:
                reason = getattr(fetcher, 'last_error', '')
                failed.append({'ticker': ticker, 'reason': reason})

        if failed:
            failed_codes = [f['ticker'] for f in failed]
            is_network = any('網路連線失敗' in (f['reason'] or '') for f in failed)
            if is_network:
                hint = '⚠️ 伺服器無法連接 Yahoo Finance，請確認網路環境或稍後再試。'
            else:
                details = '、'.join(
                    f"{f['ticker']}（{f['reason'][:40]}）" if f['reason'] else f['ticker']
                    for f in failed
                )
                hint = f"無法取得股價資料：{details}。請確認代碼格式正確（台灣上市輸入如 2330、00878）"
            return jsonify({
                'status':  'error',
                'message': hint,
                'failed':  failed_codes,
                'success': list(results.keys())
            }), 422

        return jsonify({
            'status':  'success',
            'message': f'成功取得 {len(results)} 支股票資料',
            'tickers': tickers,
            'results': {k: True for k in results}
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


def restore_cache_to_disk():
    """從記憶體快取還原資料到磁碟"""
    if not etf_memory_cache:
        return 0
    restored = 0
    for code, etf_data in etf_memory_cache.items():
        try:
            if etf_data.get('price_data'):
                price_df = pd.DataFrame(etf_data['price_data'])
                price_df = price_df.rename(columns={'date': '日期', 'close': '收盤價'})
                price_df.to_csv(
                    os.path.join(DATA_DIR, f"{code}_price.csv"),
                    index=False, encoding='utf-8-sig'
                )
            if etf_data.get('dividend_data'):
                div_df = pd.DataFrame(etf_data['dividend_data'])
                div_df = div_df.rename(columns={'date': '除息日', 'dividend': '股利'})
                div_df.to_csv(
                    os.path.join(DATA_DIR, f"{code}_hist_配息.csv"),
                    index=False, encoding='utf-8-sig'
                )
            json_path = os.path.join(DATA_DIR, f"{code}_data.json")
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(etf_data, f, ensure_ascii=False, indent=2)
            restored += 1
        except Exception as e:
            print(f"⚠️ 還原 {code} 失敗: {e}")
    return restored


@app.route('/api/backtest', methods=['POST'])
def run_backtest():
    """執行回測API（V3版本）"""
    try:
        data = request.get_json(force=True) or {}

        portfolio_type        = data.get('portfolio_type', 'conservative')
        initial_capital       = int(data.get('initial_capital', 100)) * 10000
        monthly_investment    = int(data.get('monthly_investment', 3)) * 10000
        current_age           = int(data.get('current_age', 30))
        target_monthly_spend  = int(data.get('target_monthly_spend', 4)) * 10000
        custom_tickers        = data.get('custom_tickers', None)
        custom_weights        = data.get('custom_weights', None)
        custom_withdrawal_rate= float(data.get('custom_withdrawal_rate', 0.04))

        if portfolio_type == 'custom':
            if not custom_tickers or len(custom_tickers) < 2:
                return jsonify({'status': 'error', 'message': '自訂模式需提供至少2支股票代碼'}), 400
            needed_etfs = [t.strip().upper() for t in custom_tickers]
        else:
            portfolio_etfs = {
                'conservative': ['00878', '00713', '00679B'],
                'balanced':     ['00919', '00929', '0056'],
                'aggressive':   ['006208', '00929', '00915']
            }
            needed_etfs = portfolio_etfs.get(portfolio_type, [])

        missing = [
            etf for etf in needed_etfs
            if not os.path.exists(os.path.join(DATA_DIR, f"{etf}_price.csv"))
        ]
        if missing:
            restore_cache_to_disk()
            still_missing = [
                etf for etf in needed_etfs
                if not os.path.exists(os.path.join(DATA_DIR, f"{etf}_price.csv"))
            ]
            if still_missing:
                return jsonify({
                    'status':  'error',
                    'message': f'找不到 {still_missing} 的資料，請先點擊「查詢股票資料」'
                }), 400

        backtester = PortfolioBacktestV3(data_dir=DATA_DIR)
        result = backtester.backtest_portfolio(
            portfolio_type=portfolio_type,
            initial_capital=initial_capital,
            monthly_investment=monthly_investment,
            current_age=current_age,
            target_monthly_spend=target_monthly_spend,
            custom_tickers=custom_tickers,
            custom_weights=custom_weights,
            custom_withdrawal_rate=custom_withdrawal_rate,
        )

        if result is None:
            return jsonify({
                'status':  'error',
                'message': '回測失敗，請先點擊「爬取資料」後再執行回測'
            }), 400

        cached_results[portfolio_type] = result
        chart_data = prepare_chart_data_from_annual(result)
        table_data = prepare_table_data(result)

        mc_data        = result.get('monte_carlo', {})
        scenarios_data = result.get('scenarios', {})
        fp_data        = result.get('forecast_params', {})

        return jsonify({
            'status': 'success',
            'result': {
                'portfolio_name':    result['portfolio_name'],
                'finish_year':       result['finish_year'],
                'finish_age':        result['finish_age'],
                'final_assets':      round(result['final_assets']),
                'actual_invested':   round(result['actual_invested']),
                'actual_dividend':   round(result['actual_dividend']),
                'forecast_assets':   round(result['forecast_assets']),
                'total_invested':    round(result['total_invested']),
                'total_dividend':    round(result['total_dividend']),
                'chart_data':        chart_data,
                'table_data':        table_data,
                'etf_weights':       result['etf_weights'],
                'etf_details':       [],
                'etf_tracking':      result['etf_annual_tracking'],
                'hist_stats': {
                    t: {
                        'cagr':            round(v['cagr'] * 100, 2),
                        'avg_div_per_share':round(v['avg_div_per_share'], 4),
                        'avg_div_times':   round(v['avg_div_times'], 1),
                        'avg_price':       round(v['avg_price'], 2),
                        'last_price':      round(v['last_price'], 2)
                    }
                    for t, v in result['hist_stats'].items()
                },
                'monte_carlo':      mc_data,
                'scenarios':        scenarios_data,
                'forecast_params':  fp_data,
                'retirement_stop':  result.get('retirement_stop', []),
                'retirement_continue': result.get('retirement_continue', []),
                'retire_yr_idx':    result.get('retire_yr_idx'),
                'withdrawal_rate':  result.get('withdrawal_rate', 0.04),
            }
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/download_csv/<portfolio_type>')
def download_csv(portfolio_type):
    """下載CSV報表"""
    try:
        if portfolio_type not in cached_results:
            return jsonify({'status': 'error', 'message': '請先執行回測'}), 400
        result = cached_results[portfolio_type]
        df     = pd.DataFrame(result['results']['annual_summary'])
        output = io.StringIO()
        df.to_csv(output, index=False, encoding='utf-8-sig')
        output.seek(0)
        return send_file(
            io.BytesIO(output.getvalue().encode('utf-8-sig')),
            mimetype='text/csv',
            as_attachment=True,
            download_name=f'{result["portfolio_name"]}_回測報表.csv'
        )
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# 股票分析頁 API
# ═══════════════════════════════════════════════════════════════

@app.route('/api/popular_stocks')
def get_popular_stocks():
    """回傳熱門股票清單"""
    return jsonify({'status': 'success', 'stocks': POPULAR_STOCKS})


# ─────────────────────────────────────────────────────────────────
# 強制從 yfinance 取得最新股價（熱門股票/ETF/個股共用）
# ─────────────────────────────────────────────────────────────────
# GET /api/health — 服務健康檢查（前端 warmup ping 用）
# 讓 Render 冷啟動時前端可先 ping 此 endpoint 預熱，
# 避免 /api/hot_summary 首次請求遇到 502 Bad Gateway
# ─────────────────────────────────────────────────────────────────
@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({'status': 'ok', 'ts': pd.Timestamp.now().isoformat()})


# POST /api/force_refresh_price
# 輸入: { "tickers": ["2330", "00878"] }  或  { "ticker": "2330" }
# 說明: 忽略所有快取，直接向 yfinance 重抓，並同步存本機 + GitHub
# ─────────────────────────────────────────────────────────────────
@app.route('/api/force_refresh_price', methods=['POST'])
def force_refresh_price():
    """強制從 yfinance 取得最新股價並更新快取（跳過所有快取層）"""
    import time as _time
    try:
        body = request.get_json(force=True) or {}
        # 支援單支（ticker）或批次（tickers）
        if 'ticker' in body:
            tickers = [str(body['ticker']).strip().upper()]
        else:
            tickers = [str(t).strip().upper() for t in body.get('tickers', []) if str(t).strip()]
        if not tickers:
            return jsonify({'status': 'error', 'message': '請提供 ticker 或 tickers'}), 400

        MAX_BATCH = 10
        tickers = tickers[:MAX_BATCH]

        fetcher = ETFDataFetcher(output_dir=DATA_DIR)
        results = {}
        for tk in tickers:
            t0 = _time.time()
            try:
                # ★ 修正：清除所有快取層（L1 記憶體 + L1.5 磁碟的 meta.json mtime）
                # 清 L1 記憶體
                if tk in analysis_cache:
                    del analysis_cache[tk]

                # ★ 修正：呼叫 force_refresh=True，強制跳過 L1.5/L2，直接打 yfinance
                raw = fetcher.fetch_stock_analysis(tk, force_refresh=True)
                if not raw or not raw.get('ohlcv'):
                    results[tk] = {'status': 'error', 'message': fetcher.last_error or '無法取得資料'}
                    continue

                ohlcv = raw['ohlcv']
                # 轉換為 price list 格式存快取（github_cache 格式）
                price_list = [
                    {'date': r['date'], 'open': r.get('open'), 'high': r.get('high'),
                     'low': r.get('low'), 'close': r['close'], 'volume': r.get('volume', 0)}
                    for r in ohlcv
                ]
                # ★ 修正：_save_data 內部已同步存子資料夾 + 舊版平面路徑，無需再次呼叫
                # 但仍額外呼叫 gh_save_price 推送到 GitHub
                try:
                    from github_cache import gh_save_price
                    gh_save_price(tk, price_list)
                except Exception as _e_gh:
                    print(f"  [forceRefresh] GitHub 推送失敗（非致命）: {_e_gh}")

                last = ohlcv[-1]
                prev = ohlcv[-2] if len(ohlcv) >= 2 else last
                change = round(last['close'] - prev['close'], 2)
                change_pct = round(change / prev['close'] * 100, 2) if prev['close'] else 0

                results[tk] = {
                    'status':     'success',
                    'date':       last['date'],
                    'close':      last['close'],
                    'change':     change,
                    'change_pct': change_pct,
                    'rows':       len(ohlcv),
                    'elapsed_ms': round((_time.time() - t0) * 1000),
                }
            except Exception as e:
                results[tk] = {'status': 'error', 'message': str(e)}

        all_ok = all(v.get('status') == 'success' for v in results.values())
        return jsonify({
            'status':     'success' if all_ok else 'partial',
            'results':    results,
            'updated_at': pd.Timestamp.now().isoformat(),
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ─────────────────────────────────────────────────────────────────
# 批次摘要 API（熱門股票頁 / 自選股頁使用）
# 前端一次 POST 多個 ticker，後端批次回傳摘要，避免逐支請求
# ─────────────────────────────────────────────────────────────────
def _build_hot_summary(ticker: str, raw: dict) -> dict | None:
    """將 fetch_stock_analysis 結果轉為前端 hot_summary 所需格式"""
    ohlcv = raw.get('ohlcv', [])
    if not ohlcv:
        return None
    last = ohlcv[-1]
    prev = ohlcv[-2] if len(ohlcv) >= 2 else last
    change     = round(last['close'] - prev['close'], 2)
    change_pct = round(change / prev['close'] * 100, 2) if prev['close'] else 0

    indicators = raw.get('indicators', {})
    def last_val(lst):
        return next((v for v in reversed(lst or []) if v is not None), None)

    # ── 技術指標最新值 ──────────────────────────────────────────
    ma5   = last_val(indicators.get('ma5'))
    ma10  = last_val(indicators.get('ma10'))
    ma20  = last_val(indicators.get('ma20'))
    ma60  = last_val(indicators.get('ma60'))
    ma120 = last_val(indicators.get('ma120'))
    ma200 = last_val(indicators.get('ma200'))
    macd  = last_val(indicators.get('macd'))
    macd_signal = last_val(indicators.get('macd_signal'))
    rsi   = last_val(indicators.get('rsi'))
    k     = last_val(indicators.get('k'))
    d     = last_val(indicators.get('d'))

    # ── 近 12 個月配息殖利率 ────────────────────────────────────
    divs = raw.get('dividend_data', [])
    annual_div = 0.0
    if divs:
        one_yr_ago = (pd.Timestamp.now() - pd.DateOffset(years=1)).strftime('%Y-%m-%d')
        annual_div = sum(float(d.get('dividend', 0) or 0) for d in divs if d.get('date', '') >= one_yr_ago)
    div_yield = round(annual_div / last['close'] * 100, 2) if last['close'] and annual_div else None

    info = raw.get('info', {})

    # ── 均線排列（matrend）─────────────────────────────────────
    matrend = 0
    if ma5 and ma20 and ma60:
        if   ma5 > ma20 and ma20 > ma60: matrend =  1   # 多頭排列
        elif ma5 < ma20 and ma20 < ma60: matrend = -1   # 空頭排列
        elif ma5 > ma20:                 matrend =  2   # 偏多整理
        else:                            matrend = -2   # 偏空整理

    # ── 近 60 日漲跌統計 ───────────────────────────────────────
    recent = ohlcv[-60:] if len(ohlcv) >= 60 else ohlcv
    up_days = down_days = flat_days = 0
    up_vol  = down_vol  = total_vol = 0
    for bar in recent:
        diff = bar['close'] - bar.get('open', bar['close'])
        v    = bar.get('volume', 0) or 0
        total_vol += v
        if diff > 0:      up_days   += 1; up_vol   += v
        elif diff < 0:    down_days += 1; down_vol += v
        else:             flat_days += 1
    up_vol_pct   = round(up_vol   / total_vol * 100) if total_vol else 0
    down_vol_pct = round(down_vol / total_vol * 100) if total_vol else 0

    # ── 近 60 日支撐/壓力 ──────────────────────────────────────
    support = round(min(bar['low']  for bar in recent), 2)
    resist  = round(max(bar['high'] for bar in recent), 2)

    # ── 趨勢判斷 ──────────────────────────────────────────────
    latest_ind = {'ma5': ma5, 'ma20': ma20, 'ma60': ma60,
                  'macd': macd, 'macd_signal': macd_signal, 'rsi': rsi,
                  'k': k, 'd': d}
    # ── 均線序列 dict（供葛蘭碧/交叉預測使用）─────────────────
    ma_series_dict = {
        'ma20': indicators.get('ma20', []),
        'ma60': indicators.get('ma60', []),
    }
    trend = _calc_trend(last['close'], latest_ind, ohlcv=ohlcv, ma_series_dict=ma_series_dict)

    # ── 法人籌碼估算 ───────────────────────────────────────────
    chip = _estimate_chip(ohlcv, trend)

    # ── 投資評級 ──────────────────────────────────────────────
    rec = _generate_recommendation(
        ticker, last['close'], latest_ind, trend, chip,
        info, div_yield, support, resist
    )

    # yfinance volume 是「股數」，台灣慣用「張」（1張=1000股）
    # 若 volume < 10000 視為已換算過（如來自本機快取）；否則除以 1000
    def _to_lots(v):
        if not v:
            return 0
        return max(1, round(v / 1000)) if v >= 10000 else int(v)

    return {
        'ticker':       ticker,
        'name':         raw.get('name', ticker),
        'close':        last['close'],
        'open':         last.get('open'),
        'high':         last['high'],
        'low':          last['low'],
        'volume':       _to_lots(last.get('volume', 0)),
        'change':       change,
        'change_pct':   change_pct,
        'date':         last['date'],
        # 技術指標
        'ma5': ma5, 'ma10': ma10, 'ma20': ma20,
        'ma60': ma60, 'ma120': ma120, 'ma200': ma200,
        'macd': macd, 'macd_signal': macd_signal,
        'rsi': rsi, 'k': k, 'd': d,
        # 基本面
        'div_yield':  div_yield,
        'pe_ratio':   info.get('pe_ratio'),
        'pb_ratio':   info.get('pb_ratio'),
        'market_cap': info.get('market_cap'),
        # 均線排列 & 60 日統計
        'matrend':    matrend,
        'updays':     up_days,
        'downdays':   down_days,
        'flatdays':   flat_days,
        'upvolpct':   up_vol_pct,
        'downvolpct': down_vol_pct,
        # 趨勢
        'trend_label': trend.get('label'),
        'trend_color': trend.get('color'),
        # 投資評級
        'rating':       rec.get('rating'),
        'rating_score': rec.get('total_score', 0),
        'rating_color': rec.get('rating_color', '#64748b'),
        'rating_bg':    rec.get('rating_bg', '#1e293b'),
        'rating_icon':  rec.get('rating_icon', ''),
        'target_price': rec.get('target_price'),
        'target_type':  rec.get('target_type', 'none'),
        'reasons_buy':  rec.get('reasons_buy', []),
        'reasons_sell': rec.get('reasons_sell', []),
        # ★ 修正：加入近 1260 日完整 chart 陣列，供個股頁成交量圖使用
        # _summaryToDataFormat 的 chart.volumes 目前為空陣列導致圖表空白
        'chart': (lambda rows: {
            'dates':   [r['date']   for r in rows],
            # ★ open 缺值時用前根 close（保留漲跌色判斷），避免日線 K 棒全部消失
            'opens':   [rows[i].get('open') if rows[i].get('open')
                        else (rows[i-1]['close'] if i > 0 else rows[i]['close'])
                        for i in range(len(rows))],
            'highs':   [r.get('high', r['close']) for r in rows],
            'lows':    [r.get('low',  r['close']) for r in rows],
            'closes':  [r['close']  for r in rows],
            # ★ volume 閾值改為 100000：快取已是「張」(< 10萬)，yfinance 原始股數才 >= 10萬
            'volumes': [round(r.get('volume', 0) / 1000) if (r.get('volume') or 0) >= 100000
                        else int(r.get('volume') or 0) for r in rows],
        })(ohlcv[-1260:]),
    }


def _fetch_one_hot(ticker: str, fetcher) -> tuple:
    """
    單支股票查詢（供 ThreadPoolExecutor 呼叫）。
    查詢順序：
      L0 記憶體快取（5 分鐘）
      L1 cache_mgr 本機快取（免網路）
      L2 cache_mgr GitHub 快取（免 yfinance）
      L3 yfinance 網路抓取（前三層皆無才觸發，結果存回快取）
    回傳 (ticker, summary_dict_or_None)

    ★ 修正 v3：0000（大盤指數）特殊處理，從 analysis_cache 直接取已存的
      _get_twii_analysis 結果，避免重複走 yfinance 造成 worker timeout。
    """
    import time as _time

    # ── 特殊處理：0000 = 台灣加權指數（背景轉換為 ^TWII 查詢）───────
    if ticker == '0000':
        # L0：先檢查記憶體快取（10 分鐘內有效）
        cache_entry = analysis_cache.get('0000')
        if cache_entry and (_time.time() - cache_entry.get('ts', 0)) < 600:
            raw = cache_entry.get('data')
            if raw and isinstance(raw, dict) and raw.get('chart', {}).get('closes'):
                return ticker, _build_twii_hot_summary(raw)

        # L1：快取過期或無快取 → 在當前 thread 直接查 yfinance ^TWII
        #   （此函數已被 ThreadPoolExecutor 包住，不會 block gunicorn worker）
        try:
            data_out = _fetch_twii_data_raw()
            if data_out:
                analysis_cache['0000'] = {'data': data_out, 'ts': _time.time()}
                return ticker, _build_twii_hot_summary(data_out)
        except Exception as _e:
            print(f"  [hot_summary/0000] ^TWII 查詢失敗: {_e}")
            # 有舊快取就降級使用
            stale = analysis_cache.get('0000')
            if stale and stale.get('data'):
                return ticker, _build_twii_hot_summary(stale['data'])
        return ticker, None

    # ── L0：記憶體快取 ─────────────────────────────────────────────
    cache_entry = analysis_cache.get(ticker)
    if cache_entry and (_time.time() - cache_entry.get('ts', 0)) < 300:
        raw = cache_entry.get('data')
        if raw:
            return ticker, _build_hot_summary(ticker, raw)

    # ── L1/L2：cache_mgr 三層快取（fetcher=None 不觸發網路）─────────
    try:
        ohlcv_rows = cache_mgr.get_price(ticker, fetcher=None)
        div_rows   = cache_mgr.get_dividend(ticker, fetcher=None)
        info       = cache_mgr.get_fundamental(ticker, fetcher=None)

        if ohlcv_rows and len(ohlcv_rows) >= 20:
            def _norm_ohlcv(rows):
                result = []
                prev_close = None
                for r in rows:
                    date_val  = r.get('date') or r.get('日期') or ''
                    close_val = r.get('close') or r.get('Close') or r.get('收盤價')
                    open_val  = r.get('open')  or r.get('Open')  or r.get('開盤價')
                    high_val  = r.get('high')  or r.get('High')  or r.get('最高價')
                    low_val   = r.get('low')   or r.get('Low')   or r.get('最低價')
                    vol_val   = r.get('volume') or r.get('Volume') or r.get('成交量') or 0
                    if not close_val:
                        continue
                    try:
                        close_f = float(close_val)
                        # ★ 修正：open 缺值時優先用前根 close（保留漲跌方向），再 fallback 當根 close
                        open_f  = float(open_val) if open_val else (prev_close if prev_close else close_f)
                        high_f  = float(high_val) if high_val else close_f
                        low_f   = float(low_val)  if low_val  else close_f
                        # ★ 修正：volume 閾值從 10000 改為 100000（快取已是「張」通常 < 10萬，原始股數才 > 10萬）
                        try:
                            raw_vol = float(vol_val) if vol_val else 0.0
                        except (ValueError, TypeError):
                            raw_vol = 0.0
                        lot_vol = round(raw_vol / 1000) if raw_vol >= 100000 else int(raw_vol)
                        result.append({
                                'date':   str(date_val)[:10],
                                'open':   open_f,
                                'high':   max(high_f, open_f, close_f),
                                'low':    min(low_f,  open_f, close_f),
                                'close':  close_f,
                                'volume': lot_vol,
                            })
                        prev_close = close_f
                    except (ValueError, TypeError):
                        continue
                return result

            ohlcv = _norm_ohlcv(ohlcv_rows)
            if len(ohlcv) >= 20:
                indicators = calc_technical_indicators(ohlcv)
                if isinstance(POPULAR_STOCKS, dict):
                    stock_name = POPULAR_STOCKS.get(ticker, ticker)
                elif isinstance(POPULAR_STOCKS, list):
                    stock_name = next(
                        (s.get('name', ticker) for s in POPULAR_STOCKS
                         if s.get('code') == ticker or s.get('ticker') == ticker),
                        ticker
                    )
                else:
                    stock_name = ticker

                raw = {
                    'ohlcv':         ohlcv,
                    'indicators':    indicators,
                    'dividend_data': div_rows or [],
                    'info':          info or {},
                    'name':          stock_name,
                }
                analysis_cache[ticker] = {'data': raw, 'ts': _time.time()}
                print(f"  [hot_summary/{ticker}] ✅ 快取命中（本機/GitHub），免 yfinance")
                return ticker, _build_hot_summary(ticker, raw)
    except Exception as e:
        print(f"  [hot_summary/{ticker}] 快取組裝失敗，降級至 yfinance: {e}")

    # ── L3：yfinance 網路抓取 ──────────────────────────────────────
    if fetcher is None:
        return ticker, None
    try:
        raw = None
        for _attempt in range(2):
            try:
                raw = fetcher.fetch_stock_analysis(ticker)
                if raw and raw.get('ohlcv'):
                    break
            except Exception:
                pass
        if raw and raw.get('ohlcv'):
            analysis_cache[ticker] = {'data': raw, 'ts': _time.time()}
            # 存回本機 + GitHub，下次可直接命中快取
            try:
                from github_cache import (local_save_price, local_save_dividend,
                                          local_save_fundamental, gh_save_price,
                                          gh_save_dividend, gh_save_fundamental)
                if raw.get('ohlcv'):
                    local_save_price(DATA_DIR, ticker, raw['ohlcv'])
                    gh_save_price(ticker, raw['ohlcv'])
                if raw.get('dividend_data'):
                    local_save_dividend(DATA_DIR, ticker, raw['dividend_data'])
                    gh_save_dividend(ticker, raw['dividend_data'])
                if raw.get('info'):
                    local_save_fundamental(DATA_DIR, ticker, raw['info'])
                    gh_save_fundamental(ticker, raw['info'])
            except Exception as e_save:
                print(f"  [hot_summary/{ticker}] 快取存檔失敗（不影響回應）: {e_save}")
            print(f"  [hot_summary/{ticker}] 🌐 yfinance 成功，已存快取")
            return ticker, _build_hot_summary(ticker, raw)
        return ticker, None
    except Exception as e:
        print(f"  [hot_summary/{ticker}] 失敗: {e}")
        return ticker, None


@app.route('/api/hot_summary', methods=['POST'])
def hot_summary():
    """
    批次取得多支股票摘要（熱門股票頁 / 自選股一次請求）
    輸入: { tickers: ['2330', '2317', ...] }
    輸出: { status: 'success', data: { '2330': {...}, '2317': {...} } }

    ★ 修復版 v3：
    - 0000（大盤）單獨先處理，不佔用一般股票 thread
    - 批次上限 8 支，每批 3 支並行，batch timeout 30s
    - 每批 timeout 30s，單支 timeout 8s
    - 任一批失敗不影響已完成的批次結果
    """
    try:
        body    = request.get_json(force=True) or {}
        tickers = [str(t).strip().upper() for t in body.get('tickers', []) if str(t).strip()]
        if not tickers:
            return jsonify({'status': 'error', 'message': '請提供 tickers 清單'}), 400

        MAX_BATCH  = 8
        CHUNK_SIZE = 3   # 每批 3 支（降低單批壓力），timeout 更寬裕
        tickers = tickers[:MAX_BATCH]

        result  = {}

        fetcher = ETFDataFetcher(output_dir=DATA_DIR)

        # ★ 0000（大盤指數）單獨先處理，避免拖慢一般股票批次
        if '0000' in tickers:
            try:
                tk, summary = _fetch_one_hot('0000', fetcher)
                result['0000'] = summary
            except Exception as _e0:
                print(f"  [hot_summary/0000] 單獨處理失敗: {_e0}")
                result['0000'] = None
            tickers = [t for t in tickers if t != '0000']

        # 分批執行：每批 3 支，timeout 30s（各支並行）
        chunks = [tickers[i:i+CHUNK_SIZE] for i in range(0, len(tickers), CHUNK_SIZE)]
        for chunk in chunks:
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = {pool.submit(_fetch_one_hot, tk, fetcher): tk for tk in chunk}
                try:
                    for fut in as_completed(futures, timeout=30):
                        try:
                            tk, summary = fut.result(timeout=8)
                            result[tk] = summary
                        except Exception as e:
                            tk = futures[fut]
                            print(f"  [hot_summary] {tk} future 例外: {e}")
                            result[tk] = None
                except Exception as e:
                    # 本批次超時：取已完成結果，其餘補 None
                    print(f"  [hot_summary] 批次超時 ({chunk}): {e}")
                    for fut, tk in futures.items():
                        if tk not in result:
                            if fut.done():
                                try:
                                    _, summary = fut.result(timeout=2)
                                    result[tk] = summary
                                except Exception:
                                    result[tk] = None
                            else:
                                fut.cancel()  # 取消尚未完成的 future
                                result[tk] = None

        # 補齊任何未處理的 ticker
        for tk in tickers:
            if tk not in result:
                result[tk] = None

        return jsonify({'status': 'success', 'data': result})

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/stock_analysis/<ticker>', methods=['GET'])
def get_stock_analysis(ticker):
    """
    取得單支股票完整分析資料
    回傳：OHLCV、技術指標、基本面、籌碼估算、投資建議
    特殊：ticker=0000 → 台灣加權指數 ^TWII
    """
    ticker = ticker.strip().upper()

    # ── 特殊處理：0000 = 台灣加權指數 ^TWII ────────────────────
    if ticker == '0000':
        return _get_twii_analysis()

    # 快取（5分鐘）
    # 注意：只有 data_out 格式（含 chart.volumes）才可直接回傳；
    # _fetch_one_hot 存入的是 raw 格式（無 chart），需繼續往下組裝。
    import time
    cache_entry = analysis_cache.get(ticker)
    if cache_entry and (time.time() - cache_entry.get('ts', 0)) < 300:
        cached_data = cache_entry['data']
        # data_out 格式必定含 'chart' 鍵且 volumes 非空陣列
        if isinstance(cached_data, dict) and cached_data.get('chart', {}).get('volumes'):
            return jsonify({'status': 'success', 'data': cached_data})
        # raw 格式（無 chart.volumes）→ 繼續往下重新組裝完整 data_out

    # ── ★ L1/L2：cache_mgr 三層快取（本機 + GitHub，不觸發 yfinance）──────────
    # 與 _fetch_one_hot 相同邏輯，避免熱門股票頁每支都走 yfinance 造成 timeout
    raw = None
    last_exc = None
    try:
        ohlcv_rows = cache_mgr.get_price(ticker, fetcher=None)
        div_rows   = cache_mgr.get_dividend(ticker, fetcher=None)
        info_cache = cache_mgr.get_fundamental(ticker, fetcher=None)

        if ohlcv_rows and len(ohlcv_rows) >= 20:
            def _norm_cache_ohlcv(rows):
                result = []
                prev_close = None
                for r in rows:
                    date_val  = r.get('date') or r.get('日期') or ''
                    close_val = r.get('close') or r.get('Close') or r.get('收盤價')
                    open_val  = r.get('open')  or r.get('Open')  or r.get('開盤價')
                    high_val  = r.get('high')  or r.get('High')  or r.get('最高價')
                    low_val   = r.get('low')   or r.get('Low')   or r.get('最低價')
                    vol_val   = r.get('volume') or r.get('Volume') or r.get('成交量') or 0
                    if not close_val:
                        continue
                    try:
                        close_f = float(close_val)
                        # ★ 修正：open 缺值時優先用前根 close（保留漲跌方向），再 fallback 當根 close
                        open_f  = float(open_val) if open_val else (prev_close if prev_close else close_f)
                        high_f  = float(high_val) if high_val else close_f
                        low_f   = float(low_val)  if low_val  else close_f
                        # ★ 修正：volume 閾值從 10000 改為 100000（快取已是「張」通常 < 10萬，原始股數才 > 10萬）
                        try:
                            raw_vol = float(vol_val) if vol_val else 0.0
                        except (ValueError, TypeError):
                            raw_vol = 0.0
                        lot_vol = round(raw_vol / 1000) if raw_vol >= 100000 else int(raw_vol)
                        result.append({
                            'date':   str(date_val)[:10],
                            'open':   open_f,
                            'high':   max(high_f, open_f, close_f),
                            'low':    min(low_f,  open_f, close_f),
                            'close':  close_f,
                            'volume': lot_vol,
                        })
                        prev_close = close_f
                    except (ValueError, TypeError):
                        continue
                return result

            ohlcv_cached = _norm_cache_ohlcv(ohlcv_rows)
            if len(ohlcv_cached) >= 20:
                print(f"  [分析模式] 取得 {ticker} 完整資料...")
                print(f"  [L2 GitHub] {ticker} 命中快取 ({len(ohlcv_rows)} 筆)")
                _info_ok = bool(info_cache and any(
                    info_cache.get(k) for k in ('pe_ratio', 'pb_ratio', 'roe', 'eps', 'profit_margin')
                ))
                raw = {
                    'ohlcv':            ohlcv_cached,
                    'indicators':       calc_technical_indicators(ohlcv_cached),
                    'dividend_data':    div_rows or [],
                    'info':             info_cache or {},
                    'name':             ticker,
                    'source':           'local/github',
                    '_info_incomplete': not _info_ok,
                }
    except Exception as _e_cache:
        print(f"  [{ticker}] L1/L2 快取讀取失敗，降級至 yfinance: {_e_cache}")
        raw = None

    # ── L3：yfinance 補充（ohlcv 未命中，或 info 不完整時僅補基本面）──────────
    _only_info_supplement = bool(raw and raw.get('ohlcv') and raw.get('_info_incomplete'))
    if not raw or not raw.get('ohlcv') or _only_info_supplement:
        try:
            fetcher = ETFDataFetcher(output_dir=DATA_DIR)
            MAX_RETRY = 3
            _yf_raw = None
            for attempt in range(1, MAX_RETRY + 1):
                try:
                    _yf_raw = fetcher.fetch_stock_analysis(ticker)
                    if _yf_raw and _yf_raw.get('ohlcv'):
                        break
                    print(f"  [{ticker}] 第{attempt}次取得資料為空，{'重試...' if attempt < MAX_RETRY else '放棄'}")
                except Exception as e_retry:
                    last_exc = e_retry
                    print(f"  [{ticker}] 第{attempt}次例外: {e_retry}，{'重試...' if attempt < MAX_RETRY else '放棄'}")
                if attempt < MAX_RETRY:
                    time.sleep(0.5 * attempt)

            if _yf_raw and _yf_raw.get('ohlcv'):
                if _only_info_supplement:
                    # 只補 info，保留 ohlcv 快取（不重新下載價格）
                    if _yf_raw.get('info'):
                        raw['info'] = _yf_raw['info']
                    if _yf_raw.get('dividend_data'):
                        raw['dividend_data'] = _yf_raw['dividend_data']
                    raw.pop('_info_incomplete', None)
                    print(f"  [{ticker}] 基本面補充完成（保留 ohlcv 快取）")
                else:
                    raw = _yf_raw

                # 存入本機 + GitHub，下次 L1/L2 可命中
                try:
                    from github_cache import (local_save_price, local_save_dividend,
                                              local_save_fundamental, gh_save_price,
                                              gh_save_dividend, gh_save_fundamental)
                    if not _only_info_supplement:
                        local_save_price(DATA_DIR, ticker, raw['ohlcv'])
                        gh_save_price(ticker, raw['ohlcv'])
                    if raw.get('dividend_data'):
                        local_save_dividend(DATA_DIR, ticker, raw['dividend_data'])
                        gh_save_dividend(ticker, raw['dividend_data'])
                    if raw.get('info'):
                        local_save_fundamental(DATA_DIR, ticker, raw['info'])
                        gh_save_fundamental(ticker, raw['info'])
                    print(f"  [{ticker}] yfinance 成功，已存入本機/GitHub 快取")
                except Exception as e_save:
                    print(f"  [{ticker}] 快取存檔失敗（不影響回應）: {e_save}")
        except Exception as _e_yf:
            last_exc = _e_yf

    try:
        if not raw or not raw.get('ohlcv'):
            err_msg = str(last_exc) if last_exc else f'無法取得 {ticker} 資料，請確認股票代碼正確（台灣上市如：2330、00878）'
            return jsonify({
                'status':  'error',
                'message': err_msg
            }), 404

        ohlcv      = raw['ohlcv']
        indicators = raw.get('indicators', {})
        info       = raw.get('info', {})
        divs       = raw.get('dividend_data', [])

        # ── 若 yfinance 缺少基本面資料，從 TWSE/MOPS 補充 ──────
        # 注意：從 GitHub 快取讀回的欄位可能是 0 或 ''（非 None），
        # 因此用 in (None, '', 0) 判斷「有意義的值是否存在」。
        # 若來自本機/GitHub 快取且基本面欄位大多已有值，不必補充。
        def _has_val(v):
            return v not in (None, '', 0)

        needs_supplement = (
            not _has_val(info.get('pe_ratio'))      or
            not _has_val(info.get('pb_ratio'))      or
            not _has_val(info.get('eps'))           or
            not _has_val(info.get('roe'))           or
            not _has_val(info.get('profit_margin')) or
            not info.get('description')
        )
        # ★ 若資料來自本機/GitHub 快取且至少有 pe/pb/eps 其中兩項，跳過高延遲的 TWSE 補充
        _cache_source = raw.get('source', '')
        if _cache_source == 'local/github':
            _filled = sum(1 for k in ('pe_ratio', 'pb_ratio', 'eps', 'roe')
                          if _has_val(info.get(k)))
            if _filled >= 2:
                needs_supplement = False
        # ★ 若資料來自本機/GitHub 快取且至少有 pe/pb/eps 其中兩項，跳過高延遲的 TWSE 補充
        _cache_source = raw.get('source', '')
        if _cache_source == 'local/github':
            _filled = sum(1 for k in ('pe_ratio', 'pb_ratio', 'eps', 'roe')
                          if _has_val(info.get(k)))
            if _filled >= 2:
                needs_supplement = False

        if needs_supplement:
            try:
                twse_extra = _fetch_twse_fundamentals(ticker)
                for key in ('pe_ratio', 'pb_ratio', 'eps', 'roe', 'profit_margin', 'description'):
                    if twse_extra.get(key) is not None and info.get(key) in (None, '', 0):
                        info[key] = twse_extra[key]
                # div_yield_pct 是百分比，轉為小數存入 div_yield（僅當 yfinance 未提供時備用）
                if twse_extra.get('div_yield_pct') is not None and info.get('div_yield') in (None, 0):
                    info['_twse_div_yield_pct'] = twse_extra['div_yield_pct']
                print(f"  TWSE/MOPS 補充完成，補充欄位: {list(twse_extra.keys())}")
            except Exception as e2:
                print(f"  TWSE/MOPS 補充失敗（非致命）: {e2}")

        # ── 最新資料 ────────────────────────────────────────────
        last = ohlcv[-1]
        prev = ohlcv[-2] if len(ohlcv) >= 2 else last
        change     = round(last['close'] - prev['close'], 2)
        change_pct = round(change / prev['close'] * 100, 2) if prev['close'] else 0

        # ── 殖利率計算（近12個月配息合計）────────────────────
        annual_div = 0
        if divs:
            one_yr_ago = (pd.Timestamp.now() - pd.DateOffset(years=1)).strftime('%Y-%m-%d')
            annual_div = sum(float(d.get('dividend', 0) or 0) for d in divs if d.get('date', '') >= one_yr_ago)
        div_yield = round(annual_div / last['close'] * 100, 2) if last['close'] and annual_div else None

        # 若 yfinance 配息資料為空，改用 TWSE 殖利率備援值
        if div_yield is None and info.get('_twse_div_yield_pct'):
            div_yield = round(float(info['_twse_div_yield_pct']), 2)
        # 若 yfinance dividendYield 有值也可作備援
        if div_yield is None and info.get('dividend_yield'):
            div_yield = round(float(info['dividend_yield']) * 100, 2)

        # ── 技術指標最新值 ─────────────────────────────────────
        def last_val(lst):
            if not lst:
                return None
            return next((v for v in reversed(lst) if v is not None), None)

        latest_ind = {
            'ma5':         last_val(indicators.get('ma5')),
            'ma10':        last_val(indicators.get('ma10')),
            'ma20':        last_val(indicators.get('ma20')),
            'ma60':        last_val(indicators.get('ma60')),
            'ma120':       last_val(indicators.get('ma120')),
            'ma200':       last_val(indicators.get('ma200')),
            'macd':        last_val(indicators.get('macd')),
            'macd_signal': last_val(indicators.get('macd_signal')),
            'macd_hist':   last_val(indicators.get('macd_hist')),
            'rsi':         last_val(indicators.get('rsi')),
            'k':           last_val(indicators.get('k')),
            'd':           last_val(indicators.get('d')),
        }

        # ── 支撐/壓力（近60日最高/最低）──────────────────────
        recent60 = ohlcv[-60:] if len(ohlcv) >= 60 else ohlcv
        support  = round(min(r['low']  for r in recent60), 2)
        resist   = round(max(r['high'] for r in recent60), 2)

        # ── 趨勢判斷 ─────────────────────────────────────────
        # 均線序列 dict（供葛蘭碧/交叉預測使用）
        ma_series_dict = {
            'ma20': indicators.get('ma20', []),
            'ma60': indicators.get('ma60', []),
        }
        trend = _calc_trend(last['close'], latest_ind, ohlcv=ohlcv, ma_series_dict=ma_series_dict)

        # ── 法人籌碼估算（根據成交量及趨勢模擬）──────────────
        chip = _estimate_chip(ohlcv, trend)

        # ── 投資建議與評級 ─────────────────────────────────────
        recommendation = _generate_recommendation(
            ticker, last['close'], latest_ind, trend, chip,
            info, div_yield, support, resist
        )

        # ── 圖表資料（近5年 OHLCV，指標來自全量歷史確保 EMA 收斂）──
        # yfinance 回傳的 volume 單位是「股」，台灣習慣用「張」(1張=1000股)
        CHART_DAYS  = 1260          # 約5年交易日（252×5）
        chart_ohlcv = ohlcv[-CHART_DAYS:] if len(ohlcv) > CHART_DAYS else ohlcv
        chart_len   = len(chart_ohlcv)
        offset      = len(ohlcv) - chart_len   # 對齊指標陣列（指標與全量 ohlcv 等長）

        # 判斷 ohlcv 的 volume 單位：
        # - local/github 快取路徑：_norm_cache_ohlcv 已換算為張，直接用整數
        # - yfinance 路徑：data_fetcher 回傳股數，需 ÷1000
        _src_is_cached = raw.get('source', '') == 'local/github'

        def to_lots(v):
            """stock → lot（÷1000 only for yfinance raw data）"""
            if not v:
                return 0
            if _src_is_cached:
                return int(v)   # 快取已換算為張，直接回傳
            # ★ 修正：yfinance 原始股數通常 >= 100000，改用此閾值避免誤算
            return round(float(v) / 1000) if float(v) >= 100000 else int(float(v))

        def slice_ind(key):
            lst = indicators.get(key, [])
            return lst[offset:offset + chart_len] if len(lst) >= offset + chart_len else lst[-chart_len:]

        data_out = {
            'ticker':    ticker,
            'name':      raw.get('name', ticker),
            'source':    raw.get('source', 'yfinance'),
            'is_simulated': raw.get('is_simulated', False),
            # 最新行情
            'latest': {
                'date':       last['date'],
                'open':       last['open'],
                'high':       last['high'],
                'low':        last['low'],
                'close':      last['close'],
                'volume':     to_lots(last['volume']),
                'change':     change,
                'change_pct': change_pct,
            },
            # 基本面
            'fundamentals': {
                'pe_ratio':      info.get('pe_ratio'),
                'pb_ratio':      info.get('pb_ratio'),
                'div_yield':     div_yield,
                'annual_div':    round(annual_div, 4),
                'eps':           info.get('eps'),
                'roe':           info.get('roe'),
                'profit_margin': info.get('profit_margin'),
                'market_cap':    info.get('market_cap'),
                'sector':        info.get('sector', ''),
                'industry':      info.get('industry', ''),
                '52w_high':      info.get('52w_high'),
                '52w_low':       info.get('52w_low'),
                'description':   info.get('description', ''),
            },
            # 技術分析
            'technical': {
                'latest':   latest_ind,
                'support':  support,
                'resist':   resist,
                'trend':    trend,
                # 強化版均線分析（均線排列、乖離率、葛蘭碧、交叉預測）
                'ma_analysis': {
                    'array':        trend.get('ma_array', {}),
                    'bias_ma20':    trend.get('bias_ma20'),
                    'bias_warn':    trend.get('bias_warn_ma20', {}),
                    'granville':    trend.get('granville', []),
                    'cross_5_20':   trend.get('cross_5_20', {}),
                    'cross_20_60':  trend.get('cross_20_60', {}),
                },
            },
            # 籌碼面
            'chip': chip,
            # 投資建議
            'recommendation': recommendation,
            # 配息歷史
            'dividends': divs[-20:] if divs else [],
            # 圖表資料
            'chart': {
                'dates':        [r['date']   for r in chart_ohlcv],
                # ★ 修正：open 缺值時用前根 close 作為 fallback，確保日線 K 棒正常顯示紅/綠色
                'opens':        [chart_ohlcv[i].get('open') if chart_ohlcv[i].get('open')
                                 else (chart_ohlcv[i-1]['close'] if i > 0 else chart_ohlcv[i]['close'])
                                 for i in range(len(chart_ohlcv))],
                'highs':        [r['high']   for r in chart_ohlcv],
                'lows':         [r['low']    for r in chart_ohlcv],
                'closes':       [r['close']  for r in chart_ohlcv],
                'volumes':      [to_lots(r.get('volume', 0)) for r in chart_ohlcv],
                'ma5':          slice_ind('ma5'),
                'ma10':         slice_ind('ma10'),
                'ma20':         slice_ind('ma20'),
                'ma60':         slice_ind('ma60'),
                'ma120':        slice_ind('ma120'),
                'ma200':        slice_ind('ma200'),
                'macd':         slice_ind('macd'),
                'macd_signal':  slice_ind('macd_signal'),
                'macd_hist':    slice_ind('macd_hist'),
                'rsi':          slice_ind('rsi'),
                'k':            slice_ind('k'),
                'd':            slice_ind('d'),
            }
        }

        analysis_cache[ticker] = {'data': data_out, 'ts': time.time()}
        return jsonify({'status': 'success', 'data': data_out})

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


def _fetch_twii_data_raw() -> dict | None:
    """
    從 yfinance 抓取台灣加權指數（^TWII）並回傳純 dict（不 jsonify）。
    供 _fetch_one_hot 在 ThreadPoolExecutor 內背景呼叫，
    避免 0000 混入一般批次時走到無效的 0000.TW / 0000.TWO 查詢。
    逾時設 20s，最多重試 2 次，快取命中直接回傳。
    """
    import time as _t
    import yfinance as yf

    # 先查快取（10 分鐘）
    cache_entry = analysis_cache.get('0000')
    if cache_entry and (_t.time() - cache_entry.get('ts', 0)) < 600:
        return cache_entry.get('data')

    hist = None
    for _attempt in range(1, 3):
        try:
            tk = yf.Ticker('^TWII')
            hist = tk.history(period='5y', timeout=20)
            if hist is not None and not hist.empty:
                break
        except Exception as _e:
            print(f"  [TWII_raw] 第{_attempt}次失敗: {_e}")
        if _attempt < 2:
            _t.sleep(1.5)

    if hist is None or hist.empty:
        return None

    hist = hist.sort_index()
    ohlcv = []
    for dt, row in hist.iterrows():
        ohlcv.append({
            'date':   str(dt.date()),
            'open':   round(float(row['Open']),  2),
            'high':   round(float(row['High']),  2),
            'low':    round(float(row['Low']),   2),
            'close':  round(float(row['Close']), 2),
            'volume': int(row.get('Volume', 0) or 0),
        })

    if not ohlcv:
        return None

    from data_fetcher import calc_technical_indicators
    indicators = calc_technical_indicators(ohlcv)

    last = ohlcv[-1]
    prev = ohlcv[-2] if len(ohlcv) >= 2 else last
    change     = round(last['close'] - prev['close'], 2)
    change_pct = round(change / prev['close'] * 100, 2) if prev['close'] else 0

    def last_val(lst):
        if not lst: return None
        return next((v for v in reversed(lst) if v is not None), None)

    latest_ind = {k: last_val(indicators.get(k)) for k in
                  ('ma5','ma10','ma20','ma60','ma120','ma200',
                   'macd','macd_signal','macd_hist','rsi','k','d')}

    CHART_DAYS  = 1260
    chart_ohlcv = ohlcv[-CHART_DAYS:] if len(ohlcv) > CHART_DAYS else ohlcv
    chart_len   = len(chart_ohlcv)
    offset      = len(ohlcv) - chart_len

    def slice_ind(key):
        lst = indicators.get(key, [])
        return lst[offset:offset + chart_len] if len(lst) >= offset + chart_len else lst[-chart_len:]

    recent60 = ohlcv[-60:] if len(ohlcv) >= 60 else ohlcv
    support  = round(min(r['low']  for r in recent60), 2)
    resist   = round(max(r['high'] for r in recent60), 2)
    trend    = _calc_trend(last['close'], latest_ind)

    data_out = {
        'ticker':       '0000',
        'name':         '台灣加權指數',
        'source':       'yfinance(^TWII)',
        'is_simulated': False,
        'is_index':     True,
        'latest': {
            'date':       last['date'],
            'open':       last['open'],
            'high':       last['high'],
            'low':        last['low'],
            'close':      last['close'],
            'volume':     last['volume'],
            'change':     change,
            'change_pct': change_pct,
        },
        'fundamentals': {
            'pe_ratio': None, 'pb_ratio': None, 'div_yield': None,
            'annual_div': 0, 'eps': None, 'roe': None,
            'profit_margin': None, 'market_cap': None,
            'sector': '指數', 'industry': '台灣加權股價指數',
            '52w_high': max(r['high'] for r in ohlcv[-252:]) if len(ohlcv) >= 252 else None,
            '52w_low':  min(r['low']  for r in ohlcv[-252:]) if len(ohlcv) >= 252 else None,
            'description': '台灣加權股價指數（TAIEX）追蹤台灣證券交易所全體上市股票之加權市值，是衡量台股整體表現的主要基準指標。',
        },
        'technical': {
            'latest':  latest_ind,
            'support': support,
            'resist':  resist,
            'trend':   trend,
        },
        'chip': {'note': '指數無籌碼資料', 'estimated': True},
        'recommendation': {
            'rating':        trend['label'],
            'rating_color':  trend['color'],
            'rating_bg':     '#1e293b',
            'rating_icon':   '',
            'total_score':   trend['score'],
            'tech_score':    trend['score'],
            'fund_score':    0,
            'reasons_buy':   trend['signals'],
            'reasons_sell':  [],
            'risks':         [],
            'target_price':  None,
            'target_type':   'none',
            'target_desc':   '',
            'support':       support,
            'resist':        resist,
            'price_position': None,
            'summary':       f'台灣加權指數目前報 {last["close"]:,.2f} 點，技術面呈「{trend["label"]}」態勢。',
        },
        'dividends': [],
        'chart': {
            'dates':       [r['date']  for r in chart_ohlcv],
            'opens':       [r['open']  for r in chart_ohlcv],
            'highs':       [r['high']  for r in chart_ohlcv],
            'lows':        [r['low']   for r in chart_ohlcv],
            'closes':      [r['close'] for r in chart_ohlcv],
            'volumes':     [r['volume']for r in chart_ohlcv],
            'ma5':         slice_ind('ma5'),
            'ma10':        slice_ind('ma10'),
            'ma20':        slice_ind('ma20'),
            'ma60':        slice_ind('ma60'),
            'ma120':       slice_ind('ma120'),
            'ma200':       slice_ind('ma200'),
            'macd':        slice_ind('macd'),
            'macd_signal': slice_ind('macd_signal'),
            'macd_hist':   slice_ind('macd_hist'),
            'rsi':         slice_ind('rsi'),
            'k':           slice_ind('k'),
            'd':           slice_ind('d'),
        },
    }
    analysis_cache['0000'] = {'data': data_out, 'ts': _t.time()}
    return data_out


def _build_twii_hot_summary(data_out: dict) -> dict:
    """
    從 _fetch_twii_data_raw / _get_twii_analysis 回傳的 data_out dict
    組出 hot_summary 格式（與一般股票 _build_hot_summary 介面相容）。
    """
    latest = data_out.get('latest', {})
    tech   = data_out.get('technical', {})
    rec    = data_out.get('recommendation', {})
    ind    = tech.get('latest', {})
    return {
        'name':           '台灣加權指數',
        'close':          latest.get('close'),
        'change_pct':     latest.get('change_pct', 0),
        'volume':         latest.get('volume', 0),
        'high':           latest.get('high'),
        'low':            latest.get('low'),
        'ma5':            ind.get('ma5'),
        'ma20':           ind.get('ma20'),
        'ma60':           ind.get('ma60'),
        'rsi':            ind.get('rsi'),
        'trend':          tech.get('trend', {}).get('label', ''),
        'recommendation': rec.get('rating', ''),
        'score':          rec.get('total_score', 0),
        'is_index':       True,
    }


def _get_twii_analysis():
    """
    取得台灣加權指數（^TWII）分析資料，供 /api/stock_analysis/0000 路由呼叫。
    以 ticker='0000' 作為識別，對外格式與一般股票相同。

    修正 v3：邏輯統一委派給 _fetch_twii_data_raw()，
    消除重複 yfinance 程式碼，確保快取共用。
    """
    import time
    try:
        data_out = _fetch_twii_data_raw()
        if data_out:
            return jsonify({'status': 'success', 'data': data_out})
        # 無資料時嘗試降級舊快取
        stale = analysis_cache.get('0000')
        if stale and stale.get('data'):
            print("  [TWII] yfinance 失敗，回傳舊快取資料")
            return jsonify({'status': 'success', 'data': stale['data'],
                            'from_stale_cache': True})
        return jsonify({'status': 'error', 'message': '無法取得台灣加權指數資料'}), 404
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


def _fetch_twse_fundamentals(ticker: str) -> dict:
    """
    從多個公開資料源補充基本面資料（優先順序：yfinance info → TWSE → TPEx → MOPS → 備援）
    支援欄位：pe_ratio, pb_ratio, eps, roe, profit_margin, description, div_yield_pct
    任何子請求失敗皆 silently skip，回傳已取得的欄位。
    """
    import re as _re
    import html as _html

    result = {}
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/124.0.0.0 Safari/537.36',
        'Accept-Language': 'zh-TW,zh;q=0.9',
        'Referer': 'https://www.twse.com.tw/',
    }

    def safe_float(s):
        """安全轉換字串為浮點數，失敗回傳 None"""
        if s is None:
            return None
        try:
            v = float(str(s).replace(',', '').strip())
            return v if v != 0 else None
        except Exception:
            return None

    # ══════════════════════════════════════════════════════════
    # 1. TWSE 每日本益比/殖利率/淨值比（上市主板）
    # ══════════════════════════════════════════════════════════
    try:
        # 嘗試當天；若無資料（例如休市）往前找最近 5 個交易日
        for day_back in range(0, 6):
            ts = pd.Timestamp.now() - pd.DateOffset(days=day_back)
            if ts.weekday() >= 5:       # 跳過週末
                continue
            date_str = ts.strftime('%Y%m%d')
            url = (f'https://www.twse.com.tw/exchangeReport/BWIBBU_d'
                   f'?response=json&date={date_str}&stockNo={ticker}')
            r = _req.get(url, headers=headers, timeout=10, verify=False)
            if not r.ok:
                continue
            jd = r.json()
            rows = jd.get('data', [])
            for row in rows:
                if len(row) >= 7 and str(row[1]).strip() == str(ticker):
                    # 欄位: 0=日期,1=代號,2=名稱,3=殖利率(%),4=股利年度,5=本益比,6=淨值比
                    pe = safe_float(row[5]) if row[5] not in ('-', '') else None
                    pb = safe_float(row[6]) if row[6] not in ('-', '') else None
                    dy = safe_float(row[3]) if row[3] not in ('-', '') else None
                    if pe: result['pe_ratio']    = pe
                    if pb: result['pb_ratio']    = pb
                    if dy: result['div_yield_pct'] = dy
                    break
            if 'pe_ratio' in result or rows:
                break   # 找到資料或有回應就停止
    except Exception as e:
        print(f'  TWSE BWIBBU_d 查詢失敗（非致命）: {e}')

    # ══════════════════════════════════════════════════════════
    # 2. TPEx 上櫃市場本益比/淨值比備援
    # ══════════════════════════════════════════════════════════
    if 'pe_ratio' not in result:
        try:
            date_fmt = pd.Timestamp.now().strftime('%Y/%m/%d')
            url2 = (f'https://www.tpex.org.tw/web/stock/aftertrading/peratio_result/'
                    f'pera_result.php?l=zh-tw&o=json&d={date_fmt}&s=0,asc&stkno={ticker}')
            r2 = _req.get(url2, headers=headers, timeout=10, verify=False)
            if r2.ok:
                jd2 = r2.json()
                for row in jd2.get('aaData', []):
                    if str(row[0]).strip() == str(ticker):
                        # 欄位: 0=代號,1=名稱,2=收盤,3=EPS,4=本益比,5=殖利率,6=淨值比
                        pe = safe_float(row[4]) if len(row) > 4 else None
                        dy = safe_float(row[5]) if len(row) > 5 else None
                        pb = safe_float(row[6]) if len(row) > 6 else None
                        if pe: result['pe_ratio']      = pe
                        if dy: result['div_yield_pct'] = dy
                        if pb: result['pb_ratio']      = pb
                        break
        except Exception as e:
            print(f'  TPEx 查詢失敗（非致命）: {e}')

    # ══════════════════════════════════════════════════════════
    # 3. MOPS 財務摘要：EPS / ROE / 淨利率（往前追溯最近 5 季）
    # ══════════════════════════════════════════════════════════
    if not all(k in result for k in ('eps', 'roe', 'profit_margin')):
        try:
            now = pd.Timestamp.now()
            for offset in range(0, 6):
                if all(k in result for k in ('eps', 'roe', 'profit_margin')):
                    break
                qtr_ts   = now - pd.DateOffset(months=3 * offset)
                year_roc = qtr_ts.year - 1911
                qtr_num  = (qtr_ts.month - 1) // 3 + 1
                post_data = {
                    'encodeURIComponent': '1', 'step': '1', 'firstin': '1', 'off': '1',
                    'queryName': 'co_id', 'inpuType': 'co_id', 'TYPEK': 'all',
                    'isnew': 'false', 'co_id': ticker,
                    'year': str(year_roc), 'season': str(qtr_num).zfill(2),
                }
                try:
                    r = _req.post('https://mops.twse.com.tw/mops/web/ajax_t05st22',
                                  data=post_data, headers=headers, timeout=15)
                    if not r.ok:
                        continue
                    r.encoding = 'utf-8'
                    text = r.text

                    # 更寬鬆的 regex：允許 td 標籤有任意 class/style
                    # Pattern: 欄位名稱 → 下一個 td 的數值
                    def extract_kpi(pattern_name):
                        m = _re.search(
                            pattern_name + r'[^<]{0,30}</td>\s*<td[^>]*>\s*(-?[\d,\.]+)',
                            text, _re.IGNORECASE | _re.DOTALL
                        )
                        return safe_float(m.group(1)) if m else None

                    eps_val = extract_kpi(r'基本每股盈餘')
                    roe_val = extract_kpi(r'股東權益報酬率')
                    npm_val = extract_kpi(r'稅後淨利率')

                    # 備用 pattern（部分年份欄位名稱略有不同）
                    if eps_val is None:
                        eps_val = extract_kpi(r'每股盈餘')
                    if roe_val is None:
                        roe_val = extract_kpi(r'權益報酬率')
                    if npm_val is None:
                        npm_val = extract_kpi(r'淨利率')

                    got_any = False
                    if eps_val is not None and 'eps' not in result:
                        result['eps'] = eps_val;  got_any = True
                    if roe_val is not None and 'roe' not in result:
                        result['roe'] = round(roe_val / 100, 6);  got_any = True   # % → 小數
                    if npm_val is not None and 'profit_margin' not in result:
                        result['profit_margin'] = round(npm_val / 100, 6);  got_any = True

                    if got_any:
                        print(f'  MOPS 財務摘要 {year_roc}Q{qtr_num} 取得成功')
                        break
                except Exception as inner_e:
                    print(f'  MOPS Q{offset} 查詢失敗: {inner_e}')
                    continue
        except Exception as e:
            print(f'  MOPS 財務摘要查詢失敗（非致命）: {e}')

    # ══════════════════════════════════════════════════════════
    # 4. MOPS 公司基本資料：公司簡介（主要業務）
    # ══════════════════════════════════════════════════════════
    if 'description' not in result:
        try:
            for typek in ('sii', 'otc', 'all'):   # sii=上市, otc=上櫃, all=全部
                r = _req.post(
                    'https://mops.twse.com.tw/mops/web/ajax_t05st03',
                    data={
                        'encodeURIComponent': '1', 'step': '1', 'firstin': '1',
                        'off': '1', 'co_id': ticker, 'TYPEK': typek
                    },
                    headers=headers, timeout=15
                )
                if not r.ok:
                    continue
                r.encoding = 'utf-8'
                text = r.text

                # 嘗試多種欄位名稱
                desc_raw = None
                for pattern in [
                    r'主要業務(?:及產品)?[^<]{0,30}</td>\s*<td[^>]*>([\s\S]{10,800}?)</td>',
                    r'主要產品[^<]{0,30}</td>\s*<td[^>]*>([\s\S]{10,800}?)</td>',
                    r'業務內容[^<]{0,30}</td>\s*<td[^>]*>([\s\S]{10,800}?)</td>',
                ]:
                    biz_m = _re.search(pattern, text, _re.IGNORECASE)
                    if biz_m:
                        raw = _re.sub(r'<[^>]+>', '', biz_m.group(1)).strip()
                        raw = _html.unescape(raw).strip()
                        raw = _re.sub(r'\s+', ' ', raw)
                        if len(raw) > 20:
                            desc_raw = raw[:600]
                            break

                if desc_raw:
                    result['description'] = desc_raw
                    print(f'  MOPS 公司簡介取得成功 (typek={typek})')
                    break
        except Exception as e:
            print(f'  MOPS 公司基本資料查詢失敗（非致命）: {e}')

    # ══════════════════════════════════════════════════════════
    # 5. TWSE 個股基本資料 API（備援名稱/產業）
    # ══════════════════════════════════════════════════════════
    if 'description' not in result:
        try:
            url_co = f'https://www.twse.com.tw/zh/api/basic/company?stockNo={ticker}'
            r_co = _req.get(url_co, headers=headers, timeout=8, verify=False)
            if r_co.ok:
                jco = r_co.json()
                # 嘗試從公司資訊建構簡短描述
                industry  = jco.get('industryZhTw') or jco.get('industry', '')
                comp_name = jco.get('companyNameZhTw') or jco.get('companyName', '')
                cap_str   = jco.get('capitalAmount', '')
                if comp_name and industry:
                    desc_gen = f'{comp_name}，所屬產業：{industry}。'
                    if cap_str:
                        try:
                            cap_val = int(str(cap_str).replace(',',''))
                            desc_gen += f' 實收資本額 {cap_val:,} 元。'
                        except Exception:
                            pass
                    result['description'] = desc_gen
        except Exception as e:
            print(f'  TWSE 個股基本資料查詢失敗（非致命）: {e}')

    # ══════════════════════════════════════════════════════════
    # 6. 靜態備援資料庫（當所有線上來源均失敗時使用）
    #    資料來源：公開資訊觀測站、各券商公開資料（定期人工維護）
    #    適用場景：Render/雲端 Egress Proxy 封鎖外部 API 時
    # ══════════════════════════════════════════════════════════
    STATIC_FUNDAMENTALS = {
        # 半導體
        '2330': {'pe_ratio': 22.5, 'pb_ratio': 6.8, 'eps': 45.25, 'roe': 0.32,
                 'profit_margin': 0.385, 'market_cap': 20_800_000_000_000,
                 '52w_high': 1150.0, '52w_low': 780.0,
                 'description': '台積電（台灣積體電路製造）是全球最大的專業積體電路製造服務公司，提供晶圓代工服務。主要客戶涵蓋全球主要半導體設計公司，製程技術領先業界。'},
        '2454': {'pe_ratio': 18.2, 'pb_ratio': 5.1, 'eps': 98.5, 'roe': 0.28,
                 'profit_margin': 0.32, 'market_cap': 3_200_000_000_000,
                 '52w_high': 1350.0, '52w_low': 920.0,
                 'description': '聯發科為全球前三大無晶圓廠半導體設計公司，專注於手機、智慧家電、Wi-Fi等晶片設計。'},
        '2303': {'pe_ratio': 16.8, 'pb_ratio': 2.1, 'eps': 4.2, 'roe': 0.12,
                 'profit_margin': 0.18, 'market_cap': 580_000_000_000,
                 '52w_high': 58.0, '52w_low': 36.0,
                 'description': '聯電為台灣第二大晶圓代工廠，專注於成熟製程，提供多元化的晶圓代工服務。'},
        # 電子/科技
        '2317': {'pe_ratio': 11.5, 'pb_ratio': 1.8, 'eps': 10.8, 'roe': 0.16,
                 'profit_margin': 0.04, 'market_cap': 1_480_000_000_000,
                 '52w_high': 225.0, '52w_low': 145.0,
                 'description': '鴻海精密（富士康）為全球最大電子製造服務廠商，主要從事電子產品組裝代工，客戶包括蘋果等國際大廠。'},
        '2382': {'pe_ratio': 15.3, 'pb_ratio': 4.2, 'eps': 32.5, 'roe': 0.27,
                 'profit_margin': 0.065, 'market_cap': 780_000_000_000,
                 '52w_high': 320.0, '52w_low': 195.0,
                 'description': '廣達電腦為全球最大筆記型電腦代工廠之一，近年積極布局 AI 伺服器業務。'},
        '2308': {'pe_ratio': 21.0, 'pb_ratio': 5.5, 'eps': 20.1, 'roe': 0.26,
                 'profit_margin': 0.08, 'market_cap': 720_000_000_000,
                 '52w_high': 450.0, '52w_low': 290.0,
                 'description': '台達電子為全球電源供應器龍頭，並深耕工業自動化、網路電信及能源解決方案。'},
        '2357': {'pe_ratio': 14.8, 'pb_ratio': 2.8, 'eps': 65.0, 'roe': 0.19,
                 'profit_margin': 0.065, 'market_cap': 280_000_000_000,
                 '52w_high': 560.0, '52w_low': 340.0,
                 'description': '華碩為台灣主要電腦品牌廠商，產品涵蓋筆電、主機板、顯示卡及手機等消費性電子產品。'},
        '3008': {'pe_ratio': 35.2, 'pb_ratio': 9.8, 'eps': 168.0, 'roe': 0.27,
                 'profit_margin': 0.55, 'market_cap': 600_000_000_000,
                 '52w_high': 2500.0, '52w_low': 1600.0,
                 'description': '大立光為全球手機鏡頭模組主要供應商，技術實力居業界領先地位，是蘋果等品牌的核心供應商。'},
        '2395': {'pe_ratio': 20.5, 'pb_ratio': 3.1, 'eps': 20.5, 'roe': 0.15,
                 'profit_margin': 0.12, 'market_cap': 140_000_000_000,
                 '52w_high': 440.0, '52w_low': 280.0,
                 'description': '研華科技為工業電腦全球領導品牌，專注於工業 IoT、嵌入式運算及智能系統整合解決方案。'},
        '4938': {'pe_ratio': 9.8, 'pb_ratio': 1.5, 'eps': 5.8, 'roe': 0.15,
                 'profit_margin': 0.03, 'market_cap': 230_000_000_000,
                 '52w_high': 72.0, '52w_low': 45.0,
                 'description': '和碩聯合科技為全球主要電子製造服務廠商，主要承接手機、平板及電腦等組裝代工業務。'},
        # 金融
        '2881': {'pe_ratio': 13.2, 'pb_ratio': 1.5, 'eps': 5.8, 'roe': 0.115,
                 'profit_margin': 0.22, 'market_cap': 1_050_000_000_000,
                 '52w_high': 92.0, '52w_low': 68.0,
                 'description': '富邦金控旗下包含台北富邦銀行、富邦人壽、富邦產險等子公司，為台灣規模最大的金融控股公司之一。'},
        '2882': {'pe_ratio': 14.0, 'pb_ratio': 1.6, 'eps': 5.2, 'roe': 0.11,
                 'profit_margin': 0.20, 'market_cap': 920_000_000_000,
                 '52w_high': 82.0, '52w_low': 58.0,
                 'description': '國泰金控旗下包含國泰世華銀行、國泰人壽等子公司，提供完整的金融服務。'},
        '2891': {'pe_ratio': 11.5, 'pb_ratio': 1.3, 'eps': 3.2, 'roe': 0.112,
                 'profit_margin': 0.25, 'market_cap': 530_000_000_000,
                 '52w_high': 42.0, '52w_low': 30.0,
                 'description': '中信金控旗下含中國信託商業銀行，為台灣最大民營銀行，業務涵蓋銀行、保險及證券。'},
        '2886': {'pe_ratio': 10.8, 'pb_ratio': 1.1, 'eps': 2.8, 'roe': 0.105,
                 'profit_margin': 0.28, 'market_cap': 290_000_000_000,
                 '52w_high': 38.5, '52w_low': 26.5,
                 'description': '兆豐金控旗下含兆豐國際商業銀行，業務涵蓋國內外銀行、票券及證券服務。'},
        # 傳產/原物料
        '1301': {'pe_ratio': 18.5, 'pb_ratio': 1.3, 'eps': 4.8, 'roe': 0.07,
                 'profit_margin': 0.06, 'market_cap': 520_000_000_000,
                 '52w_high': 98.0, '52w_low': 68.0,
                 'description': '台塑為台灣最大的塑膠原料製造商，生產 PVC、石化原料及塑膠加工品。'},
        '1303': {'pe_ratio': 16.2, 'pb_ratio': 1.2, 'eps': 4.2, 'roe': 0.074,
                 'profit_margin': 0.055, 'market_cap': 450_000_000_000,
                 '52w_high': 88.0, '52w_low': 60.0,
                 'description': '南亞塑膠為台灣主要塑膠加工廠，生產 PVC 管材、銅箔基板及電子材料等產品。'},
        '2002': {'pe_ratio': 14.5, 'pb_ratio': 0.85, 'eps': 1.8, 'roe': 0.058,
                 'profit_margin': 0.04, 'market_cap': 180_000_000_000,
                 '52w_high': 32.5, '52w_low': 22.0,
                 'description': '中鋼為台灣最大的鋼鐵製造商，產品涵蓋熱軋、冷軋鋼捲及各式鋼材。'},
        # 電信
        '2412': {'pe_ratio': 23.5, 'pb_ratio': 2.5, 'eps': 5.2, 'roe': 0.105,
                 'profit_margin': 0.115, 'market_cap': 640_000_000_000,
                 '52w_high': 138.0, '52w_low': 112.0,
                 'description': '中華電信為台灣最大電信業者，提供固網、行動、寬頻及雲端等全方位電信服務。'},
        # 航運
        '2603': {'pe_ratio': 8.5, 'pb_ratio': 1.2, 'eps': 12.5, 'roe': 0.14,
                 'profit_margin': 0.165, 'market_cap': 280_000_000_000,
                 '52w_high': 155.0, '52w_low': 90.0,
                 'description': '長榮海運為台灣最大貨櫃航運公司，航線遍及全球，是全球前五大貨櫃航運集團。'},
        '2615': {'pe_ratio': 7.8, 'pb_ratio': 1.0, 'eps': 8.2, 'roe': 0.125,
                 'profit_margin': 0.12, 'market_cap': 85_000_000_000,
                 '52w_high': 78.0, '52w_low': 45.0,
                 'description': '萬海航運專注於亞洲區域內的貨櫃航運服務，提供快速、密集的亞洲航線運輸。'},
        '2609': {'pe_ratio': 7.2, 'pb_ratio': 0.9, 'eps': 9.5, 'roe': 0.13,
                 'profit_margin': 0.14, 'market_cap': 110_000_000_000,
                 '52w_high': 82.0, '52w_low': 48.0,
                 'description': '陽明海運為台灣第二大貨櫃航運公司，提供全球貨櫃航運服務，近年財務大幅改善。'},
        # ETF
        '0050':  {'pe_ratio': None, 'pb_ratio': None, 'eps': None, 'roe': None,
                  'profit_margin': None, 'market_cap': None,
                  '52w_high': None, '52w_low': None,
                  'description': '元大台灣50 ETF 追蹤台灣50指數，持有台灣市值前50大企業，是最具代表性的台股指數型基金。'},
        '0056':  {'pe_ratio': None, 'pb_ratio': None, 'eps': None, 'roe': None,
                  'profit_margin': None, 'market_cap': None,
                  '52w_high': None, '52w_low': None,
                  'description': '元大高股息 ETF 追蹤台灣高股息指數，精選預測殖利率最高的30檔股票，以高配息為訴求。'},
        '00878': {'pe_ratio': None, 'pb_ratio': None, 'eps': None, 'roe': None,
                  'profit_margin': None, 'market_cap': None,
                  '52w_high': None, '52w_low': None,
                  'description': '國泰永續高股息 ETF 追蹤 MSCI 台灣 ESG 永續高股息精選30指數，兼顧 ESG 永續及高股息特性，每季配息。'},
        '00919': {'pe_ratio': None, 'pb_ratio': None, 'eps': None, 'roe': None,
                  'profit_margin': None, 'market_cap': None,
                  '52w_high': None, '52w_low': None,
                  'description': '群益台灣精選高息 ETF 篩選台灣高殖利率且財務健全的成份股，追求穩定的高股息收益，每季配息。'},
        '00929': {'pe_ratio': None, 'pb_ratio': None, 'eps': None, 'roe': None,
                  'profit_margin': None, 'market_cap': None,
                  '52w_high': None, '52w_low': None,
                  'description': '復華台灣科技優息 ETF 以台灣科技產業高殖利率股票為主要成份，兼顧科技成長與股息收益，每月配息。'},
        '006208':{'pe_ratio': None, 'pb_ratio': None, 'eps': None, 'roe': None,
                  'profit_margin': None, 'market_cap': None,
                  '52w_high': None, '52w_low': None,
                  'description': '富邦台50 ETF 追蹤台灣50指數，持有台灣市值前50大企業，費用率低廉，適合長期指數化投資。'},
        '00713': {'pe_ratio': None, 'pb_ratio': None, 'eps': None, 'roe': None,
                  'profit_margin': None, 'market_cap': None,
                  '52w_high': None, '52w_low': None,
                  'description': '元大台灣高息低波 ETF 篩選高股息且低波動的台股，降低投資組合波動風險同時獲取穩定配息。'},
    }

    # 僅補充仍缺失的欄位
    static = STATIC_FUNDAMENTALS.get(ticker, {})
    if static:
        for key in ('pe_ratio', 'pb_ratio', 'eps', 'roe', 'profit_margin',
                    'market_cap', '52w_high', '52w_low', 'description'):
            if result.get(key) is None and static.get(key) is not None:
                result[key] = static[key]
        if static:
            print(f'  靜態備援已補充 [{ticker}]，keys={list(result.keys())}')

    print(f'  _fetch_twse_fundamentals 結果: { {k: v for k, v in result.items() if k != "description"} }')
    return result


def _calc_trend(close, ind, ohlcv=None, ma_series_dict=None):
    """
    強化版趨勢判斷 — 整合均線排列、乖離率、葛蘭碧法則、交叉預測。
    直接呼叫 ma_analysis_enhanced.enhanced_calc_trend()。
    回傳格式向下相容原版，並擴充 ma_array/bias/granville/cross 欄位。
    """
    return enhanced_calc_trend(close, ind, ohlcv=ohlcv, ma_series_dict=ma_series_dict)


def _estimate_chip(ohlcv, trend):
    """
    法人籌碼估算（基於成交量分布與趨勢推估）
    注意：此為統計模型估算，非真實申報資料
    """
    if len(ohlcv) < 20:
        return {'note': '資料不足，無法估算籌碼', 'estimated': True}

    recent20 = ohlcv[-20:]
    avg_vol  = np.mean([r['volume'] for r in ohlcv[-60:]] or [1])

    # 近5日相對成交量
    recent5_vol = np.mean([r['volume'] for r in ohlcv[-5:]])
    vol_ratio   = round(recent5_vol / avg_vol, 2) if avg_vol > 0 else 1.0

    trend_score = trend.get('score', 0)

    # 法人方向估算（趨勢偏多且量增 → 法人偏買；趨勢偏空且量增 → 法人偏賣）
    if trend_score >= 2 and vol_ratio >= 1.2:
        foreign_dir = '偏向買超'
        trust_dir   = '偏向買超'
    elif trend_score <= -2 and vol_ratio >= 1.2:
        foreign_dir = '偏向賣超'
        trust_dir   = '偏向賣超'
    elif trend_score >= 1:
        foreign_dir = '小幅買超'
        trust_dir   = '中性偏多'
    elif trend_score <= -1:
        foreign_dir = '小幅賣超'
        trust_dir   = '中性偏空'
    else:
        foreign_dir = '觀望中性'
        trust_dir   = '觀望中性'

    # 近20日漲跌家數估算（量縮上漲 vs 量增下跌）
    up_days   = sum(1 for r in recent20 if r['close'] >= r['open'])
    down_days = len(recent20) - up_days
    up_vol    = sum(r['volume'] for r in recent20 if r['close'] >= r['open'])
    down_vol  = sum(r['volume'] for r in recent20 if r['close'] < r['open'])

    return {
        'foreign_dir':    foreign_dir,
        'trust_dir':      trust_dir,
        'vol_ratio':      vol_ratio,
        'avg_volume':     max(1, round(avg_vol / 1000)),        # 股→張
        'recent5_volume': max(1, round(recent5_vol / 1000)),    # 股→張
        'up_days_20':     up_days,
        'down_days_20':   down_days,
        'up_vol_20':      round(int(up_vol) / 1000),            # 股→張
        'down_vol_20':    round(int(down_vol) / 1000),          # 股→張
        'estimated':      True,
        'note':           '⚠ 籌碼資料為統計模型估算，僅供參考，非真實法人申報數據'
    }


def _generate_recommendation(ticker, close, ind, trend, chip, info, div_yield, support, resist):
    """
    強化版投資建議 — 整合葛蘭碧訊號、均線排列、乖離率、交叉預測（V3）。
    直接呼叫 ma_analysis_enhanced.enhanced_generate_recommendation()。

    技術面評分不設上下限，直接累加所有項目：
      原有 8 項各 ±1  → ±8
      均線排列         ±2
      葛蘭碧強訊號     ±2/則（最多 2 則計分，±4）
      死亡/黃金交叉    10日內 ±2，10~20日 ±1（兩組最多 ±4）
      乖離率警戒       超買/超賣 ±1
    技術面上限約 ±16（不截斷），加上基本面（-1~+4），總分約 -20~+20。

    評級門檻（v2 調整）：
      強力買進 ≥ 6 ／ 買進 ≥ 3 ／ 持有 ≥ 0 ／ 減碼 ≥ -3 ／ 賣出 < -3

    回傳格式完全相容原版，並新增 ma_analysis 欄位。
    """
    rec = enhanced_generate_recommendation(
        ticker, close, ind, trend, chip, info, div_yield, support, resist
    )

    # ── 覆蓋評級門檻（v2：縮短門檻使評級更靈敏）──────────────────
    _score = rec.get('total_score', 0)
    if _score >= 6:
        _rating = '強力買進'
        _color  = '#16a34a'; _bg = '#14532d'; _icon = '🚀'
    elif _score >= 3:
        _rating = '買進'
        _color  = '#2563eb'; _bg = '#1e3a8a'; _icon = '✅'
    elif _score >= 0:
        _rating = '持有'
        _color  = '#d97706'; _bg = '#78350f'; _icon = '⚖️'
    elif _score >= -3:
        _rating = '減碼'
        _color  = '#ea580c'; _bg = '#7c2d12'; _icon = '⚠️'
    else:
        _rating = '賣出'
        _color  = '#fbbf24'; _bg = '#7f1d1d'; _icon = '🔴'
    rec['rating']       = _rating
    rec['rating_color'] = _color
    rec['rating_bg']    = _bg
    rec['rating_icon']  = _icon
    # 補充 summary（原版需要）
    rec['summary'] = _build_summary(
        ticker, close, trend, rec['rating'],
        rec['reasons_buy'], rec['reasons_sell'], rec['risks'], div_yield, info
    )
    return rec


def _build_summary(ticker, close, trend, rating, reasons_buy, reasons_sell, risks, div_yield, info):
    """產生投資分析摘要文字"""
    name    = info.get('name', ticker)
    sector  = info.get('sector', '')
    trend_l = trend.get('label', '盤整')

    lines = [f"📊 **{name}（{ticker}）投資分析摘要**\n"]
    lines.append(f"目前股價 **{close}** 元，技術面呈 **{trend_l}** 態勢，綜合評級為「**{rating}**」。\n")

    if sector:
        lines.append(f"所屬產業：{sector}。")

    if reasons_buy:
        lines.append("\n✅ **買進/持有理由：**")
        for r in reasons_buy[:4]:
            lines.append(f"• {r}")

    if reasons_sell:
        lines.append("\n⚠️ **賣出/觀望考量：**")
        for r in reasons_sell[:3]:
            lines.append(f"• {r}")

    if risks:
        lines.append("\n🔴 **主要風險：**")
        for r in risks[:3]:
            lines.append(f"• {r}")

    if div_yield:
        lines.append(f"\n💰 近12個月殖利率約 **{div_yield:.2f}%**，{'適合存股領息。' if div_yield >= 4 else '配息穩定。'}")

    lines.append("\n⚠️ *以上分析僅供參考，投資有風險，決策前請自行審慎評估。*")
    return '\n'.join(lines)


# ═══════════════════════════════════════════════════════════════
# 回測圖表/表格輔助函式（原有，保持不變）
# ═══════════════════════════════════════════════════════════════

def prepare_chart_data_from_annual(result):
    annual_summary     = result['results']['annual_summary']
    retirement_stop    = result.get('retirement_stop', [])
    retirement_cont    = result.get('retirement_continue', [])
    retire_yr_idx      = result.get('retire_yr_idx')
    initial_capital    = result['initial_capital']
    monthly_investment = result['monthly_investment']
    inflation_target   = result.get('inflation_target', 0)

    total_all = len(annual_summary)
    max_retire_seq = retirement_stop[-1]['年序'] if retirement_stop else 0
    total_points = max(total_all, max_retire_seq + 1)

    sample_interval = max(1, total_points // 20)
    sampled_set = set(range(0, total_points, sample_interval))
    sampled_set |= {0, total_points - 1}
    if retire_yr_idx is not None:
        sampled_set.add(retire_yr_idx)
    sorted_idx = sorted(sampled_set)

    stop_lk = {r['年序']: r for r in retirement_stop}
    cont_lk = {r['年序']: r for r in retirement_cont}
    last_actual_idx = max((i for i, r in enumerate(annual_summary)
                           if r['資料類型'] == 'actual'), default=-1)

    labels = []
    actual_series = []; forecast_series = []; stop_series = []
    cont_series = []; threshold_series = []

    for idx in sorted_idx:
        labels.append(f"第{idx}年")
        actual_series.append(
            round(annual_summary[idx]['年末資產']) if idx < total_all and idx <= last_actual_idx else None)
        forecast_series.append(
            round(annual_summary[idx]['年末資產'])
            if (idx < total_all and idx > last_actual_idx
                and (retire_yr_idx is None or idx <= retire_yr_idx)) else None)
        if retire_yr_idx is not None and idx == retire_yr_idx:
            base = round(annual_summary[idx]['年末資產'])
            stop_series.append(base); cont_series.append(base)
        elif retire_yr_idx is not None and idx > retire_yr_idx:
            stop_series.append(stop_lk[idx]['年末資產'] if idx in stop_lk else None)
            cont_series.append(cont_lk[idx]['年末資產'] if idx in cont_lk else None)
        else:
            stop_series.append(None); cont_series.append(None)
        if idx < total_all:
            threshold_series.append(round(annual_summary[idx]['通膨門檻']))
        else:
            threshold_series.append(round(inflation_target * (1 + 0.03) ** idx))

    r8 = (1 + 0.08) ** (1/12) - 1
    return_8_series = []
    for label in labels:
        year_num = int(label.replace('第','').replace('年',''))
        wealth = float(initial_capital)
        for _ in range(year_num * 12):
            wealth = (wealth + monthly_investment) * (1 + r8)
        return_8_series.append(round(wealth))

    return {
        'labels':              labels,
        'actual_assets':       actual_series,
        'forecast_assets':     forecast_series,
        'retire_stop_assets':  stop_series,
        'retire_cont_assets':  cont_series,
        'inflation_threshold': threshold_series,
        'return_8_assets':     return_8_series,
        'start_year':          annual_summary[0]['年份'] if annual_summary else 2023,
    }


def prepare_table_data(result):
    table_data = []
    annual_summary = result['results']['annual_summary']
    prev_end_assets = 0
    for row in annual_summary:
        year_return_val = row['年度報酬']
        year_invested   = row['年度投入']
        year_end        = row['年末資產']
        year_dividend   = row['年度股利']
        base = prev_end_assets + year_invested
        year_return_rate = round(year_return_val / base * 100, 2) if base > 0 else 0.0
        beg_assets = prev_end_assets
        prev_end_assets = year_end
        table_data.append({
            'year':               row['年份'],
            'data_type':          row['資料類型'],
            'year_invested':      f"{year_invested:,}",
            'year_dividend':      f"{year_dividend:,}",
            'year_return':        f"{year_return_val:,}",
            'year_return_rate':   year_return_rate,
            'year_end_assets':    f"{year_end:,}",
            'remaining_cash':     f"{row.get('剩餘現金', 0):,}",
            'inflation_threshold':f"{row['通膨門檻']:,}",
            'year_return_raw':    year_return_val,
            'beg_assets':         f"{beg_assets:,}",
        })
    return table_data



# ═══════════════════════════════════════════════════════════════
# 效益邊緣線 (Efficient Frontier) API
# ═══════════════════════════════════════════════════════════════

@app.route('/api/efficient_frontier', methods=['POST'])
def efficient_frontier():
    """
    計算效益邊緣線
    輸入: { tickers: ['2330','2317',...], n_sim: 3000, period: '2y' }
    輸出: 隨機模擬點、效率前緣、最小變異組合、最大夏普組合
    修復：超時保護 + 降低計算量 + 確保永遠回傳有效 JSON
    """
    import signal as _signal, sys as _sys

    def _timeout_handler(signum, frame):
        raise TimeoutError('EF 計算超時')

    # ── 超時保護（Unix/Render only；Windows 不支援 SIGALRM）───
    _alarm_set = False
    if _sys.platform != 'win32':
        try:
            _signal.signal(_signal.SIGALRM, _timeout_handler)
            _signal.alarm(55)   # 55秒後強制中斷（gunicorn 逾時前的保護層）
            _alarm_set = True
        except Exception:
            pass

    try:
        import yfinance as yf
        from functools import reduce

        data    = request.json or {}
        tickers = [t.strip().upper() for t in data.get('tickers', [])]
        # 安全限制模擬次數，避免超時
        n_sim   = min(int(data.get('n_sim', 2000)), 5000)
        period  = data.get('period', '2y')

        if len(tickers) < 2:
            return jsonify({'status':'error','message':'請至少輸入2支股票代碼'}), 400
        if len(tickers) > 15:
            return jsonify({'status':'error','message':'最多支援15支股票'}), 400

        # ── L1/L2：三層快取（本機 → GitHub，不觸發網路）────────
        prices = {}
        cached_from = {}
        for tk in tickers:
            rows = cache_mgr.get_price(tk, fetcher=None)   # fetcher=None 不觸發 yfinance
            if rows and len(rows) >= 20:
                try:
                    date_col  = next((c for c in rows[0] if c in ['日期','date','Date']), None)
                    close_col = next((c for c in rows[0] if c in ['收盤價','close','Close']), None)
                    if date_col and close_col:
                        s = pd.Series(
                            [float(r[close_col]) for r in rows],
                            index=pd.to_datetime([r[date_col] for r in rows])
                        ).dropna()
                        if len(s) >= 20:
                            prices[tk] = s
                            cached_from[tk] = 'local/github'
                except Exception:
                    pass

        need_fetch = [tk for tk in tickers if tk not in prices]
        if cached_from:
            print(f"  [EF] 快取命中: {cached_from}")
        if need_fetch:
            print(f"  [EF] 需從 yfinance 下載: {need_fetch}")

        # ── L3: yfinance 下載 ────────────────────────────────
        failed = []
        if need_fetch:
            # 先嘗試 .TW 批次
            tw_syms = [f"{tk}.TW" for tk in need_fetch]
            try:
                batch = yf.download(
                    tw_syms, period=period, auto_adjust=True,
                    progress=False, timeout=25, threads=True
                )
                if not batch.empty:
                    # 相容新版 yfinance MultiIndex 與舊版
                    if isinstance(batch.columns, pd.MultiIndex):
                        close_df = batch['Close'] if 'Close' in batch.columns.get_level_values(0) else batch.xs('Close', axis=1, level=0)
                    else:
                        close_df = batch['Close'] if 'Close' in batch.columns else batch
                    for tk in need_fetch:
                        sym = f"{tk}.TW"
                        col = sym if sym in close_df.columns else (tk if tk in close_df.columns else None)
                        if col:
                            s = close_df[col].dropna()
                            if len(s) >= 20:
                                prices[tk] = s
            except Exception as e:
                print(f"  [EF] .TW 批次失敗: {e}")

            # 仍未取得的改試 .TWO
            still_missing = [tk for tk in need_fetch if tk not in prices]
            if still_missing:
                two_syms = [f"{tk}.TWO" for tk in still_missing]
                try:
                    batch2 = yf.download(
                        two_syms, period=period, auto_adjust=True,
                        progress=False, timeout=25, threads=True
                    )
                    if not batch2.empty:
                        if isinstance(batch2.columns, pd.MultiIndex):
                            close_df2 = batch2['Close'] if 'Close' in batch2.columns.get_level_values(0) else batch2.xs('Close', axis=1, level=0)
                        else:
                            close_df2 = batch2['Close'] if 'Close' in batch2.columns else batch2
                        for tk in still_missing:
                            sym = f"{tk}.TWO"
                            col = sym if sym in close_df2.columns else (tk if tk in close_df2.columns else None)
                            if col:
                                s = close_df2[col].dropna()
                                if len(s) >= 20:
                                    prices[tk] = s
                except Exception as e:
                    print(f"  [EF] .TWO 批次失敗: {e}")

            # 逐一補抓
            for tk in need_fetch:
                if tk not in prices:
                    for suffix in ['.TW', '.TWO']:
                        try:
                            hist = yf.Ticker(f"{tk}{suffix}").history(period=period, timeout=12)
                            if hist is not None and not hist.empty:
                                s = hist['Close'].dropna()
                                if len(s) >= 20:
                                    prices[tk] = s
                                    break
                        except Exception:
                            continue
                if tk not in prices:
                    failed.append(tk)

            # 存快取（本機 + GitHub，統一由 cache_mgr 輔助函式處理）
            from github_cache import local_save_price, gh_save_price
            for tk in need_fetch:
                if tk not in prices:
                    continue
                price_series = prices[tk]
                try:
                    price_list = [
                        {'date': str(d)[:10], 'close': round(float(v), 2)}
                        for d, v in price_series.items() if pd.notna(v)
                    ]
                    local_save_price(DATA_DIR, tk, price_list)
                    gh_save_price(tk, price_list)
                except Exception as e_save:
                    print(f"  [EF] 存快取 {tk} 失敗（非致命）: {e_save}")

        if failed:
            return jsonify({'status':'error',
                           'message': f'無法取得以下股票資料：{", ".join(failed)}，'
                                      f'請確認代碼正確（台灣上市如 2330、00878）'}), 422

        # ── 對齊日期、計算報酬率 ─────────────────────────────
        df = pd.DataFrame(prices).dropna()
        if len(df) < 30:
            return jsonify({'status':'error','message':'歷史資料不足（需至少30個交易日）'}), 422

        returns = df.pct_change().dropna()
        mu      = (returns.mean() * 252).values.astype(float)
        cov     = (returns.cov()  * 252).values.astype(float)
        n       = len(tickers)

        # ── 蒙地卡羅模擬 ────────────────────────────────────
        sim_ret, sim_risk, sim_sharpe = [], [], []
        risk_free = 0.02
        np.random.seed(42)
        for _ in range(n_sim):
            w = np.random.dirichlet(np.ones(n))
            r = float(w @ mu)
            v = float(np.sqrt(w @ cov @ w))
            s = (r - risk_free) / v if v > 0 else 0.0
            sim_ret.append(round(r, 6))
            sim_risk.append(round(v, 6))
            sim_sharpe.append(round(s, 4))

        # ── MVP / MSP 最佳化 ─────────────────────────────────
        from scipy.optimize import minimize as sp_minimize

        def port_std(w):  return float(np.sqrt(w @ cov @ w))
        def port_ret(w):  return float(w @ mu)
        def neg_sharpe(w):
            r, s = port_ret(w), port_std(w)
            return -(r - risk_free) / s if s > 0 else 0.0

        bounds = tuple((0.0, 1.0) for _ in range(n))
        w0     = np.full(n, 1.0/n)
        eq_con = {'type': 'eq', 'fun': lambda w: float(np.sum(w)) - 1.0}
        opts   = {'ftol': 1e-8, 'maxiter': 300}

        mvp_res = sp_minimize(port_std, w0, method='SLSQP',
                              bounds=bounds, constraints=[eq_con], options=opts)
        msp_res = sp_minimize(neg_sharpe, w0, method='SLSQP',
                              bounds=bounds, constraints=[eq_con], options=opts)

        mvp_risk    = round(float(mvp_res.fun), 6)
        mvp_ret     = round(port_ret(mvp_res.x), 6)
        mvp_weights = {tk: round(float(x), 4) for tk, x in zip(tickers, mvp_res.x)}

        msp_risk    = round(port_std(msp_res.x), 6)
        msp_ret     = round(port_ret(msp_res.x), 6)
        msp_sharpe  = round(float(-msp_res.fun), 4)
        msp_weights = {tk: round(float(x), 4) for tk, x in zip(tickers, msp_res.x)}

        # ── 效率前緣曲線（40點，減少計算量）─────────────────
        ret_max   = float(np.max(mu))
        ret_range = np.linspace(mvp_ret, ret_max, 40)   # 40點足夠顯示曲線
        stock_inits = [np.eye(n)[i] for i in range(n)]

        ef_risks, ef_rets = [], []
        for target_r in ret_range:
            constraints = [eq_con,
                           {'type':'eq','fun': lambda w, r=target_r: port_ret(w) - r}]
            best = None
            # 只用 3 個初始點（減少計算量）
            for w_init in [w0, mvp_res.x, msp_res.x]:
                try:
                    res = sp_minimize(port_std, w_init, method='SLSQP',
                                      bounds=bounds, constraints=constraints,
                                      options={'ftol':1e-8,'maxiter':200})
                    if res.success and (best is None or res.fun < best.fun):
                        best = res
                except Exception:
                    continue
            if best is not None:
                ef_risks.append(round(float(best.fun), 6))
                ef_rets.append(round(float(target_r), 6))

        # 補入各股 100% 持有端點
        for i in range(n):
            single_ret  = float(mu[i])
            single_risk = float(np.sqrt(cov[i][i]))
            if not ef_rets or single_ret > max(ef_rets) + 1e-6:
                ef_risks.append(round(single_risk, 6))
                ef_rets.append(round(single_ret, 6))

        ef_sorted = sorted(zip(ef_rets, ef_risks))
        ef_rets   = [x[0] for x in ef_sorted]
        ef_risks  = [x[1] for x in ef_sorted]

        # ── 各股個別風險報酬 ─────────────────────────────────
        from data_fetcher import STOCK_NAMES_ZH_BACKEND
        stock_stats = [
            {
                'ticker': tk,
                'name':   STOCK_NAMES_ZH_BACKEND.get(tk, '') or tk,
                'ret':    round(float(mu[i]), 6),
                'risk':   round(float(np.sqrt(cov[i][i])), 6),
            }
            for i, tk in enumerate(tickers)
        ]

        resp = jsonify({
            'status':    'success',
            'tickers':   tickers,
            'n_sim':     n_sim,
            'risk_free': risk_free,
            'sim': {
                'risk':   sim_risk,
                'ret':    sim_ret,
                'sharpe': sim_sharpe,
            },
            'ef': {
                'risk': ef_risks,
                'ret':  ef_rets,
            },
            'mvp': {
                'risk':    mvp_risk,
                'ret':     mvp_ret,
                'weights': mvp_weights,
            },
            'msp': {
                'risk':    msp_risk,
                'ret':     msp_ret,
                'sharpe':  msp_sharpe,
                'weights': msp_weights,
            },
            'stocks':   stock_stats,
            'mu':       [round(float(x), 6) for x in mu],
            'cov_diag': [round(float(np.sqrt(cov[i][i])), 6) for i in range(n)],
        })
        if _alarm_set:
            try: _signal.alarm(0)
            except Exception: pass
        return resp

    except TimeoutError:
        if _alarm_set:
            try: _signal.alarm(0)
            except Exception: pass
        return jsonify({'status': 'error',
                        'message': '計算超時（>55秒），請減少股票數量或縮短期間後重試'}), 504

    except Exception as e:
        if _alarm_set:
            try: _signal.alarm(0)
            except Exception: pass
        import traceback; traceback.print_exc()
        return jsonify({'status':'error','message': str(e)}), 500




# ═══════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
# API：強化版移動平均線分析（含葛蘭碧法則 + 死亡/黃金交叉預測）
# ══════════════════════════════════════════════════════════════════════════════
@app.route('/api/ma_analysis/<ticker>', methods=['GET'])
def ma_analysis_endpoint(ticker):
    """
    GET /api/ma_analysis/<ticker>?short=5&long=20&forecast=30

    回傳完整均線分析：
      - 均線排列型態（多頭/空頭排列）
      - 各週期乖離率與警戒等級
      - 葛蘭碧八大法則觸發清單
      - MA5/MA20、MA20/MA60 死亡/黃金交叉預測（預估交易日數）
      - 綜合均線分析摘要
    """
    import time
    ticker = ticker.strip().upper()
    ma_short   = int(request.args.get('short',    5))
    ma_long    = int(request.args.get('long',    20))
    forecast   = int(request.args.get('forecast', 30))

    try:
        fetcher = ETFDataFetcher(output_dir=DATA_DIR)
        raw = fetcher.fetch_stock_analysis(ticker)
        if not raw or not raw.get('ohlcv'):
            return jsonify({'status': 'error', 'message': f'無法取得 {ticker} 資料'}), 404

        ohlcv      = raw['ohlcv']
        indicators = raw.get('indicators', {})
        close_series = [r['close'] for r in ohlcv]

        def last_val(lst):
            if not lst: return None
            return next((v for v in reversed(lst) if v is not None), None)

        latest_ind = {k: last_val(indicators.get(k)) for k in
                      ['ma5','ma10','ma20','ma60','ma120','ma200',
                       'macd','macd_signal','rsi','k','d']}
        close = close_series[-1]

        ma_series_dict = {
            'ma5':  indicators.get('ma5',  []),
            'ma20': indicators.get('ma20', []),
            'ma60': indicators.get('ma60', []),
        }

        # 完整均線分析
        result = analyze_ma(close_series, latest_ind, ma_series_dict)

        # 自訂週期交叉預測
        custom_cross = None
        if ma_short != 5 or ma_long != 20:
            custom_cross = estimate_cross_days(close_series, ma_short, ma_long, forecast)

        # 葛蘭碧（MA20 + MA60 雙週期）
        gran_20 = calc_granville_signals(close_series, indicators.get('ma20', []))
        gran_60 = calc_granville_signals(close_series, indicators.get('ma60', []))

        # 各週期乖離率
        bias_all = {}
        for period, key in [(5,'ma5'),(10,'ma10'),(20,'ma20'),(60,'ma60'),(120,'ma120'),(200,'ma200')]:
            mv = latest_ind.get(key)
            b  = calc_bias(close, mv)
            bias_all[f'ma{period}'] = {
                'value': b,
                'ma_value': mv,
                'warning': bias_warning(b, period) if b is not None else None,
            }

        return jsonify({
            'status':  'success',
            'ticker':  ticker,
            'name':    raw.get('name', ticker),
            'close':   close,
            'date':    ohlcv[-1]['date'],
            'ma_array': result['array'],
            'bias':    bias_all,
            'granville': {
                'ma20': gran_20,
                'ma60': gran_60,
                'summary': (
                    [f"法則{g['rule']}「{g['name']}」({g['signal'].upper()},{g['strength']})"
                     for g in gran_20 + gran_60]
                ),
            },
            'cross': {
                'ma5_ma20':  result['cross_5_20'],
                'ma20_ma60': result['cross_20_60'],
                'ma5_ma60':  result['cross_5_60'],
                'custom':    custom_cross,
            },
            'latest_ma': {k: latest_ind.get(k) for k in
                          ['ma5','ma10','ma20','ma60','ma120','ma200']},
            'summary':   result['summary'],
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500




# ═══════════════════════════════════════════════════════════════
# SIM 單一指數模型分析 API
# ═══════════════════════════════════════════════════════════════

@app.route('/api/sim_analysis/<ticker>', methods=['GET'])
def sim_analysis(ticker):
    """
    單一指數模型分析
    以台灣加權指數 ^TWII 為市場指標
    回傳: alpha, beta, R², 系統性風險, 非系統性風險, SML資料, 回歸散點
    """
    try:
        import yfinance as yf
        from scipy import stats as sp_stats

        ticker  = ticker.strip().upper()
        period  = request.args.get('period', '2y')

        # 快取5分鐘
        import time
        cache_key = f'sim_{ticker}'
        cached    = analysis_cache.get(cache_key)
        if cached and (time.time() - cached.get('ts', 0)) < 300:
            return jsonify({'status':'success', 'data': cached['data']})

        # ── 取得個股資料 ─────────────────────────────────────
        stock_df = None
        for suffix in ['.TW', '.TWO']:
            try:
                h = yf.Ticker(f"{ticker}{suffix}").history(period=period, timeout=12)
                if h is not None and not h.empty:
                    stock_df = h['Close'].dropna()
                    break
            except Exception:
                continue

        if stock_df is None or len(stock_df) < 30:
            return jsonify({'status':'error',
                           'message': f'無法取得 {ticker} 資料，請確認代碼正確'}), 404

        # ── 取得台灣加權指數 ^TWII ────────────────────────────
        try:
            mkt_df = yf.Ticker('^TWII').history(period=period, timeout=12)['Close'].dropna()
        except Exception as e:
            return jsonify({'status':'error','message': f'無法取得加權指數資料: {e}'}), 500

        # ── 對齊日期計算日報酬率 ─────────────────────────────
        combined = pd.DataFrame({'stock': stock_df, 'market': mkt_df}).dropna()
        if len(combined) < 30:
            return jsonify({'status':'error','message':'資料對齊後筆數不足（需至少30筆）'}), 422

        ret_s = combined['stock'].pct_change().dropna()
        ret_m = combined['market'].pct_change().dropna()

        common_idx = ret_s.index.intersection(ret_m.index)
        ret_s = ret_s.loc[common_idx].values
        ret_m = ret_m.loc[common_idx].values

        # ── 線性迴歸：r_i = alpha + beta * r_m + epsilon ─────
        slope, intercept, r_value, p_value, std_err = sp_stats.linregress(ret_m, ret_s)
        beta       = round(float(slope), 4)
        alpha      = round(float(intercept) * 252, 4)   # 年化 alpha
        alpha_d    = round(float(intercept), 6)          # 日度 alpha
        r_squared  = round(float(r_value ** 2), 4)

        # ── 風險分解 ─────────────────────────────────────────
        sigma_m2   = float(np.var(ret_m)) * 252           # 市場年化變異數
        sigma_m    = float(np.sqrt(sigma_m2))             # 市場年化標準差
        sigma_i2   = float(np.var(ret_s)) * 252           # 個股年化變異數
        sys_risk2  = (beta ** 2) * sigma_m2               # 系統性風險（變異數）
        idio_risk2 = max(sigma_i2 - sys_risk2, 0)         # 非系統性風險（變異數）
        total_risk = round(float(np.sqrt(sigma_i2)), 4)
        sys_risk   = round(float(np.sqrt(sys_risk2)), 4)
        idio_risk  = round(float(np.sqrt(idio_risk2)), 4)

        # ── SML（證券市場線）資料點 ──────────────────────────
        # 使用長期歷史市場溢酬（台灣加權指數長期約 7%），避免用近期樣本導致 SML 斜率失真
        risk_free             = 0.02
        market_premium_hist   = 0.07   # 台灣長期歷史市場風険溢酬（固定值）
        ret_m_annual          = risk_free + market_premium_hist  # 市場預期報酬 9%
        sml_betas             = [round(b * 0.1, 1) for b in range(0, 26)]  # β = 0 ~ 2.5
        sml_rets              = [round(risk_free + b * market_premium_hist, 4) for b in sml_betas]

        # 個股在SML上的預期報酬 vs 實際報酬
        stock_annual_ret   = round(float(np.mean(ret_s)) * 252, 4)
        expected_ret_capm  = round(risk_free + beta * market_premium_hist, 4)
        alpha_sml          = round(stock_annual_ret - expected_ret_capm, 4)  # Jensen's alpha

        # ── 散點資料（縮減至最多 200 點）────────────────────
        n_pts = len(ret_m)
        step  = max(1, n_pts // 200)
        scatter_m = [round(float(x), 5) for x in ret_m[::step]]
        scatter_s = [round(float(x), 5) for x in ret_s[::step]]
        # 迴歸線（兩端點）
        rm_min, rm_max = float(min(ret_m)), float(max(ret_m))
        reg_x = [round(rm_min, 5), round(rm_max, 5)]
        reg_y = [round(alpha_d + beta * rm_min, 5), round(alpha_d + beta * rm_max, 5)]

        # ── 殘差（epsilon）統計 ──────────────────────────────
        residuals    = ret_s - (alpha_d + beta * ret_m)
        resid_std    = round(float(np.std(residuals)) * np.sqrt(252), 4)

        data_out = {
            'ticker':           ticker,
            'period':           period,
            'n_obs':            len(ret_s),
            # SIM 核心參數
            'beta':             beta,
            'alpha':            alpha,            # 年化 alpha (%)
            'alpha_daily':      alpha_d,
            'r_squared':        r_squared,
            'p_value':          round(float(p_value), 6),
            'std_err':          round(float(std_err), 6),
            # 風險分解
            'total_risk':       total_risk,
            'sys_risk':         sys_risk,
            'idio_risk':        idio_risk,
            'sys_pct':          round(sys_risk2 / sigma_i2 * 100, 1) if sigma_i2 > 0 else 0,
            'idio_pct':         round(idio_risk2 / sigma_i2 * 100, 1) if sigma_i2 > 0 else 0,
            # 報酬率統計
            'stock_annual_ret': stock_annual_ret,
            'market_annual_ret':round(ret_m_annual, 4),
            'risk_free':        risk_free,
            'market_premium':   round(market_premium_hist, 4),
            'expected_ret_capm':expected_ret_capm,
            'alpha_sml':        alpha_sml,        # Jensen's alpha
            'resid_std':        resid_std,
            'market_sigma':     round(sigma_m, 4),        # 市場年化標準差（用於CML）
            # SML 資料
            'sml': {
                'betas': sml_betas,
                'rets':  sml_rets,
            },
            # 回歸散點
            'scatter': {
                'market': scatter_m,
                'stock':  scatter_s,
            },
            'regression_line': {
                'x': reg_x,
                'y': reg_y,
            },
        }

        analysis_cache[cache_key] = {'data': data_out, 'ts': time.time()}
        return jsonify({'status':'success', 'data': data_out})

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'status':'error','message': str(e)}), 500


# ═══════════════════════════════════════════════════════════════
# 新聞 API（後端爬取，避免前端 CORS 問題）
# ═══════════════════════════════════════════════════════════════

@app.route('/api/news/<ticker>', methods=['GET'])
def get_stock_news(ticker):
    """
    從 Google News RSS 取得個股新聞
    後端爬取後回傳 JSON，解決 Render 環境前端 CORS Proxy 失敗問題
    """
    try:
        import requests as req
        import xml.etree.ElementTree as ET
        import time

        ticker  = ticker.strip().upper()

        # 快取 5 分鐘
        cache_key = f'news_{ticker}'
        cached    = analysis_cache.get(cache_key)
        if cached and (time.time() - cached.get('ts', 0)) < 300:
            return jsonify({'status': 'success', 'articles': cached['data']})

        from data_fetcher import STOCK_NAMES_ZH_BACKEND
        zh_name = STOCK_NAMES_ZH_BACKEND.get(ticker, '')

        # 依序嘗試多個查詢詞
        query_terms = []
        if zh_name:
            query_terms.append(zh_name)
        query_terms.append(f'{ticker} 台灣股票')

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/rss+xml, application/xml, text/xml, */*',
        }

        articles = []
        seen_titles = set()

        for q in query_terms:
            try:
                rss_url = (
                    f'https://news.google.com/rss/search'
                    f'?q={req.utils.quote(q)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant'
                )
                resp = req.get(rss_url, headers=headers, timeout=12)
                if not resp.ok:
                    continue

                root = ET.fromstring(resp.content)
                for item in root.findall('.//item')[:15]:
                    title   = (item.findtext('title')   or '').strip()
                    link    = (item.findtext('link')    or '#').strip()
                    pub     = (item.findtext('pubDate') or '').strip()
                    source  = (item.findtext('source')  or 'Google News').strip()
                    desc    = (item.findtext('description') or '').strip()
                    # 移除 HTML 標籤
                    import re as _re
                    desc = _re.sub(r'<[^>]+>', '', desc).strip()

                    if title and title not in seen_titles:
                        seen_titles.add(title)
                        articles.append({
                            'title':   title,
                            'link':    link,
                            'pubDate': pub,
                            'source':  source,
                            'desc':    desc[:150],
                        })

                if len(articles) >= 20:
                    break
            except Exception as e:
                print(f'  Google News 查詢「{q}」失敗: {e}')
                continue

        # 依時間排序（最新在前）
        def parse_dt(s):
            try:
                from email.utils import parsedate_to_datetime
                return parsedate_to_datetime(s).timestamp()
            except Exception:
                return 0
        articles.sort(key=lambda a: parse_dt(a['pubDate']), reverse=True)

        analysis_cache[cache_key] = {'data': articles, 'ts': time.time()}
        return jsonify({'status': 'success', 'articles': articles})

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/goodinfo/<ticker>', methods=['GET'])
def get_goodinfo_news(ticker):
    """
    從 Goodinfo 公告列表頁面爬取個股最新訊息
    目標頁面: https://goodinfo.tw/tw/StockAnnouncementList.asp?STOCK_ID={ticker}
    使用 html.parser（Python 內建）解析，不依賴 BeautifulSoup
    """
    try:
        import requests as req
        import html
        import re as _re
        import time
        from html.parser import HTMLParser

        ticker = ticker.strip().upper()

        # 快取 5 分鐘
        cache_key = f'goodinfo_{ticker}'
        cached    = analysis_cache.get(cache_key)
        if cached and (time.time() - cached.get('ts', 0)) < 300:
            return jsonify({'status': 'success', 'items': cached['data']})

        # ── 取得頁面 ─────────────────────────────────────────────
        url = f'https://goodinfo.tw/tw/StockAnnouncementList.asp?STOCK_ID={ticker}'
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/124.0.0.0 Safari/537.36',
            'Referer':    'https://goodinfo.tw/tw/index.asp',
            'Accept':     'text/html,application/xhtml+xml,*/*',
            'Accept-Language': 'zh-TW,zh;q=0.9',
        }

        resp = req.get(url, headers=headers, timeout=15)
        resp.encoding = 'utf-8'

        if not resp.ok:
            return jsonify({
                'status':  'error',
                'message': f'Goodinfo 回應 HTTP {resp.status_code}，可能封鎖了伺服器 IP'
            }), 502

        page_html = resp.text

        # ── 解析右側「最新訊息」區塊 ─────────────────────────────
        # Goodinfo 最新訊息在 id="divAnnounceList" 或類似的 <table> 中
        # 每筆格式：時間 | 標題（含連結）
        items = []

        # 先嘗試提取 <a> 連結中的公告標題（最新訊息區塊）
        # 找出含有公告連結的 <tr> 行
        # 典型格式: <tr>...<td>01/02 10:22</td><td><a href="...">標題</a></td></tr>
        row_pattern = _re.compile(
            r'<tr[^>]*>.*?'
            r'<td[^>]*>\s*(\d{2}/\d{2}\s+\d{2}:\d{2})\s*</td>\s*'  # 時間
            r'<td[^>]*>.*?<a\s+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>.*?</td>'  # 連結+標題
            r'.*?</tr>',
            _re.DOTALL | _re.IGNORECASE
        )

        seen = set()
        for m in row_pattern.finditer(page_html):
            time_str = m.group(1).strip()
            href     = m.group(2).strip()
            title    = _re.sub(r'<[^>]+>', '', m.group(3)).strip()
            title    = html.unescape(title)

            if not title or title in seen:
                continue
            seen.add(title)

            # 補全相對連結
            if href.startswith('http'):
                link = href
            else:
                link = 'https://goodinfo.tw/tw/' + href.lstrip('/')

            items.append({
                'title':   title,
                'link':    link,
                'pubDate': time_str,   # 格式 MM/DD HH:MM
                'desc':    '',
            })

        # ── 備援：若上述正則沒匹配到，改用更寬鬆的方式 ──────────
        if not items:
            # 找所有含 Anue/goodinfo 公告的 <a> 連結
            loose = _re.compile(
                r'(\d{2}/\d{2}\s+\d{2}:\d{2}).*?'
                r'<a\s+href=["\']([^"\']*StockAnnounce[^"\']*)["\'][^>]*>(.*?)</a>',
                _re.DOTALL | _re.IGNORECASE
            )
            for m in loose.finditer(page_html):
                time_str = m.group(1).strip()
                href     = m.group(2).strip()
                title    = _re.sub(r'<[^>]+>', '', m.group(3)).strip()
                title    = html.unescape(title)
                if not title or title in seen:
                    continue
                seen.add(title)
                link = href if href.startswith('http') else 'https://goodinfo.tw/tw/' + href.lstrip('/')
                items.append({'title': title, 'link': link, 'pubDate': time_str, 'desc': ''})

        if not items:
            return jsonify({
                'status':  'error',
                'message': 'Goodinfo 頁面結構無法解析，可能已變更或被封鎖'
            }), 502

        # 最多回傳 20 筆
        items = items[:20]

        analysis_cache[cache_key] = {'data': items, 'ts': time.time()}
        return jsonify({'status': 'success', 'items': items})

    except req.exceptions.ConnectionError:
        return jsonify({
            'status':  'error',
            'message': 'Goodinfo 連線失敗（Render IP 可能被封鎖），請點右上角按鈕直接查看'
        }), 502
    except req.exceptions.Timeout:
        return jsonify({
            'status':  'error',
            'message': 'Goodinfo 連線逾時，請稍後再試'
        }), 504
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500




# ═══════════════════════════════════════════════════════════════
# 財報分析 API
# ═══════════════════════════════════════════════════════════════

@app.route('/api/claude_proxy', methods=['POST'])
def claude_proxy():
    """
    Claude API 後端代理
    API Key 從環境變數 ANTHROPIC_API_KEY 讀取，不暴露於前端
    本機開發：在 .env 或系統環境變數設定 ANTHROPIC_API_KEY=sk-ant-api03-...
    Render 部署：在 Render Dashboard > Environment 設定同名變數
    """
    import requests as req

    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return jsonify({'error': '伺服器未設定 ANTHROPIC_API_KEY 環境變數'}), 500

    try:
        payload = request.get_json(force=True)
        resp = req.post(
            'https://api.anthropic.com/v1/messages',
            headers={
                'Content-Type':    'application/json',
                'x-api-key':       api_key,
                'anthropic-version': '2023-06-01',
            },
            json=payload,
            timeout=60
        )
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 500




import time as _time


# ═══════════════════════════════════════════════════════════════
# ★ 修復新增路由
# ═══════════════════════════════════════════════════════════════

def _get_period_key() -> str:
    """台灣時間 period key，16:00 為當日/昨日分界"""
    from datetime import datetime, timezone, timedelta
    tz_tw = timezone(timedelta(hours=8))
    now = datetime.now(tz_tw)
    if now.hour < 16:
        now = now - timedelta(days=1)
    return now.strftime('%Y%m%d')


@app.route('/api/stock_cache/<ticker>', methods=['GET'])
def stock_cache(ticker: str):
    """前端 ghStockCacheLoad() 呼叫此端點，原版未定義導致 404"""
    from github_cache import _gh_raw_get
    import json as _json
    ticker = ticker.strip().upper()
    period_key = _get_period_key()
    gh_path = f"ai_reports/{ticker}/{period_key}.json"
    try:
        content = _gh_raw_get(gh_path)
        if content:
            return jsonify({'status': 'cached', 'period_key': period_key,
                            'data': _json.loads(content)})
    except Exception as e:
        print(f"  [stock_cache] 讀取失敗 {ticker}: {e}")
    return jsonify({'status': 'not_found', 'period_key': period_key, 'data': None})


@app.route('/api/ai_report/<ticker>', methods=['GET', 'POST'])
def ai_report(ticker: str):
    """
    AI 財務健診報告
    GET  → 查詢快取（本機 Storage 優先，再查 GitHub），不呼叫 Claude
    POST → 重新呼叫 Claude API 產生新報告，結果同時存本機 Storage + GitHub
    """
    from github_cache import _gh_raw_get, _gh_writer
    import requests as _req2
    import json as _json
    ticker = ticker.strip().upper()
    period_key = _get_period_key()
    local_path = os.path.join(DATA_DIR, f"ai_report_{ticker}_{period_key}.json")
    gh_path = f"ai_reports/{ticker}/{period_key}.json"

    if request.method == 'GET':
        # ── L1：本機 Storage（最快，零網路延遲）───────────────
        if os.path.exists(local_path):
            try:
                with open(local_path, 'r', encoding='utf-8') as f:
                    data = _json.load(f)
                if data.get('report_text'):
                    print(f"  [ai_report] {ticker} GET 本機快取命中")
                    return jsonify({'status': 'cached', 'source': 'local',
                                    'period_key': period_key, 'data': data})
            except Exception:
                pass
        # ── L2：GitHub Public Repo ────────────────────────────
        try:
            content = _gh_raw_get(gh_path)
            if content:
                data = _json.loads(content)
                if data.get('report_text'):
                    # 回填本機，下次直接從本機讀
                    try:
                        with open(local_path, 'w', encoding='utf-8') as f:
                            _json.dump(data, f, ensure_ascii=False, indent=2)
                    except Exception:
                        pass
                    print(f"  [ai_report] {ticker} GET GitHub 命中，已回填本機")
                    return jsonify({'status': 'cached', 'source': 'github',
                                    'period_key': period_key, 'data': data})
        except Exception:
            pass
        return jsonify({'status': 'not_found', 'period_key': period_key, 'data': None})

    # ── POST：呼叫 Claude API 產生新報告 ─────────────────────
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return jsonify({'error': '伺服器未設定 ANTHROPIC_API_KEY 環境變數'}), 500
    try:
        payload = request.get_json(force=True)
        resp = _req2.post(
            'https://api.anthropic.com/v1/messages',
            headers={'Content-Type': 'application/json', 'x-api-key': api_key,
                     'anthropic-version': '2023-06-01'},
            json=payload, timeout=90
        )
        if not resp.ok:
            return jsonify({'error': f'Claude API 錯誤：{resp.status_code}'}), resp.status_code
        report_text = ''.join(
            b.get('text', '') for b in resp.json().get('content', [])
            if b.get('type') == 'text'
        )
        if not report_text:
            return jsonify({'error': '取得的 AI 分析內容為空'}), 500
        from datetime import datetime, timezone, timedelta
        tz_tw = timezone(timedelta(hours=8))
        report_data = {'ticker': ticker, 'period_key': period_key,
                       'report_text': report_text,
                       'generated_at': datetime.now(tz_tw).isoformat()}
        # ── 存本機 Storage ────────────────────────────────────
        try:
            with open(local_path, 'w', encoding='utf-8') as f:
                _json.dump(report_data, f, ensure_ascii=False, indent=2)
            print(f"  [ai_report] {ticker} 已存本機 Storage")
        except Exception as e:
            print(f'  [ai_report] 本機存檔失敗（非致命）: {e}')
        # ── 同步 GitHub Repo ──────────────────────────────────
        try:
            _gh_writer.put(gh_path, _json.dumps(report_data, ensure_ascii=False, indent=2),
                           f'ai_report: {ticker} {period_key}')
        except Exception as e:
            print(f'  [ai_report] GitHub 存檔失敗（非致命）: {e}')
        return jsonify({'status': 'generated', 'period_key': period_key, 'data': report_data})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/cache_version', methods=['GET'])
def cache_version():
    v = getattr(app, '_cache_version', '')
    if not v:
        v = str(int(_time.time()))
        app._cache_version = v
    return jsonify({'version': v})


@app.route('/api/finreport/<ticker>', methods=['GET'])
def get_finreport(ticker):
    """
    取得個股三大財務報表（年報5年 + 季報4季）
    資料來源：yfinance 優先，失敗改 MOPS/TWSE
    回傳：income（損益）、balance（資產負債）、cashflow（現金流量）
    """
    import time, traceback
    ticker = ticker.strip().upper()

    # 快取 6 小時
    cache_key = f'finreport_{ticker}'
    cached = analysis_cache.get(cache_key)
    if cached and (time.time() - cached.get('ts', 0)) < 21600:
        return jsonify({'status': 'success', 'data': cached['data']})

    try:
        result = _fetch_finreport(ticker)
        if result.get('error'):
            return jsonify({'status': 'error', 'message': result['error']}), 404

        analysis_cache[cache_key] = {'data': result, 'ts': time.time()}
        return jsonify({'status': 'success', 'data': result})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


def _fetch_finreport(ticker: str) -> dict:
    """
    抓取三大財務報表核心邏輯
    優先順序: yfinance → MOPS/TWSE 備援
    """
    import yfinance as yf
    import requests as req
    import re as _re
    import json as _json

    result = {
        'ticker':   ticker,
        'source':   'yfinance',
        'annual':   {},   # 年報 {'income':[], 'balance':[], 'cashflow':[]}
        'quarterly':{},   # 季報
        'ratios':   {},   # 計算出的財務比率
        'error':    None,
    }

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36',
        'Accept-Language': 'zh-TW,zh;q=0.9',
    }

    def safe_float(v):
        try:
            if v is None or (hasattr(v, '__class__') and 'NaT' in str(type(v))): return None
            f = float(str(v).replace(',', ''))
            return None if (f != f) else round(f, 4)   # NaN check
        except Exception:
            return None

    def to_b(v):
        """轉換為億元（保留2位小數）"""
        f = safe_float(v)
        return round(f / 1e8, 2) if f is not None else None

    def to_m(v):
        """轉換為百萬元"""
        f = safe_float(v)
        return round(f / 1e6, 2) if f is not None else None

    # ══════════════════════════════════════════════════════════
    # L1: yfinance
    # ══════════════════════════════════════════════════════════
    yf_ok = False
    for suffix in ['.TW', '.TWO', '']:
        try:
            tk = yf.Ticker(ticker + suffix)

            # ── 年報 ──────────────────────────────────────────
            inc_a  = tk.income_stmt
            bal_a  = tk.balance_sheet
            cf_a   = tk.cashflow
            inc_q  = tk.quarterly_income_stmt
            bal_q  = tk.quarterly_balance_sheet
            cf_q   = tk.quarterly_cashflow

            if inc_a is not None and not inc_a.empty:
                yf_ok = True
                result['source'] = f'yfinance({ticker}{suffix})'

                def parse_annual(df, converter=to_b):
                    if df is None or df.empty: return []
                    rows = []
                    # 取最多6年並依日期由新到舊排序，確保含2021年資料
                    sorted_cols = sorted(df.columns, reverse=True)[:6]
                    for col in sorted_cols:
                        yr = str(col)[:10]
                        row = {'period': yr, 'period_type': 'annual'}
                        for idx in df.index:
                            key = str(idx).strip().replace(' ', '_').lower()
                            row[key] = converter(df.loc[idx, col])
                        rows.append(row)
                    return rows

                def parse_quarterly(df, converter=to_b):
                    if df is None or df.empty: return []
                    rows = []
                    for col in df.columns[:4]:   # 最近4季
                        qr = str(col)[:10]
                        row = {'period': qr, 'period_type': 'quarterly'}
                        for idx in df.index:
                            key = str(idx).strip().replace(' ', '_').lower()
                            row[key] = converter(df.loc[idx, col])
                        rows.append(row)
                    return rows

                result['annual']['income']   = parse_annual(inc_a)
                result['annual']['balance']  = parse_annual(bal_a)
                result['annual']['cashflow'] = parse_annual(cf_a)
                result['quarterly']['income']   = parse_quarterly(inc_q)
                result['quarterly']['balance']  = parse_quarterly(bal_q)
                result['quarterly']['cashflow'] = parse_quarterly(cf_q)
                break
        except Exception as e:
            print(f'  [finreport] yfinance {ticker}{suffix} 失敗: {e}')
            continue

    # ══════════════════════════════════════════════════════════
    # L2: MOPS/TWSE 備援（yfinance 無資料時啟動）
    # ══════════════════════════════════════════════════════════
    if not yf_ok:
        print(f'  [finreport] yfinance 無資料，改用 MOPS 備援 ({ticker})')
        try:
            mops_data = _fetch_mops_finreport(ticker, headers)
            if mops_data:
                result['annual']   = mops_data.get('annual', {})
                result['quarterly']= mops_data.get('quarterly', {})
                result['source']   = 'MOPS'
                yf_ok = True
        except Exception as e:
            print(f'  [finreport] MOPS 備援失敗: {e}')

    if not yf_ok:
        result['error'] = f'無法取得 {ticker} 財務報表，請確認股票代碼（台灣上市如 2330、2412）'
        return result

    # ══════════════════════════════════════════════════════════
    # 計算衍生財務比率（從年報推算）
    # ══════════════════════════════════════════════════════════
    ratios_annual = []
    for row in result['annual'].get('income', []):
        period = row.get('period', '')
        rev    = row.get('total_revenue') or row.get('totalrevenue')
        gp     = row.get('gross_profit')  or row.get('grossprofit')
        op     = row.get('operating_income') or row.get('ebit')
        ni     = row.get('net_income') or row.get('netincome') or row.get('net_income_common_stockholders')

        # 對應資產負債表
        bal_row = next((b for b in result['annual'].get('balance', []) if b.get('period') == period), {})
        assets     = bal_row.get('total_assets') or bal_row.get('totalassets')
        equity     = bal_row.get('stockholders_equity') or bal_row.get('common_stock_equity') or bal_row.get('total_equity_gross_minority_interest')
        total_liab = bal_row.get('total_liabilities_net_minority_interest') or bal_row.get('total_liabilities')
        cur_assets = bal_row.get('current_assets') or bal_row.get('total_current_assets')
        cur_liab   = bal_row.get('current_liabilities') or bal_row.get('total_current_liabilities')

        # 對應現金流量表
        cf_row = next((c for c in result['annual'].get('cashflow', []) if c.get('period') == period), {})
        op_cf  = cf_row.get('operating_cash_flow') or cf_row.get('cash_from_operating_activities') or cf_row.get('total_cash_from_operating_activities')
        cap_ex = cf_row.get('capital_expenditure') or cf_row.get('capital_expenditures')

        def pct(a, b):
            fa, fb = safe_float(a), safe_float(b)
            if fa is None or fb is None or fb == 0: return None
            return round(fa / fb * 100, 2)

        gpm   = pct(gp, rev)
        opm   = pct(op, rev)
        npm   = pct(ni, rev)
        roe   = pct(ni, equity)
        roa   = pct(ni, assets)
        debt_ratio = pct(total_liab, assets)
        cur_ratio  = None
        if cur_assets is not None and cur_liab is not None and safe_float(cur_liab) and safe_float(cur_liab) != 0:
            ca_f, cl_f = safe_float(cur_assets), safe_float(cur_liab)
            if ca_f and cl_f:
                cur_ratio = round(ca_f / cl_f, 2)

        fcf = None
        if op_cf is not None and cap_ex is not None:
            o_f, c_f = safe_float(op_cf), safe_float(cap_ex)
            if o_f is not None and c_f is not None:
                fcf = round(o_f - abs(c_f), 2)

        ratios_annual.append({
            'period':      period,
            'revenue_b':   safe_float(rev),    # 億元
            'gross_profit_b': safe_float(gp),
            'op_income_b': safe_float(op),
            'net_income_b':safe_float(ni),
            'op_cashflow_b': safe_float(op_cf),
            'fcf_b':       fcf,
            'gpm':         gpm,
            'opm':         opm,
            'npm':         npm,
            'roe':         roe,
            'roa':         roa,
            'debt_ratio':  debt_ratio,
            'cur_ratio':   cur_ratio,
            'equity_b':    safe_float(equity),
            'assets_b':    safe_float(assets),
        })

    result['ratios']['annual'] = ratios_annual

    # 季報衍生比率（完整版，補全所有前端需要的欄位）
    ratios_q = []

    def pct_q(a, b):
        fa, fb = safe_float(a), safe_float(b)
        if fa is None or fb is None or fb == 0:
            return None
        return round(fa / fb * 100, 2)

    for row in result['quarterly'].get('income', []):
        period = row.get('period', '')
        rev = row.get('total_revenue')    or row.get('totalrevenue')
        gp  = row.get('gross_profit')     or row.get('grossprofit')
        op  = row.get('operating_income') or row.get('ebit')
        ni  = (row.get('net_income')
               or row.get('netincome')
               or row.get('net_income_common_stockholders'))

        # 對應同期資產負債表
        bal_row = next(
            (b for b in result['quarterly'].get('balance', [])
             if b.get('period') == period), {}
        )
        assets     = bal_row.get('total_assets')   or bal_row.get('totalassets')
        equity     = (bal_row.get('stockholders_equity')
                      or bal_row.get('common_stock_equity')
                      or bal_row.get('total_equity_gross_minority_interest'))
        total_liab = (bal_row.get('total_liabilities_net_minority_interest')
                      or bal_row.get('total_liabilities'))
        cur_assets = (bal_row.get('current_assets')
                      or bal_row.get('total_current_assets'))
        cur_liab   = (bal_row.get('current_liabilities')
                      or bal_row.get('total_current_liabilities'))

        # 對應同期現金流量表
        cf_row = next(
            (c for c in result['quarterly'].get('cashflow', [])
             if c.get('period') == period), {}
        )
        op_cf  = (cf_row.get('operating_cash_flow')
                  or cf_row.get('cash_from_operating_activities')
                  or cf_row.get('total_cash_from_operating_activities'))
        cap_ex = (cf_row.get('capital_expenditure')
                  or cf_row.get('capital_expenditures'))

        # 流動比率
        cur_ratio = None
        ca_f, cl_f = safe_float(cur_assets), safe_float(cur_liab)
        if ca_f is not None and cl_f and cl_f != 0:
            cur_ratio = round(ca_f / cl_f, 2)

        # 自由現金流
        fcf = None
        o_f, c_f = safe_float(op_cf), safe_float(cap_ex)
        if o_f is not None and c_f is not None:
            fcf = round(o_f - abs(c_f), 2)

        ratios_q.append({
            'period':         period,
            'revenue_b':      safe_float(rev),
            'gross_profit_b': safe_float(gp),
            'op_income_b':    safe_float(op),
            'net_income_b':   safe_float(ni),
            'op_cashflow_b':  safe_float(op_cf),
            'fcf_b':          fcf,
            'gpm':            pct_q(gp,  rev),
            'opm':            pct_q(op,  rev),
            'npm':            pct_q(ni,  rev),
            'roe':            pct_q(ni,  equity),
            'roa':            pct_q(ni,  assets),
            'debt_ratio':     pct_q(total_liab, assets),
            'cur_ratio':      cur_ratio,
            'equity_b':       safe_float(equity),
            'assets_b':       safe_float(assets),
        })

    result['ratios']['quarterly'] = ratios_q

    return result


def _fetch_mops_finreport(ticker: str, headers: dict) -> dict:
    """
    MOPS 備援：抓取台灣公開資訊觀測站財報（年報）
    主要目標：綜合損益表 (t05st22) + 資產負債表 (t51sb08) + 現金流量表 (t05st36)
    """
    import requests as req
    import re as _re

    result = {'annual': {'income': [], 'balance': [], 'cashflow': []}, 'quarterly': {}}
    base_url = 'https://mops.twse.com.tw/mops/web/'

    def safe_float_str(s):
        try:
            s = str(s).replace(',', '').replace('(', '-').replace(')', '').strip()
            return float(s)
        except Exception:
            return None

    # 判斷市場（上市/上櫃）
    market_type = '1'   # 預設上市
    try:
        r = req.get(
            f'https://www.twse.com.tw/zh/api/basic/company?stockNo={ticker}',
            headers=headers, timeout=8
        )
        if r.ok and r.json():
            pass   # 有回應 = 上市
    except Exception:
        market_type = '2'   # 上櫃

    # 抓近5年度損益表（簡版：合併報表）
    cur_year = pd.Timestamp.now().year
    for yr in range(cur_year - 1, cur_year - 6, -1):
        roc_yr = yr - 1911   # 民國年
        try:
            form_data = {
                'encodeURIComponent': '1',
                'step': '1',
                'firstin': '1',
                'off': '1',
                'co_id': ticker,
                'year': str(roc_yr),
                'season': '04',   # 第4季=年報
                'report_id': 'C',
            }
            url = base_url + 'ajax_t05st22'
            r = req.post(url, data=form_data, headers={**headers, 'Content-Type': 'application/x-www-form-urlencoded'}, timeout=15)
            if not r.ok: continue
            # 用 regex 抓關鍵數字（HTML 表格解析）
            text = r.text
            rows_raw = _re.findall(r'<tr[^>]*>(.*?)</tr>', text, _re.DOTALL | _re.IGNORECASE)
            income_row = {
                'period': f'{yr}-12-31',
                'period_type': 'annual',
            }
            for row_html in rows_raw:
                cells = _re.findall(r'<td[^>]*>(.*?)</td>', row_html, _re.DOTALL | _re.IGNORECASE)
                cells_clean = [_re.sub(r'<[^>]+>', '', c).strip() for c in cells]
                if len(cells_clean) < 2: continue
                label = cells_clean[0]
                val_str = cells_clean[-1] if len(cells_clean) >= 2 else None
                val = safe_float_str(val_str) if val_str else None
                if '營業收入' in label and '合計' in label:
                    income_row['total_revenue'] = round(val / 1e8, 2) if val else None
                elif '毛利' in label or '營業毛利' in label:
                    income_row['gross_profit'] = round(val / 1e8, 2) if val else None
                elif '營業利益' in label and '合計' not in label:
                    income_row['operating_income'] = round(val / 1e8, 2) if val else None
                elif '本期淨利' in label or '本期稅後淨利' in label:
                    income_row['net_income'] = round(val / 1e8, 2) if val else None
            if income_row.get('total_revenue'):
                result['annual']['income'].append(income_row)
        except Exception as e:
            print(f'  [MOPS] {yr} 損益表失敗: {e}')
            continue

    return result if result['annual']['income'] else None



# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
# 自選股 API（已移除）
# 自選股功能已從前端移除（btnWatchlist 按鈕已刪除）
# 以下 watchlist helper 保留供 GitHub 備援讀取使用，不對外暴露路由
# ═══════════════════════════════════════════════════════════════

WL_FILE = os.path.join(DATA_DIR, 'watchlist.json')

def _wl_read() -> list:
    """讀取自選股代碼清單（供內部使用）"""
    if os.path.exists(WL_FILE):
        try:
            with open(WL_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get('codes', [])
        except Exception:
            pass
    return []


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("=" * 60)
    print("台灣ETF/股票投資分析系統啟動 (V4)")
    print(f"執行環境: {'Render (Production)' if os.environ.get('RENDER') else 'Local Development'}")
    print(f"Port: {port}")
    print("=" * 60)

    # ── 啟動每日 16:00 自動更新排程 ──────────────────────────
    # 更新範圍：TOP50_STOCKS（定義於 github_cache.py）
    # 儲存目標：本機 Storage ＋ GitHub Repo（需 GH_CACHE_TOKEN）
    start_scheduler(
        cache=cache_mgr,
        fetcher_factory=lambda: ETFDataFetcher(output_dir=DATA_DIR),
        popular_stocks=[s['code'] for s in POPULAR_STOCKS] if isinstance(POPULAR_STOCKS[0], dict)
                       else list(POPULAR_STOCKS),
    )

    host = '0.0.0.0' if os.environ.get('RENDER') else '127.0.0.1'
    app.run(debug=False, host=host, port=port)
