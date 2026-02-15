"""
快速測試腳本
測試資料爬取和回測功能
"""

from data_fetcher import ETFDataFetcher
from backtest import PortfolioBacktest
import os

def test_system():
    """測試系統功能"""
    
    print("="*80)
    print("台灣ETF投資回測分析系統 - 快速測試")
    print("="*80)
    
    # 建立資料目錄
    data_dir = "data"
    os.makedirs(data_dir, exist_ok=True)
    
    # 1. 測試資料爬取
    print("\n【步驟 1】測試資料爬取功能")
    print("-"*80)
    
    fetcher = ETFDataFetcher(output_dir=data_dir)
    
    # 測試單支ETF
    test_ticker = "0056"
    print(f"\n測試爬取: {test_ticker} (元大高股息)")
    data = fetcher.fetch_data(test_ticker)
    
    if data and data.get('price_data') is not None:
        print(f"✓ 資料爬取成功")
        print(f"  股價資料: {len(data['price_data'])} 筆")
        if data.get('dividend_data') is not None:
            print(f"  配息資料: {len(data['dividend_data'])} 筆")
    else:
        print(f"✗ 資料爬取失敗")
        return
    
    # 2. 測試回測功能
    print("\n【步驟 2】測試回測分析功能")
    print("-"*80)
    
    # 為了測試，先爬取保守型投資組合的所有ETF
    conservative_etfs = ['00878', '00713', '00679B']
    print(f"\n爬取保守型投資組合 ETF: {', '.join(conservative_etfs)}")
    
    for ticker in conservative_etfs:
        if ticker == test_ticker:
            continue  # 已經爬取過
        print(f"  爬取 {ticker}...")
        fetcher.fetch_data(ticker)
    
    print("\n執行回測分析...")
    backtester = PortfolioBacktest(data_dir=data_dir)
    
    result = backtester.backtest_portfolio(
        portfolio_type="conservative",
        initial_capital=1000000,  # 100萬
        monthly_investment=30000,  # 3萬/月
        current_age=30,
        target_monthly_spend=40000  # 4萬/月
    )
    
    if result:
        print("\n✓ 回測分析完成")
        print("-"*80)
        print(f"投資組合: {result['portfolio_name']}")
        print(f"最終資產: {result['final_assets']:,.0f} 元")
        print(f"累計投入: {result['total_invested']:,.0f} 元")
        print(f"累計股利: {result['total_dividend']:,.0f} 元")
        print(f"投資報酬: {result['final_assets'] - result['total_invested']:,.0f} 元")
        
        if result['finish_year'] is not None:
            print(f"達成年份: {result['finish_year']} 年")
            print(f"退休年齡: {result['finish_age']} 歲")
        else:
            print("提醒: 在模擬期間內未達成目標")
        
        print("\n年度摘要 (前5年):")
        print("-"*80)
        for i, row in enumerate(result['results']['annual_summary'][:5]):
            print(f"{row['年份']}年: 資產 {row['年末資產']:,.0f} 元, "
                  f"股利 {row['年度股利']:,.0f} 元")
    else:
        print("✗ 回測分析失敗")
        return
    
    # 3. 測試總結
    print("\n【測試總結】")
    print("="*80)
    print("✓ 資料爬取功能正常")
    print("✓ 回測分析功能正常")
    print("\n系統測試完成！可以啟動 Web 介面了：")
    print("  python app.py")
    print("\n然後在瀏覽器開啟：http://127.0.0.1:5000")
    print("="*80)


if __name__ == "__main__":
    try:
        test_system()
    except Exception as e:
        print(f"\n測試過程發生錯誤: {str(e)}")
        import traceback
        traceback.print_exc()
