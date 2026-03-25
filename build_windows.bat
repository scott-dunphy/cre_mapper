@echo off
echo ============================================
echo   CRE Property Mapper - Windows Build
echo ============================================
echo.

echo Installing dependencies...
pip install flask pyinstaller
if %errorlevel% neq 0 (
    echo.
    echo ERROR: pip failed. Make sure Python is installed and added to PATH.
    echo Download Python from https://python.org
    echo IMPORTANT: Check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

echo.
echo Building executable...
pyinstaller --name "CRE Property Mapper" --onefile --windowed --add-data "templates;templates" --icon=NONE app.py
if %errorlevel% neq 0 (
    echo.
    echo ERROR: Build failed.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Build complete!
echo   The app is at: dist\CRE Property Mapper.exe
echo ============================================
echo.

copy "dist\CRE Property Mapper.exe" "%USERPROFILE%\Desktop\CRE Property Mapper.exe" >nul 2>&1
if %errorlevel% equ 0 (
    echo Copied to your Desktop.
) else (
    echo Could not copy to Desktop. Find it in the dist folder.
)

echo.
pause
