import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sug_evoting.settings")

app = Celery("sug_evoting")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
