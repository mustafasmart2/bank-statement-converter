@echo off
cd /d "%~dp0"
echo.
echo ================================================
echo   BANK STATEMENT CONVERTER
echo ================================================
echo.

echo [1/4] Checking Python...
python --version
if errorlevel 1 (
    echo.
    echo  ERROR: Python nahi mila.
    echo  python.org/downloads se install karo aur
    echo  "Add python.exe to PATH" checkbox TICK karna.
    echo.
    pause
    exit /b 1
)

if not exist .venv (
    echo.
    echo [2/4] Pehli baar setup ho raha hai... 1-2 minute lagenge.
    echo       Screen ruki hui lage to bhi band mat karna.
    python -m venv .venv
    if errorlevel 1 (
        echo  ERROR: virtual environment nahi bana.
        pause
        exit /b 1
    )
) else (
    echo [2/4] Setup pehle se maujood hai.
)

echo.
echo [3/4] Libraries install ho rahi hain...
echo       Pehli baar 2-4 minute. Agli baar 5 second.
echo.
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo  ERROR: libraries install nahi hui. Internet check karo.
    pause
    exit /b 1
)

echo.
echo [4/4] App start ho rahi hai...
echo.
echo   Browser khud khulega. Agar na khule to ye address kholo:
echo   http://localhost:8501
echo.
echo   Band karne ke liye: is window mein Ctrl+C dabao.
echo.
streamlit run app.py

echo.
echo App band ho gayi.
pause
