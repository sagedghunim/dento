FROM python:3.13-slim
WORKDIR /app
COPY requirements*.txt .
RUN pip install --no-cache-dir -r requirements-prod.txt
COPY . .
RUN DJANGO_SECRET_KEY=build-only python manage.py collectstatic --noinput
USER nobody
EXPOSE 8000
CMD ["gunicorn", "config.wsgi", "--bind", "0.0.0.0:8000", "--workers", "4", "--threads", "4", "--timeout", "30", "--access-logfile", "-"]
