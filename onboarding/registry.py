"""
Onboarding question registry — per-product_type definition of the
wizard's questions.

Each product_type maps to a list of section dicts. Each section has
a key, a title, and a list of question dicts. Question dicts shape:

    {
        'key':           str,    # unique within the registry, persists to DB
        'label':         str,    # question text shown to the operator
        'type':          'text'|'textarea'|'select'|'file'|'bool'|'cred_access',
        'placeholder':   str (optional),
        'help':          str (optional, shown under the input),
        'required':      bool   (default True),
        'skip_allowed':  bool   (default True),
        'choices':       [(value, label), ...]  (for type='select')
        'rows':          int (optional, for textarea)
        'cred_category': str (for type='cred_access', drives SetupTodo slug)
        'cred_type':     str (for type='cred_access', drives SetupTodo slug)
    }

Conditional rules let a section appear/disappear based on the
customer's tier or other state. See CONDITIONAL_RULES.
"""

# Per-tier maps that the conditional rules reference. Phase 4 will
# populate the question lists.
SOCIAL_TIER_CHANNELS = {
    'social-basic':    2,
    'social-standard': 3,
    'social-full':     5,
}

# ── Maintenance questions ──────────────────────────────────────────

_MAINTENANCE = [
    {
        'key': 'M1',
        'title': 'About your current site',
        'questions': [
            {'key': 'current_site_url', 'label': 'Site URL we will maintain',
             'type': 'text', 'placeholder': 'https://...',
             'required': True, 'skip_allowed': False},
            {'key': 'current_platform', 'label': 'Platform it is built on',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '— pick —'),
                 ('wordpress', 'WordPress'),
                 ('squarespace', 'Squarespace'),
                 ('wix', 'Wix'),
                 ('shopify', 'Shopify'),
                 ('webflow', 'Webflow'),
                 ('custom_html', 'Custom HTML'),
                 ('other', 'Other'),
             ]},
            {'key': 'current_site_age', 'label': 'Roughly how old is the site?',
             'type': 'select', 'skip_allowed': True,
             'choices': [
                 ('', '—'),
                 ('lt_1y', 'Under 1 year'),
                 ('1_to_3y', '1–3 years'),
                 ('3_to_7y', '3–7 years'),
                 ('gt_7y', 'Over 7 years'),
                 ('unknown', 'Not sure'),
             ]},
            {'key': 'accepts_payments', 'label': 'Does the site accept payments?',
             'type': 'bool', 'required': True, 'skip_allowed': False},
        ],
    },
    {
        'key': 'M2',
        'title': 'Access we will need',
        'intro': 'For anything you say "Yes I will share," after onboarding '
                 'you will be prompted via your To-Do List to add the '
                 'credentials to your secure vault.',
        'questions': [
            {'key': 'access_admin_login',
             'label': 'Site admin login (WordPress, Shopify, etc.)',
             'type': 'cred_access', 'required': True, 'skip_allowed': False,
             'cred_category': 'cms', 'cred_type': 'wordpress_admin'},
            {'key': 'access_hosting_panel',
             'label': 'Hosting control panel (cPanel, hosting login)',
             'type': 'cred_access', 'required': True, 'skip_allowed': False,
             'cred_category': 'server', 'cred_type': 'cpanel'},
            {'key': 'access_domain_registrar',
             'label': 'Domain registrar',
             'type': 'cred_access', 'required': True, 'skip_allowed': False,
             'cred_category': 'infra', 'cred_type': 'domain_registrar'},
            {'key': 'access_email_workspace',
             'label': 'Email accounts on this domain (Google Workspace / 365)',
             'type': 'cred_access', 'skip_allowed': True,
             'cred_category': 'infra', 'cred_type': 'email_workspace'},
            {'key': 'access_google_analytics',
             'label': 'Google Analytics — Yes (share) / No (we will set up)',
             'type': 'cred_access', 'required': True, 'skip_allowed': False,
             'cred_category': 'google', 'cred_type': 'google_analytics'},
            {'key': 'access_google_search_console',
             'label': 'Google Search Console — Yes / No',
             'type': 'cred_access', 'required': True, 'skip_allowed': False,
             'cred_category': 'google', 'cred_type': 'google_search_console'},
            {'key': 'access_payment_processor',
             'label': 'Payment processor — Read-only or No access',
             'help': 'Only shown if the site accepts payments. '
                     'Read-only or none.',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '—'),
                 ('read_only', 'Read-only access (vault)'),
                 ('no_access', 'No access at all'),
             ]},
        ],
    },
    {
        'key': 'M3',
        'title': 'Site health snapshot',
        'questions': [
            {'key': 'site_known_issues',
             'label': 'Anything currently broken or annoying?',
             'type': 'textarea', 'rows': 3, 'skip_allowed': True},
            {'key': 'site_recent_changes',
             'label': 'Anything changed in the last 30 days?',
             'type': 'textarea', 'rows': 3, 'skip_allowed': True},
            {'key': 'site_backup_today',
             'label': 'Backup schedule today — does anyone back it up?',
             'type': 'select', 'skip_allowed': True,
             'choices': [('', '—'), ('yes', 'Yes'), ('no', 'No'),
                         ('not_sure', 'Not sure')]},
            {'key': 'site_past_incidents',
             'label': 'Any known security issues or past incidents?',
             'type': 'textarea', 'rows': 3, 'skip_allowed': True},
            {'key': 'site_most_important',
             'label': 'Most important page or feature on the site',
             'type': 'text', 'required': True, 'skip_allowed': False},
        ],
    },
    {
        'key': 'M4',
        'title': 'How we will work together',
        'questions': [
            {'key': 'approval_workflow', 'label': 'Approval workflow',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '— pick —'),
                 ('every_change', 'Approve every change'),
                 ('routine_carte', 'Carte blanche on routine, ask for big'),
                 ('full_carte', 'Full carte blanche'),
             ]},
            {'key': 'emergency_contact',
             'label': 'Emergency contact name + phone (site-down at 2am)',
             'type': 'text', 'required': True, 'skip_allowed': False},
            {'key': 'preferred_contact_method',
             'label': 'Best contact for non-urgent', 'type': 'select',
             'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '—'),
                 ('email', 'Email'), ('slack', 'Slack'),
                 ('portal', 'Portal messages'), ('sms', 'SMS'),
             ]},
            {'key': 'content_update_cadence',
             'label': 'Content update cadence expected',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '—'),
                 ('as_needed', 'As needed'),
                 ('weekly', 'Weekly digest'),
                 ('monthly', 'Monthly digest'),
             ]},
        ],
    },
    {
        'key': 'M5',
        'title': 'Migration details',
        'intro': 'You opted into our hosting move-over — just a few extra '
                 'questions so the migration goes smoothly.',
        'questions': [
            {'key': 'migration_preferred_window',
             'label': 'Preferred migration window (low-traffic day/time)',
             'type': 'text', 'required': True, 'skip_allowed': False},
            {'key': 'migration_acceptable_downtime',
             'label': 'Acceptable downtime', 'type': 'select',
             'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '—'),
                 ('minutes', 'Minutes only'),
                 ('hours', 'A few hours'),
                 ('long', 'As long as it takes to do it right'),
             ]},
            {'key': 'migration_source_access',
             'label': 'Source server access', 'type': 'select',
             'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '—'),
                 ('vault_share', 'Yes — share via vault'),
                 ('same_as_hosting', 'Same as hosting panel above'),
                 ('need_help', 'Need help getting it'),
             ]},
            {'key': 'migration_db_size', 'label': 'Rough database size + type',
             'type': 'select', 'skip_allowed': True,
             'choices': [
                 ('', '—'),
                 ('small', 'Small (under 100MB)'),
                 ('medium', 'Medium (100MB–1GB)'),
                 ('large', 'Large (over 1GB)'),
                 ('unknown', 'Don\'t know'),
             ]},
            {'key': 'migration_custom_code',
             'label': 'Custom plugins, integrations, or scripts to know about?',
             'type': 'textarea', 'rows': 3, 'skip_allowed': True},
            {'key': 'migration_cdn',
             'label': 'CDN currently in use', 'type': 'select',
             'skip_allowed': True,
             'choices': [
                 ('', '—'),
                 ('none', 'None'),
                 ('cloudflare', 'Cloudflare'),
                 ('fastly', 'Fastly'),
                 ('other', 'Other'),
                 ('unknown', 'Don\'t know'),
             ]},
            {'key': 'migration_cron_jobs',
             'label': 'Any cron jobs or scheduled scripts running?',
             'type': 'textarea', 'rows': 3, 'skip_allowed': True},
            {'key': 'migration_email_hosting',
             'label': 'Where are the domain\'s email accounts hosted?',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '—'),
                 ('google_workspace', 'Google Workspace'),
                 ('m365', 'Microsoft 365'),
                 ('current_host', 'Hosted by current host'),
                 ('other', 'Other'),
                 ('none', 'None'),
             ]},
            {'key': 'migration_ssl_type',
             'label': 'SSL certificate today', 'type': 'select',
             'skip_allowed': True,
             'choices': [
                 ('', '—'),
                 ('letsencrypt', 'Let\'s Encrypt'),
                 ('paid', 'Paid cert'),
                 ('unsure', 'Not sure'),
             ]},
            {'key': 'migration_subdomains',
             'label': 'Subdomains (blog.x.com, store.x.com, etc.)',
             'type': 'textarea', 'rows': 2, 'skip_allowed': True},
            {'key': 'migration_webhooks',
             'label': 'Webhooks pointing at the site / APIs the site consumes',
             'type': 'textarea', 'rows': 3, 'skip_allowed': True},
        ],
    },
]


