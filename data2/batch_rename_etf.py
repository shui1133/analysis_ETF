#!/usr/bin/env python3
"""
ETF 資料檔案批次重命名工具 - 專用版

將您的檔案格式：
  0056_元大高股息.csv → 0056_price.csv
  0056_元大高股息_配息.csv → 0056_hist_配息.csv

支援的 ETF：
  - 0056, 00679B, 00713, 00878 (保守型)
  - 00915, 00919, 00929 (穩健型)
  - 006208 (積極型)
"""

import os
import shutil
from pathlib import Path


def rename_files_in_directory(directory='.'):
    """批次重命名目錄中的所有 ETF CSV 檔案"""
    
    directory = Path(directory)
    
    # 重命名對應表
    rename_map = {
        # 股價檔案
        '0056_元大高股息.csv': '0056_price.csv',
        '00679B_元大美債20年.csv': '00679B_price.csv',
        '00713_元大台灣高息低波.csv': '00713_price.csv',
        '00878_國泰永續高股息.csv': '00878_price.csv',
        '00915_凱基優選高股息30.csv': '00915_price.csv',
        '00919_群益台灣精選高息.csv': '00919_price.csv',
        '00929_復華台灣科技優息.csv': '00929_price.csv',
        '006208_富邦台50.csv': '006208_price.csv',
        
        # 配息檔案
        '0056_元大高股息_配息.csv': '0056_hist_配息.csv',
        '00679B_元大美債20年_配息.csv': '00679B_hist_配息.csv',
        '00713_元大台灣高息低波_配息.csv': '00713_hist_配息.csv',
        '00878_國泰永續高股息_配息.csv': '00878_hist_配息.csv',
        '00915_凱基優選高股息30_配息.csv': '00915_hist_配息.csv',
        '00919_群益台灣精選高息_配息.csv': '00919_hist_配息.csv',
        '00929_復華台灣科技優息_配息.csv': '00929_hist_配息.csv',
        '006208_富邦台50_配息.csv': '006208_hist_配息.csv',
    }
    
    print("=" * 80)
    print("ETF 資料檔案批次重命名")
    print("=" * 80)
    print(f"\n目標目錄: {directory.absolute()}\n")
    
    # 統計
    renamed_count = 0
    skipped_count = 0
    error_count = 0
    
    # 檢查所有檔案
    csv_files = list(directory.glob('*.csv'))
    
    if not csv_files:
        print("❌ 目錄中沒有找到 CSV 檔案")
        return
    
    print(f"找到 {len(csv_files)} 個 CSV 檔案\n")
    
    # 執行重命名
    for old_name, new_name in rename_map.items():
        old_path = directory / old_name
        new_path = directory / new_name
        
        if old_path.exists():
            if new_path.exists():
                print(f"⚠️  {old_name}")
                print(f"   → {new_name} (目標已存在，跳過)")
                skipped_count += 1
            else:
                try:
                    shutil.copy2(old_path, new_path)
                    print(f"✓  {old_name}")
                    print(f"   → {new_name}")
                    renamed_count += 1
                except Exception as e:
                    print(f"❌ {old_name}")
                    print(f"   重命名失敗: {e}")
                    error_count += 1
    
    # 檢查是否有未匹配的檔案
    processed_files = set(rename_map.keys()) | set(rename_map.values())
    unmatched = []
    
    for csv_file in csv_files:
        if csv_file.name not in processed_files:
            unmatched.append(csv_file.name)
    
    if unmatched:
        print(f"\n⚠️  發現 {len(unmatched)} 個未匹配的檔案:")
        for filename in unmatched:
            print(f"   - {filename}")
    
    # 總結
    print("\n" + "=" * 80)
    print("處理結果")
    print("=" * 80)
    print(f"\n✓ 成功重命名: {renamed_count} 個檔案")
    print(f"⚠️  已存在跳過: {skipped_count} 個檔案")
    print(f"❌ 錯誤: {error_count} 個檔案")
    
    # 檢查各投資組合的完整性
    print("\n" + "=" * 80)
    print("投資組合檔案檢查")
    print("=" * 80)
    
    portfolios = {
        '保守型 4%': ['00878', '00713', '00679B'],
        '穩健型 6%': ['00919', '00929', '0056'],
        '積極型 8%': ['006208', '00929', '00915']
    }
    
    for portfolio_name, etfs in portfolios.items():
        print(f"\n{portfolio_name}:")
        complete = True
        
        for etf in etfs:
            price_file = directory / f"{etf}_price.csv"
            div_file = directory / f"{etf}_hist_配息.csv"
            
            price_exists = price_file.exists()
            div_exists = div_file.exists()
            
            if price_exists and div_exists:
                status = "✓"
            elif price_exists:
                status = "⚠️  (缺配息)"
                complete = False
            elif div_exists:
                status = "⚠️  (缺股價)"
                complete = False
            else:
                status = "❌ (完全缺少)"
                complete = False
            
            print(f"  {status} {etf}")
        
        if complete:
            print(f"  ➜ ✅ 可以使用此組合！")
        else:
            print(f"  ➜ ⚠️  此組合資料不完整")
    
    # 列出最終檔案
    print("\n" + "=" * 80)
    print("最終檔案清單")
    print("=" * 80)
    
    standard_files = sorted([f for f in directory.glob('*_price.csv')] + 
                          [f for f in directory.glob('*_hist_配息.csv')])
    
    if standard_files:
        print()
        etf_codes = set()
        for f in standard_files:
            print(f"  ✓ {f.name}")
            code = f.name.split('_')[0]
            etf_codes.add(code)
        
        print(f"\n共 {len(standard_files)} 個標準格式檔案")
        print(f"涵蓋 {len(etf_codes)} 支 ETF: {', '.join(sorted(etf_codes))}")
    else:
        print("\n  (沒有標準格式的檔案)")
    
    print("\n" + "=" * 80)
    print("💡 下一步")
    print("=" * 80)
    print("\n1. 將處理好的檔案複製到系統的 data/ 目錄")
    print("2. 在網頁選擇有完整資料的投資組合")
    print("3. 點擊「執行回測+推估」")
    print("4. 「完整現金流分析」按鈕應該會出現！")
    print()


if __name__ == '__main__':
    import sys
    
    if len(sys.argv) > 1:
        target_dir = sys.argv[1]
    else:
        target_dir = '.'
    
    if not os.path.exists(target_dir):
        print(f"❌ 目錄不存在: {target_dir}")
        sys.exit(1)
    
    rename_files_in_directory(target_dir)
