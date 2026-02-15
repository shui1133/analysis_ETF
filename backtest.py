"""
投資組合回測模組
根據歷史股價和配息進行真實回測,並以歷史統計推估未來
"""

import pandas as pd
import numpy as np
from datetime import datetime
import os
import platform


def get_data_dir():
    """
    根據環境自動選擇資料目錄
    Render/Production: 使用 /tmp/data (暫存)
    Windows 開發: C:\Python\退休理財規劃分析\data
    其他: ./data (當前目錄下)
    """
    # 檢查是否在 Render 或其他雲端環境
    if os.environ.get('RENDER'):
        data_dir = "/tmp/data"  # Render 的暫存目錄
    elif platform.system() == 'Windows':
        data_dir = r"C:\Python\退休理財規劃分析\data"
    else:
        # 本地開發環境,使用相對路徑
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


class PortfolioBacktest:
    """投資組合回測器"""

    def __init__(self, data_dir=None):
        if data_dir is None:
            self.data_dir = get_data_dir()
        else:
            self.data_dir = data_dir

        self.portfolios = {
            "conservative": {
                "name": "保守型投資者",
                "etfs": {"00878": 0.40, "00713": 0.30, "00679B": 0.30},
                "withdraw_rate": 0.04
            },
            "balanced": {
                "name": "穩健型投資者",
                "etfs": {"00919": 0.35, "00929": 0.40, "0056": 0.25},
                "withdraw_rate": 0.06
            },
            "aggressive": {
                "name": "積極型投資者",
                "etfs": {"006208": 0.30, "00929": 0.50, "00915": 0.20},
                "withdraw_rate": 0.08
            }
        }

    # ──────────────────────────────────────────────────────────────
    def load_etf_data(self, ticker):
        """載入ETF資料"""
        try:
            import glob
            
            # 修正：明確指定只讀取 _price.csv 檔案
            price_file = os.path.join(self.data_dir, f"{ticker}_price.csv")
            
            if not os.path.exists(price_file):
                # 嘗試舊格式（向後相容）
                files = glob.glob(os.path.join(self.data_dir, f"{ticker}_*.csv"))
                # 過濾掉配息檔案
                files = [f for f in files if '_配息' not in f and '_hist_配息' not in f]
                
                if not files:
                    print(f"⚠️  找不到 {ticker} 的股價資料")
                    return None, None
                
                price_file = files[0]
            
            # 讀取股價資料
            price_data = pd.read_csv(price_file, parse_dates=['日期'])
            if price_data['日期'].dtype.tz is not None:
                price_data['日期'] = price_data['日期'].dt.tz_localize(None)

            # 讀取配息資料
            div_files = glob.glob(os.path.join(self.data_dir, f"{ticker}_*配息.csv"))
            dividend_data = None
            if div_files:
                dividend_data = pd.read_csv(div_files[0], parse_dates=['除息日'])
                if dividend_data['除息日'].dtype.tz is not None:
                    dividend_data['除息日'] = dividend_data['除息日'].dt.tz_localize(None)

            return price_data, dividend_data
        except Exception as e:
            print(f"❌ 載入 {ticker} 資料錯誤: {str(e)}")
            import traceback
            traceback.print_exc()
            return None, None

    # ──────────────────────────────────────────────────────────────
    def _calc_historical_stats(self, etf_data):
        """
        計算各ETF歷史統計：
          - CAGR（年化股價成長率）
          - 平均每股股利金額（所有歷史配息的算術平均）
          - 年均配息次數
          - 歷史平均股價（用於推估配息計算）
          - 歷史末日收盤價（作為推估起點股價）
        """
        stats = {}
        for ticker, data in etf_data.items():
            price_df = data['price'].sort_values('日期')
            div_df   = data['dividend']

            # CAGR
            p_start = price_df.iloc[0]['收盤價']
            p_end   = price_df.iloc[-1]['收盤價']
            n_years = (price_df.iloc[-1]['日期'] - price_df.iloc[0]['日期']).days / 365.25
            cagr = (p_end / p_start) ** (1 / n_years) - 1 if (n_years > 0 and p_start > 0) else 0.05

            # 歷史平均股價
            avg_price = float(price_df['收盤價'].mean())

            # 平均股利 & 年均次數
            if div_df is not None and len(div_df) > 0:
                avg_div_per_share = float(div_df['股利'].mean())
                div_years = (div_df['除息日'].max() - div_df['除息日'].min()).days / 365.25
                avg_div_times = len(div_df) / div_years if div_years >= 1 else float(len(div_df))
            else:
                avg_div_per_share = 0.0
                avg_div_times     = 0.0

            stats[ticker] = {
                'cagr':              cagr,
                'avg_div_per_share': avg_div_per_share,
                'avg_div_times':     avg_div_times,
                'avg_price':         avg_price,
                'last_price':        float(price_df.iloc[-1]['收盤價'])
            }
            print(f"  [{ticker}] CAGR={cagr*100:.2f}%  "
                  f"平均每股股利={avg_div_per_share:.4f}  "
                  f"年均配息次數={avg_div_times:.1f}  "
                  f"歷史平均股價={avg_price:.2f}  "
                  f"末日收盤={stats[ticker]['last_price']:.2f}")
        return stats

    # ──────────────────────────────────────────────────────────────
    def backtest_portfolio(self, portfolio_type, initial_capital=1000000,
                           monthly_investment=30000, current_age=30,
                           target_monthly_spend=40000):
        """
        第一段：歷史真實回測（從ETF上市日→今天）
        第二段：推估（從今天→達成通膨門檻,最多30年）
        """
        print(f"\n{'='*60}")
        print(f"開始回測: {self.portfolios[portfolio_type]['name']}")
        print(f"{'='*60}")

        portfolio     = self.portfolios[portfolio_type]
        etfs          = portfolio['etfs']
        withdraw_rate = portfolio['withdraw_rate']
        r_inf         = 0.03
        target_base   = (target_monthly_spend * 12) / withdraw_rate

        # ── 載入資料 ──────────────────────────────────────────────
        etf_data     = {}
        earliest_date = None
        for ticker, weight in etfs.items():
            price_data, dividend_data = self.load_etf_data(ticker)
            if price_data is None:
                print(f"❌ 無法載入 {ticker},回測終止")
                return None
            etf_data[ticker] = {'price': price_data, 'dividend': dividend_data, 'weight': weight}
            min_date = price_data['日期'].min()
            if earliest_date is None or min_date > earliest_date:
                earliest_date = min_date

        if hasattr(earliest_date, 'tz') and earliest_date.tz is not None:
            earliest_date = earliest_date.tz_localize(None)
        print(f"✓ 回測起始日期: {earliest_date.date()}")

        # ── 歷史統計 ──────────────────────────────────────────────
        print("\n計算歷史統計：")
        hist_stats = self._calc_historical_stats(etf_data)

        # ── 結果容器 ──────────────────────────────────────────────
        results = {
            'dates': [], 'total_assets': [], 'invested_amount': [],
            'dividend_received': [], 'inflation_threshold': [],
            'data_type': [],       # 'actual' | 'forecast'
            'annual_summary': [],
            'etf_details': []      # 新增：每年各ETF持倉明細
        }

        # ================================================================
        # 第一段：歷史真實回測
        # ================================================================
        print("\n執行歷史回測...")
        holdings = {t: 0 for t in etfs}
        cash = float(initial_capital)

        total_invested = float(initial_capital)
        total_dividend = 0.0
        year_start     = float(initial_capital)
        year_inv       = float(initial_capital)
        year_div       = 0.0
        year_count     = 0
        last_year      = earliest_date.year
        finish_year    = None
        finish_age     = None

        current_date = earliest_date
        today_ts     = pd.Timestamp.now().normalize()
        if hasattr(today_ts, 'tz') and today_ts.tz is not None:
            today_ts = today_ts.tz_localize(None)

        month_count = 0

        while current_date <= today_ts:
            month_count += 1

            # 1) 定期定額
            if month_count > 1:
                cash           += monthly_investment
                total_invested += monthly_investment
                year_inv       += monthly_investment

            # 2) 配息（若存在記錄）
            for ticker, etf_info in etf_data.items():
                div_df = etf_info['dividend']
                if div_df is not None:
                    mask = (div_df['除息日'].dt.year == current_date.year) & \
                           (div_df['除息日'].dt.month == current_date.month)
                    matched = div_df[mask]
                    for _, row in matched.iterrows():
                        div_amount     = holdings[ticker] * row['股利']
                        cash           += div_amount
                        total_dividend += div_amount
                        year_div       += div_amount

            # 3) 買股（以當月收盤價計算）
            if cash > 0:
                for ticker, weight in etfs.items():
                    alloc = cash * weight
                    price_df = etf_data[ticker]['price']
                    row = price_df[
                        (price_df['日期'].dt.year == current_date.year) &
                        (price_df['日期'].dt.month == current_date.month)
                    ]
                    if not row.empty:
                        price = float(row.iloc[-1]['收盤價'])
                        if price > 0:
                            shares = int(alloc / price / 1000) * 1000
                            if shares > 0:
                                holdings[ticker] += shares
                cash = 0.0

            # 4) 總資產（月末市值）
            total_assets = 0.0
            for ticker, etf_info in etf_data.items():
                price_df = etf_info['price']
                row = price_df[
                    (price_df['日期'].dt.year == current_date.year) &
                    (price_df['日期'].dt.month == current_date.month)
                ]
                if not row.empty:
                    price = float(row.iloc[-1]['收盤價'])
                    total_assets += holdings[ticker] * price
            total_assets += cash

            # 5) 通膨門檻
            inf_target = target_base * (1 + r_inf) ** (month_count / 12)

            results['dates'].append(current_date)
            results['total_assets'].append(total_assets)
            results['invested_amount'].append(total_invested)
            results['dividend_received'].append(total_dividend)
            results['inflation_threshold'].append(inf_target)
            results['data_type'].append('actual')

            # 6) 年度摘要
            if current_date.year != last_year:
                year_return = total_assets - year_start - year_inv
                results['annual_summary'].append({
                    '年份':    last_year,
                    '年初資產': round(year_start),
                    '年度投入': round(year_inv),
                    '年度股利': round(year_div),
                    '年度報酬': round(year_return),
                    '年末資產': round(total_assets),
                    '通膨門檻': round(inf_target),
                    '資料類型': 'actual'
                })
                
                # 收集各ETF年末持倉明細（歷史實際）
                etf_detail = {'年份': last_year, '資料類型': 'actual', 'etfs': {}}
                for ticker in etfs:
                    price_df = etf_data[ticker]['price']
                    row = price_df[price_df['日期'].dt.year == last_year]
                    if not row.empty:
                        year_end_price = float(row.iloc[-1]['收盤價'])
                        etf_detail['etfs'][ticker] = {
                            '持股數': holdings[ticker],
                            '股價': round(year_end_price, 2),
                            '市值': round(holdings[ticker] * year_end_price),
                            '權重': etfs[ticker]
                        }
                results['etf_details'].append(etf_detail)
                
                year_start = total_assets
                year_inv   = 0.0
                year_div   = 0.0
                year_count += 1
                last_year  = current_date.year

            # 7) 達成判斷
            if finish_year is None and total_assets >= inf_target:
                finish_year = month_count // 12
                finish_age  = current_age + finish_year
                print(f"  ✓ 已達成目標！第 {finish_year} 年（{finish_age} 歲）")

            # 8) 下一個月
            if current_date.month == 12:
                current_date = pd.Timestamp(year=current_date.year + 1, month=1, day=1)
            else:
                current_date = pd.Timestamp(year=current_date.year,
                                             month=current_date.month + 1, day=1)

        # 歷史回測最後一年（若不完整）
        if year_inv > 0:
            year_return = total_assets - year_start - year_inv
            results['annual_summary'].append({
                '年份':    last_year,
                '年初資產': round(year_start),
                '年度投入': round(year_inv),
                '年度股利': round(year_div),
                '年度報酬': round(year_return),
                '年末資產': round(total_assets),
                '通膨門檻': round(inf_target),
                '資料類型': 'actual'
            })
            
            # 收集最後一年的ETF明細（歷史實際）
            etf_detail = {'年份': last_year, '資料類型': 'actual', 'etfs': {}}
            for ticker in etfs:
                price_df = etf_data[ticker]['price']
                row = price_df[price_df['日期'].dt.year == last_year]
                if not row.empty:
                    year_end_price = float(row.iloc[-1]['收盤價'])
                    etf_detail['etfs'][ticker] = {
                        '持股數': holdings[ticker],
                        '股價': round(year_end_price, 2),
                        '市值': round(holdings[ticker] * year_end_price),
                        '權重': etfs[ticker]
                    }
            results['etf_details'].append(etf_detail)

        actual_final_assets   = total_assets
        actual_total_invested = total_invested
        actual_total_dividend = total_dividend
        actual_months         = month_count

        print(f"✓ 歷史回測完成  期末資產: {actual_final_assets:,.0f} 元  "
              f"累計投入: {actual_total_invested:,.0f}  累計股利: {actual_total_dividend:,.0f}")

        # ================================================================
        # 第二段：推估（若尚未達標）
        # ================================================================
        if finish_year is None:
            # 推估股價（以歷史末日價為起點）
            forecast_price = {t: hist_stats[t]['last_price'] for t in etfs}
            monthly_growth = {t: (1 + hist_stats[t]['cagr']) ** (1/12) - 1 for t in etfs}

            fc_holdings       = dict(holdings)
            fc_cash           = 0.0
            fc_total_invested = actual_total_invested
            fc_total_dividend = actual_total_dividend

            fc_year_start = actual_final_assets
            fc_year_inv   = 0.0
            fc_year_div   = 0.0
            fc_year_count = year_count
            fc_last_year  = current_date.year

            max_fc_months = 30 * 12
            fc_offset     = 0
            fc_assets     = actual_final_assets

            print(f"\n開始推估（最多30年）...")

            while fc_offset < max_fc_months:
                # 投入
                fc_cash           += monthly_investment
                fc_total_invested += monthly_investment
                fc_year_inv       += monthly_investment

                # 💡 每月重新計算配息（反映持股數量和資產價值的變化）
                # 配息計算公式：
                # 月配息 = (當月該ETF總資產 / 歷史平均股價) × (年均配息次數/12) × 平均每次配息金額
                for ticker in etfs:
                    # 當月該ETF的總資產 = 持有股數 × 當前預估股價
                    etf_market_value = fc_holdings[ticker] * forecast_price[ticker]
                    
                    # 換算成以歷史平均股價為基準的持股數
                    if hist_stats[ticker]['avg_price'] > 0:
                        estimated_shares = etf_market_value / hist_stats[ticker]['avg_price']
                        # 月配息 = 換算持股數 × (年均配息次數/12) × 平均每次配息金額
                        monthly_div = (estimated_shares * 
                                     (hist_stats[ticker]['avg_div_times'] / 12.0) * 
                                     hist_stats[ticker]['avg_div_per_share'])
                    else:
                        monthly_div = 0.0
                    
                    fc_cash           += monthly_div
                    fc_total_dividend += monthly_div
                    fc_year_div       += monthly_div

                # 買股（以當月預估股價）
                if fc_cash > 0:
                    for ticker, weight in etfs.items():
                        alloc = fc_cash * weight
                        price = forecast_price[ticker]
                        if price > 0:
                            shares = int(alloc / price / 1000) * 1000
                            if shares > 0:
                                fc_holdings[ticker] += shares
                    fc_cash = 0.0

                # 股價月成長
                for ticker in etfs:
                    forecast_price[ticker] *= (1 + monthly_growth[ticker])

                # 總資產
                fc_assets = sum(fc_holdings[t] * forecast_price[t] for t in etfs) + fc_cash

                # 通膨門檻（從投資起點算）
                total_months = actual_months + fc_offset + 1
                inf_target   = target_base * (1 + r_inf) ** (total_months / 12)

                results['dates'].append(current_date)
                results['total_assets'].append(fc_assets)
                results['invested_amount'].append(fc_total_invested)
                results['dividend_received'].append(fc_total_dividend)
                results['inflation_threshold'].append(inf_target)
                results['data_type'].append('forecast')

                # 年度摘要
                if current_date.year != fc_last_year:
                    yr_return = fc_assets - fc_year_start - fc_year_inv
                    results['annual_summary'].append({
                        '年份':    fc_last_year,
                        '年初資產': round(fc_year_start),
                        '年度投入': round(fc_year_inv),
                        '年度股利': round(fc_year_div),
                        '年度報酬': round(yr_return),
                        '年末資產': round(fc_assets),
                        '通膨門檻': round(inf_target),
                        '資料類型': 'forecast'
                    })
                    
                    # 收集各ETF年末持倉明細（推估）
                    etf_detail = {'年份': fc_last_year, '資料類型': 'forecast', 'etfs': {}}
                    for ticker in etfs:
                        etf_detail['etfs'][ticker] = {
                            '持股數': fc_holdings[ticker],
                            '股價': round(forecast_price[ticker], 2),
                            '市值': round(fc_holdings[ticker] * forecast_price[ticker]),
                            '權重': etfs[ticker]
                        }
                    results['etf_details'].append(etf_detail)
                    
                    fc_year_start = fc_assets
                    fc_year_inv   = 0.0
                    fc_year_div   = 0.0
                    fc_year_count += 1
                    fc_last_year  = current_date.year

                # 達成判斷
                if finish_year is None and fc_assets >= inf_target:
                    finish_year = total_months // 12
                    finish_age  = current_age + finish_year
                    print(f"  ✓ 推估達成！第 {finish_year} 年（{finish_age} 歲）")
                    break

                fc_offset += 1
                if current_date.month == 12:
                    current_date = pd.Timestamp(year=current_date.year + 1, month=1, day=1)
                else:
                    current_date = pd.Timestamp(year=current_date.year,
                                                 month=current_date.month + 1, day=1)

            # 最後不完整年度
            if fc_year_inv > 0:
                total_months = actual_months + fc_offset + 1
                inf_target   = target_base * (1 + r_inf) ** (total_months / 12)
                yr_return    = fc_assets - fc_year_start - fc_year_inv
                results['annual_summary'].append({
                    '年份':    fc_last_year,
                    '年初資產': round(fc_year_start),
                    '年度投入': round(fc_year_inv),
                    '年度股利': round(fc_year_div),
                    '年度報酬': round(yr_return),
                    '年末資產': round(fc_assets),
                    '通膨門檻': round(inf_target),
                    '資料類型': 'forecast'
                })
                
                # 收集最後一年的ETF明細
                etf_detail = {'年份': fc_last_year, '資料類型': 'forecast', 'etfs': {}}
                for ticker in etfs:
                    etf_detail['etfs'][ticker] = {
                        '持股數': fc_holdings[ticker],
                        '股價': round(forecast_price[ticker], 2),
                        '市值': round(fc_holdings[ticker] * forecast_price[ticker]),
                        '權重': etfs[ticker]
                    }
                results['etf_details'].append(etf_detail)

            if finish_year is None:
                print("  ⚠️  30年內未達成通膨門檻")

            print(f"✓ 推估完成  末期資產: {fc_assets:,.0f} 元")

        return {
            'portfolio_type':       portfolio_type,
            'portfolio_name':       portfolio['name'],
            'initial_capital':      initial_capital,
            'monthly_investment':   monthly_investment,
            'current_age':          current_age,
            'target_monthly_spend': target_monthly_spend,
            'withdraw_rate':        withdraw_rate,
            'finish_year':          finish_year,
            'finish_age':           finish_age,
            # 歷史
            'final_assets':         actual_final_assets,
            'actual_invested':      actual_total_invested,
            'actual_dividend':      actual_total_dividend,
            # 推估最終
            'forecast_assets':      fc_assets,
            'total_invested':       fc_total_invested,
            'total_dividend':       fc_total_dividend,
            # 統計 & 原始資料
            'hist_stats':           hist_stats,
            'results':              results,
            'etf_weights':          etfs
        }


if __name__ == "__main__":
    backtester = PortfolioBacktest(data_dir="data")
    result = backtester.backtest_portfolio(
        portfolio_type="conservative",
        initial_capital=1000000,
        monthly_investment=30000,
        current_age=30,
        target_monthly_spend=40000
    )
    if result:
        print(f"\n歷史最終資產: {result['final_assets']:,.0f}")
        print(f"推估最終資產: {result['forecast_assets']:,.0f}")
        print(f"達成年份: {result['finish_year']}")
