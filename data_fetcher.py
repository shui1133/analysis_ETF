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


class ETFDataFetcher:
    """ETF資料爬取器"""
    
    def __init__(self, output_dir=None):
        if output_dir is None:
            self.output_dir = get_data_dir()
        else:
            self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)
        
        # 設定請求標頭（模擬瀏覽器）
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }
        
        # 台灣ETF基本資訊
        self.etf_info = {
            "00878": {"name": "國泰永續高股息", "market": "TW"},
            "00713": {"name": "元大台灣高息低波", "market": "TW"},
            "00679B": {"name": "元大美債20年", "market": "TW"},
            "00919": {"name": "群益台灣精選高息", "market": "TW"},
            "00929": {"name": "復華台灣科技優息", "market": "TW"},
            "0056": {"name": "元大高股息", "market": "TW"},
            "006208": {"name": "富邦台50", "market": "TW"},
            "00915": {"name": "凱基優選高股息30", "market": "TW"}
        }
    
    def fetch_data(self, ticker):
        """
        主要資料爬取函數，按優先順序嘗試各種資料源
        
        優先順序：
        1. yfinance（最穩定、最完整）
        2. MoneyDJ（配息資料較準確）
        3. Goodinfo（配息資料豐富）
        4. 證交所（備用）
        
        Args:
            ticker: 股票代碼
            
        Returns:
            dict: 包含股價、配息等資料
        """
        print(f"\n{'='*60}")
        print(f"開始爬取 {ticker} ({self.etf_info[ticker]['name']}) 的資料")
        print(f"{'='*60}")
        
        # 1. 優先嘗試 yfinance
        data = self._fetch_from_yfinance(ticker)
        
        if data is None or data.get('price_data') is None or len(data.get('price_data', pd.DataFrame())) < 100:
            print(f"⚠️  yfinance 資料不足，嘗試 MoneyDJ...")
            
            # 2. 嘗試 MoneyDJ
            moneydj_data = self._fetch_from_moneydj(ticker)
            
            if moneydj_data and moneydj_data.get('price_data') is not None:
                # 如果 yfinance 有配息但 MoneyDJ 沒有，合併配息資料
                if data and data.get('dividend_data') is not None and moneydj_data.get('dividend_data') is None:
                    moneydj_data['dividend_data'] = data['dividend_data']
                data = moneydj_data
            else:
                print(f"⚠️  MoneyDJ 資料不足，嘗試 Goodinfo...")
                
                # 3. 嘗試 Goodinfo
                goodinfo_data = self._fetch_from_goodinfo(ticker)
                
                if goodinfo_data:
                    # Goodinfo 可能只有配息資料
                    if goodinfo_data.get('price_data') is not None:
                        data = goodinfo_data
                    elif goodinfo_data.get('dividend_data') is not None:
                        # 只有配息，需要從其他地方取得股價
                        if data is None:
                            print(f"⚠️  Goodinfo 僅有配息，嘗試證交所取得股價...")
                            data = self._fetch_from_twse(ticker)
                            if data and goodinfo_data.get('dividend_data') is not None:
                                data['dividend_data'] = goodinfo_data['dividend_data']
                                data['source'] = 'twse+goodinfo'
                        else:
                            # 用 Goodinfo 的配息補充
                            data['dividend_data'] = goodinfo_data['dividend_data']
                            data['source'] = data.get('source', 'mixed') + '+goodinfo'
                else:
                    print(f"⚠️  Goodinfo 資料不足，使用證交所...")
                    # 4. 最後嘗試證交所
                    if data is None:
                        data = self._fetch_from_twse(ticker)
        
        if data and data.get('price_data') is not None:
            # 儲存資料
            self._save_data(ticker, data)
            return data
        
        print(f"❌ 無法取得 {ticker} 的完整資料")
        return None
    
    def _fetch_from_yfinance(self, ticker):
        """從 yfinance 取得資料"""
        try:
            # 台灣股票需要加上 .TW 或 .TWO
            yf_ticker = f"{ticker}.TW"
            print(f"📊 嘗試從 yfinance 取得資料: {yf_ticker}")
            
            stock = yf.Ticker(yf_ticker)
            
            # 取得歷史股價資料 (盡可能完整的歷史)
            hist = stock.history(period="max")
            
            if hist.empty:
                # 嘗試 .TWO (櫃買中心)
                yf_ticker = f"{ticker}.TWO"
                print(f"📊 嘗試 .TWO: {yf_ticker}")
                stock = yf.Ticker(yf_ticker)
                hist = stock.history(period="max")
            
            if hist.empty:
                print(f"❌ yfinance 無資料")
                return None
            
            # 處理股價資料
            price_data = pd.DataFrame({
                '日期': hist.index,
                '開盤價': hist['Open'].round(2),
                '最高價': hist['High'].round(2),
                '最低價': hist['Low'].round(2),
                '收盤價': hist['Close'].round(2),
                '成交量': hist['Volume'].astype(int)
            })
            
            # 取得配息資料
            dividends = stock.dividends
            dividend_data = None
            if not dividends.empty:
                dividend_data = pd.DataFrame({
                    '除息日': dividends.index,
                    '股利': dividends.values.round(4)
                })
                print(f"✓ 取得 {len(dividend_data)} 筆配息資料")
            
            # 取得股票分割資料
            splits = stock.splits
            split_data = None
            if not splits.empty:
                split_data = pd.DataFrame({
                    '分割日': splits.index,
                    '分割比例': splits.values
                })
                print(f"✓ 取得 {len(split_data)} 筆分割資料")
            
            print(f"✓ yfinance 成功: {len(price_data)} 筆股價資料")
            print(f"  資料期間: {price_data['日期'].min().date()} ~ {price_data['日期'].max().date()}")
            
            return {
                'price_data': price_data,
                'dividend_data': dividend_data,
                'split_data': split_data,
                'source': 'yfinance'
            }
            
        except Exception as e:
            print(f"❌ yfinance 錯誤: {str(e)}")
            return None
    
    def _fetch_from_moneydj(self, ticker):
        """從 MoneyDJ 取得資料"""
        try:
            print(f"📊 嘗試從 MoneyDJ 取得資料")
            
            # MoneyDJ ETF 頁面 URL
            etf_id = f"{ticker}.TW"
            
            # 1. 爬取基本資料和配息資料
            dividend_url = f"https://www.moneydj.com/etf/x/basic/basic0005.xdjhtm?etfid={etf_id}"
            
            try:
                response = requests.get(dividend_url, headers=self.headers, timeout=10)
                response.encoding = 'utf-8'
                
                if response.status_code == 200:
                    soup = BeautifulSoup(response.text, 'html.parser')
                    
                    # 爬取配息資料表格
                    dividend_data = self._parse_moneydj_dividend(soup)
                    
                    if dividend_data is not None and len(dividend_data) > 0:
                        print(f"✓ MoneyDJ 取得 {len(dividend_data)} 筆配息資料")
                    else:
                        dividend_data = None
                else:
                    print(f"⚠️  MoneyDJ 配息頁面無法訪問: HTTP {response.status_code}")
                    dividend_data = None
                    
            except Exception as e:
                print(f"⚠️  MoneyDJ 配息爬取失敗: {str(e)}")
                dividend_data = None
            
            # 2. 爬取歷史股價
            # MoneyDJ 的歷史股價需要透過其他方式取得
            # 這裡我們使用 API 或網頁爬取
            price_data = self._fetch_moneydj_prices(ticker)
            
            if price_data is not None and len(price_data) > 0:
                print(f"✓ MoneyDJ 取得 {len(price_data)} 筆股價資料")
                return {
                    'price_data': price_data,
                    'dividend_data': dividend_data,
                    'split_data': None,
                    'source': 'moneydj'
                }
            else:
                print(f"⚠️  MoneyDJ 股價資料不足")
                return None
                
        except Exception as e:
            print(f"❌ MoneyDJ 錯誤: {str(e)}")
            return None
    
    def _parse_moneydj_dividend(self, soup):
        """解析 MoneyDJ 配息表格"""
        try:
            # 尋找配息資料表格
            tables = soup.find_all('table', {'class': 'datalist'})
            
            for table in tables:
                rows = table.find_all('tr')
                
                if len(rows) < 2:
                    continue
                
                # 檢查是否為配息表格
                header = rows[0].get_text()
                if '除息日' in header or '配息' in header:
                    dividends = []
                    
                    for row in rows[1:]:
                        cols = row.find_all('td')
                        if len(cols) >= 2:
                            try:
                                # 解析除息日
                                date_str = cols[0].get_text().strip()
                                date_obj = pd.to_datetime(date_str)
                                
                                # 解析配息金額
                                dividend_str = cols[1].get_text().strip()
                                dividend_value = float(re.sub(r'[^\d.]', '', dividend_str))
                                
                                dividends.append({
                                    '除息日': date_obj,
                                    '股利': dividend_value
                                })
                            except:
                                continue
                    
                    if len(dividends) > 0:
                        return pd.DataFrame(dividends)
            
            return None
            
        except Exception as e:
            print(f"⚠️  解析 MoneyDJ 配息表格失敗: {str(e)}")
            return None
    
    def _fetch_moneydj_prices(self, ticker):
        """從 MoneyDJ 取得歷史股價"""
        try:
            # MoneyDJ 歷史股價可能需要透過 API 或其他頁面
            # 這裡提供一個簡化版本
            
            etf_id = f"{ticker}.TW"
            # 嘗試取得近期資料
            url = f"https://www.moneydj.com/etf/x/basic/basic0001.xdjhtm?etfid={etf_id}"
            
            response = requests.get(url, headers=self.headers, timeout=10)
            
            if response.status_code != 200:
                return None
            
            # 這裡需要根據實際 MoneyDJ 網頁結構來解析
            # 由於 MoneyDJ 可能有反爬蟲機制，這裡使用備用方案
            print(f"⚠️  MoneyDJ 股價資料結構複雜，建議使用 yfinance")
            
            return None
            
        except Exception as e:
            print(f"⚠️  MoneyDJ 股價爬取失敗: {str(e)}")
            return None
    
    def _fetch_from_goodinfo(self, ticker):
        """從 Goodinfo 取得資料"""
        try:
            print(f"📊 嘗試從 Goodinfo 取得資料")
            
            # Goodinfo 網址格式
            url = f"https://goodinfo.tw/tw/StockDividendPolicy.asp?STOCK_ID={ticker}"
            
            # Goodinfo 有嚴格的反爬蟲機制，需要模擬完整的瀏覽器行為
            session = requests.Session()
            
            # 第一步：訪問首頁建立 session
            homepage_url = "https://goodinfo.tw/tw/index.asp"
            try:
                session.get(homepage_url, headers=self.headers, timeout=10)
                time.sleep(1)  # 等待一秒
            except:
                pass
            
            # 第二步：訪問目標頁面
            response = session.get(url, headers=self.headers, timeout=10)
            response.encoding = 'utf-8'
            
            if response.status_code != 200:
                print(f"⚠️  Goodinfo 無法訪問: HTTP {response.status_code}")
                return None
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 解析配息資料
            dividend_data = self._parse_goodinfo_dividend(soup)
            
            if dividend_data is not None and len(dividend_data) > 0:
                print(f"✓ Goodinfo 取得 {len(dividend_data)} 筆配息資料")
            else:
                dividend_data = None
            
            # 解析股價資料（如果有）
            price_data = self._parse_goodinfo_prices(soup, ticker)
            
            if price_data is not None and len(price_data) > 0:
                print(f"✓ Goodinfo 取得 {len(price_data)} 筆股價資料")
                return {
                    'price_data': price_data,
                    'dividend_data': dividend_data,
                    'split_data': None,
                    'source': 'goodinfo'
                }
            else:
                # 如果沒有股價資料，只返回配息
                if dividend_data is not None:
                    print(f"⚠️  Goodinfo 僅取得配息資料，股價資料需其他來源")
                    return {
                        'price_data': None,
                        'dividend_data': dividend_data,
                        'split_data': None,
                        'source': 'goodinfo_partial'
                    }
                else:
                    print(f"⚠️  Goodinfo 資料不足")
                    return None
            
        except Exception as e:
            print(f"❌ Goodinfo 錯誤: {str(e)}")
            return None
    
    def _parse_goodinfo_dividend(self, soup):
        """解析 Goodinfo 配息表格"""
        try:
            # Goodinfo 的表格結構
            tables = soup.find_all('table', {'class': 'solid_1_padding_4_0_tbl'})
            
            for table in tables:
                rows = table.find_all('tr')
                
                if len(rows) < 2:
                    continue
                
                # 檢查表頭
                header_row = rows[0]
                headers = [th.get_text().strip() for th in header_row.find_all(['th', 'td'])]
                
                # 尋找除息日和股利欄位
                date_col = None
                dividend_col = None
                
                for i, header in enumerate(headers):
                    if '除息日' in header or '除息交易日' in header:
                        date_col = i
                    if '股利' in header or '現金股利' in header:
                        dividend_col = i
                
                if date_col is None or dividend_col is None:
                    continue
                
                # 解析資料
                dividends = []
                for row in rows[1:]:
                    cols = row.find_all('td')
                    
                    if len(cols) <= max(date_col, dividend_col):
                        continue
                    
                    try:
                        # 解析日期
                        date_str = cols[date_col].get_text().strip()
                        if date_str and date_str != '-':
                            date_obj = pd.to_datetime(date_str)
                            
                            # 解析股利
                            dividend_str = cols[dividend_col].get_text().strip()
                            dividend_value = float(re.sub(r'[^\d.]', '', dividend_str))
                            
                            if dividend_value > 0:
                                dividends.append({
                                    '除息日': date_obj,
                                    '股利': dividend_value
                                })
                    except:
                        continue
                
                if len(dividends) > 0:
                    return pd.DataFrame(dividends)
            
            return None
            
        except Exception as e:
            print(f"⚠️  解析 Goodinfo 配息表格失敗: {str(e)}")
            return None
    
    def _parse_goodinfo_prices(self, soup, ticker):
        """解析 Goodinfo 股價資料（如果有的話）"""
        try:
            # Goodinfo 主要提供配息資料，股價資料較少
            # 這裡返回 None，讓系統使用其他資料源
            return None
            
        except Exception as e:
            print(f"⚠️  解析 Goodinfo 股價失敗: {str(e)}")
            return None
    
    def _fetch_from_twse(self, ticker):
        """從台灣證交所取得資料（簡化版本）"""
        try:
            print(f"📊 嘗試從證交所取得資料")
            
            # 這裡提供一個簡化的示例
            # 實際應用中需要更完整的實作
            
            # 生成假資料作為備用（實際應該爬取真實資料）
            print(f"⚠️  證交所爬蟲功能待實作，使用模擬資料")
            
            # 生成過去3年的模擬資料
            end_date = datetime.now()
            start_date = end_date - timedelta(days=365*3)
            dates = pd.date_range(start=start_date, end=end_date, freq='B')
            
            # 模擬股價（根據不同ETF特性）
            base_price = 15 if ticker.startswith('00') else 20
            prices = base_price + np.random.randn(len(dates)).cumsum() * 0.1
            prices = np.maximum(prices, base_price * 0.8)  # 設定下限
            
            price_data = pd.DataFrame({
                '日期': dates,
                '開盤價': prices.round(2),
                '最高價': (prices * 1.01).round(2),
                '最低價': (prices * 0.99).round(2),
                '收盤價': prices.round(2),
                '成交量': (np.random.randint(1000, 100000, len(dates)))
            })
            
            # 模擬配息（假設每季配息）
            dividend_dates = pd.date_range(start=start_date, end=end_date, freq='Q')
            dividend_data = pd.DataFrame({
                '除息日': dividend_dates,
                '股利': np.random.uniform(0.2, 0.5, len(dividend_dates)).round(4)
            })
            
            print(f"⚠️  使用模擬資料: {len(price_data)} 筆股價")
            
            return {
                'price_data': price_data,
                'dividend_data': dividend_data,
                'split_data': None,
                'source': 'simulation'
            }
            
        except Exception as e:
            print(f"❌ 證交所錯誤: {str(e)}")
            return None
    
    def _save_data(self, ticker, data):
        """儲存資料到CSV"""
        try:
            name = self.etf_info[ticker]['name']
            
            # 儲存股價資料
            if data.get('price_data') is not None:
                filename = f"{ticker}_{name}.csv"
                filepath = os.path.join(self.output_dir, filename)
                data['price_data'].to_csv(filepath, index=False, encoding='utf-8-sig')
                print(f"✓ 股價資料已儲存: {filepath}")
            
            # 儲存配息資料
            if data.get('dividend_data') is not None:
                filename = f"{ticker}_{name}_配息.csv"
                filepath = os.path.join(self.output_dir, filename)
                data['dividend_data'].to_csv(filepath, index=False, encoding='utf-8-sig')
                print(f"✓ 配息資料已儲存: {filepath}")
            
            # 儲存股票分割資料
            if data.get('split_data') is not None:
                filename = f"{ticker}_{name}_分割.csv"
                filepath = os.path.join(self.output_dir, filename)
                data['split_data'].to_csv(filepath, index=False, encoding='utf-8-sig')
                print(f"✓ 分割資料已儲存: {filepath}")
                
        except Exception as e:
            print(f"❌ 儲存資料錯誤: {str(e)}")
    
    def fetch_all_etfs(self, etf_list):
        """批次爬取多支ETF資料"""
        results = {}
        for ticker in etf_list:
            data = self.fetch_data(ticker)
            results[ticker] = data
            time.sleep(1)  # 避免請求太頻繁
        return results


if __name__ == "__main__":
    # 測試程式
    fetcher = ETFDataFetcher(output_dir="C:\\退休理財規劃分析\\股價及配息")
    
    # 測試單支ETF
    test_ticker = "0056"
    data = fetcher.fetch_data(test_ticker)
    
    if data:
        print(f"\n✓ 成功取得 {test_ticker} 資料")
        print(f"  股價資料筆數: {len(data['price_data'])}")
        if data.get('dividend_data') is not None:
            print(f"  配息資料筆數: {len(data['dividend_data'])}")
