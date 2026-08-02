from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify


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

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Audit Lead'
        verbose_name_plural = 'Audit Leads'

    def __str__(self):
        return f'{self.url} — perf {self.performance_score}'

    @property
    def average_score(self):
        return round(
            (self.performance_score + self.seo_score
             + self.best_practices_score + self.accessibility_score) / 4
        )