# ── Social Media questions ─────────────────────────────────────────

def _build_channel_questions(n):
    """Generate N channel-slot question groups for the S1 section."""
    out = []
    for i in range(1, n + 1):
        out.extend([
            {'key': f'channel_{i}_platform',
             'label': f'Channel {i} — platform',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '—'),
                 ('facebook', 'Facebook'),
                 ('instagram', 'Instagram'),
                 ('linkedin', 'LinkedIn'),
                 ('twitter', 'X (Twitter)'),
                 ('tiktok', 'TikTok'),
                 ('youtube', 'YouTube'),
                 ('pinterest', 'Pinterest'),
                 ('threads', 'Threads'),
                 ('other', 'Other'),
             ]},
            {'key': f'channel_{i}_handle',
             'label': f'Channel {i} — account URL or handle',
             'type': 'text', 'required': True, 'skip_allowed': False},
            {'key': f'channel_{i}_status',
             'label': f'Channel {i} — status', 'type': 'select',
             'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '—'),
                 ('active', 'Active — posting'),
                 ('dormant', 'Has account, dormant'),
                 ('need_create', 'Need to create'),
             ]},
            {'key': f'channel_{i}_followers',
             'label': f'Channel {i} — approximate follower count',
             'type': 'text', 'skip_allowed': True},
            {'key': f'channel_{i}_best_post',
             'label': f'Channel {i} — best-performing post (link or note)',
             'type': 'textarea', 'rows': 2, 'skip_allowed': True},
            {'key': f'channel_{i}_worst_post',
             'label': f'Channel {i} — cringe / worst post',
             'type': 'textarea', 'rows': 2, 'skip_allowed': True},
            {'key': f'channel_{i}_access',
             'label': f'Channel {i} — how will you give us access?',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '—'),
                 ('meta_bm', 'Meta Business Manager invite'),
                 ('linkedin_admin', 'LinkedIn page admin'),
                 ('vault_share', 'Direct login via vault'),
                 ('defer', 'I will figure it out later'),
             ]},
        ])
    return out


