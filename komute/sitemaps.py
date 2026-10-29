from django.contrib.sitemaps import Sitemap
from django.urls import reverse

# Pages anyone can open without signing in. Everything else needs a login,
# so it's left out here and disallowed in robots.txt.
PUBLIC_PAGES = (
    'home', 'about', 'how-it-works', 'signup', 'login', 'contact-us',
    'terms-condition', 'privacy-policy', 'community-guidelines',
    'accessibility', 'delete-account',
)


class PublicPagesSitemap(Sitemap):
    """/sitemap.xml - the public pages, on the Site domain (migration 0039)."""

    protocol = 'https'
    changefreq = 'monthly'

    def items(self):
        return PUBLIC_PAGES

    def location(self, item):
        return reverse(item)
