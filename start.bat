@echo off
chcp 65001
echo ========================================
echo 台灣ETF投資回測分析系統
echo ========================================
echo.

echo 檢查 Python 是否已安裝...
python --version >nul 2>&1
if errorlevel 1 (
    echo 錯誤: 找不到 Python，請先安裝 Python 3.8 或以上版本
    pause
    exit /b 1
)

echo.
echo 安裝必要套件...
pip install -r requirements.txt

echo.
echo 啟動系統...
echo 請在瀏覽器開啟: http://127.0.0.1:5000
echo.
python app.py

pause