def _social_sections(channel_count):
    """Build the social-media section list, parametrised by tier's
    channel count."""
    return [
        {'key': 'S1', 'title': 'Your social channels',
         'questions': _build_channel_questions(channel_count)},
        {
            'key': 'S2',
            'title': 'Brand voice & content',
            'questions': [
                {'key': 'brand_voice_adjectives',
                 'label': 'Brand voice — 3-5 adjectives',
                 'type': 'text', 'required': True, 'skip_allowed': False},
                {'key': 'known_for',
                 'label': 'What you want to be known for',
                 'type': 'textarea', 'rows': 3, 'required': True,
                 'skip_allowed': False},
                {'key': 'content_pillars',
                 'label': '3-5 content pillars (topics to dominate)',
                 'type': 'textarea', 'rows': 4, 'required': True,
                 'skip_allowed': False},
                {'key': 'off_limits_topics',
                 'label': 'Topics that are OFF-LIMITS',
                 'type': 'textarea', 'rows': 3, 'required': True,
                 'skip_allowed': False},
                {'key': 'industry_sensitivities',
                 'label': 'Industry restrictions on advertising or '
                          'required disclosures',
                 'type': 'textarea', 'rows': 2, 'skip_allowed': True},
                {'key': 'accounts_love',
                 'label': '2 brands or accounts whose social you love',
                 'type': 'textarea', 'rows': 2, 'skip_allowed': True},
                {'key': 'accounts_cringe',
                 'label': '1 brand or account whose social makes you cringe',
                 'type': 'textarea', 'rows': 2, 'skip_allowed': True},
            ],
        },
        {
            'key': 'S3',
            'title': 'Operational policy',
            'intro': 'For your tier we run reply/DM management on your '
                     'behalf — every question below is required so we '
                     'know how to represent you.',
            'questions': [
                {'key': 'posting_frequency_expected',
                 'label': 'Posting frequency expected per channel per week',
                 'type': 'select', 'required': True, 'skip_allowed': False,
                 'choices': [
                     ('', '—'),
                     ('1_2', '1–2 posts/week'),
                     ('3_4', '3–4 posts/week'),
                     ('5_7', '5–7 posts/week'),
                 ]},
                {'key': 'approval_workflow_social',
                 'label': 'Approval workflow', 'type': 'select',
                 'required': True, 'skip_allowed': False,
                 'choices': [
                     ('', '—'),
                     ('every', 'Every post'),
                     ('weekly', 'Weekly batch'),
                     ('monthly', 'Monthly batch'),
                     ('carte', 'Carte blanche'),
                 ]},
                {'key': 'reply_policy',
                 'label': 'Reply policy on public comments', 'type': 'select',
                 'required': True, 'skip_allowed': False,
                 'choices': [
                     ('', '—'),
                     ('all', 'All replies'),
                     ('positive', 'Only positive'),
                     ('questions', 'Only questions'),
                     ('forward', 'Forward to you'),
                     ('dont_touch', 'Don\'t touch'),
                 ]},
                {'key': 'dm_policy',
                 'label': 'DM policy', 'type': 'select',
                 'required': True, 'skip_allowed': False,
                 'choices': [
                     ('', '—'),
                     ('all', 'All replies'),
                     ('positive', 'Only positive'),
                     ('questions', 'Only questions'),
                     ('forward', 'Forward to you'),
                     ('dont_touch', 'Don\'t touch'),
                 ]},
                {'key': 'crisis_protocol',
                 'label': 'Crisis protocol — viral negative / bad review — '
                          'who do we call within how many minutes?',
                 'type': 'textarea', 'rows': 3,
                 'required': True, 'skip_allowed': False},
            ],
        },
        {
            'key': 'S4',
            'title': 'Brand assets',
            'questions': [
                {'key': 'logo_upload_social',
                 'label': 'Logo upload (PNG or SVG)', 'type': 'text',
                 'placeholder': 'Will be a file upload in v2; paste URL for now',
                 'required': True, 'skip_allowed': False},
                {'key': 'brand_colors_social',
                 'label': 'Brand colors (hex codes, or "match my website")',
                 'type': 'text', 'required': True, 'skip_allowed': False},
                {'key': 'stock_photo_library',
                 'label': 'Headshots / action shots / work shots we can use?',
                 'type': 'textarea', 'rows': 3, 'skip_allowed': True},
                {'key': 'photo_shoot_budget',
                 'label': 'Can we hire a local photographer if needed? '
                          'Budget per shoot?',
                 'type': 'text', 'skip_allowed': True},
                {'key': 'existing_graphics_templates',
                 'label': 'Existing Canva account or brand guide?',
                 'type': 'text', 'skip_allowed': True},
            ],
        },
        {
            'key': 'S5',
            'title': 'Campaign calendar',
            'questions': [
                {'key': 'upcoming_90_day_events',
                 'label': 'Events, launches, or promotions in the next 90 days',
                 'type': 'textarea', 'rows': 3, 'skip_allowed': True},
                {'key': 'annual_recurring_dates',
                 'label': 'Annual recurring dates '
                          '(anniversaries, industry events)',
                 'type': 'textarea', 'rows': 3, 'skip_allowed': True},
                {'key': 'lead_magnets',
                 'label': 'Offers / lead magnets we should promote',
                 'type': 'textarea', 'rows': 3, 'skip_allowed': True},
            ],
        },
    ]


