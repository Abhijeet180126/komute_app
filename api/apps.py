from django.apps import AppConfig


class ApiConfig(AppConfig):
    name = 'api'
    verbose_name = 'Mobile app API'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        # Registers the X-Session-Token scheme with drf-spectacular.
        from api.common import schema  # noqa: F401
