"""
投資組合回測模組
根據歷史股價和配息進行真實回測，並以歷史統計推估未來
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
        # 本地開發環境，使用相對路徑
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
            files = glob.glob(os.path.join(self.data_dir, f"{ticker}_*.csv"))
            if not files:
                print(f"⚠️  找不到 {ticker} 的股價資料")
                return None, None

            price_data = pd.read_csv(files[0], parse_dates=['日期'])
            if price_data['日期'].dtype.tz is not None:
                price_data['日期'] = price_data['日期'].dt.tz_localize(None)

            div_files = glob.glob(os.path.join(self.data_dir, f"{ticker}_*_配息.csv"))
            dividend_data = None
            if div_files:
                dividend_data = pd.read_csv(div_files[0], parse_dates=['除息日'])
                if dividend_data['除息日'].dtype.tz is not None:
                    dividend_data['除息日'] = dividend_data['除息日'].dt.tz_localize(None)

            return price_data, dividend_data
        except Exception as e:
            print(f"❌ 載入 {ticker} 資料錯誤: {str(e)}")
            return None, None

    # ──────────────────────────────────────────────────────────────
    def _calc_historical_stats(self, etf_data):
        """
        計算各ETF歷史統計：
          - CAGR（年化股價成長率）
          - 平均每股股利金額（所有歷史配息的算術平均）
          - 年均配息次數
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
                'last_price':        float(price_df.iloc[-1]['收盤價'])
            }
            print(f"  [{ticker}] CAGR={cagr*100:.2f}%  "
                  f"平均每股股利={avg_div_per_share:.4f}  "
                  f"年均配息次數={avg_div_times:.1f}  "
                  f"末日收盤={stats[ticker]['last_price']:.2f}")
        return stats

    # ──────────────────────────────────────────────────────────────
    def backtest_portfolio(self, portfolio_type, initial_capital=1000000,
                           monthly_investment=30000, current_age=30,
                           target_monthly_spend=40000):
        """
        第一段：歷史真實回測（從ETF上市日→今天）
        第二段：推估（從今天→達成通膨門檻，最多30年）
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
                print(f"❌ 無法載入 {ticker}，回測終止")
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
            'annual_summary': []
        }

        # ── 狀態變數 ──────────────────────────────────────────────
        total_cash     = float(initial_capital)
        holdings       = {t: 0 for t in etfs}
        total_invested = float(initial_capital)
        total_dividend = 0.0
        total_value    = 0.0

        current_date      = earliest_date
        end_date          = pd.Timestamp.now().tz_localize(None)
        month_count       = 0
        year_count        = 0
        last_year         = current_date.year
        year_start_assets = 0.0
        year_invested     = 0.0
        year_dividend     = 0.0

        # ═══════════════════════════════════════════════════════════
        #  第一段：歷史真實回測
        # ═══════════════════════════════════════════════════════════
        while current_date <= end_date:
            # 每月投入
            if month_count > 0:
                total_cash     += monthly_investment
                total_invested += monthly_investment
                year_invested  += monthly_investment

            # 配息
            for ticker, data in etf_data.items():
                if data['dividend'] is not None:
                    mask = ((data['dividend']['除息日'].dt.year  == current_date.year) &
                            (data['dividend']['除息日'].dt.month == current_date.month))
                    month_divs = data['dividend'][mask]
                    if len(month_divs) > 0:
                        div_per_share  = month_divs['股利'].sum()
                        div_amount     = holdings[ticker] * div_per_share
                        total_dividend += div_amount
                        year_dividend  += div_amount
                        total_cash     += div_amount

            # 買股
            if total_cash > 0:
                for ticker, weight in etfs.items():
                    alloc = total_cash * weight
                    pdf   = etf_data[ticker]['price']
                    mp    = pdf[(pdf['日期'].dt.year  == current_date.year) &
                                (pdf['日期'].dt.month == current_date.month)]
                    if len(mp) > 0:
                        price = float(mp.iloc[-1]['收盤價'])
                        if price > 0:
                            shares = int(alloc / price / 1000) * 1000
                            if shares > 0:
                                holdings[ticker] += shares
                total_cash = 0.0

            # 市值
            total_value = 0.0
            for ticker, shares in holdings.items():
                pdf = etf_data[ticker]['price']
                mp  = pdf[(pdf['日期'].dt.year  == current_date.year) &
                          (pdf['日期'].dt.month == current_date.month)]
                if len(mp) > 0:
                    total_value += shares * float(mp.iloc[-1]['收盤價'])
            total_value += total_cash

            years_passed     = month_count / 12
            inflation_target = target_base * (1 + r_inf) ** years_passed

            results['dates'].append(current_date)
            results['total_assets'].append(total_value)
            results['invested_amount'].append(total_invested)
            results['dividend_received'].append(total_dividend)
            results['inflation_threshold'].append(inflation_target)
            results['data_type'].append('actual')

            # 年度摘要
            if current_date.year != last_year or current_date >= end_date:
                if month_count > 0:
                    yr_return = total_value - year_start_assets - year_invested
                    results['annual_summary'].append({
                        '年份':    last_year,
                        '年初資產': round(year_start_assets),
                        '年度投入': round(year_invested),
                        '年度股利': round(year_dividend),
                        '年度報酬': round(yr_return),
                        '年末資產': round(total_value),
                        '通膨門檻': round(target_base * (1 + r_inf) ** year_count),
                        '資料類型': 'actual'
                    })
                year_start_assets = total_value
                year_invested = 0.0
                year_dividend = 0.0
                year_count   += 1
                last_year     = current_date.year

            month_count += 1
            if current_date.month == 12:
                current_date = pd.Timestamp(year=current_date.year + 1, month=1, day=1)
            else:
                current_date = pd.Timestamp(year=current_date.year,
                                             month=current_date.month + 1, day=1)

        # 快照：歷史回測結束時
        actual_final_assets   = total_value
        actual_total_invested = total_invested
        actual_total_dividend = total_dividend
        actual_months         = month_count

        print(f"✓ 歷史回測完成  期末資產: {actual_final_assets:,.0f} 元")

        # 歷史期間有無達成？
        finish_year = None
        finish_age  = None
        for i, (a, th) in enumerate(zip(results['total_assets'], results['inflation_threshold'])):
            if a >= th:
                finish_year = i // 12
                finish_age  = current_age + finish_year
                break

        # ═══════════════════════════════════════════════════════════
        #  第二段：推估（從今天起，最多30年，或達成即止）
        # ═══════════════════════════════════════════════════════════
        # 以歷史末日收盤價為起點，每月以CAGR月化成長
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

            # 配息（歷史平均每股 × 年均次數/12）
            for ticker in etfs:
                monthly_div = (hist_stats[ticker]['avg_div_per_share'] *
                               hist_stats[ticker]['avg_div_times'] / 12.0)
                div_amount        = fc_holdings[ticker] * monthly_div
                fc_cash           += div_amount
                fc_total_dividend += div_amount
                fc_year_div       += div_amount

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