# The registry is built dynamically when get_sections() is called for
# social_media so the channel count matches the user's tier.
ONBOARDING_QUESTIONS = {
    'maintenance':    _MAINTENANCE,
    'social_media':   _social_sections(3),  # default 3 — overridden below
    'website_design': [],
}


# Override get_sections() to dispatch social_media to the right tier.
_get_sections_orig = None  # set below to avoid forward-ref issues


def _legacy_get_sections(product_type, tier_slug=None):
    """Python-defined fallback (used only if the DB tables are empty)."""
    if product_type == 'social_media':
        count = SOCIAL_TIER_CHANNELS.get(tier_slug, 3)
        return _social_sections(count)
    return ONBOARDING_QUESTIONS.get(product_type, [])


def _q_to_dict(q, channel_index=None):
    """Turn an OnboardingQuestionDef into the dict shape the wizard
    consumes. For channel-template sections, prefix the key + label with
    the channel index so answers persist as channel_<n>_<key>."""
    key, label = q.key, q.label
    if channel_index is not None:
        key = f'channel_{channel_index}_{q.key}'
        label = f'Channel {channel_index} — {q.label}'
    d = {
        'key': key, 'label': label, 'type': q.qtype,
        'required': q.required, 'skip_allowed': q.skip_allowed,
    }
    if q.help:
        d['help'] = q.help
    if q.placeholder:
        d['placeholder'] = q.placeholder
    if q.rows:
        d['rows'] = q.rows
    if q.choices:
        d['choices'] = [tuple(c) for c in q.choices]
    if q.cred_category:
        d['cred_category'] = q.cred_category
    if q.cred_type:
        d['cred_type'] = q.cred_type
    return d


