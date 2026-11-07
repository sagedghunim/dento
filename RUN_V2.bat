@echo off
rem Starts Hamada v2 on http://127.0.0.1:8002. The prototype in clinic\ is untouched.
rem First-time setup is in v2\README.md.
cd /d %~dp0v2
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\pg.ps1 start
if errorlevel 1 goto :pgfail
.venv\Scripts\python.exe manage.py migrate_all
if errorlevel 1 goto :fail
.venv\Scripts\python.exe manage.py compile_translations
echo.
echo Hamada v2 is starting on http://127.0.0.1:8002
start "" http://127.0.0.1:8002/
.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8002
goto :eof

:pgfail
echo PostgreSQL did not start. See C:\work\pgsql\postgres.log
pause
goto :eof

:fail
echo Something went wrong. Read the message above.
pause
