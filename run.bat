@echo off
chcp 65001 >nul
title OneNote 日记导入工具
setlocal

set "HERE=%~dp0"
set "APP=%HERE%app.py"
set "PYEXE="
set "PYWEXE="

rem ---- 依次候选解释器，逐个验证能否 import tkinter，取第一个可用的 ----
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

if not defined PYEXE goto :noPython

echo 使用解释器：%PYEXE%

rem ---- 启动前自检：依赖缺失时在这里报错，而不是让窗口无声闪退 ----
set "ERRLOG=%TEMP%\onenote-importer-startup.log"
"%PYEXE%" -c "import sys; sys.path.insert(0, r'%HERE%.'); import tkinter, core.pipeline, writers.factory" >nul 2>"%ERRLOG%"
if errorlevel 1 (
  echo.
  echo [启动失败] 依赖自检没通过，下面是详细错误：
  echo ------------------------------------------------------------
  type "%ERRLOG%"
  echo ------------------------------------------------------------
  echo 最常见的原因：这个 Python 没装 tkinter。
  echo 解决办法：重新运行 Python 官方安装包 ^(python.org^)，
  echo           选 Modify，确认 "Tcl/Tk and IDLE" 已勾选。
  echo.
  pause
  exit /b 1
)

rem ---- 有同目录 pythonw 就用它，避免多开一个黑窗口 ----
set "PYWEXE=%PYEXE:python.exe=pythonw.exe%"
if /i not "%PYEXE%"=="%PYWEXE%" if exist "%PYWEXE%" (
  start "" "%PYWEXE%" "%APP%"
) else (
  start "" "%PYEXE%" "%APP%"
)

endlocal
exit /b 0

rem ================= 子过程 =================
:check
set "C=%~1"
if not defined C exit /b 1
"%C%" -c "import tkinter" >nul 2>nul
if errorlevel 1 exit /b 1
set "PYEXE=%C%"
exit /b 0

:noPython
echo.
echo [错误] 在这台机器上没找到带 tkinter 的 Python。
echo        请到 python.org 安装 Python 3.10 - 3.13，
echo        安装时务必勾选 "Add python.exe to PATH" 和 "Tcl/Tk and IDLE"。
echo        也可以手动建虚拟环境到本目录的 .venv。
echo.
pause
exit /b 1
