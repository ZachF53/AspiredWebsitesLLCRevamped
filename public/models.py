from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify


class City(models.Model):
    """
    One /locations/<city>/ page's worth of content.

    Backs the generic `location_city` view (public/views.py) — one view
    and one template now serve every city page. Before this model, each
    city was a fully hand-written view + template with its own copy,
    schema and section structure; this model exists to hold that same
    copy as data instead of markup, so a new city doesn't need a new
    view/template pair.

    URL paths and their `name=` reversals are UNCHANGED — each city
    still gets its own literal `path()` entry in public/urls.py (see
    the comment there), routed to the one shared view with this row's
    `slug` passed as a URL kwarg. `url_name` stores the matching
    reversible name so `get_absolute_url()` and cross-links elsewhere
    (e.g. case_study_detail.html) don't have to special-case city slugs.

    Several fields hold raw HTML (``*_html``) rather than being broken
    into further sub-fields. The three original pages disagreed on
    structure as much as content — Warner Robins doesn't have a "Who We
    Work With" section at all, Atlanta has a Georgia cross-link that San
    Antonio doesn't — so the sections that varied in shape as well as
    wording are stored as blocks an editor writes directly, matching
    what was already hand-written per page. A block left blank hides
    that section entirely rather than rendering an empty one.
    """

    name = models.CharField(max_length=100)
    slug = models.SlugField(max_length=100, unique=True)
    state = models.CharField(max_length=2, blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    # The reversible URL name for this city's page, e.g.
    # 'public:location_san_antonio' — see public/urls.py. Used by
    # get_absolute_url() and by any page that links to a specific city.
    url_name = models.CharField(max_length=100, blank=True)

    # ── <head> ───────────────────────────────────────────────────────
    meta_title = models.CharField(max_length=200, blank=True)
    meta_description = models.CharField(max_length=300, blank=True)
    og_title = models.CharField(max_length=200, blank=True)
    og_description = models.CharField(max_length=300, blank=True)

    # ── Hero ─────────────────────────────────────────────────────────
    hero_eyebrow = models.CharField(max_length=100, blank=True)
    hero_heading_html = models.CharField(
        max_length=300, blank=True,
        help_text='Full <h1> inner markup, e.g. \'Web Design for '
                  '<span class="accent">San Antonio</span> Businesses\'.',
    )
    hero_lead = models.TextField(blank=True)

    # ── "How we actually work here" section ─────────────────────────
    honesty_eyebrow = models.CharField(
        max_length=100, blank=True, default='Straight Up')
    honesty_heading = models.CharField(max_length=200, blank=True)
    honesty_subheading = models.CharField(max_length=300, blank=True)
    honesty_html = models.TextField(
        blank=True,
        help_text='Raw HTML for this section\'s card(s).',
    )

    # ── Proof (case studies) section ─────────────────────────────────
    proof_eyebrow = models.CharField(
        max_length=100, blank=True, default='Proof')
    proof_heading = models.CharField(
        max_length=200, blank=True,
        default='Work You Can Go And Look At',
    )
    proof_subheading = models.CharField(max_length=300, blank=True)

    # ── "What You Get" value-tile section — blank heading hides it ──
    value_tiles_eyebrow = models.CharField(max_length=100, blank=True)
    value_tiles_heading = models.CharField(max_length=200, blank=True)

    # ── Secondary section — "Who We Work With" / local-need cards /
    # whatever this city's second proof-of-fit section actually is.
    # Blank heading hides the section.
    secondary_eyebrow = models.CharField(max_length=100, blank=True)
    secondary_heading = models.CharField(max_length=200, blank=True)
    secondary_html = models.TextField(blank=True)

    # ── Cross-link to sibling city pages — blank hides it ───────────
    cross_link_html = models.TextField(blank=True)

    # ── CTA ──────────────────────────────────────────────────────────
    cta_heading = models.CharField(max_length=200, blank=True)
    cta_body_html = models.TextField(blank=True)

    # ── Service schema (Master Plan §8/D8 — one Organization node,
    # everything else references it; this feeds the page's Service
    # block, never a second LocalBusiness/Organization node) ─────────
    schema_service_name = models.CharField(max_length=200, blank=True)
    schema_description = models.TextField(blank=True)
    schema_area_served = models.JSONField(
        default=list, blank=True,
        help_text=('List of {"type": "City"|"AdministrativeArea"|'
                   '"State", "name": "..."} for the Service schema\'s '
                   'areaServed.'),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['sort_order', 'name']
        verbose_name = 'City'
        verbose_name_plural = 'Cities'

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        if self.url_name:
            return reverse(self.url_name)
        return ''


class Article(models.Model):
    """
    An /insights/ post — Aspired's own marketing blog.

    Deliberately NOT reporting.BlogPost. That model is the AI-draft
    pipeline for CLIENT blogs sold as a maintenance deliverable: it is
    keyed to a client, carries generation parameters, and moves through
    a staff review workflow. Reusing it would tangle our own editorial
    content with a client-facing product and make "whose post is this?"
    a query rather than a fact.

    Master Plan §12 sets a hard quality gate — every article must carry
    firsthand expertise, real examples, a decision framework or original
    data, plus internal links to its commercial page. §11 additionally
    requires named authorship on every article for E-E-A-T, which is why
    `author_name` is a required field with no anonymous default.
    """

    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('published', 'Published'),
    ]

    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    # The short promise that appears on the index card AND as the meta
    # description — one field so the two can never drift apart.
    summary = models.CharField(
        max_length=300,
        help_text='One or two sentences. Used on the index card and as '
                  'the meta description, so write it to earn a click.')
    body = models.TextField(
        help_text='HTML. Headings should start at <h2> — the article '
                  'title is the page\'s only <h1>.')

    author_name = models.CharField(max_length=120, default='Zachery Long')
    author_title = models.CharField(
        max_length=160, blank=True,
        default='Founder & Lead Developer, CISSP')

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default='draft')
    published_at = models.DateTimeField(null=True, blank=True)

    # The commercial page this article exists to support. §12 requires
    # every supporting article to link back to its money page, and §8
    # requires the internal-link cluster to be wired both ways.
    related_url = models.CharField(
        max_length=200, blank=True,
        help_text='Path of the commercial page this article supports, '
                  'e.g. /pricing/. Rendered as the in-article CTA.')
    related_label = models.CharField(max_length=120, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-published_at', '-created_at']

    def __str__(self):
        return self.title[:60]

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title)[:210] or 'article'
            slug, n = base, 2
            while Article.objects.filter(slug=slug).exclude(
                    pk=self.pk).exists():
                slug = f'{base}-{n}'
                n += 1
            self.slug = slug
        if self.status == 'published' and self.published_at is None:
            self.published_at = timezone.now()
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse('public:insight_detail', kwargs={'slug': self.slug})


