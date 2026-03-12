"""
Flask Web應用主程式 V3
台灣ETF投資分析系統 - 整合個股效益分析V3
"""

from flask import Flask, render_template, request, jsonify, send_file
from flask.json.provider import DefaultJSONProvider
import pandas as pd
import json
import os
import platform
import numpy as np
from data_fetcher import ETFDataFetcher, get_data_dir
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

# 全域變數
cached_results = {}
etf_memory_cache = {}


@app.route('/')
def index():
    """首頁"""
    return render_template('index.html')


@app.route('/api/fetch_data', methods=['POST'])
def fetch_data():
    """爬取ETF資料API"""
    try:
        data = request.json
        portfolio_type = data.get('portfolio_type', 'conservative')

        portfolio_etfs = {
            'conservative': ['00878', '00713', '00679B'],
            'balanced': ['00919', '00929', '0056'],
            'aggressive': ['006208', '00929', '00915']
        }

        etf_list = portfolio_etfs.get(portfolio_type, [])

        fetcher = ETFDataFetcher(output_dir=DATA_DIR)
        results = fetcher.fetch_all_etfs(etf_list)

        for etf_code, etf_data in results.items():
            if etf_data is not None:
                etf_memory_cache[etf_code] = etf_data

        success_count = sum(1 for r in results.values() if r is not None)

        return jsonify({
            'status': 'success',
            'message': f'成功爬取 {success_count}/{len(etf_list)} 支ETF資料',
            'results': {k: (v is not None) for k, v in results.items()}
        })

    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/fetch_custom', methods=['POST'])
def fetch_custom():
    """爬取自訂3支台灣上市股票/ETF資料"""
    try:
        data = request.json
        tickers_raw = data.get('tickers', [])

        if not tickers_raw or len(tickers_raw) != 3:
            return jsonify({'status': 'error', 'message': '請輸入恰好3支股票代碼'}), 400

        tickers = [t.strip().upper() for t in tickers_raw if t.strip()]
        if len(tickers) != 3:
            return jsonify({'status': 'error', 'message': '請輸入3支不重複的股票代碼'}), 400

        fetcher = ETFDataFetcher(output_dir=DATA_DIR)
        results = {}
        failed  = []

        for ticker in tickers:
            result = fetcher.fetch_custom_stock(ticker)
            if result and result.get('price_data'):
                results[ticker] = result
                etf_memory_cache[ticker] = result
            else:
                reason = getattr(fetcher, 'last_error', '')
                failed.append({'ticker': ticker, 'reason': reason})

        if failed:
            failed_codes = [f['ticker'] for f in failed]
            # 判斷是否為網路問題
            is_network = any('網路連線失敗' in f['reason'] for f in failed)
            if is_network:
                hint = '⚠️ 伺服器無法連接 Yahoo Finance，請確認 Render 環境允許對外連線，或稍後再試。'
            else:
                details = '、'.join(
                    f"{f['ticker']}（{f['reason'][:40]}）" if f['reason'] else f['ticker']
                    for f in failed
                )
                hint = f"無法取得股價資料：{details}。請確認代碼格式正確（台灣上市輸入如 2330、00878，不含 .TW）"
            return jsonify({
                'status': 'error',
                'message': hint,
                'failed': failed_codes,
                'success': list(results.keys())
            }), 422

        return jsonify({
            'status': 'success',
            'message': f'成功取得 {len(results)} 支股票資料',
            'tickers': tickers,
            'results': {k: True for k in results}
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'status': 'error', 'message': str(e)}), 500


