@echo off
REM Local run: refresh the trade store from SEC EDGAR, then build the PDF.
REM The hosted service does the same thing on a schedule — see SERVICE.md.

echo ============================================
echo    Insider Trading Briefing
echo ============================================
echo.

echo [1/2] Updating trades from SEC EDGAR...
python "%~dp0main.py" fetch
if %ERRORLEVEL% neq 0 (
    echo.
    echo ERROR: Fetch failed. Aborting.
    pause
    exit /b 1
)
echo.

echo [2/2] Building the report...
python "%~dp0main.py" report
if %ERRORLEVEL% neq 0 (
    echo.
    echo ERROR: Report generation failed.
    pause
    exit /b 1
)

echo.
echo ============================================
echo    Done.
echo ============================================
pause
