"""
Flask Web應用主程式
台灣ETF投資分析系統
"""

from flask import Flask, render_template, request, jsonify, send_file
import pandas as pd
import json
import os
import platform
from data_fetcher import ETFDataFetcher, get_data_dir
from backtest import PortfolioBacktest
import io

app = Flask(__name__)

# 設定資料目錄（自動偵測作業系統）
DATA_DIR = get_data_dir()
print(f"資料目錄: {DATA_DIR}")
os.makedirs(DATA_DIR, exist_ok=True)

# 全域變數存放回測結果
cached_results = {}

# ✅ 記憶體快取：存放爬取到的原始資料（dict 格式）
# 格式：{ 'ETF代碼': {'ticker':..., 'price_data':[...], 'dividend_data':[...]} }
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

        # 取得對應投資組合的ETF列表
        portfolio_etfs = {
            'conservative': ['00878', '00713', '00679B'],
            'balanced': ['00919', '00929', '0056'],
            'aggressive': ['006208', '00929', '00915']
        }

        etf_list = portfolio_etfs.get(portfolio_type, [])

        # 爬取資料（fetch_all_etfs 內部已呼叫 _save_data 存到磁碟）
        fetcher = ETFDataFetcher(output_dir=DATA_DIR)
        results = fetcher.fetch_all_etfs(etf_list)

        # ✅ 同時存入記憶體快取（備用，防止 /tmp 被清空）
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


def restore_cache_to_disk():
    """
    ✅ 從記憶體快取還原資料到磁碟
    用於 Render 休眠重啟後 /tmp 被清空的情況
    欄位名稱與檔名必須符合 backtest.py 的格式
    """
    if not etf_memory_cache:
        return 0

    restored = 0
    for etf_code, etf_data in etf_memory_cache.items():
        try:
            # 還原股價 CSV：欄位 日期、收盤價
            if etf_data.get('price_data'):
                price_df = pd.DataFrame(etf_data['price_data'])
                price_df = price_df.rename(columns={'date': '日期', 'close': '收盤價'})
                price_path = os.path.join(DATA_DIR, f"{etf_code}_price.csv")
                price_df.to_csv(price_path, index=False, encoding='utf-8-sig')

            # 還原配息 CSV：欄位 除息日、股利，檔名含 _配息
            if etf_data.get('dividend_data'):
                div_df = pd.DataFrame(etf_data['dividend_data'])
                div_df = div_df.rename(columns={'date': '除息日', 'dividend': '股利'})
                div_path = os.path.join(DATA_DIR, f"{etf_code}_hist_配息.csv")
                div_df.to_csv(div_path, index=False, encoding='utf-8-sig')

            # 還原 JSON
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
    """執行回測API"""
    try:
        data = request.json

        portfolio_type = data.get('portfolio_type', 'conservative')
        initial_capital = int(data.get('initial_capital', 100)) * 10000
        monthly_investment = int(data.get('monthly_investment', 3)) * 10000
        current_age = int(data.get('current_age', 30))
        target_monthly_spend = int(data.get('target_monthly_spend', 4)) * 10000

        # ✅ 檢查磁碟是否有資料，沒有就從記憶體還原
        portfolio_etfs = {
            'conservative': ['00878', '00713', '00679B'],
            'balanced': ['00919', '00929', '0056'],
            'aggressive': ['006208', '00929', '00915']
        }
        needed_etfs = portfolio_etfs.get(portfolio_type, [])

        # 檢查是否有任何一支 ETF 的 CSV 不存在
        missing = [
            etf for etf in needed_etfs
            if not os.path.exists(os.path.join(DATA_DIR, f"{etf}_price.csv"))
        ]

        if missing:
            print(f"⚠️ 磁碟缺少資料: {missing}，嘗試從記憶體還原...")
            restore_cache_to_disk()

            # 還原後再次檢查
            still_missing = [
                etf for etf in needed_etfs
                if not os.path.exists(os.path.join(DATA_DIR, f"{etf}_price.csv"))
            ]

            if still_missing:
                return jsonify({
                    'status': 'error',
                    'message': f'找不到 {still_missing} 的資料，請先點擊「爬取資料」'
                }), 400

        # 執行回測
        backtester = PortfolioBacktest(data_dir=DATA_DIR)
        result = backtester.backtest_portfolio(
            portfolio_type=portfolio_type,
            initial_capital=initial_capital,
            monthly_investment=monthly_investment,
            current_age=current_age,
            target_monthly_spend=target_monthly_spend
        )

        if result is None:
            return jsonify({
                'status': 'error',
                'message': '回測失敗，請先點擊「爬取資料」後再執行回測'
            }), 400

        # 快取結果
        cached_results[portfolio_type] = result

        # 準備圖表資料
        chart_data = prepare_chart_data(result)

        # 準備表格資料
        table_data = prepare_table_data(result)

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
                'etf_details': result['results']['etf_details'],
                'etf_tracking': result['results']['etf_tracking'],
                'hist_stats': {
                    t: {
                        'cagr': round(v['cagr'] * 100, 2),
                        'avg_div_per_share': round(v['avg_div_per_share'], 4),
                        'avg_div_times': round(v['avg_div_times'], 1),
                        'avg_price': round(v['avg_price'], 2),
                        'last_price': round(v['last_price'], 2)
                    }
                    for t, v in result['hist_stats'].items()
                }
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


