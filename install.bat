@echo off
chcp 65001 >nul
setlocal

REM ============== 一次性安装脚本 ==============
REM 作用：创建虚拟环境，安装依赖，复制 config.example.json -> config.json
REM 使用：双击运行
REM ============================================

set "HERE=%~dp0"
set "PYTHON="

REM 优先用 WorkBuddy 托管的 Python 3.13.12
if exist "%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe" (
    set "PYTHON=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
    goto :run
)

REM 退化：系统 Python
where python >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON=python"
    goto :run
)

echo [错误] 找不到 Python。请先安装 Python 3.10+。
pause
exit /b 1

:run
echo [1/3] 使用 Python: %PYTHON%

if not exist "%HERE%venv" (
    echo [2/3] 创建虚拟环境 venv\ ...
    "%PYTHON%" -m venv "%HERE%venv"
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败
        pause
        exit /b 1
    )
) else (
    echo [2/3] 虚拟环境已存在
)

echo [3/3] 安装依赖 ...
"%HERE%venv\Scripts\python.exe" -m pip install --upgrade pip
"%HERE%venv\Scripts\python.exe" -m pip install -r "%HERE%requirements.txt"
if errorlevel 1 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)

if not exist "%HERE%config.json" (
    copy "%HERE%config.example.json" "%HERE%config.json" >nul
    echo.
    echo ============================================
    echo  已复制 config.example.json 为 config.json
    echo  请用记事本编辑 config.json，填入飞书凭据
    echo  然后双击 start.bat 启动工作台
    echo ============================================
    echo.
) else (
    echo config.json 已存在，未覆盖
)

pause
