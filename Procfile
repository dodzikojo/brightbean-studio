release: python manage.py migrate
web: gunicorn config.asgi:application --bind 0.0.0.0:$PORT --workers 2 --worker-class uvicorn_worker.UvicornWorker
worker: python manage.py process_tasks