class AuditLead(models.Model):
    """
    Captures a website audit run + (optionally) the email address of someone
    who asked for the full report. The URL is only persisted when the visitor
    opts in by submitting their email on the results page.
    """

    url = models.URLField(max_length=500)
    performance_score = models.PositiveSmallIntegerField()
    seo_score = models.PositiveSmallIntegerField()
    best_practices_score = models.PositiveSmallIntegerField()
    accessibility_score = models.PositiveSmallIntegerField()
    issues = models.JSONField(default=list, blank=True)
    email = models.EmailField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # ── Follow-up sequence (public/audit_sequence.py) ──────────────────
    #
    # These people ASKED for the report, so this is a warm sequence and
    # not cold outreach: it goes out through SendGrid from the main
    # domain, which is where they expect it from, rather than through
    # Instantly from a secondary sending domain.
    #
    # It is still commercial email, so it still needs a working opt-out.
    # The original report had none, which was a CAN-SPAM gap on the one
    # message the business sends most often.
    report_sent_at = models.DateTimeField(null=True, blank=True)
    followup_1_sent_at = models.DateTimeField(null=True, blank=True)
    followup_2_sent_at = models.DateTimeField(null=True, blank=True)

    unsubscribed = models.BooleanField(default=False, db_index=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Audit Lead'
        verbose_name_plural = 'Audit Leads'

    def __str__(self):
        return f'{self.url} — perf {self.performance_score}'

    @property
    def worst_category(self):
        """(key, score) of the lowest-scoring category.

        The sequence is built around this: a site failing accessibility
        needs a different conversation from one that is merely slow, and
        sending both people the same follow-up wastes the one thing that
        makes this sequence worth reading.
        """
        scores = {
            'performance': self.performance_score,
            'seo': self.seo_score,
            'accessibility': self.accessibility_score,
            'best_practices': self.best_practices_score,
        }
        key = min(scores, key=scores.get)
        return key, scores[key]

    @property
    def is_healthy(self):
        """True when nothing here is worth writing a follow-up about.

        A site scoring well everywhere gets the report and then silence.
        Manufacturing a problem to justify a follow-up is how a useful
        tool turns into a sales funnel people resent.
        """
        return self.average_score >= 85 and self.worst_category[1] >= 70

    @property
    def average_score(self):
        return round(
            (self.performance_score + self.seo_score
             + self.best_practices_score + self.accessibility_score) / 4
        )
