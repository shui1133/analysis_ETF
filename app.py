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
                'portfolio_name': result['portfolio_name'],
                'finish_year': result['finish_year'],
                'finish_age': result['finish_age'],
                'final_assets': round(result['final_assets']),
                'total_invested': round(result['total_invested']),
                'total_dividend': round(result['total_dividend']),
                'chart_data': chart_data,
                'table_data': table_data,
                'etf_weights': result['etf_weights']
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
    results = result['results']
    
    # 按年度採樣 (減少資料點)
    dates = results['dates']
    sample_indices = [i for i in range(0, len(dates), 12)]  # 每年採樣一次
    
    return {
        'labels': [f"第{i//12}年" for i in sample_indices],
        'inflation_threshold': [round(results['inflation_threshold'][i]) for i in sample_indices],
        'predicted_assets': [round(results['predicted_assets'][i]) for i in sample_indices],
        'actual_assets': [round(results['total_assets'][i]) for i in sample_indices]
    }


def prepare_table_data(result):
    """準備表格資料"""
    annual_summary = result['results']['annual_summary']
    
    # 轉換為前端格式
    table_data = []
    for row in annual_summary:
        table_data.append({
            'year': row['年份'],
            'year_invested': f"{row['年度投入']:,}",
            'year_dividend': f"{row['年度股利']:,}",
            'year_end_assets': f"{row['年末資產']:,}",
            'inflation_threshold': f"{row['通膨門檻']:,}",
            'year_return': f"{row['年度報酬']:,}"
        })
    
    return table_data


if __name__ == '__main__':
    print("="*60)
    print("台灣ETF投資分析系統啟動")
    print("請在瀏覽器開啟: http://127.0.0.1:5000")
    print("="*60)
    app.run(debug=True, host='0.0.0.0', port=5000)
