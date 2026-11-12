"""Business logic shared by the website's views (komute/views.py) and the
mobile app API (api/). See docs/api-modular-breakup.md.

Services don't know about responses, messages or rate limits: callers apply
their own limits (in the same order as before) and turn AccountError and
friends into a page message or an API error."""
