"""
檔案語法檢查腳本
在推送到 GitHub 之前執行此腳本檢查所有 Python 檔案
"""

import py_compile
import os
import sys

def check_file(filepath):
    """檢查單個檔案"""
    try:
        py_compile.compile(filepath, doraise=True)
        print(f"✓ {filepath} - 語法正確")
        return True
    except py_compile.PyCompileError as e:
        print(f"✗ {filepath} - 語法錯誤:")
        print(f"  {e}")
        return False

def main():
    """主函數"""
    print("="*60)
    print("Python 檔案語法檢查")
    print("="*60)
    
    # 要檢查的檔案
    files_to_check = [
        'app.py',
        'data_fetcher.py',
        'backtest.py',
        'test_crawlers.py',
        'test_system.py'
    ]
    
    all_passed = True
    
    for filename in files_to_check:
        if os.path.exists(filename):
            if not check_file(filename):
                all_passed = False
        else:
            print(f"⚠ {filename} - 檔案不存在")
    
    print("="*60)
    
    if all_passed:
        print("✓ 所有檔案語法檢查通過！")
        print("可以安全地推送到 GitHub 了。")
        return 0
    else:
        print("✗ 有檔案存在語法錯誤！")
        print("請修正錯誤後再推送。")
        return 1

if __name__ == "__main__":
    sys.exit(main())
