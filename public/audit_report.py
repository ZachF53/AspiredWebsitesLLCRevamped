"""
The detailed audit report: every check, passed and failed, with why it
matters and how to fix it.

Two halves:

  * collect_details() turns a raw Lighthouse result into a compact,
    JSON-safe dict stored on the session and on AuditLead.details.
  * build_report_context() turns an AuditLead into the context the
    emails/audit_report.html template renders: the summary card and the
    four category cards from the results page, then the full breakdown.

FIX_GUIDE holds our own plain-English "why" and "how to fix" for the
checks small-business sites fail most. Anything not in it falls back to
Google's own description and its "learn more" link, so a new Lighthouse
check never shows up blank.
"""

import re

CATEGORY_ORDER = ('performance', 'seo', 'accessibility', 'best_practices')

_LH_KEYS = {
    'performance': 'performance',
    'seo': 'seo',
    'best_practices': 'best-practices',
    'accessibility': 'accessibility',
}

# Score-display modes that represent a real pass/fail check. Everything
# else ('informative', 'notApplicable', 'manual', 'error') is skipped.
_SCORED_MODES = {'binary', 'numeric', 'metricSavings'}

_LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')

# Lighthouse's core speed metrics, shown as their own table.
METRIC_IDS = (
    'first-contentful-paint', 'largest-contentful-paint',
    'total-blocking-time', 'cumulative-layout-shift', 'speed-index',
)


def _strip_links(text):
    """'Foo [Learn more](url).' -> ('Foo.', url)."""
    text = text or ''
    m = _LINK_RE.search(text)
    url = m.group(2) if m else ''
    clean = _LINK_RE.sub(r'\1', text)
    # Drop the trailing "Learn more ..." sentence; we render it as a link.
    clean = re.sub(r'\s*Learn (more|how)[^.]*\.?\s*$', '', clean).strip()
    return clean, url


def collect_details(lighthouse):
    """Compact per-category pass/fail lists from a Lighthouse result."""
    categories = lighthouse.get('categories') or {}
    audits = lighthouse.get('audits') or {}
    out = {}
    for key, lh_key in _LH_KEYS.items():
        failed, passed = [], []
        for ref in (categories.get(lh_key) or {}).get('auditRefs') or []:
            if ref.get('group') == 'hidden':
                continue
            audit_id = ref.get('id')
            if audit_id in METRIC_IDS:
                continue
            a = audits.get(audit_id) or {}
            score = a.get('score')
            if a.get('scoreDisplayMode') not in _SCORED_MODES or score is None:
                continue
            title = (a.get('title') or '').strip()
            if not title:
                continue
            if score >= 0.9:
                passed.append({'id': audit_id, 'title': title})
                continue
            description, learn_more = _strip_links(a.get('description'))
            failed.append({
                'id': audit_id,
                'title': title,
                'description': description[:600],
                'display_value': (a.get('displayValue') or '')[:120],
                'score': score,
                'learn_more': learn_more,
                'weight': ref.get('weight') or 0,
            })
        # Heaviest-weighted, lowest-scoring first: what moves the score.
        failed.sort(key=lambda f: (-f['weight'], f['score']))
        out[key] = {'failed': failed, 'passed': passed}

    metrics = []
    for audit_id in METRIC_IDS:
        a = audits.get(audit_id) or {}
        if not a.get('displayValue'):
            continue
        score = a.get('score')
        metrics.append({
            'id': audit_id,
            'title': a.get('title') or audit_id,
            'display_value': a.get('displayValue'),
            'score': score,
        })
    out['metrics'] = metrics
    return out


# ── Plain-English guidance ─────────────────────────────────────────────
# (why it matters, how to fix). Keyed by Lighthouse audit id. Newer
# Lighthouse "insight" ids share guidance with their older equivalents.

