"""
Static-HTML hygiene checks for the public-facing templates.

tests_regression.py never renders a page and inspects the markup -- it asserts
on HTTP status codes and DB state, so dead UI (a placeholder href="#", a
<button> with no click handler and no form to submit) ships silently. That's
exactly how the footer "Subscribe" button and the login-page "or continue
with" divider went unnoticed. This suite renders every public page as an
anonymous visitor, parses the returned HTML, and fails on that specific
failure class plus a same-host link crawl for 404s.

Run with:
    python manage.py test komute.tests_html_hygiene --settings=config.settings_regression -v 2

Limitations (by design, keeps this dependency-free and fast):
- Only crawls pages reachable without login (see PUBLIC_PAGE_NAMES below).
  Shared partials like footer.html are still covered because they're
  included on every public page.
- The inert-button check only sees handlers wired via an inline <script> or
  an onclick="" attribute on the page itself. A button wired up from an
  external .js file (<script src=...>) won't be recognized and will report
  as a false positive -- if that happens, add its id/class to
  KNOWN_EXTERNALLY_WIRED below rather than weakening the check.
"""
from html.parser import HTMLParser
from urllib.parse import urlsplit

from django.test import TestCase, Client
from django.urls import reverse

# Public, unauthenticated pages worth crawling for dead UI / broken links.
PUBLIC_PAGE_NAMES = [
    'home', 'login', 'signup', 'about', 'how-it-works',
    'privacy-policy', 'contact-us', 'terms-condition', 'community-guidelines',
]

# (page_name, href) pairs where href="#" is intentional (e.g. a JS-driven
# in-page toggle with no real destination). Empty until one is confirmed.
ALLOWED_HASH_HREFS = set()

# (page_name, id_or_first_class) pairs for buttons wired from an external
# .js file, which this parser can't see. Empty until one is confirmed.
KNOWN_EXTERNALLY_WIRED = set()


class PageElementCollector(HTMLParser):
    """Collects <a> / <button> start tags and inline <script> bodies."""

    def __init__(self):
        super().__init__()
        self.links = []
        self.buttons = []
        self.scripts = []
        self._in_script = False
        self._script_buf = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a':
            self.links.append(attrs)
        elif tag == 'button':
            self.buttons.append(attrs)
        elif tag == 'script':
            self._in_script = True
            self._script_buf = []

    def handle_endtag(self, tag):
        if tag == 'script' and self._in_script:
            self.scripts.append(''.join(self._script_buf))
            self._in_script = False

    def handle_data(self, data):
        if self._in_script:
            self._script_buf.append(data)


def _render(page_name, client):
    resp = client.get(reverse(page_name))
    assert resp.status_code == 200, f"{page_name} did not render (status {resp.status_code})"
    return resp.content.decode()


def _collect(page_name, client):
    parser = PageElementCollector()
    parser.feed(_render(page_name, client))
    return parser


class DeadLinkTests(TestCase):
    """Flags <a href="#"> placeholders on rendered public pages."""

    def test_no_placeholder_hash_links(self):
        client = Client()
        offenders = []
        for name in PUBLIC_PAGE_NAMES:
            parser = _collect(name, client)
            for attrs in parser.links:
                href = (attrs.get('href') or '').strip()
                if href != '#':
                    continue
                if (name, href) in ALLOWED_HASH_HREFS:
                    continue
                label = attrs.get('aria-label') or attrs.get('class') or attrs.get('id') or '(unlabeled)'
                offenders.append(f"{name}: <a href=\"#\"> ({label})")
        self.assertEqual(
            offenders, [],
            "Found dead placeholder links -- either wire them to a real "
            "destination or comment the element out of the template:\n  "
            + "\n  ".join(offenders)
        )


class InertButtonTests(TestCase):
    """Flags <button type="button"> with no click handler anywhere on the
    page -- the footer 'Subscribe' bug's exact signature."""

    def test_no_inert_buttons(self):
        client = Client()
        offenders = []
        for name in PUBLIC_PAGE_NAMES:
            parser = _collect(name, client)
            script_blob = '\n'.join(parser.scripts)
            for attrs in parser.buttons:
                btn_type = (attrs.get('type') or '').lower()
                if btn_type != 'button':
                    continue  # type="submit" or unset-in-a-form is a real submit control
                if 'onclick' in attrs:
                    continue
                hook = attrs.get('id') or (attrs.get('class') or '').split()[:1]
                hook = hook[0] if isinstance(hook, list) and hook else hook
                if hook and (name, hook) in KNOWN_EXTERNALLY_WIRED:
                    continue
                if hook and hook in script_blob:
                    continue  # referenced by an inline <script> on this page
                label = attrs.get('id') or attrs.get('class') or '(unlabeled)'
                offenders.append(f"{name}: <button type=\"button\"> ({label})")
        self.assertEqual(
            offenders, [],
            "Found buttons with no onclick and no matching inline-script "
            "hook -- likely dead. Wire them up, or comment the element out:\n  "
            + "\n  ".join(offenders)
        )


class BrokenInternalLinkTests(TestCase):
    """Follows every same-host link found on a public page and asserts it
    doesn't 404/500. Catches stale hrefs that {% url %} wouldn't catch
    (e.g. hand-typed paths) before they reach production."""

    def test_internal_links_resolve(self):
        client = Client()
        broken = []
        checked = set()
        for name in PUBLIC_PAGE_NAMES:
            parser = _collect(name, client)
            for attrs in parser.links:
                href = (attrs.get('href') or '').strip()
                if not href or href == '#' or href.startswith(('mailto:', 'tel:', 'javascript:')):
                    continue
                parts = urlsplit(href)
                if parts.netloc:
                    continue  # external link, not ours to guarantee
                path = parts.path
                if not path.startswith('/') or path in checked:
                    continue
                checked.add(path)
                resp = client.get(path)
                if resp.status_code >= 400:
                    broken.append(f"{name}: {path} -> {resp.status_code}")
        self.assertEqual(
            broken, [],
            "Found internal links that don't resolve:\n  " + "\n  ".join(broken)
        )
