"""
WSGI config for SUG E-Voting Platform.
"""
import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "sug_evoting.settings")

application = get_wsgi_application()
