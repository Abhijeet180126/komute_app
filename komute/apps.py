from django.apps import AppConfig


class KomuteConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'komute'

    def ready(self):
        import komute.signals