def _db_sections(product_type, tier_slug):
    """Build the section list from the DB-backed registry, or None if the
    tables are empty / unavailable (→ caller falls back to the Python
    definitions)."""
    try:
        from onboarding.question_models import OnboardingSectionDef
        secs = list(
            OnboardingSectionDef.objects
            .filter(product_type=product_type, is_active=True)
            .order_by('sort_order', 'key')
            .prefetch_related('questions'))
    except Exception:
        return None
    if not secs:
        return None

    channel_count = SOCIAL_TIER_CHANNELS.get(tier_slug, 3)
    out = []
    for sec in secs:
        active_qs = sorted(
            (q for q in sec.questions.all() if q.is_active),
            key=lambda q: (q.sort_order, q.id))
        if sec.is_channel_template:
            qlist = []
            for i in range(1, channel_count + 1):
                qlist.extend(_q_to_dict(q, channel_index=i)
                             for q in active_qs)
        else:
            qlist = [_q_to_dict(q) for q in active_qs]
        out.append({
            'key': sec.key,
            'title': sec.title,
            'intro': sec.intro,
            'questions': qlist,
            '_flags': {
                'tier_visibility': sec.tier_visibility or [],
                'requires_hosting_moveover': sec.requires_hosting_moveover,
                'skip_if_completed_intake': sec.skip_if_completed_intake,
            },
        })
    return out


