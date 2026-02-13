"""
投資組合回測模組
根據歷史股價和配息進行真實回測
"""

import pandas as pd
import numpy as np
from datetime import datetime
import os
import platform


def get_data_dir():
    """
    根據作業系統自動選擇資料目錄
    Windows: C:\Python\退休理財規劃分析\data
    Linux/Mac: /data
    """
    if platform.system() == 'Windows':
        data_dir = r"C:\Python\退休理財規劃分析\data"
    else:
        data_dir = "/data"
    
    # 確保目錄存在
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


class PortfolioBacktest:
    """投資組合回測器"""
    
    def __init__(self, data_dir=None):
        if data_dir is None:
            self.data_dir = get_data_dir()
        else:
            self.data_dir = data_dir
        
        # 投資組合配置
        self.portfolios = {
            "conservative": {  # 保守型
                "name": "保守型投資者",
                "etfs": {
                    "00878": 0.40,
                    "00713": 0.30,
                    "00679B": 0.30
                },
                "withdraw_rate": 0.04
            },
            "balanced": {  # 穩健型
                "name": "穩健型投資者",
                "etfs": {
                    "00919": 0.35,
                    "00929": 0.40,
                    "0056": 0.25
                },
                "withdraw_rate": 0.06
            },
            "aggressive": {  # 積極型
                "name": "積極型投資者",
                "etfs": {
                    "006208": 0.30,
                    "00929": 0.50,
                    "00915": 0.20
                },
                "withdraw_rate": 0.08
            }
        }
    
    def load_etf_data(self, ticker):
        """載入ETF資料"""
        try:
            # 載入股價資料
            price_file = f"{ticker}_*.csv"
            import glob
            files = glob.glob(os.path.join(self.data_dir, price_file))
            
            if not files:
                print(f"⚠️  找不到 {ticker} 的股價資料")
                return None, None
            
            price_data = pd.read_csv(files[0], parse_dates=['日期'])
            
            # 載入配息資料
            dividend_file = f"{ticker}_*_配息.csv"
            div_files = glob.glob(os.path.join(self.data_dir, dividend_file))
            
            dividend_data = None
            if div_files:
                dividend_data = pd.read_csv(div_files[0], parse_dates=['除息日'])
            
            return price_data, dividend_data
            
        except Exception as e:
            print(f"❌ 載入 {ticker} 資料錯誤: {str(e)}")
            return None, None
    
    def backtest_portfolio(self, portfolio_type, initial_capital=1000000, 
                          monthly_investment=30000, current_age=30, 
                          target_monthly_spend=40000):
        """
        回測投資組合
        
        Args:
            portfolio_type: 投資組合類型 (conservative/balanced/aggressive)
            initial_capital: 啟動資金 (元)
            monthly_investment: 每月定期定額 (元)
            current_age: 目前年齡
            target_monthly_spend: 目標退休月領 (元)
        
        Returns:
            dict: 回測結果
        """
        print(f"\n{'='*60}")
        print(f"開始回測: {self.portfolios[portfolio_type]['name']}")
        print(f"{'='*60}")
        
        portfolio = self.portfolios[portfolio_type]
        etfs = portfolio['etfs']
        withdraw_rate = portfolio['withdraw_rate']
        
        # 載入所有ETF資料
        etf_data = {}
        earliest_date = None
        
        for ticker, weight in etfs.items():
            price_data, dividend_data = self.load_etf_data(ticker)
            if price_data is None:
                print(f"❌ 無法載入 {ticker} 資料，回測終止")
                return None
            
            etf_data[ticker] = {
                'price': price_data,
                'dividend': dividend_data,
                'weight': weight
            }
            
            # 找出最早的共同日期
            min_date = price_data['日期'].min()
            if earliest_date is None or min_date > earliest_date:
                earliest_date = min_date
        
        print(f"✓ 回測起始日期: {earliest_date.date()}")
        
        # 準備回測資料結構
        results = {
            'dates': [],
            'total_assets': [],  # 總資產
            'invested_amount': [],  # 累計投入金額
            'dividend_received': [],  # 累計收到股利
            'inflation_threshold': [],  # 通膨門檻
            'predicted_assets': [],  # 預期資產 (8%年化)
            'holdings': {ticker: [] for ticker in etfs.keys()},  # 各ETF持股數
            'annual_summary': []  # 年度摘要
        }
        
        # 初始化
        total_cash = initial_capital  # 可用現金
        holdings = {ticker: 0 for ticker in etfs.keys()}  # 持股數
        total_invested = initial_capital  # 累計投入
        total_dividend = 0  # 累計股利
        
        # 計算目標退休金
        r_inf = 0.03  # 通膨率
        target_base = (target_monthly_spend * 12) / withdraw_rate
        
        # 預期資產計算
        r_invest = 0.08  # 預期年化報酬
        predicted_wealth = initial_capital
        
        # 開始回測 - 按月
        current_date = earliest_date
        end_date = pd.Timestamp.now()
        month_count = 0
        year_count = 0
        last_year = current_date.year
        
        # 年度統計
        year_start_assets = 0
        year_invested = 0
        year_dividend = 0
        
        while current_date <= end_date:
            # 每月初投入資金
            if month_count > 0:  # 第0個月已經投入啟動資金
                total_cash += monthly_investment
                total_invested += monthly_investment
                year_invested += monthly_investment
            
            # 檢查是否有配息
            month_dividend = 0
            for ticker, data in etf_data.items():
                if data['dividend'] is not None:
                    # 找出本月的配息
                    month_divs = data['dividend'][
                        (data['dividend']['除息日'].dt.year == current_date.year) &
                        (data['dividend']['除息日'].dt.month == current_date.month)
                    ]
                    if len(month_divs) > 0:
                        div_per_share = month_divs['股利'].sum()
                        div_amount = holdings[ticker] * div_per_share
                        month_dividend += div_amount
                        total_dividend += div_amount
                        year_dividend += div_amount
                        total_cash += div_amount
            
            # 用可用現金買股票 (按權重分配)
            if total_cash > 0:
                for ticker, weight in etfs.items():
                    allocated_cash = total_cash * weight
                    
                    # 取得當月最後一個交易日的收盤價
                    price_df = etf_data[ticker]['price']
                    month_prices = price_df[
                        (price_df['日期'].dt.year == current_date.year) &
                        (price_df['日期'].dt.month == current_date.month)
                    ]
                    
                    if len(month_prices) > 0:
                        current_price = month_prices.iloc[-1]['收盤價']
                        if current_price > 0:
                            shares = int(allocated_cash / current_price / 1000) * 1000  # 台股以千股為單位
                            if shares > 0:
                                holdings[ticker] += shares
                
                total_cash = 0  # 全部投入
            
            # 計算當月總資產
            total_value = 0
            for ticker, shares in holdings.items():
                price_df = etf_data[ticker]['price']
                month_prices = price_df[
                    (price_df['日期'].dt.year == current_date.year) &
                    (price_df['日期'].dt.month == current_date.month)
                ]
                if len(month_prices) > 0:
                    current_price = month_prices.iloc[-1]['收盤價']
                    total_value += shares * current_price
            
            total_value += total_cash
            
            # 計算通膨門檻
            years_passed = month_count / 12
            inflation_target = target_base * (1 + r_inf) ** years_passed
            
            # 計算預期資產
            if month_count > 0:
                predicted_wealth = (predicted_wealth + monthly_investment * 12) * (1 + r_invest)
            
            # 記錄結果
            results['dates'].append(current_date)
            results['total_assets'].append(total_value)
            results['invested_amount'].append(total_invested)
            results['dividend_received'].append(total_dividend)
            results['inflation_threshold'].append(inflation_target)
            results['predicted_assets'].append(predicted_wealth)
            
            for ticker, shares in holdings.items():
                results['holdings'][ticker].append(shares)
            
            # 年度摘要
            if current_date.year != last_year or current_date >= end_date:
                if month_count > 0:
                    year_end_assets = total_value
                    year_return = year_end_assets - year_start_assets - year_invested
                    
                    results['annual_summary'].append({
                        '年份': last_year,
                        '年初資產': round(year_start_assets),
                        '年度投入': round(year_invested),
                        '年度股利': round(year_dividend),
                        '年度報酬': round(year_return),
                        '年末資產': round(year_end_assets),
                        '通膨門檻': round(target_base * (1 + r_inf) ** year_count)
                    })
                
                year_start_assets = total_value
                year_invested = 0
                year_dividend = 0
                year_count += 1
                last_year = current_date.year
            
            # 下個月
            month_count += 1
            if current_date.month == 12:
                current_date = pd.Timestamp(year=current_date.year + 1, month=1, day=1)
            else:
                current_date = pd.Timestamp(year=current_date.year, month=current_date.month + 1, day=1)
        
        # 判斷達成年份
        finish_year = None
        finish_age = None
        for i, (assets, threshold) in enumerate(zip(results['total_assets'], results['inflation_threshold'])):
            if assets >= threshold:
                finish_year = i // 12
                finish_age = current_age + finish_year
                break
        
        print(f"✓ 回測完成")
        print(f"  總資產: {total_value:,.0f} 元")
        print(f"  累計投入: {total_invested:,.0f} 元")
        print(f"  累計股利: {total_dividend:,.0f} 元")
        if finish_year is not None:
            print(f"  達成年份: {finish_year} 年 (退休年齡: {finish_age} 歲)")
        
        return {
            'portfolio_type': portfolio_type,
            'portfolio_name': portfolio['name'],
            'initial_capital': initial_capital,
            'monthly_investment': monthly_investment,
            'current_age': current_age,
            'target_monthly_spend': target_monthly_spend,
            'withdraw_rate': withdraw_rate,
            'finish_year': finish_year,
            'finish_age': finish_age,
            'final_assets': total_value,
            'total_invested': total_invested,
            'total_dividend': total_dividend,
            'results': results,
            'etf_weights': etfs
        }


if __name__ == "__main__":
    # 測試
    backtester = PortfolioBacktest(data_dir="data")
    result = backtester.backtest_portfolio(
        portfolio_type="conservative",
        initial_capital=1000000,
        monthly_investment=30000,
        current_age=30,
        target_monthly_spend=40000
    )
    
    if result:
        print(f"\n回測結果:")
        print(f"  最終資產: {result['final_assets']:,.0f}")
        print(f"  達成年份: {result['finish_year']}")
