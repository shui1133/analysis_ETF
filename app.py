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
        
        # 爬取資料
        fetcher = ETFDataFetcher(output_dir=DATA_DIR)
        results = fetcher.fetch_all_etfs(etf_list)
        
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


@app.route('/api/backtest', methods=['POST'])
def run_backtest():
    """執行回測API"""
    try:
        data = request.json
        
        portfolio_type = data.get('portfolio_type', 'conservative')
        initial_capital = int(data.get('initial_capital', 100)) * 10000  # 萬元轉元
        monthly_investment = int(data.get('monthly_investment', 3)) * 10000
        current_age = int(data.get('current_age', 30))
        target_monthly_spend = int(data.get('target_monthly_spend', 4)) * 10000
        
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
                'message': '回測失敗，請先爬取資料'
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
                'portfolio_name':    result['portfolio_name'],
                'finish_year':       result['finish_year'],
                'finish_age':        result['finish_age'],
                # 歷史實際
                'final_assets':      round(result['final_assets']),
                'actual_invested':   round(result['actual_invested']),
                'actual_dividend':   round(result['actual_dividend']),
                # 推估最終
                'forecast_assets':   round(result['forecast_assets']),
                'total_invested':    round(result['total_invested']),
                'total_dividend':    round(result['total_dividend']),
                # 圖表 & 表格
                'chart_data':        chart_data,
                'table_data':        table_data,
                'etf_weights':       result['etf_weights'],
                # 歷史統計數據（CAGR、平均股利等）
                'hist_stats': {
                    t: {
                        'cagr':              round(v['cagr'] * 100, 2),
                        'avg_div_per_share': round(v['avg_div_per_share'], 4),
                        'avg_div_times':     round(v['avg_div_times'], 1),
                        'last_price':        round(v['last_price'], 2)
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
        
        # 建立DataFrame（含資料類型欄位）
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
    """
    準備圖表資料：
      actual_assets       -> 歷史實際線（含交界點，之後為 null）
      forecast_assets     -> 推估線（從交界點開始，之前為 null）
      inflation_threshold -> 通膨門檻（完整覆蓋歷史+推估）
      return_8_assets     -> 8% 年化報酬預期線（月複利）

    銜接策略：找到最後一筆 actual 月資料的精確索引，
    在該索引同時對 actual 與 forecast 都填值，兩線在此相連。
    """
    res        = result['results']
    assets     = res['total_assets']
    thresholds = res['inflation_threshold']
    data_types = res['data_type']
    total_n    = len(assets)

    # 找出最後一筆 actual 的月索引
    last_actual_idx = 0
    for i, dt in enumerate(data_types):
        if dt == 'actual':
            last_actual_idx = i

    # 每12個月採樣，確保最後一個點也被包含
    sample_indices = list(range(0, total_n, 12))
    if (total_n - 1) not in sample_indices:
        sample_indices.append(total_n - 1)

    # 8% 年化報酬：月複利，每月再投入 monthly_investment
    initial_capital    = result['initial_capital']
    monthly_investment = result['monthly_investment']
    r_monthly_8 = (1 + 0.08) ** (1 / 12) - 1
    wealth_8 = []
    w = float(initial_capital)
    for m in range(total_n):
        if m > 0:
            w = (w + monthly_investment) * (1 + r_monthly_8)
        wealth_8.append(w)

    labels           = []
    actual_series    = []
    forecast_series  = []
    threshold_series = []
    return_8_series  = []

    for i in sample_indices:
        year_num = i // 12
        labels.append(f"第{year_num}年")
        threshold_series.append(round(thresholds[i]))
        return_8_series.append(round(wealth_8[i]))

        if i < last_actual_idx:
            # 純歷史段
            actual_series.append(round(assets[i]))
            forecast_series.append(None)
        elif i <= last_actual_idx or data_types[i] == 'actual':
            # 交界點：兩線同時填值，視覺上相連
            actual_series.append(round(assets[i]))
            forecast_series.append(round(assets[i]))
        else:
            # 純推估段
            actual_series.append(None)
            forecast_series.append(round(assets[i]))

    return {
        'labels':              labels,
        'inflation_threshold': threshold_series,
        'actual_assets':       actual_series,
        'forecast_assets':     forecast_series,
        'return_8_assets':     return_8_series
    }


def prepare_table_data(result):
    """準備表格資料（含資料類型欄位）"""
    table_data = []
    for row in result['results']['annual_summary']:
        year_return_val = row['年度報酬']
        table_data.append({
            'year':               row['年份'],
            'data_type':          row['資料類型'],           # 'actual' | 'forecast'
            'year_invested':      f"{row['年度投入']:,}",
            'year_dividend':      f"{row['年度股利']:,}",
            'year_end_assets':    f"{row['年末資產']:,}",
            'inflation_threshold': f"{row['通膨門檻']:,}",
            'year_return':        f"{year_return_val:,}",
            'year_return_raw':    year_return_val            # 給前端判斷正負色
        })
    return table_data


if __name__ == '__main__':
    # 從環境變數取得 PORT，Render 會自動設定
    port = int(os.environ.get('PORT', 5000))
    
    print("="*60)
    print("台灣ETF投資分析系統啟動")
    print(f"執行環境: {'Render (Production)' if os.environ.get('RENDER') else 'Local Development'}")
    print(f"Port: {port}")
    print("="*60)
    
    # Render 環境使用 0.0.0.0，本地開發使用 127.0.0.1
    host = '0.0.0.0' if os.environ.get('RENDER') else '127.0.0.1'
    
    app.run(debug=False, host=host, port=port)