def restore_cache_to_disk():
    """從記憶體快取還原資料到磁碟"""
    if not etf_memory_cache:
        return 0

    restored = 0
    for etf_code, etf_data in etf_memory_cache.items():
        try:
            if etf_data.get('price_data'):
                price_df = pd.DataFrame(etf_data['price_data'])
                price_df = price_df.rename(columns={'date': '日期', 'close': '收盤價'})
                price_path = os.path.join(DATA_DIR, f"{etf_code}_price.csv")
                price_df.to_csv(price_path, index=False, encoding='utf-8-sig')

            if etf_data.get('dividend_data'):
                div_df = pd.DataFrame(etf_data['dividend_data'])
                div_df = div_df.rename(columns={'date': '除息日', 'dividend': '股利'})
                div_path = os.path.join(DATA_DIR, f"{etf_code}_hist_配息.csv")
                div_df.to_csv(div_path, index=False, encoding='utf-8-sig')

            json_path = os.path.join(DATA_DIR, f"{etf_code}_data.json")
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(etf_data, f, ensure_ascii=False, indent=2)

            print(f"✅ 已從記憶體還原 {etf_code} 資料到磁碟")
            restored += 1

        except Exception as e:
            print(f"⚠️ 還原 {etf_code} 失敗: {e}")

    return restored


