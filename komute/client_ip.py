def client_ip(request):
    """The address Nginx saw. Nginx appends it to X-Forwarded-For
    ($proxy_add_x_forwarded_for), so it is the last entry; earlier entries
    come from the client and can be anything.

    Also django-axes' AXES_CLIENT_IP_CALLABLE: Daphne runs without
    --proxy-headers, so REMOTE_ADDR is 127.0.0.1 for every visitor (audit S-16)."""
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.rsplit(',', 1)[-1].strip()
    return request.META.get('REMOTE_ADDR', 'unknown')
