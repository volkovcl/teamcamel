@echo off
rem TeamCamel precheck for Windows. Run it first: double-click this file, or type  precheck.bat
rem
rem Says whether anything TeamCamel needs is missing on this computer and what to
rem do about it. It looks for Python here, then lets TeamCamel check the rest.
rem Changes nothing on this computer.

setlocal
cd /d "%~dp0"

if not exist teamcamel.py goto nofiles

for %%P in (py python python3) do (
    %%P -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
    if not errorlevel 1 (
        %%P teamcamel.py --precheck %*
        goto done
    )
)

echo TeamCamel precheck
echo   [MISSING] Python 3.9 or newer
echo             -^> install it from https://www.python.org/downloads/ or from the Microsoft Store,
echo                then run this again
echo Result: 1 item missing. The other components are checked once Python is there.
goto done

:nofiles
echo TeamCamel precheck
echo   [MISSING] teamcamel.py next to this file
echo             -^> download the complete TeamCamel folder again and run precheck.bat from inside it
echo Result: 1 item missing.

:done
echo.
pause