@app.route('/api/backtest', methods=['POST'])
def run_backtest():
    """執行回測API（使用V3版本）"""
    try:
        data = request.json

        portfolio_type = data.get('portfolio_type', 'conservative')
        initial_capital = int(data.get('initial_capital', 100)) * 10000
        monthly_investment = int(data.get('monthly_investment', 3)) * 10000
        current_age = int(data.get('current_age', 30))
        target_monthly_spend = int(data.get('target_monthly_spend', 4)) * 10000

        # 自訂模式
        custom_tickers = data.get('custom_tickers', None)
        custom_withdrawal_rate = float(data.get('custom_withdrawal_rate', 0.04))

        # 決定需要的 ETF 清單
        if portfolio_type == 'custom':
            if not custom_tickers or len(custom_tickers) != 3:
                return jsonify({'status': 'error', 'message': '自訂模式需提供3支股票代碼'}), 400
            needed_etfs = [t.strip().upper() for t in custom_tickers]
        else:
            portfolio_etfs = {
                'conservative': ['00878', '00713', '00679B'],
                'balanced': ['00919', '00929', '0056'],
                'aggressive': ['006208', '00929', '00915']
            }
            needed_etfs = portfolio_etfs.get(portfolio_type, [])

        missing = [
            etf for etf in needed_etfs
            if not os.path.exists(os.path.join(DATA_DIR, f"{etf}_price.csv"))
        ]

        if missing:
            print(f"⚠️ 磁碟缺少資料: {missing}，嘗試從記憶體還原...")
            restore_cache_to_disk()

            still_missing = [
                etf for etf in needed_etfs
                if not os.path.exists(os.path.join(DATA_DIR, f"{etf}_price.csv"))
            ]

            if still_missing:
                return jsonify({
                    'status': 'error',
                    'message': f'找不到 {still_missing} 的資料，請先點擊「查詢股票資料」'
                }), 400

        # 執行回測（使用V3）
        backtester = PortfolioBacktestV3(data_dir=DATA_DIR)
        result = backtester.backtest_portfolio(
            portfolio_type=portfolio_type,
            initial_capital=initial_capital,
            monthly_investment=monthly_investment,
            current_age=current_age,
            target_monthly_spend=target_monthly_spend,
            custom_tickers=custom_tickers,
            custom_withdrawal_rate=custom_withdrawal_rate,
        )

        if result is None:
            return jsonify({
                'status': 'error',
                'message': '回測失敗，請先點擊「爬取資料」後再執行回測'
            }), 400

        # 快取結果
        cached_results[portfolio_type] = result

        # 準備圖表資料（簡化版，用年度摘要）
        chart_data = prepare_chart_data_from_annual(result)

        # 準備表格資料
        table_data = prepare_table_data(result)

        # ── 蒙地卡羅 & 情境分析資料 ──────────────────────────
        mc_data        = result.get('monte_carlo', {})
        scenarios_data = result.get('scenarios', {})
        fp_data        = result.get('forecast_params', {})

        return jsonify({
            'status': 'success',
            'result': {
                'portfolio_name': result['portfolio_name'],
                'finish_year': result['finish_year'],
                'finish_age': result['finish_age'],
                'final_assets': round(result['final_assets']),
                'actual_invested': round(result['actual_invested']),
                'actual_dividend': round(result['actual_dividend']),
                'forecast_assets': round(result['forecast_assets']),
                'total_invested': round(result['total_invested']),
                'total_dividend': round(result['total_dividend']),
                'chart_data': chart_data,
                'table_data': table_data,
                'etf_weights': result['etf_weights'],
                'etf_details': [],
                'etf_tracking': result['etf_annual_tracking'],
                'hist_stats': {
                    t: {
                        'cagr': round(v['cagr'] * 100, 2),
                        'avg_div_per_share': round(v['avg_div_per_share'], 4),
                        'avg_div_times': round(v['avg_div_times'], 1),
                        'avg_price': round(v['avg_price'], 2),
                        'last_price': round(v['last_price'], 2)
                    }
                    for t, v in result['hist_stats'].items()
                },
                # ── 新功能 ──────────────────────────────────────
                'monte_carlo': mc_data,
                'scenarios': scenarios_data,
                'forecast_params': fp_data,
                # ── 退休後情境 ──────────────────────────────────
                'retirement_stop':     result.get('retirement_stop', []),
                'retirement_continue': result.get('retirement_continue', []),
                'retire_yr_idx':       result.get('retire_yr_idx'),
                'withdrawal_rate':     result.get('withdrawal_rate', 0.04),
            }
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/download_csv/<portfolio_type>')
def download_csv(portfolio_type):
    """下載CSV報表"""
    try:
        if portfolio_type not in cached_results:
            return jsonify({
                'status': 'error',
                'message': '請先執行回測'
            }), 400

        result = cached_results[portfolio_type]

        # 建立DataFrame
        df = pd.DataFrame(result['results']['annual_summary'])

        # 轉換為CSV
        output = io.StringIO()
        df.to_csv(output, index=False, encoding='utf-8-sig')
        output.seek(0)

        # 建立response
        return send_file(
            io.BytesIO(output.getvalue().encode('utf-8-sig')),
            mimetype='text/csv',
            as_attachment=True,
            download_name=f'{result["portfolio_name"]}_回測報表.csv'
        )

    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


def prepare_chart_data_from_annual(result):
    """從年度摘要準備圖表資料（含退休後兩條情境線）"""
    annual_summary     = result['results']['annual_summary']
    retirement_stop    = result.get('retirement_stop', [])
    retirement_cont    = result.get('retirement_continue', [])
    retire_yr_idx      = result.get('retire_yr_idx')   # 0-based in all_rows
    initial_capital    = result['initial_capital']
    monthly_investment = result['monthly_investment']
    inflation_target   = result.get('inflation_target', 0)

    # ── 時間軸長度 ────────────────────────────────────────────
    total_all = len(annual_summary)
    # 退休後情境的最大年序（以 stop 為準，stop/cont 長度相同）
    max_retire_seq = retirement_stop[-1]['年序'] if retirement_stop else 0
    total_points = max(total_all, max_retire_seq + 1)

    # ── 取樣策略（最多 20 個點，但必須含退休點）────────────────
    sample_interval = max(1, total_points // 20)
    sampled_set = set(range(0, total_points, sample_interval))
    sampled_set.add(0)
    sampled_set.add(total_points - 1)
    if retire_yr_idx is not None:
        sampled_set.add(retire_yr_idx)
    sorted_idx = sorted(sampled_set)

    # ── 退休後情境 lookup（key = 年序）────────────────────────
    stop_lk = {r['年序']: r for r in retirement_stop}
    cont_lk = {r['年序']: r for r in retirement_cont}

    # 找最後一筆 actual 行的 index
    last_actual_idx = max((i for i, r in enumerate(annual_summary)
                           if r['資料類型'] == 'actual'), default=-1)

    labels = []
    actual_series     = []
    forecast_series   = []
    stop_series       = []
    cont_series       = []
    threshold_series  = []

    for idx in sorted_idx:
        labels.append(f"第{idx}年")

        # actual（實際歷史行）
        if idx < total_all and idx <= last_actual_idx:
            actual_series.append(round(annual_summary[idx]['年末資產']))
        else:
            actual_series.append(None)

        # forecast（推估行，只到退休點）
        if (idx < total_all
                and idx > last_actual_idx
                and (retire_yr_idx is None or idx <= retire_yr_idx)):
            forecast_series.append(round(annual_summary[idx]['年末資產']))
        else:
            forecast_series.append(None)

        # 退休後-停止投入（退休點開始，含退休點本身作連線起點）
        if retire_yr_idx is not None and idx == retire_yr_idx:
            base = round(annual_summary[idx]['年末資產'])
            stop_series.append(base)
            cont_series.append(base)
        elif retire_yr_idx is not None and idx > retire_yr_idx:
            stop_series.append(stop_lk[idx]['年末資產'] if idx in stop_lk else None)
            cont_series.append(cont_lk[idx]['年末資產'] if idx in cont_lk else None)
        else:
            stop_series.append(None)
            cont_series.append(None)

        # 通膨門檻
        if idx < total_all:
            threshold_series.append(round(annual_summary[idx]['通膨門檻']))
        else:
            threshold_series.append(round(inflation_target * (1 + 0.03) ** idx))

    # ── 8% 年化報酬參考線 ─────────────────────────────────────
    r8 = (1 + 0.08) ** (1 / 12) - 1
    return_8_series = []
    for label in labels:
        year_num = int(label.replace('第', '').replace('年', ''))
        wealth = float(initial_capital)
        for _ in range(year_num * 12):
            wealth = (wealth + monthly_investment) * (1 + r8)
        return_8_series.append(round(wealth))

    return {
        'labels':               labels,
        'actual_assets':        actual_series,
        'forecast_assets':      forecast_series,
        'retire_stop_assets':   stop_series,
        'retire_cont_assets':   cont_series,
        'inflation_threshold':  threshold_series,
        'return_8_assets':      return_8_series,
        'start_year':           annual_summary[0]['年份'] if annual_summary else 2023,
    }


def prepare_table_data(result):
    """準備表格資料（與 backtest 計算邏輯完全一致）"""
    table_data = []
    annual_summary = result['results']['annual_summary']
    prev_end_assets = 0
    for i, row in enumerate(annual_summary):
        year_return_val = row['年度報酬']
        year_invested   = row['年度投入']
        year_end        = row['年末資產']
        year_dividend   = row['年度股利']

        # 年度投資報酬率 = 年度報酬 / (期初資產 + 年度投入)
        # 勾稽：年度報酬 = 年末資產 - 期初資產 - 年度投入
        base = prev_end_assets + year_invested
        if base > 0:
            year_return_rate = round(year_return_val / base * 100, 2)
        else:
            year_return_rate = 0.0

        # 前年末資產（期初資產），用於前端驗算勾稽
        beg_assets      = prev_end_assets
        prev_end_assets = year_end   # 更新為本年末，供下一年使用

        table_data.append({
            'year': row['年份'],
            'data_type': row['資料類型'],
            'year_invested':      f"{year_invested:,}",
            'year_dividend':      f"{year_dividend:,}",
            'year_return':        f"{year_return_val:,}",
            'year_return_rate':   year_return_rate,
            'year_end_assets':    f"{year_end:,}",
            'remaining_cash':     f"{row.get('剩餘現金', 0):,}",
            'inflation_threshold':f"{row['通膨門檻']:,}",
            'year_return_raw':    year_return_val,
            'beg_assets':         f"{beg_assets:,}",   # 期初資產，供勾稽用
        })
    return table_data


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))

    print("="*60)
    print("台灣ETF投資分析系統啟動 (V3 - 個股效益分析版 / backtest.py)")
    print(f"執行環境: {'Render (Production)' if os.environ.get('RENDER') else 'Local Development'}")
    print(f"Port: {port}")
    print("="*60)

    host = '0.0.0.0' if os.environ.get('RENDER') else '127.0.0.1'
    app.run(debug=False, host=host, port=port)
