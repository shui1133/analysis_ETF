"""
Flask Web應用主程式 V4
台灣ETF/股票投資分析系統
新增：股票分析頁 API（OHLCV、技術指標、投資建議）
調整：蒙地卡羅/情境分析資料仍由後端計算，由新頁面 analysis.html 呈現
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
import io

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

# 全域快取
cached_results    = {}
etf_memory_cache  = {}
analysis_cache    = {}   # 股票分析快取（key=ticker）


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
        data        = request.json
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
        data = request.json

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


@app.route('/api/stock_analysis/<ticker>', methods=['GET'])
def get_stock_analysis(ticker):
    """
    取得單支股票完整分析資料
    回傳：OHLCV、技術指標、基本面、籌碼估算、投資建議
    """
    ticker = ticker.strip().upper()

    # 快取（5分鐘）
    import time
    cache_entry = analysis_cache.get(ticker)
    if cache_entry and (time.time() - cache_entry.get('ts', 0)) < 300:
        return jsonify({'status': 'success', 'data': cache_entry['data']})

    try:
        fetcher = ETFDataFetcher(output_dir=DATA_DIR)
        raw = fetcher.fetch_stock_analysis(ticker)

        if not raw or not raw.get('ohlcv'):
            return jsonify({
                'status':  'error',
                'message': f'無法取得 {ticker} 資料，請確認股票代碼正確（台灣上市如：2330、00878）'
            }), 404

        ohlcv      = raw['ohlcv']
        indicators = raw.get('indicators', {})
        info       = raw.get('info', {})
        divs       = raw.get('dividend_data', [])

        # ── 最新資料 ────────────────────────────────────────────
        last = ohlcv[-1]
        prev = ohlcv[-2] if len(ohlcv) >= 2 else last
        change     = round(last['close'] - prev['close'], 2)
        change_pct = round(change / prev['close'] * 100, 2) if prev['close'] else 0

        # ── 殖利率計算（近12個月配息合計）────────────────────
        annual_div = 0
        if divs:
            one_yr_ago = (pd.Timestamp.now() - pd.DateOffset(years=1)).strftime('%Y-%m-%d')
            annual_div = sum(d['dividend'] for d in divs if d['date'] >= one_yr_ago)
        div_yield = round(annual_div / last['close'] * 100, 2) if last['close'] and annual_div else None

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
        trend = _calc_trend(last['close'], latest_ind)

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

        def to_lots(v):
            """股 → 張（÷1000，至少1張）"""
            return max(1, round(v / 1000)) if v else 0

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
                'opens':        [r['open']   for r in chart_ohlcv],
                'highs':        [r['high']   for r in chart_ohlcv],
                'lows':         [r['low']    for r in chart_ohlcv],
                'closes':       [r['close']  for r in chart_ohlcv],
                'volumes':      [to_lots(r['volume']) for r in chart_ohlcv],
                'ma5':          slice_ind('ma5'),
                'ma10':         slice_ind('ma10'),
                'ma20':         slice_ind('ma20'),
                'ma60':         slice_ind('ma60'),
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


def _calc_trend(close, ind):
    """根據均線關係判斷趨勢"""
    ma5  = ind.get('ma5')
    ma20 = ind.get('ma20')
    ma60 = ind.get('ma60')
    macd = ind.get('macd')
    rsi  = ind.get('rsi')

    score = 0
    signals = []

    # 均線多頭排列
    if ma5 and ma20 and ma5 > ma20:
        score += 2
        signals.append('MA5>MA20（短線偏多）')
    elif ma5 and ma20 and ma5 < ma20:
        score -= 2
        signals.append('MA5<MA20（短線偏空）')

    if ma20 and ma60 and ma20 > ma60:
        score += 2
        signals.append('MA20>MA60（中線偏多）')
    elif ma20 and ma60 and ma20 < ma60:
        score -= 2
        signals.append('MA20<MA60（中線偏空）')

    # 價格與均線關係
    if ma20 and close > ma20:
        score += 1
        signals.append('價格站上MA20')
    elif ma20 and close < ma20:
        score -= 1
        signals.append('價格跌破MA20')

    # MACD 多空
    if macd and macd > 0:
        score += 1
        signals.append('MACD>0（多方）')
    elif macd and macd < 0:
        score -= 1
        signals.append('MACD<0（空方）')

    # RSI 超買超賣
    rsi_note = ''
    if rsi:
        if rsi > 70:
            score -= 1
            rsi_note = f'RSI={rsi:.1f}（超買警示）'
            signals.append(rsi_note)
        elif rsi < 30:
            score += 1
            rsi_note = f'RSI={rsi:.1f}（超賣反彈機會）'
            signals.append(rsi_note)
        else:
            signals.append(f'RSI={rsi:.1f}（中性）')

    if score >= 4:
        label = '強勢上漲'
        color = '#10b981'
    elif score >= 1:
        label = '偏多整理'
        color = '#6ee7b7'
    elif score <= -4:
        label = '弱勢下跌'
        color = '#ef4444'
    elif score <= -1:
        label = '偏空整理'
        color = '#fca5a5'
    else:
        label = '盤整'
        color = '#94a3b8'

    return {'label': label, 'color': color, 'score': score, 'signals': signals}


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
    綜合評估產生投資建議與評級
    評級：強力買進 / 買進 / 持有 / 減碼 / 賣出
    """
    score = trend.get('score', 0)
    rsi   = ind.get('rsi')
    macd  = ind.get('macd')
    macd_signal = ind.get('macd_signal')
    k     = ind.get('k')
    d     = ind.get('d')
    ma20  = ind.get('ma20')
    ma60  = ind.get('ma60')

    reasons_buy  = []
    reasons_sell = []
    risks        = []

    # ── 技術面評分 ─────────────────────────────────────────────
    tech_score = score  # -6 ~ +6

    # MACD 黃金/死亡交叉
    if macd and macd_signal:
        if macd > macd_signal:
            reasons_buy.append('MACD 黃金交叉，多方動能增強')
            tech_score += 1
        else:
            reasons_sell.append('MACD 死亡交叉，多方動能減弱')
            tech_score -= 1

    # KD 超買超賣
    if k and d:
        if k < 20 and d < 20:
            reasons_buy.append(f'KD 超賣區（K={k:.1f}），有反彈機會')
            tech_score += 1
        elif k > 80 and d > 80:
            reasons_sell.append(f'KD 超買區（K={k:.1f}），注意短線壓力')
            risks.append('KD 處於超買，短線漲幅受限')
            tech_score -= 1

    # 支撐/壓力
    price_range = resist - support
    if price_range > 0:
        pos_pct = round((close - support) / price_range * 100, 1)
    else:
        pos_pct = 50

    if pos_pct < 20:
        reasons_buy.append(f'接近近期支撐（{support}），風險相對低')
    elif pos_pct > 80:
        reasons_sell.append(f'接近近期壓力（{resist}），上漲空間受限')
        risks.append(f'股價已在近期高點附近（支撐/壓力位置：{pos_pct}%）')

    # ── 基本面加分/減分 ────────────────────────────────────────
    fund_score = 0
    pe = info.get('pe_ratio')
    pb = info.get('pb_ratio')
    roe = info.get('roe')

    if pe:
        if pe < 15:
            reasons_buy.append(f'本益比 {pe:.1f}x，估值相對合理')
            fund_score += 1
        elif pe > 30:
            risks.append(f'本益比 {pe:.1f}x，估值偏高')
            fund_score -= 1

    if pb and pb < 1.5:
        reasons_buy.append(f'股價淨值比 {pb:.2f}x，低於1.5倍')
        fund_score += 1

    if roe and roe > 0.15:
        reasons_buy.append(f'ROE {roe*100:.1f}%，獲利能力優異')
        fund_score += 1

    if div_yield:
        if div_yield >= 5:
            reasons_buy.append(f'殖利率 {div_yield:.2f}%，配息豐厚（高股息）')
            fund_score += 1
        elif div_yield >= 3:
            reasons_buy.append(f'殖利率 {div_yield:.2f}%，配息穩定')
        elif div_yield < 1:
            risks.append(f'殖利率 {div_yield:.2f}%，配息偏低')

    # ── 綜合評分 ───────────────────────────────────────────────
    total_score = tech_score + fund_score

    if total_score >= 6:
        rating = '強力買進'
        rating_color = '#065f46'
        rating_bg    = '#d1fae5'
        rating_icon  = '⬆⬆'
    elif total_score >= 3:
        rating = '買進'
        rating_color = '#15803d'
        rating_bg    = '#dcfce7'
        rating_icon  = '⬆'
    elif total_score >= 0:
        rating = '持有'
        rating_color = '#0369a1'
        rating_bg    = '#dbeafe'
        rating_icon  = '➡'
    elif total_score >= -3:
        rating = '減碼'
        rating_color = '#b45309'
        rating_bg    = '#fef3c7'
        rating_icon  = '⬇'
    else:
        rating = '賣出'
        rating_color = '#b91c1c'
        rating_bg    = '#fee2e2'
        rating_icon  = '⬇⬇'

    # 目標價（簡單估算）
    if ma20 and ma60:
        target_price = round((ma20 * 0.6 + ma60 * 0.4) * (1 + max(total_score, 0) * 0.03), 1)
    else:
        target_price = None

    summary = _build_summary(ticker, close, trend, rating, reasons_buy, reasons_sell,
                              risks, div_yield, info)

    return {
        'rating':       rating,
        'rating_color': rating_color,
        'rating_bg':    rating_bg,
        'rating_icon':  rating_icon,
        'total_score':  total_score,
        'tech_score':   tech_score,
        'fund_score':   fund_score,
        'reasons_buy':  reasons_buy,
        'reasons_sell': reasons_sell,
        'risks':        risks,
        'target_price': target_price,
        'support':      support,
        'resist':       resist,
        'price_position': pos_pct,
        'summary':      summary,
    }


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
    輸入: { tickers: ['2330','2317',...], n_sim: 5000 }
    輸出: 隨機模擬點、效率前緣、最小變異組合、最大夏普組合
    """
    try:
        import yfinance as yf
        from functools import reduce

        data      = request.json or {}
        tickers   = [t.strip().upper() for t in data.get('tickers', [])]
        n_sim     = min(int(data.get('n_sim', 3000)), 10000)
        period    = data.get('period', '2y')

        if len(tickers) < 2:
            return jsonify({'status':'error','message':'請至少輸入2支股票代碼'}), 400
        if len(tickers) > 15:
            return jsonify({'status':'error','message':'最多支援15支股票'}), 400

        # ── 取得股價資料 ─────────────────────────────────────
        prices = {}
        failed = []
        for tk in tickers:
            for suffix in ['.TW', '.TWO']:
                try:
                    hist = yf.Ticker(f"{tk}{suffix}").history(period=period, timeout=12)
                    if hist is not None and not hist.empty:
                        prices[tk] = hist['Close'].dropna()
                        break
                except Exception:
                    continue
            if tk not in prices:
                failed.append(tk)

        if failed:
            return jsonify({'status':'error',
                           'message': f'無法取得以下股票資料：{", ".join(failed)}'}), 422

        # ── 對齊日期、計算報酬率 ─────────────────────────────
        df = pd.DataFrame(prices).dropna()
        if len(df) < 30:
            return jsonify({'status':'error','message':'歷史資料不足（需至少30個交易日）'}), 422

        returns    = df.pct_change().dropna()
        mu         = (returns.mean() * 252).values          # 年化期望報酬
        cov        = (returns.cov() * 252).values            # 年化共變異數矩陣
        n          = len(tickers)

        # ── 蒙地卡羅隨機模擬 ─────────────────────────────────
        sim_ret, sim_risk, sim_sharpe, sim_weights = [], [], [], []
        risk_free = 0.02  # 無風險利率假設 2%

        np.random.seed(42)
        for _ in range(n_sim):
            w = np.random.dirichlet(np.ones(n))
            r = float(np.dot(w, mu))
            v = float(np.sqrt(reduce(np.dot, [w, cov, w.T])))
            s = (r - risk_free) / v if v > 0 else 0
            sim_ret.append(round(r, 6))
            sim_risk.append(round(v, 6))
            sim_sharpe.append(round(s, 4))
            sim_weights.append([round(x, 4) for x in w])

        # ── 最小變異組合（MVP）────────────────────────────────
        from scipy.optimize import minimize as sp_minimize

        def port_std(w):
            return float(np.sqrt(reduce(np.dot, [w, cov, w.T])))

        def port_ret(w):
            return float(np.dot(w, mu))

        def neg_sharpe(w):
            r = port_ret(w)
            s = port_std(w)
            return -(r - risk_free) / s if s > 0 else 0

        bounds      = tuple((0, 1) for _ in range(n))
        w0          = np.array([1/n] * n)
        eq_con      = {'type': 'eq', 'fun': lambda w: np.sum(w) - 1}

        mvp_res     = sp_minimize(port_std, w0, bounds=bounds, constraints=[eq_con])
        mvp_risk    = round(mvp_res.fun, 6)
        mvp_ret     = round(port_ret(mvp_res.x), 6)
        mvp_weights = [round(x, 4) for x in mvp_res.x]

        msp_res     = sp_minimize(neg_sharpe, w0, bounds=bounds, constraints=[eq_con])
        msp_risk    = round(port_std(msp_res.x), 6)
        msp_ret     = round(port_ret(msp_res.x), 6)
        msp_sharpe  = round(-msp_res.fun, 4)
        msp_weights = [round(x, 4) for x in msp_res.x]

        # ── 效率前緣曲線（最小化風險，固定報酬率）───────────
        ret_range  = np.linspace(mvp_ret, max(sim_ret) * 0.98, 40)
        ef_risks, ef_rets = [], []
        for target_r in ret_range:
            constraints = [eq_con, {'type':'eq','fun': lambda w,r=target_r: port_ret(w) - r}]
            res = sp_minimize(port_std, w0, bounds=bounds, constraints=constraints)
            if res.success:
                ef_risks.append(round(res.fun, 6))
                ef_rets.append(round(target_r, 6))

        # ── 各股個別風險報酬 ─────────────────────────────────
        stock_stats = []
        for i, tk in enumerate(tickers):
            from data_fetcher import STOCK_NAMES_ZH_BACKEND
            zh = STOCK_NAMES_ZH_BACKEND.get(tk, '')
            stock_stats.append({
                'ticker': tk,
                'name':   zh or tk,
                'ret':    round(float(mu[i]), 6),
                'risk':   round(float(np.sqrt(cov[i][i])), 6),
            })

        return jsonify({
            'status':      'success',
            'tickers':     tickers,
            'n_sim':       n_sim,
            'risk_free':   risk_free,
            'sim': {
                'risk':    sim_risk,
                'ret':     sim_ret,
                'sharpe':  sim_sharpe,
            },
            'ef': {
                'risk':    ef_risks,
                'ret':     ef_rets,
            },
            'mvp': {
                'risk':    mvp_risk,
                'ret':     mvp_ret,
                'weights': dict(zip(tickers, mvp_weights)),
            },
            'msp': {
                'risk':    msp_risk,
                'ret':     msp_ret,
                'sharpe':  msp_sharpe,
                'weights': dict(zip(tickers, msp_weights)),
            },
            'stocks':      stock_stats,
            'mu':          [round(float(x),6) for x in mu],
            'cov_diag':    [round(float(np.sqrt(cov[i][i])),6) for i in range(n)],
        })

    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'status':'error','message': str(e)}), 500


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


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("=" * 60)
    print("台灣ETF/股票投資分析系統啟動 (V4)")
    print(f"執行環境: {'Render (Production)' if os.environ.get('RENDER') else 'Local Development'}")
    print(f"Port: {port}")
    print("=" * 60)
    host = '0.0.0.0' if os.environ.get('RENDER') else '127.0.0.1'
    app.run(debug=False, host=host, port=port)