FIX_GUIDE = {
    # Performance
    'render-blocking-resources': (
        'The browser has to download and process these CSS and JavaScript '
        'files before it can show anything, so visitors stare at a blank '
        'screen.',
        'Inline the small amount of CSS needed for the top of the page, '
        'load the rest afterwards, and add "defer" to script tags that do '
        'not need to run before the page appears.'),
    'unused-css-rules': (
        'Your pages download styling code that is never used on that page, '
        'which slows the first load, especially on phones.',
        'Remove styles left over from old designs or unused plugins, and '
        'split CSS so each page only loads what it uses. Page builders and '
        'heavy themes are the usual cause.'),
    'unused-javascript': (
        'Visitors download JavaScript the page never runs. Every extra '
        'kilobyte has to be downloaded and parsed before the page responds.',
        'Remove plugins, widgets and tracking scripts you no longer use, '
        'and load the rest only on the pages that need them.'),
    'modern-image-formats': (
        'Your images are in older formats (JPEG, PNG) that are much larger '
        'than they need to be.',
        'Convert images to WebP or AVIF. This usually cuts image size by '
        '25 to 50 percent with no visible difference.'),
    'uses-optimized-images': (
        'Images are saved at a higher quality than anyone can see, making '
        'them heavier than necessary.',
        'Compress images before uploading (around 75 to 85 percent quality '
        'is plenty for photos), or use an image optimization plugin or '
        'build step.'),
    'uses-responsive-images': (
        'Phones are downloading images sized for a large desktop screen, '
        'often several times bigger than they display.',
        'Serve multiple sizes with srcset so each device downloads an '
        'image close to the size it actually shows.'),
    'offscreen-images': (
        'Images far down the page load immediately, competing with what '
        'the visitor actually sees first.',
        'Add loading="lazy" to images below the fold so they load only as '
        'the visitor scrolls toward them.'),
    'unminified-css': (
        'Your CSS files contain spaces and comments browsers do not need.',
        'Minify CSS as part of your build or with a caching plugin.'),
    'unminified-javascript': (
        'Your JavaScript files are larger than necessary because they are '
        'not minified.',
        'Minify JavaScript as part of your build or with a caching plugin.'),
    'uses-text-compression': (
        'Text files (HTML, CSS, JavaScript) are sent uncompressed, so they '
        'take longer to download.',
        'Turn on gzip or Brotli compression on your web server or host. '
        'It is usually a single setting.'),
    'server-response-time': (
        'Your server takes too long to start sending the page. Nothing can '
        'happen in the browser until it does.',
        'Add page caching, upgrade from crowded shared hosting, and check '
        'for slow plugins or database queries.'),
    'document-latency-insight': (
        'Your server takes too long to start sending the page, or the page '
        'is not compressed or goes through redirects first.',
        'Add page caching, turn on compression, remove redirects to the '
        'homepage, and move off crowded shared hosting if needed.'),
    'redirects': (
        'Visitors are bounced through one or more redirects before reaching '
        'the page, and each one adds a delay.',
        'Link directly to the final URL (https and with or without www, '
        'whichever is your real address) and remove redirect chains.'),
    'uses-rel-preconnect': (
        'The browser waits to connect to other servers (fonts, scripts) '
        'until it discovers them.',
        'Add <link rel="preconnect"> for the one or two third-party domains '
        'the page needs straight away.'),
    'efficient-animated-content': (
        'Animated GIFs are very large compared with video.',
        'Replace GIFs with short MP4 or WebM videos set to autoplay, loop '
        'and muted.'),
    'duplicated-javascript': (
        'The same JavaScript library is being loaded more than once.',
        'Find the plugins or bundles that each include it and load a single '
        'copy.'),
    'duplicated-javascript-insight': (
        'The same JavaScript library is being loaded more than once.',
        'Find the plugins or bundles that each include it and load a single '
        'copy.'),
    'legacy-javascript': (
        'Your scripts include extra code to support very old browsers that '
        'almost nobody uses any more.',
        'Update your build settings to target modern browsers, or update '
        'the plugins shipping the old code.'),
    'legacy-javascript-insight': (
        'Your scripts include extra code to support very old browsers that '
        'almost nobody uses any more.',
        'Update your build settings to target modern browsers, or update '
        'the plugins shipping the old code.'),
    'total-byte-weight': (
        'The page is heavy overall, which is slow on mobile data and costs '
        'visitors on limited plans.',
        'Start with images and video (usually the bulk), then remove unused '
        'scripts, fonts and plugins.'),
    'uses-long-cache-ttl': (
        'Returning visitors re-download files that have not changed.',
        'Set long cache lifetimes (a year) on images, CSS, JavaScript and '
        'fonts, and change the file name when a file changes.'),
    'cache-insight': (
        'Returning visitors re-download files that have not changed.',
        'Set long cache lifetimes (a year) on images, CSS, JavaScript and '
        'fonts, and change the file name when a file changes.'),
    'dom-size': (
        'The page is built from a very large number of HTML elements, which '
        'makes it slow to render and to respond to taps.',
        'Simplify the layout. Page builders nest many wrapper elements; a '
        'hand-built page needs far fewer.'),
    'dom-size-insight': (
        'The page is built from a very large number of HTML elements, which '
        'makes it slow to render and to respond to taps.',
        'Simplify the layout. Page builders nest many wrapper elements; a '
        'hand-built page needs far fewer.'),
    'bootup-time': (
        'The browser spends a long time running JavaScript before the page '
        'responds to taps and clicks.',
        'Remove unneeded scripts and third-party widgets, and delay '
        'non-essential ones (chat, analytics) until after the page loads.'),
    'mainthread-work-breakdown': (
        'The phone is busy processing the page for too long, so taps feel '
        'unresponsive.',
        'Reduce JavaScript first, then heavy CSS and very large pages.'),
    'font-display': (
        'Text stays invisible while custom fonts download.',
        'Add font-display: swap to your @font-face rules so text shows '
        'immediately in a fallback font.'),
    'font-display-insight': (
        'Text stays invisible while custom fonts download.',
        'Add font-display: swap to your @font-face rules so text shows '
        'immediately in a fallback font.'),
    'third-party-summary': (
        'Scripts from other companies (chat widgets, trackers, embeds) are '
        'slowing the page down.',
        'Remove the ones you do not use and delay the rest until the page '
        'has loaded.'),
    'third-parties-insight': (
        'Scripts from other companies (chat widgets, trackers, embeds) are '
        'slowing the page down.',
        'Remove the ones you do not use and delay the rest until the page '
        'has loaded.'),
    'lcp-lazy-loaded': (
        'The biggest image at the top of the page is lazy-loaded, so the '
        'browser waits to fetch the most important thing on screen.',
        'Remove loading="lazy" from the main hero image and add '
        'fetchpriority="high".'),
    'prioritize-lcp-image': (
        'The main image at the top of the page is discovered late.',
        'Preload it with <link rel="preload"> or add fetchpriority="high" '
        'to the image tag.'),
    'lcp-discovery-insight': (
        'The main image at the top of the page is discovered late or '
        'lazy-loaded.',
        'Put it directly in the HTML (not a CSS background or script), '
        'remove loading="lazy" and add fetchpriority="high".'),
    'largest-contentful-paint-element': (
        'The largest element at the top of the page takes too long to '
        'appear, which is what visitors judge your speed by.',
        'Make that element (usually the hero image or headline) as light '
        'as possible and load it first.'),
    'lcp-phases-insight': (
        'The largest element at the top of the page takes too long to '
        'appear, which is what visitors judge your speed by.',
        'Make that element (usually the hero image or headline) as light '
        'as possible and load it first.'),
    'layout-shifts': (
        'Content jumps around while the page loads, so people tap the wrong '
        'thing.',
        'Give images, videos, ads and embeds a fixed width and height, and '
        'avoid inserting banners above existing content.'),
    'layout-shift-elements': (
        'Content jumps around while the page loads, so people tap the wrong '
        'thing.',
        'Give images, videos, ads and embeds a fixed width and height, and '
        'avoid inserting banners above existing content.'),
    'cls-culprits-insight': (
        'Content jumps around while the page loads, so people tap the wrong '
        'thing.',
        'Give images, videos, ads and embeds a fixed width and height, and '
        'avoid inserting banners above existing content.'),
    'unsized-images': (
        'Images have no width and height set, so the page reflows as each '
        'one loads.',
        'Add width and height attributes to every <img> tag.'),
    'image-delivery-insight': (
        'Images are larger than they need to be: old formats, too much '
        'quality, or bigger than they display.',
        'Convert to WebP or AVIF, compress, and serve sizes that match the '
        'screen with srcset.'),
    'render-blocking-insight': (
        'The browser has to download and process these CSS and JavaScript '
        'files before it can show anything, so visitors stare at a blank '
        'screen.',
        'Inline the small amount of CSS needed for the top of the page, '
        'load the rest afterwards, and add "defer" to scripts.'),
    'network-dependency-tree-insight': (
        'Files load one after another in a chain instead of in parallel.',
        'Preload the critical files, cut long chains of imports, and '
        'preconnect to the domains you need first.'),
    'forced-reflow-insight': (
        'JavaScript forces the browser to recalculate the layout repeatedly.',
        'Batch reads and writes of layout properties in your scripts.'),
    'long-tasks': (
        'Long bursts of JavaScript freeze the page.',
        'Break up heavy scripts and delay non-essential ones.'),
    'non-composited-animations': (
        'Animations make the browser repaint the page each frame, which '
        'stutters on phones.',
        'Animate only transform and opacity.'),
    'viewport-insight': (
        'The page is not set up for mobile, so taps are delayed.',
        'Add <meta name="viewport" content="width=device-width, '
        'initial-scale=1">.'),

    # SEO
    'document-title': (
        'The page has no title. Titles are the clickable headline in Google '
        'results and the main signal of what the page is about.',
        'Give every page a unique title of about 50 to 60 characters, e.g. '
        '"AC Repair in Warner Robins, GA | Your Company".'),
    'meta-description': (
        'The page has no meta description, so Google picks random text from '
        'the page for your search snippet.',
        'Write a unique 140 to 160 character description for each page that '
        'says what you offer, where, and why to call.'),
    'http-status-code': (
        'The page returns an error status, so Google may not index it.',
        'Fix the page so it returns a normal 200 status, or redirect it to '
        'the right page.'),
    'link-text': (
        'Links use vague text like "click here" or "learn more", which tells '
        'Google and screen readers nothing.',
        'Use descriptive link text, e.g. "See our furnace repair services".'),
    'crawlable-anchors': (
        'Some links cannot be followed by Google because they are built '
        'with JavaScript instead of real URLs.',
        'Use normal <a href="/page/"> links for navigation.'),
    'is-crawlable': (
        'The page tells search engines not to index it. It will not appear '
        'in Google.',
        'Remove the "noindex" robots meta tag or header, unless hiding this '
        'page is intentional.'),
    'robots-txt': (
        'Your robots.txt file has errors, which can stop Google crawling '
        'your site properly.',
        'Fix the syntax of /robots.txt, or replace it with a simple valid '
        'one.'),
    'image-alt': (
        'Images have no alt text. Screen readers skip them and Google cannot '
        'tell what they show.',
        'Add a short description to every meaningful image (alt="Technician '
        'servicing an AC unit"), and alt="" to purely decorative ones.'),
    'hreflang': (
        'Language tags for alternate versions of the page are invalid.',
        'Fix or remove the hreflang tags.'),
    'canonical': (
        'The canonical tag points somewhere invalid, which confuses Google '
        'about which URL to rank.',
        'Point the canonical tag at the page\'s own preferred URL.'),
    'font-size': (
        'Text is too small to read on a phone without zooming.',
        'Use a base font size of at least 16px for body text.'),
    'tap-targets': (
        'Buttons and links are too small or too close together to tap '
        'accurately on a phone.',
        'Make tap targets at least 48 by 48 pixels with space between them.'),
    'viewport': (
        'The page is not set up for mobile screens.',
        'Add <meta name="viewport" content="width=device-width, '
        'initial-scale=1"> to the page head.'),

    # Accessibility
    'color-contrast': (
        'Some text does not stand out enough from its background, making it '
        'hard to read, especially outdoors on a phone or for older visitors.',
        'Darken the text or lighten the background until the contrast ratio '
        'is at least 4.5:1 (3:1 for large text). Free contrast checkers '
        'show the exact ratio.'),
    'button-name': (
        'Some buttons have no readable name, so screen readers announce them '
        'only as "button".',
        'Add visible text or an aria-label to every button, especially '
        'icon-only buttons like menus and close icons.'),
    'link-name': (
        'Some links have no readable text, so screen readers cannot say '
        'where they go.',
        'Add text or an aria-label to icon links (phone, social, logo).'),
    'label': (
        'Form fields have no labels, so screen-reader users do not know what '
        'to type, and the form is harder for everyone.',
        'Give every input a <label> connected to it with the for attribute.'),
    'html-has-lang': (
        'The page does not declare its language, so screen readers may read '
        'it with the wrong pronunciation.',
        'Add lang="en" to the <html> tag.'),
    'html-lang-valid': (
        'The page language attribute is invalid.',
        'Use a valid language code such as lang="en".'),
    'heading-order': (
        'Headings skip levels (for example h1 straight to h4), which breaks '
        'the outline screen-reader users navigate by.',
        'Use headings in order: one h1, then h2s, then h3s inside them.'),
    'meta-viewport': (
        'The page blocks pinch-to-zoom, which people with low vision rely on.',
        'Remove user-scalable=no and maximum-scale from the viewport tag.'),
    'list': (
        'Lists are not marked up correctly, so screen readers cannot announce '
        'them as lists.',
        'Make sure <ul> and <ol> only contain <li> elements.'),
    'listitem': (
        'List items appear outside of a list.',
        'Wrap <li> elements in a <ul> or <ol>.'),
    'frame-title': (
        'Embedded frames (maps, videos) have no title.',
        'Add a title attribute to each <iframe>, e.g. title="Map of our '
        'service area".'),
    'target-size': (
        'Buttons and links are too small or too close together to tap '
        'accurately.',
        'Make tap targets at least 24 by 24 pixels (48 is better) with space '
        'between them.'),
    'landmark-one-main': (
        'The page has no main landmark, so screen-reader users cannot jump '
        'straight to the content.',
        'Wrap the main content in a <main> element.'),
    'link-in-text-block': (
        'Links inside paragraphs can only be told apart by color.',
        'Underline links in body text, or make them clearly different in '
        'more than color.'),
    'select-name': (
        'Dropdown menus have no labels.',
        'Give every <select> a connected <label>.'),
    'input-image-alt': (
        'Image buttons have no alt text.',
        'Add alt text describing what the button does.'),
    'duplicate-id-aria': (
        'Several elements share the same id, which breaks labels and ARIA '
        'references.',
        'Give every id on the page a unique value.'),
    'tabindex': (
        'Some elements force a custom keyboard order, which confuses '
        'keyboard users.',
        'Remove tabindex values greater than 0.'),
    'aria-hidden-focus': (
        'Hidden elements can still receive keyboard focus.',
        'Remove focusable elements from aria-hidden regions, or remove '
        'aria-hidden.'),
    'label-content-name-mismatch': (
        'Visible button text does not match its accessible name, which breaks '
        'voice control.',
        'Make the aria-label start with the visible text.'),
    'identical-links-same-purpose': (
        'Links with the same text go to different places.',
        'Make link text describe each destination.'),
    'skip-link': (
        'The skip link is not focusable.',
        'Make sure the "skip to content" link points to an element that '
        'exists and can receive focus.'),

    # Best practices
    'is-on-https': (
        'Parts of the page load over insecure http. Browsers warn visitors '
        'and may block the content, which can silently break forms.',
        'Serve everything over https and update any hard-coded http:// '
        'links to images, scripts and fonts.'),
    'redirects-http': (
        'Visiting the http:// version of the site does not redirect to the '
        'secure https:// version.',
        'Add a permanent (301) redirect from http to https on the server.'),
    'errors-in-console': (
        'The page logs JavaScript errors. Something on it is broken, and it '
        'may be a form, menu or tracking script you rely on.',
        'Open the browser developer console on the page, read the errors, '
        'and fix or remove the scripts causing them.'),
    'image-aspect-ratio': (
        'Images are stretched or squashed.',
        'Display images at their natural proportions, or crop them to the '
        'shape you need before uploading.'),
    'image-size-responsive': (
        'Images look blurry on high-resolution phone screens.',
        'Serve images at least twice their displayed size, using srcset.'),
    'doctype': (
        'The page is missing a doctype, so browsers render it in an old '
        'compatibility mode.',
        'Add <!DOCTYPE html> as the first line of the HTML.'),
    'charset': (
        'The character set is missing or declared late.',
        'Add <meta charset="utf-8"> at the top of the <head>.'),
    'deprecations': (
        'The page uses browser features that are being removed and will '
        'stop working.',
        'Update the plugins or code listed so they use current APIs.'),
    'third-party-cookies': (
        'The page sets third-party cookies, which browsers are phasing out.',
        'Update or remove the embeds and trackers setting them.'),
    'inspector-issues': (
        'The browser flagged problems with the page (security, cookies, '
        'mixed content).',
        'Open the Issues panel in browser developer tools and fix what it '
        'lists.'),
    'geolocation-on-start': (
        'The page asks for the visitor\'s location as soon as it loads, '
        'which most people decline and many find off-putting.',
        'Only ask for location after the visitor clicks something that '
        'needs it.'),
    'notification-on-start': (
        'The page asks to send notifications as soon as it loads.',
        'Remove the prompt, or only show it after the visitor opts in.'),
    'paste-preventing-inputs': (
        'Some fields stop people pasting, which hurts password managers.',
        'Remove the paste-blocking script.'),
    'js-libraries': (
        'The page uses JavaScript libraries that may be outdated.',
        'Update libraries such as jQuery to current versions.'),
    'valid-source-maps': (
        'Large scripts ship without source maps.',
        'Publish source maps, or ignore this if you do not need them.'),
}