def get_sections(product_type, tier_slug=None):
    """
    Return the section list for a product_type. Reads the DB-backed
    registry first; falls back to the Python definitions only if the DB
    is empty. For social_media, S1 is expanded to the tier's channel
    count.
    """
    db = _db_sections(product_type, tier_slug)
    if db is not None:
        return db
    return _legacy_get_sections(product_type, tier_slug)


# Conditional show/skip rules keyed by section.
# Each predicate takes an Onboarding instance and returns True/False.
CONDITIONAL_RULES = {
    'maintenance': {
        'show_section_if': {
            # M5 (migration) only when hosting move-over was purchased.
            # Phase 5/6 will surface a real flag; until then we look for
            # a HostingMoveOver SetupTodo on the user.
            'M5': lambda ob: _has_hosting_moveover(ob.user),
        },
    },
    'social_media': {
        'show_section_if': {
            # S3 (reply/DM policy) only for Standard + Full tiers.
            'S3': lambda ob: ob.tier_slug in (
                'social-standard', 'social-full'),
        },
        'skip_section_if': {
            # S4 (brand assets) skipped if the user has an existing
            # IntakeResponse (i.e. they already built with us, so we
            # have their logo / colors already).
            'S4': lambda ob: _has_completed_intake(ob.user),
        },
    },
}


def visible_sections(onboarding):
    """
    Filter the registry's sections by the conditional rules for this
    specific Onboarding instance. Returns a list of section dicts.
    """
    sections = get_sections(onboarding.product_type, onboarding.tier_slug)
    rules = CONDITIONAL_RULES.get(onboarding.product_type, {})
    show_if = rules.get('show_section_if', {})
    skip_if = rules.get('skip_section_if', {})

    out = []
    for sec in sections:
        flags = sec.get('_flags')
        if flags is not None:
            # DB-backed conditional visibility.
            tiers = flags.get('tier_visibility') or []
            if tiers and onboarding.tier_slug not in tiers:
                continue
            if (flags.get('requires_hosting_moveover')
                    and not _has_hosting_moveover(onboarding.user)):
                continue
            if (flags.get('skip_if_completed_intake')
                    and _has_completed_intake(onboarding.user)):
                continue
        else:
            # Legacy Python rules (fallback path).
            key = sec['key']
            if key in show_if and not show_if[key](onboarding):
                continue
            if key in skip_if and skip_if[key](onboarding):
                continue
        out.append(sec)
    return out


def total_visible_questions(onboarding):
    """Count of every question across every visible section."""
    return sum(len(s['questions']) for s in visible_sections(onboarding))


def find_section(onboarding, section_key):
    """Return the section dict by key, or None if not visible."""
    for s in visible_sections(onboarding):
        if s['key'] == section_key:
            return s
    return None


# ── Private helpers used by conditional predicates ───────────────────

def _has_hosting_moveover(user):
    """Cheap check — does the user have an open or completed HostingMoveOver
    SetupTodo? Defensive: returns False if SetupTodo model doesn't exist
    yet (Phase 3) so the registry loads on a fresh checkout."""
    try:
        from onboarding.todo_models import SetupTodo
        return SetupTodo.objects.filter(
            user=user,
            task_type='hosting_moveover',
        ).exists()
    except Exception:
        return False


def _has_completed_intake(user):
    """True when the user already filled out the legacy IntakeResponse
    (i.e. they're an existing website-build client) so we don't need
    to re-ask for brand assets."""
    try:
        from clients.account_models import Website
        # The intake is per website, so the answer is per website too.
        site = (Website.objects
                .filter(account__user=user)
                .select_related('intake_new')
                .order_by('created_at')
                .first())
        if site is None:
            return False
        intake = getattr(site, 'intake_new', None)
        return bool(intake and intake.completed)
    except Exception:
        return False
