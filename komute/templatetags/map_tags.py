from django import template
from django.conf import settings
from django.utils.html import format_html

from komute.services import map_client

register = template.Library()


@register.simple_tag(takes_context=True)
def map_config(context):
    """Tells static/js/place-autocomplete.js where to get suggestions and where the rider probably is, and
    static/js/map-routes.js where routes come from. Usage, once per page, before those scripts:
    {% load map_tags %}{% map_config %}"""
    source = settings.MAP_AUTOCOMPLETE
    routes = settings.MAP_ROUTES
    request = context.get('request')
    user = getattr(request, 'user', None)
    bias = map_client.bias_for(user) if source == 'apple' and user is not None and user.is_authenticated else None
    if bias:
        return format_html('<div id="map-config" hidden data-autocomplete="{}" data-routes="{}" data-bias-lat="{}" data-bias-lon="{}"></div>',
                           source, routes, bias[0], bias[1])
    return format_html('<div id="map-config" hidden data-autocomplete="{}" data-routes="{}"></div>', source, routes)


@register.simple_tag
def map_setting(name):
    """A MAP_* setting by short name, for text that must match what the site really does. {% map_setting 'autocomplete' as source %}"""
    return getattr(settings, 'MAP_' + name.upper(), '')
