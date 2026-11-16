# The hosted demo (Render, free plan). Migrations, catalogues, static files and the demo data run at start,
# because the free plan has no pre-deploy step and the build has no database or secrets.
FROM python:3.15-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt requirements-prod.txt ./
RUN pip install --no-cache-dir -r requirements-prod.txt
COPY . .
RUN chmod +x scripts/start.sh
CMD ["scripts/start.sh"]
