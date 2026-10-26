@echo off
chcp 65001 >nul
cd /d %~dp0

echo [1/4] 检查图标文件...
if not exist "图标.ico" (
    echo 错误：当前目录未找到 "图标.ico"，请确认图标文件存在。
    pause
    exit /b 1
)

echo [2/4] 安装依赖...
pip install -r requirements.txt pyinstaller
if errorlevel 1 (
    echo 依赖安装失败。
    pause
    exit /b 1
)

echo [3/4] 打包 exe...
pyinstaller --noconfirm --clean --onefile --windowed ^
    --name "测试工具" ^
    --icon "图标.ico" ^
    main.py
if errorlevel 1 (
    echo 打包失败，请查看上方错误信息。
    pause
    exit /b 1
)

echo [4/4] 整理产物...
rem 创建 data 目录，把图标放进去（exe 运行时会从 data/ 加载窗口图标）
if not exist "dist\data" mkdir "dist\data"
copy /Y "图标.ico" "dist\data\图标.ico" >nul

echo.
echo ========================================
echo  打包完成
echo  exe   ：dist\测试工具.exe
echo  图标  ：dist\data\图标.ico（窗口图标）
echo  数据  ：dist\data\ 目录（servers.json / client_settings.json / crash.log 自动生成）
echo ========================================
pause
exit /b 0
