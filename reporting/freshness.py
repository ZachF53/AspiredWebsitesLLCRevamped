"""
Content-freshness crawler — walks a client's live site and scores each page
for staleness. Uses requests + parsel (already installed via Scrapy); no new
dependency. Respects robots.txt and caps the crawl at MAX_PAGES.
"""

import logging
from datetime import datetime
from datetime import timezone as dt_timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from django.utils import timezone
from parsel import Selector

logger = logging.getLogger(__name__)

MAX_PAGES = 50
CRAWL_TIMEOUT = 12
USER_AGENT = 'AspiredWebsites-FreshnessBot/1.0'


def _robots(base_url):
    """Load the site's robots.txt, or None if it can't be read."""
    parsed = urlparse(base_url)
    parser = RobotFileParser()
    parser.set_url(f'{parsed.scheme}://{parsed.netloc}/robots.txt')
    try:
        parser.read()
        return parser
    except Exception:
        return None


def _last_modified(response, selector):
    """Best-effort last-modified datetime from response headers or meta tags."""
    raw = response.headers.get('Last-Modified', '')
    if raw:
        try:
            return parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            pass
    for xpath in (
        '//meta[@property="article:modified_time"]/@content',
        '//meta[@property="article:published_time"]/@content',
        '//meta[@name="last-modified"]/@content',
    ):
        value = selector.xpath(xpath).get()
        if value:
            try:
                return datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
            except ValueError:
                continue
    return None


def crawl_site(base_url, max_pages=MAX_PAGES):
    """Breadth-first crawl of one site — returns a list of page dicts."""
    domain = urlparse(base_url).netloc
    robots = _robots(base_url)
    seen, queue, pages = set(), [base_url], []

    session = requests.Session()
    session.headers['User-Agent'] = USER_AGENT

    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        if robots and not robots.can_fetch(USER_AGENT, url):
            continue
        try:
            resp = session.get(url, timeout=CRAWL_TIMEOUT, allow_redirects=True)
        except requests.RequestException:
            continue
        if resp.status_code != 200:
            continue
        if 'text/html' not in resp.headers.get('Content-Type', ''):
            continue

        sel = Selector(text=resp.text)
        body_text = ' '.join(sel.xpath('//body//text()').getall())
        pages.append({
            'url': url,
            'title': (sel.xpath('//title/text()').get() or url).strip()[:200],
            'last_modified': _last_modified(resp, sel),
            'word_count': len(body_text.split()),
            'is_blog': '/blog/' in url.lower(),
            'has_structured_data': bool(
                sel.xpath('//script[@type="application/ld+json"]')),
        })

        for href in sel.xpath('//a/@href').getall():
            target = urljoin(url, (href or '').split('#')[0]).rstrip('/')
            parsed = urlparse(target)
            if (parsed.netloc == domain and parsed.scheme in ('http', 'https')
                    and target and target not in seen and target not in queue):
                queue.append(target)

    return pages


def calculate_freshness_score(page):
    """Score a crawled page 0-100 for content freshness."""
    score = 0

    last_mod = page.get('last_modified')
    if last_mod:
        if last_mod.tzinfo is None:
            last_mod = last_mod.replace(tzinfo=dt_timezone.utc)
        days = (timezone.now() - last_mod).days
        if days < 30:
            score += 40
        elif days < 90:
            score += 25
        elif days < 180:
            score += 10

    words = page.get('word_count') or 0
    if words >= 500:
        score += 30
    elif words >= 300:
        score += 20
    elif words >= 200:
        score += 10

    if page.get('is_blog'):
        score += 15
    if page.get('has_structured_data'):
        score += 15

    return min(score, 100)
