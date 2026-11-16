#!/bin/sh
# Start the hosted demo: bring every database up to date, compile the Arabic catalogue, collect static files,
# add the demo clinic if it is missing (seed_demo never touches what exists), then serve.
set -e
python manage.py migrate_all
python manage.py compile_translations
python manage.py collectstatic --noinput -v 0
DJANGO_DEBUG=1 python manage.py seed_demo  # demo data only: the command refuses to run without DEBUG
exec gunicorn config.wsgi --bind 0.0.0.0:${PORT:-8000} --workers 2 --timeout 60 --access-logfile -
