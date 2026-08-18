@echo off
chcp 65001 >nul
echo ============================================
echo   网站爬取工具 - EXE 打包脚本
echo ============================================
echo.

REM 检查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到 Python，请先安装 Python 3.8+
    pause
    exit /b 1
)

echo [1/3] 安装依赖...
pip install pyinstaller requests beautifulsoup4 urllib3 -q

echo [2/3] 打包为单文件 EXE（可能需要 1-2 分钟）...
pyinstaller --onefile --name "网站爬取工具" site_crawler_gui.py

echo.
echo [3/3] 复制资源文件到 dist 目录...
copy /Y urls_example.txt dist\ >nul 2>&1
copy /Y config.py dist\ >nul 2>&1
copy /Y README_使用说明.txt dist\ >nul 2>&1

echo.
echo ============================================
echo   打包完成！
echo   EXE 位置: dist\网站爬取工具.exe
echo   将 dist 文件夹打包发送给他人即可使用。
echo ============================================
echo.
echo 提示：用户需要先获取 DeepSeek API Key（点击 EXE 中的链接）
echo       然后将网址写入 .txt 文件，通过 EXE 导入。
echo 说明文件 README_使用说明.txt 已放入 dist 目录。
pause
