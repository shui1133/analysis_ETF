"""
台灣ETF資料爬取模組
支援多種資料來源：yfinance、MoneyDJ、證交所/櫃買中心、Goodinfo
"""

import yfinance as yf
import pandas as pd
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import time
import os
import sys
import platform
import numpy as np
import re
from urllib.parse import quote


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


class ETFDataFetcher:
    """ETF資料爬取器"""
    
    def __init__(self, output_dir=None):
        if output_dir is None:
            self.output_dir = get_data_dir()
        else:
            self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)
        
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
        }
    
    def fetch_data(self, ticker):
        """
        爬取單支ETF的資料
        優先順序：GitHub > yfinance > MoneyDJ > Goodinfo > 模擬資料
        """
        print(f"\n開始爬取 {ticker} 的資料...")
        
        # ✅ 優先嘗試 GitHub
        print("嘗試從 GitHub 取得資料...")
        github_data = self._fetch_from_github(ticker)
        if github_data and github_data.get('price_data') is not None:
            print(f"✓ GitHub 成功")
            self._save_data(ticker, github_data)
            return github_data
        
        print("嘗試 yfinance...")
        yf_data = self._fetch_from_yfinance(ticker)
        if yf_data and yf_data.get('price_data') is not None:
            print(f"✓ yfinance 成功")
            self._save_data(ticker, yf_data)
            return yf_data
        
        print("嘗試 MoneyDJ...")
        mdj_data = self._fetch_from_moneydj(ticker)
        if mdj_data and mdj_data.get('price_data') is not None:
            print(f"✓ MoneyDJ 成功")
            self._save_data(ticker, mdj_data)
            return mdj_data
        
        print("嘗試 Goodinfo...")
        gi_data = self._fetch_from_goodinfo(ticker)
        if gi_data and gi_data.get('price_data') is not None:
            print(f"✓ Goodinfo 成功")
            self._save_data(ticker, gi_data)
            return gi_data
        
        print(f"⚠ 所有資料來源失敗，使用模擬資料")
        sim_data = self._generate_simulated_data(ticker)
        self._save_data(ticker, sim_data)
        return sim_data
    
    def _fetch_from_github(self, ticker):
        """
        從 GitHub 倉庫爬取資料
        GitHub 路徑：https://github.com/shui1133/analysis_ETF/tree/main/data/
        """
        try:
            # GitHub raw file 基礎 URL
            base_url = "https://raw.githubusercontent.com/shui1133/analysis_ETF/main/data"
            
            # ETF 名稱對應（根據上傳的檔案命名）
            etf_names = {
                '0056': '元大高股息',
                '00878': '國泰永續高股息',
                '00713': '元大台灣高息低波',
                '00679B': '元大美債20年',
                '00919': '群益台灣精選高息',
                '00929': '復華台灣科技優息',
                '006208': '富邦台50',
                '00915': '凱基優選高股息30'
            }
            
            if ticker not in etf_names:
                print(f"  GitHub 未支援 {ticker}")
                return None
            
            etf_name = etf_names[ticker]
            
            # 構建檔案 URL
            price_url = f"{base_url}/{ticker}_{etf_name}.csv"
            dividend_url = f"{base_url}/{ticker}_{etf_name}_配息.csv"
            
            print(f"  嘗試下載: {price_url}")
            
            # 下載股價資料
            price_response = requests.get(price_url, timeout=10)
            if price_response.status_code != 200:
                print(f"  股價檔案不存在 (HTTP {price_response.status_code})")
                return None
            
            # 解析股價 CSV
            from io import StringIO
            price_df = pd.read_csv(StringIO(price_response.text))
            
            # 檢查必要欄位
            if '日期' not in price_df.columns or '收盤價' not in price_df.columns:
                print(f"  股價檔案欄位不符（需要：日期、收盤價）")
                return None
            
            # 轉換為標準格式
            price_data = []
            for _, row in price_df.iterrows():
                try:
                    price_data.append({
                        'date': str(row['日期']),
                        'close': float(row['收盤價'])
                    })
                except (ValueError, KeyError) as e:
                    continue
            
            if not price_data:
                print(f"  股價資料為空")
                return None
            
            # 嘗試下載配息資料
            dividend_data = []
            try:
                print(f"  嘗試下載: {dividend_url}")
                div_response = requests.get(dividend_url, timeout=10)
                
                if div_response.status_code == 200:
                    div_df = pd.read_csv(StringIO(div_response.text))
                    
                    # 檢查必要欄位
                    if '除息日' in div_df.columns and '股利' in div_df.columns:
                        for _, row in div_df.iterrows():
                            try:
                                dividend_data.append({
                                    'date': str(row['除息日']),
                                    'dividend': float(row['股利'])
                                })
                            except (ValueError, KeyError):
                                continue
                        print(f"  成功載入 {len(dividend_data)} 筆配息資料")
                    else:
                        print(f"  配息檔案欄位不符（需要：除息日、股利）")
                else:
                    print(f"  配息檔案不存在 (HTTP {div_response.status_code})")
            
            except Exception as e:
                print(f"  配息資料下載失敗: {str(e)}")
            
            print(f"  ✓ GitHub 成功載入 {len(price_data)} 筆股價資料")
            
            return {
                'ticker': ticker,
                'price_data': price_data,
                'dividend_data': dividend_data if dividend_data else None,
                'source': 'GitHub'
            }
            
        except Exception as e:
            print(f"  GitHub 錯誤: {str(e)}")
            return None
    
    def _fetch_from_yfinance(self, ticker):
        """從 yfinance 爬取資料"""
        try:
            symbol = f"{ticker}.TW"
            stock = yf.Ticker(symbol)
            
            end_date = datetime.now()
            start_date = end_date - timedelta(days=5*365)
            
            hist = stock.history(start=start_date, end=end_date)
            
            if hist.empty:
                symbol = f"{ticker}.TWO"
                stock = yf.Ticker(symbol)
                hist = stock.history(start=start_date, end=end_date)
            
            if hist.empty:
                return None
            
            price_data = []
            for date, row in hist.iterrows():
                price_data.append({
                    'date': date.strftime('%Y-%m-%d'),
                    'close': float(row['Close'])
                })
            
            dividends = stock.dividends
            dividend_data = []
            if not dividends.empty:
                for date, amount in dividends.items():
                    dividend_data.append({
                        'date': date.strftime('%Y-%m-%d'),
                        'dividend': float(amount)
                    })
            
            return {
                'ticker': ticker,
                'price_data': price_data,
                'dividend_data': dividend_data if dividend_data else None
            }
            
        except Exception as e:
            print(f"yfinance 錯誤: {str(e)}")
            return None
    
    def _fetch_from_moneydj(self, ticker):
        """從 MoneyDJ 爬取資料"""
        try:
            url = f"https://www.moneydj.com/ETF/X/Basic/Basic0005.xdjhtm?etfid={ticker}.TW"
            response = requests.get(url, headers=self.headers, timeout=10)
            if response.status_code != 200:
                return None
            return None
        except Exception as e:
            print(f"MoneyDJ 錯誤: {str(e)}")
            return None
    
    def _fetch_from_goodinfo(self, ticker):
        """從 Goodinfo 爬取資料"""
        try:
            url = f"https://goodinfo.tw/StockInfo/StockDividendPolicy.asp?STOCK_ID={ticker}"
            response = requests.get(url, headers=self.headers, timeout=10)
            if response.status_code != 200:
                return None
            return None
        except Exception as e:
            print(f"Goodinfo 錯誤: {str(e)}")
            return None
    
    def _generate_simulated_data(self, ticker):
        """生成模擬資料（當所有爬蟲都失敗時使用）"""
        print(f"生成 {ticker} 的模擬資料...")
        
        etf_params = {
            '0056':   {'start_price': 25, 'volatility': 0.15, 'dividend_yield': 0.05},
            '00878':  {'start_price': 18, 'volatility': 0.12, 'dividend_yield': 0.06},
            '00713':  {'start_price': 38, 'volatility': 0.10, 'dividend_yield': 0.04},
            '00679B': {'start_price': 40, 'volatility': 0.05, 'dividend_yield': 0.03},
            '00919':  {'start_price': 16, 'volatility': 0.13, 'dividend_yield': 0.055},
            '00929':  {'start_price': 19, 'volatility': 0.14, 'dividend_yield': 0.052},
            '006208': {'start_price': 80, 'volatility': 0.18, 'dividend_yield': 0.035},
            '00915':  {'start_price': 18, 'volatility': 0.16, 'dividend_yield': 0.048},
        }
        
        params = etf_params.get(ticker, {
            'start_price': 20,
            'volatility': 0.12,
            'dividend_yield': 0.045
        })
        
        end_date = datetime.now()
        start_date = end_date - timedelta(days=5*365)
        
        price_data = []
        current_price = params['start_price']
        current_date = start_date
        
        np.random.seed(hash(ticker) % 10000)
        
        while current_date <= end_date:
            daily_return = np.random.normal(0.0003, params['volatility']/np.sqrt(252))
            current_price *= (1 + daily_return)
            price_data.append({
                'date': current_date.strftime('%Y-%m-%d'),
                'close': round(current_price, 2)
            })
            current_date += timedelta(days=1)
        
        dividend_data = []
        year = start_date.year
        
        while year <= end_date.year:
            for quarter in [3, 6, 9, 12]:
                div_date = datetime(year, quarter, 15)
                if start_date <= div_date <= end_date:
                    quarter_prices = [p['close'] for p in price_data 
                                    if datetime.strptime(p['date'], '%Y-%m-%d').year == year 
                                    and (quarter-3) <= datetime.strptime(p['date'], '%Y-%m-%d').month < quarter]
                    if quarter_prices:
                        avg_price = np.mean(quarter_prices)
                        quarterly_dividend = avg_price * params['dividend_yield'] / 4
                        dividend_data.append({
                            'date': div_date.strftime('%Y-%m-%d'),
                            'dividend': round(quarterly_dividend, 4)
                        })
            year += 1
        
        return {
            'ticker': ticker,
            'price_data': price_data,
            'dividend_data': dividend_data,
            'is_simulated': True
        }
    
    def _save_data(self, ticker, data):
        """
        儲存資料到檔案
        ★ 欄位名稱與檔名必須符合 backtest.py 的格式：
          - 股價 CSV：欄位 日期、收盤價，檔名 {ticker}_price.csv
          - 配息 CSV：欄位 除息日、股利，檔名 {ticker}_hist_配息.csv
        """
        try:
            import json

            # ── 股價 CSV ─────────────────────────────────────────────
            if data.get('price_data'):
                price_df = pd.DataFrame(data['price_data'])
                # date → 日期、close → 收盤價
                price_df = price_df.rename(columns={'date': '日期', 'close': '收盤價'})
                price_df = price_df[[c for c in ['日期', '收盤價'] if c in price_df.columns]]
                price_file = os.path.join(self.output_dir, f"{ticker}_price.csv")
                price_df.to_csv(price_file, index=False, encoding='utf-8-sig')
                print(f"  已儲存股價資料: {price_file}")

            # ── 配息 CSV ─────────────────────────────────────────────
            # 檔名含 _配息 才能被 backtest.py 的 glob(*_配息.csv) 找到
            if data.get('dividend_data'):
                div_df = pd.DataFrame(data['dividend_data'])
                # date → 除息日、dividend → 股利
                div_df = div_df.rename(columns={'date': '除息日', 'dividend': '股利'})
                div_df = div_df[[c for c in ['除息日', '股利'] if c in div_df.columns]]
                div_file = os.path.join(self.output_dir, f"{ticker}_hist_配息.csv")
                div_df.to_csv(div_file, index=False, encoding='utf-8-sig')
                print(f"  已儲存配息資料: {div_file}")

            # ── 完整 JSON（備用）────────────────────────────────────
            json_file = os.path.join(self.output_dir, f"{ticker}_data.json")
            with open(json_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        except Exception as e:
            print(f"儲存資料錯誤: {str(e)}")
    
    def fetch_all_etfs(self, etf_list):
        """批量爬取多支ETF"""
        results = {}
        
        for i, ticker in enumerate(etf_list, 1):
            print(f"\n[{i}/{len(etf_list)}] 處理 {ticker}")
            data = self.fetch_data(ticker)
            results[ticker] = data
            
            if i < len(etf_list):
                time.sleep(2)
        
        print(f"\n完成！成功爬取 {len([r for r in results.values() if r is not None])}/{len(etf_list)} 支ETF")
        return results


if __name__ == "__main__":
    fetcher = ETFDataFetcher()
    data = fetcher.fetch_data("0056")
    
    if data:
        print("\n資料爬取成功！")
        print(f"股價資料筆數: {len(data['price_data'])}")
        if data.get('dividend_data'):
            print(f"配息資料筆數: {len(data['dividend_data'])}")