def guidance(item):
    """(why, how_to_fix) for one failed check, never blank."""
    if item.get('id') in FIX_GUIDE:
        return FIX_GUIDE[item['id']]
    why = item.get('description') or 'Google flagged this check as failing.'
    return why, ('Follow Google\'s guide for this check (link below), or reply '
                 'to this email and we will tell you what it involves.')


# ── Email context ──────────────────────────────────────────────────────

TIER_COLORS = {
    'strong': '#10B981',
    'needs-work': '#E8650A',
    'critical': '#EF4444',
}


def _tier(score):
    if score >= 90:
        return 'strong'
    if score >= 50:
        return 'needs-work'
    return 'critical'


def _metric_tier(score):
    if score is None:
        return 'needs-work'
    return _tier(round(score * 100))


def build_report_context(audit_lead):
    """Everything emails/audit_report.html needs for one AuditLead."""
    from public.views import (
        _CATEGORY_LABELS, _IMPACT_STATEMENTS, _TIER_LABELS, _audit_summary,
    )

    scores = {
        'performance': audit_lead.performance_score,
        'seo': audit_lead.seo_score,
        'best_practices': audit_lead.best_practices_score,
        'accessibility': audit_lead.accessibility_score,
    }
    details = audit_lead.details if isinstance(audit_lead.details, dict) else {}
    legacy = audit_lead.issues if isinstance(audit_lead.issues, dict) else {}

    categories = []
    total_failed = total_passed = 0
    for key in CATEGORY_ORDER:
        score = scores[key]
        tier = _tier(score)
        cat = details.get(key) or {}
        failed = cat.get('failed')
        passed = cat.get('passed') or []
        if failed is None:
            # Captured before full details existed: fall back to the
            # top-2 summary so the email still says what failed.
            failed = legacy.get(key) or []
        items = []
        for item in failed:
            why, fix = guidance(item)
            items.append({
                'title': (item.get('title') or '').replace('`', ''),
                'display_value': item.get('display_value', ''),
                'why': why,
                'fix': fix,
                'learn_more': item.get('learn_more', ''),
            })
        total_failed += len(items)
        total_passed += len(passed)
        categories.append({
            'key': key,
            'label': _CATEGORY_LABELS[key],
            'score': score,
            'tier': tier,
            'tier_label': _TIER_LABELS[tier],
            'color': TIER_COLORS[tier],
            'impact': _IMPACT_STATEMENTS[key][tier],
            'failed': items,
            'passed': [{**x, 'title': (x.get('title') or '').replace('`', '')}
                       for x in passed],
            # Improvement items always read as issues, even inside a
            # category whose overall score is Strong (green).
            'issue_color': (TIER_COLORS['needs-work'] if tier == 'strong'
                            else TIER_COLORS[tier]),
        })

    metrics = [{
        **m,
        'color': TIER_COLORS[_metric_tier(m.get('score'))],
    } for m in details.get('metrics') or []]

    summary = _audit_summary(audit_lead.url, scores)
    summary['color'] = TIER_COLORS.get(summary['tier'], '#E8650A')

    return {
        'audit_url': audit_lead.url,
        'summary': summary,
        'ai_paragraphs': [p.strip() for p in
                          (audit_lead.ai_review or '').split('\n\n')
                          if p.strip()],
        'categories': categories,
        'metrics': metrics,
        'total_failed': total_failed,
        'total_passed': total_passed,
        'has_details': bool(details),
    }
