@echo off
rem ---------------------------------------------------------------------------
rem  GB Energy Monitor - start the transmission wall on two screens
rem  Build 261023.1
rem
rem  Opens /wall twice in Chrome app windows (no tabs or address bar), one per
rem  monitor, each with its own layout. Both windows use your normal Chrome
rem  profile so clicks on screen 1 show on screen 2.
rem
rem  Edit the settings below to match your displays:
rem    LAYOUT1/LAYOUT2  d1 + d2 = ultrawide + HD,  h1 + h2 = two HD screens
rem    X1 / X2          left edge of each monitor in Windows display coordinates
rem                     (Settings > System > Display shows the arrangement; the
rem                     main screen starts at 0, a screen to its right at its width)
rem    W/H              each monitor's resolution
rem  If a window doesn't open full screen, click it and press F.
rem ---------------------------------------------------------------------------
set PORT=8412
set LAYOUT1=d1
set X1=0
set Y1=0
set W1=5120
set H1=1440
set LAYOUT2=d2
set X2=5120
set Y2=0
set W2=1920
set H2=1080

set CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe
if not exist "%CHROME%" set CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe
if not exist "%CHROME%" set CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe
if not exist "%CHROME%" set CHROME=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe
if not exist "%CHROME%" (
  echo Chrome or Edge not found. Edit CHROME in this file.
  pause
  exit /b 1
)

start "" "%CHROME%" --new-window --app="http://localhost:%PORT%/wall?screen=%LAYOUT1%" --window-position=%X1%,%Y1% --window-size=%W1%,%H1% --start-fullscreen
timeout /t 2 /nobreak >nul
start "" "%CHROME%" --new-window --app="http://localhost:%PORT%/wall?screen=%LAYOUT2%" --window-position=%X2%,%Y2% --window-size=%W2%,%H2% --start-fullscreen
