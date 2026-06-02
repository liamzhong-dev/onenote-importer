@echo off
chcp 65001 >nul
title OneNote 日记导入工具（排障模式）
setlocal

rem 这个脚本保留黑色控制台窗口，一旦出错错误信息能直接看到。
rem 正常使用时请用 run.bat。

set "HERE=%~dp0"
set "PYEXE="

call :check "%HERE%.venv\Scripts\python.exe"
if not defined PYEXE call :check "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if not defined PYEXE call :check "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYEXE call :check "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE call :check "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE call :check "C:\Python313\python.exe"
if not defined PYEXE call :check "C:\Python312\python.exe"
if not defined PYEXE call :check "C:\Python311\python.exe"
if not defined PYEXE call :check "C:\Python310\python.exe"
if not defined PYEXE call :check "python"
if not defined PYEXE call :check "python3"

if not defined PYEXE (
  echo.
  echo [错误] 没找到带 tkinter 的 Python。请先安装官方 Python 安装包。
  echo.
  pause
  exit /b 1
)

echo 使用解释器：%PYEXE%
echo.
"%PYEXE%" "%HERE%app.py"
echo.
echo 程序已退出，退出码 %errorlevel%。上面若有 Traceback 请截图。
pause
endlocal
exit /b 0

:check
set "C=%~1"
if not defined C exit /b 1
"%C%" -c "import tkinter" >nul 2>nul
if errorlevel 1 exit /b 1
set "PYEXE=%C%"
exit /b 0