def prepare_chart_data(result):
    """準備圖表資料"""
    res = result['results']
    assets = res['total_assets']
    thresholds = res['inflation_threshold']
    data_types = res['data_type']
    total_n = len(assets)

    last_actual_idx = 0
    for i, dt in enumerate(data_types):
        if dt == 'actual':
            last_actual_idx = i

    sample_indices = list(range(0, total_n, 12))
    if (total_n - 1) not in sample_indices:
        sample_indices.append(total_n - 1)

    initial_capital = result['initial_capital']
    monthly_investment = result['monthly_investment']
    r_monthly_8 = (1 + 0.08) ** (1 / 12) - 1
    wealth_8 = []
    w = float(initial_capital)
    for m in range(total_n):
        if m > 0:
            w = (w + monthly_investment) * (1 + r_monthly_8)
        wealth_8.append(w)

    labels = []
    actual_series = []
    forecast_series = []
    threshold_series = []
    return_8_series = []

    for i in sample_indices:
        year_num = i // 12
        labels.append(f"第{year_num}年")
        threshold_series.append(round(thresholds[i]))
        return_8_series.append(round(wealth_8[i]))

        if i < last_actual_idx:
            actual_series.append(round(assets[i]))
            forecast_series.append(None)
        elif i <= last_actual_idx or data_types[i] == 'actual':
            actual_series.append(round(assets[i]))
            forecast_series.append(round(assets[i]))
        else:
            actual_series.append(None)
            forecast_series.append(round(assets[i]))

    return {
        'labels': labels,
        'inflation_threshold': threshold_series,
        'actual_assets': actual_series,
        'forecast_assets': forecast_series,
        'return_8_assets': return_8_series
    }


def prepare_table_data(result):
    """準備表格資料"""
    table_data = []
    for row in result['results']['annual_summary']:
        year_return_val = row['年度報酬']
        table_data.append({
            'year': row['年份'],
            'data_type': row['資料類型'],
            'year_invested': f"{row['年度投入']:,}",
            'year_dividend': f"{row['年度股利']:,}",
            'year_end_assets': f"{row['年末資產']:,}",
            'inflation_threshold': f"{row['通膨門檻']:,}",
            'year_return': f"{year_return_val:,}",
            'year_return_raw': year_return_val
        })
    return table_data


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))

    print("="*60)
    print("台灣ETF投資分析系統啟動")
    print(f"執行環境: {'Render (Production)' if os.environ.get('RENDER') else 'Local Development'}")
    print(f"Port: {port}")
    print("="*60)

    host = '0.0.0.0' if os.environ.get('RENDER') else '127.0.0.1'
    app.run(debug=False, host=host, port=port)
