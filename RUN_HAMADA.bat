@echo off
cd /d "%~dp0"
if not exist .venv python -m venv .venv
call .venv\Scripts\activate
pip install -r requirements.txt
set DJANGO_DEBUG=1
python manage.py migrate
python manage.py seed_demo
python manage.py test
start http://127.0.0.1:8000/
python manage.py runserver
pause
