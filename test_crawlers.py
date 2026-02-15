"""
爬蟲功能測試腳本
測試 yfinance, MoneyDJ, Goodinfo 等資料來源
"""

from data_fetcher import ETFDataFetcher, get_data_dir
import time

def test_data_sources():
    """測試各個資料來源"""
    
    print("="*80)
    print("資料來源測試")
    print("="*80)
    
    data_dir = get_data_dir()
    print(f"資料目錄: {data_dir}\n")
    
    fetcher = ETFDataFetcher(output_dir=data_dir)
    
    # 測試 ETF 列表
    test_etfs = {
        "0056": "元大高股息",
        "00878": "國泰永續高股息",
        "00929": "復華台灣科技優息"
    }
    
    results = {}
    
    for ticker, name in test_etfs.items():
        print(f"\n{'='*80}")
        print(f"測試 {ticker} ({name})")
        print(f"{'='*80}\n")
        
        # 測試各個資料來源
        test_results = {
            'yfinance': None,
            'moneydj': None,
            'goodinfo': None
        }
        
        # 1. 測試 yfinance
        print(f"\n【測試 yfinance】")
        try:
            yf_data = fetcher._fetch_from_yfinance(ticker)
            if yf_data:
                test_results['yfinance'] = {
                    'status': 'success',
                    'price_count': len(yf_data.get('price_data', [])),
                    'dividend_count': len(yf_data.get('dividend_data', [])) if yf_data.get('dividend_data') is not None else 0
                }
                print(f"✓ 成功")
                print(f"  股價資料: {test_results['yfinance']['price_count']} 筆")
                print(f"  配息資料: {test_results['yfinance']['dividend_count']} 筆")
            else:
                test_results['yfinance'] = {'status': 'failed'}
                print(f"✗ 失敗")
        except Exception as e:
            test_results['yfinance'] = {'status': 'error', 'message': str(e)}
            print(f"✗ 錯誤: {str(e)}")
        
        time.sleep(2)  # 避免請求太頻繁
        
        # 2. 測試 MoneyDJ
        print(f"\n【測試 MoneyDJ】")
        try:
            mdj_data = fetcher._fetch_from_moneydj(ticker)
            if mdj_data:
                test_results['moneydj'] = {
                    'status': 'success',
                    'price_count': len(mdj_data.get('price_data', [])) if mdj_data.get('price_data') is not None else 0,
                    'dividend_count': len(mdj_data.get('dividend_data', [])) if mdj_data.get('dividend_data') is not None else 0
                }
                print(f"✓ 成功")
                print(f"  股價資料: {test_results['moneydj']['price_count']} 筆")
                print(f"  配息資料: {test_results['moneydj']['dividend_count']} 筆")
            else:
                test_results['moneydj'] = {'status': 'failed'}
                print(f"✗ 失敗")
        except Exception as e:
            test_results['moneydj'] = {'status': 'error', 'message': str(e)}
            print(f"✗ 錯誤: {str(e)}")
        
        time.sleep(2)
        
        # 3. 測試 Goodinfo
        print(f"\n【測試 Goodinfo】")
        try:
            gi_data = fetcher._fetch_from_goodinfo(ticker)
            if gi_data:
                test_results['goodinfo'] = {
                    'status': 'success',
                    'price_count': len(gi_data.get('price_data', [])) if gi_data.get('price_data') is not None else 0,
                    'dividend_count': len(gi_data.get('dividend_data', [])) if gi_data.get('dividend_data') is not None else 0
                }
                print(f"✓ 成功")
                print(f"  股價資料: {test_results['goodinfo']['price_count']} 筆")
                print(f"  配息資料: {test_results['goodinfo']['dividend_count']} 筆")
            else:
                test_results['goodinfo'] = {'status': 'failed'}
                print(f"✗ 失敗")
        except Exception as e:
            test_results['goodinfo'] = {'status': 'error', 'message': str(e)}
            print(f"✗ 錯誤: {str(e)}")
        
        results[ticker] = test_results
        time.sleep(2)
    
    # 總結
    print(f"\n{'='*80}")
    print("測試總結")
    print(f"{'='*80}\n")
    
    for ticker, name in test_etfs.items():
        print(f"\n{ticker} ({name}):")
        test_result = results[ticker]
        
        for source, result in test_result.items():
            status = result.get('status', 'unknown')
            if status == 'success':
                print(f"  ✓ {source:10s}: 股價 {result.get('price_count', 0):4d} 筆, 配息 {result.get('dividend_count', 0):3d} 筆")
            elif status == 'failed':
                print(f"  ✗ {source:10s}: 無資料")
            else:
                print(f"  ✗ {source:10s}: 錯誤")
    
    print(f"\n{'='*80}")
    print("建議:")
    print("="*80)
    print("1. yfinance 是最穩定的資料來源，建議優先使用")
    print("2. MoneyDJ 和 Goodinfo 可能有反爬蟲機制")
    print("3. 如果所有來源都失敗，系統會使用模擬資料")
    print("4. 建議定期手動驗證重要的配息資料")
    print("="*80)


if __name__ == "__main__":
    try:
        test_data_sources()
    except Exception as e:
        print(f"\n測試過程發生錯誤: {str(e)}")
        import traceback
        traceback.print_exc()
