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


# ── Review System questions ────────────────────────────────────────
#
# This wizard has one job: collect everything needed to (a) submit the
# client's A2P 10DLC brand and campaign and (b) build their GoHighLevel
# workflow. GHL does the actual sending — see reviews/models.py for the
# boundary.
#
# Two constraints shaped these questions:
#
#  1. Only text/textarea/select/bool/cred_access render properly in
#     onboarding/templates/onboarding/step.html. There is no working
#     'file' type, so nothing here needs an upload — the A2P brand match
#     is on EIN digits plus the exact legal name, not a document.
#  2. R5 is a legal record. Every question there is required with
#     skip_allowed=False, and completing the wizard writes a
#     ReviewConsentAttestation snapshotting what the client was shown.

# Starter wording lives in reviews.templating alongside the guard that
# validates it, so the default copy and the rules it must satisfy can
# never drift apart. Safe to import at module level: templating.py pulls
# in no models and no onboarding code.
from reviews import templating as _rt  # noqa: E402

_REVIEW_SYSTEM = [
    {
        'key': 'R1',
        'title': 'Your business',
        'intro': 'Phone carriers require every business that sends text '
                 'messages to be registered first. These details go on that '
                 'registration and have to match your IRS paperwork exactly '
                 '— if they do not, it gets rejected and we start over, '
                 'which costs about a week.',
        'questions': [
            {'key': 'legal_business_name',
             'label': 'Legal business name',
             'type': 'text', 'required': True, 'skip_allowed': False,
             'placeholder': 'Johnson Heating & Cooling LLC',
             'help': 'Exactly as it appears on your IRS EIN letter — not '
                     'your trading name. "LLC" or "Inc" included if it is '
                     'part of the registered name.'},
            {'key': 'business_entity_type',
             'label': 'Business type',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '— pick —'),
                 ('sole_prop', 'Sole proprietor (no EIN)'),
                 ('llc', 'LLC'),
                 ('s_corp', 'S-Corporation'),
                 ('c_corp', 'C-Corporation'),
                 ('partnership', 'Partnership'),
                 ('non_profit', 'Non-profit'),
             ],
             'help': 'Sole proprietors are registered differently and do '
                     'not need an EIN.'},
            {'key': 'ein',
             'label': 'EIN (federal tax ID)',
             'type': 'text', 'required': False, 'skip_allowed': True,
             'placeholder': '12-3456789',
             'help': 'Skip this if you are a sole proprietor without one. '
                     'Stored encrypted and used only for the carrier '
                     'registration.'},
            {'key': 'dba_name',
             'label': 'Trading name, if different',
             'type': 'text', 'required': False, 'skip_allowed': True,
             'placeholder': 'Johnson HVAC'},
            {'key': 'business_street', 'label': 'Street address',
             'type': 'text', 'required': True, 'skip_allowed': False},
            {'key': 'business_city', 'label': 'City',
             'type': 'text', 'required': True, 'skip_allowed': False},
            {'key': 'business_state', 'label': 'State',
             'type': 'text', 'required': True, 'skip_allowed': False,
             'placeholder': 'TX'},
            {'key': 'business_zip', 'label': 'ZIP code',
             'type': 'text', 'required': True, 'skip_allowed': False,
             'placeholder': '78205'},
            {'key': 'business_phone',
             'label': 'Your main business phone',
             'type': 'text', 'required': True, 'skip_allowed': False,
             'help': 'The number your customers already know. We register a '
                     'separate number for sending the review requests, and '
                     'point any replies back to you.'},
            {'key': 'business_website_url',
             'label': 'Your website',
             'type': 'text', 'required': True, 'skip_allowed': False,
             'placeholder': 'https://...',
             'help': 'Carriers check this page during registration. If you '
                     'do not have one yet, tell us on the call.'},
            {'key': 'support_email',
             'label': 'Where customer replies should go',
             'type': 'text', 'required': True, 'skip_allowed': False,
             'placeholder': 'office@yourcompany.com'},
            {'key': 'business_timezone',
             'label': 'Your timezone',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '— pick —'),
                 ('America/New_York', 'Eastern'),
                 ('America/Chicago', 'Central'),
                 ('America/Denver', 'Mountain'),
                 ('America/Phoenix', 'Arizona (no daylight saving)'),
                 ('America/Los_Angeles', 'Pacific'),
                 ('America/Anchorage', 'Alaska'),
                 ('Pacific/Honolulu', 'Hawaii'),
             ],
             'help': 'So requests never go out at nine at night.'},
        ],
    },
    {
        'key': 'R2',
        'title': 'Where your reviews go',
        'intro': 'This is the link your customers will be sent to. Getting '
                 'it wrong means every request lands somewhere useless, so '
                 'we check it.',
        'questions': [
            {'key': 'google_review_url',
             'label': 'Your Google review link',
             'type': 'text', 'required': True, 'skip_allowed': False,
             'placeholder': 'https://g.page/r/.../review',
             'help': 'In your Google Business Profile, click "Ask for '
                     'reviews" and copy the link. Paste the whole thing.'},
            {'key': 'google_business_name',
             'label': 'The name on your Google listing',
             'type': 'text', 'required': True, 'skip_allowed': False,
             'help': 'Can differ from your legal name — this is whatever '
                     'customers see on Google.'},
        ],
    },
    {
        'key': 'R3',
        'title': 'Your job system',
        'intro': 'A review request goes out when a job is marked complete. '
                 'We need to know what to listen to.',
        'questions': [
            {'key': 'job_system',
             'label': 'What do you use to track jobs?',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '— pick —'),
                 ('servicetitan', 'ServiceTitan'),
                 ('housecall_pro', 'Housecall Pro'),
                 ('jobber', 'Jobber'),
                 ('fieldedge', 'FieldEdge'),
                 ('service_fusion', 'Service Fusion'),
                 ('workiz', 'Workiz'),
                 ('other', 'Something else'),
                 ('none_paper', 'Nothing — paper or whiteboard'),
             ]},
            {'key': 'job_system_other',
             'label': 'Which one?',
             'type': 'text', 'required': False, 'skip_allowed': True},
            {'key': 'job_trigger_method',
             'label': 'How should we hear about completed jobs?',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '— pick —'),
                 ('native', 'My software can call a web address directly'),
                 ('zapier', 'Through Zapier'),
                 ('manual', 'I will mark them myself'),
                 ('unsure', 'Not sure — help me work it out'),
             ],
             'help': '"Not sure" is a perfectly good answer; we will sort '
                     'it out together on the setup call.'},
            {'key': 'who_configures',
             'label': 'Who sets that up?',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '— pick —'),
                 ('client', 'I will do it'),
                 ('aspired_with_access', 'You do it — I will share access'),
                 ('aspired_needs_call', 'You do it — let us get on a call'),
             ]},
            {'key': 'job_system_access',
             'label': 'Job system login',
             'type': 'cred_access', 'required': False, 'skip_allowed': True,
             'cred_category': 'jobs', 'cred_type': 'job_system_admin',
             'help': 'Only if you want us to configure it for you.'},
            {'key': 'avg_jobs_per_week',
             'label': 'Roughly how many jobs a week?',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '— pick —'),
                 ('lt_10', 'Under 10'),
                 ('10_25', '10 to 25'),
                 ('25_50', '25 to 50'),
                 ('50_100', '50 to 100'),
                 ('gt_100', 'More than 100'),
             ],
             'help': 'Carriers cap how many texts a new number can send '
                     'per day. This tells us what to ask them for.'},
        ],
    },
    {
        'key': 'R4',
        'title': 'What your customers will receive',
        'intro': 'You can edit this any time later. The review link and '
                 'the opt-out line are always included and cannot be '
                 'removed — carriers require the opt-out, and the link is '
                 'the whole point.',
        'questions': [
            {'key': 'sms_body_template',
             'label': 'Text message wording',
             'type': 'textarea', 'rows': 4,
             'required': True, 'skip_allowed': False,
             'default': _rt.DEFAULT_SMS,
             'help': 'Available placeholders: {customer_first_name}, '
                     '{business_name}, {job_type}, {technician}, '
                     '{review_link}. No discounts or incentives — Google '
                     'forbids offering anything in exchange for a review.'},
            {'key': 'email_subject_template',
             'label': 'Email subject',
             'type': 'text', 'required': True, 'skip_allowed': False,
             'default': _rt.DEFAULT_EMAIL_SUBJECT},
            {'key': 'email_body_template',
             'label': 'Email wording',
             'type': 'textarea', 'rows': 6,
             'required': True, 'skip_allowed': False,
             'default': _rt.DEFAULT_EMAIL_BODY},
            {'key': 'reminder_enabled',
             'label': 'Send one reminder 3 days later if they never opened '
                      'the link?',
             'type': 'bool', 'required': True, 'skip_allowed': False},
            {'key': 'send_delay',
             'label': 'When should the request go out?',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '— pick —'),
                 ('immediate', 'As soon as the job is marked complete'),
                 ('1_hour', 'About an hour afterwards'),
                 ('2_hours', 'About two hours afterwards'),
                 ('next_morning', 'The next morning'),
             ]},
            {'key': 'technician_name_in_message',
             'label': 'Mention the technician by name?',
             'type': 'bool', 'required': False, 'skip_allowed': True},
        ],
    },
    {
        'key': 'R5',
        'title': 'Consent and authorisation',
        'intro': 'Texting a customer who never agreed to hear from you is '
                 'against federal law (the TCPA), and the penalties are '
                 'per message. We need you to confirm the basics below '
                 'before anything sends. None of this is optional.',
        'questions': [
            {'key': 'consent_work_order_discloses',
             'label': 'Your work order, invoice, or service agreement tells '
                      'the customer you may text them about their service.',
             'type': 'bool', 'required': True, 'skip_allowed': False},
            {'key': 'consent_numbers_lawful',
             'label': 'Every number you send us was given to you by that '
                      'customer for their own service, and you have the '
                      'right to text it.',
             'type': 'bool', 'required': True, 'skip_allowed': False},
            {'key': 'consent_no_purchased_lists',
             'label': 'You will never send us numbers from a purchased, '
                      'rented, or scraped list.',
             'type': 'bool', 'required': True, 'skip_allowed': False},
            {'key': 'consent_no_gating',
             'label': 'You understand we ask every customer the same way, '
                      'with no filtering by expected rating and no '
                      'incentives, because Google forbids both.',
             'type': 'bool', 'required': True, 'skip_allowed': False},
            {'key': 'consent_honor_stop',
             'label': 'You understand that a customer replying STOP is '
                      'permanent, and we will never text that number again.',
             'type': 'bool', 'required': True, 'skip_allowed': False},
            {'key': 'attestation_typed_name',
             'label': 'Type your full legal name to sign',
             'type': 'text', 'required': True, 'skip_allowed': False},
            {'key': 'attestation_title',
             'label': 'Your title at the business',
             'type': 'text', 'required': False, 'skip_allowed': True,
             'placeholder': 'Owner'},
        ],
    },
]


# The registry is built dynamically when get_sections() is called for
# social_media so the channel count matches the user's tier.
# ── AI Voice Receptionist questions ─────────────────────────────────
#
# Two notes before editing this list.
#
#  1. Every answer here becomes GHL Voice AI agent configuration. The
#     mapping is in voice/materialise.py and the push payload shape is in
#     voice/schedule.py. A new question that nothing reads is dead weight
#     on a form the client has to fill in before their phone is covered,
#     so add questions only when something consumes them.
#  2. V3 is the question clients get wrong. "Should it answer first, or
#     ring you first?" sounds like a preference and is actually a
#     capability choice: spam screening is only possible when the agent
#     answers, because in ring-you-first the robocall has already reached
#     the owner's phone. The help text says so on both options rather
#     than burying it, and ai_first is the default.
#  3. V5 is a legal record. Completing the wizard writes a
#     VoiceConsentAttestation snapshotting what the client was shown.

_VOICE_RECEPTIONIST = [
    {
        'key': 'V1',
        'title': 'Your business and your number',
        'intro': 'The basics your receptionist needs before it can pick up '
                 'a call, plus how we get calls to it.',
        'questions': [
            {'key': 'legal_business_name',
             'label': 'Business name',
             'type': 'text', 'required': True, 'skip_allowed': False,
             'placeholder': 'Johnson Heating & Cooling',
             'help': 'What you want the receptionist to say when it '
                     'answers.'},
            {'key': 'business_phone',
             'label': 'The number your customers call now',
             'type': 'text', 'required': True, 'skip_allowed': False,
             'placeholder': '(210) 555-0142'},
            {'key': 'number_strategy',
             'label': 'How should we handle that number?',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '— pick —'),
                 ('forward_existing',
                  'Forward it to us (easiest, nothing changes for you)'),
                 ('new_number',
                  'Give me a new number to advertise'),
                 ('port_existing',
                  'Move my number over to you completely'),
             ],
             'help': 'Forwarding is what most shops pick. You keep your '
                     'number and your carrier, and you can switch the '
                     'forwarding off whenever you want. It does mean one '
                     'setup step with your phone company, which we walk '
                     'you through. Porting takes about two weeks, after '
                     'which your number lives with us: more work up '
                     'front, but it is the only option where we control '
                     'the ring timing exactly.'},
            {'key': 'business_timezone',
             'label': 'Your timezone',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'choices': [
                 ('', '— pick —'),
                 ('America/New_York', 'Eastern'),
                 ('America/Chicago', 'Central'),
                 ('America/Denver', 'Mountain'),
                 ('America/Phoenix', 'Arizona (no daylight saving)'),
                 ('America/Los_Angeles', 'Pacific'),
                 ('America/Anchorage', 'Alaska'),
                 ('Pacific/Honolulu', 'Hawaii'),
             ],
             'help': 'Required, not optional: every "after hours" rule '
                     'below is calculated against this.'},
            {'key': 'business_city', 'label': 'City',
             'type': 'text', 'required': True, 'skip_allowed': False},
            {'key': 'business_state', 'label': 'State',
             'type': 'text', 'required': True, 'skip_allowed': False,
             'placeholder': 'TX'},
            {'key': 'support_email',
             'label': 'Where call summaries should go',
             'type': 'text', 'required': True, 'skip_allowed': False,
             'placeholder': 'office@yourcompany.com',
             'help': 'Every call gets summarised and sent here. Usage '
                     'notices go here too.'},
        ],
    },
    {
        'key': 'V2',
        'title': 'When it answers',
        'intro': 'You can have it cover everything, or only the hours you '
                 'are not there. Most shops start with after hours and '
                 'move to all day once they trust it.',
        'questions': [
            {'key': 'coverage_mode',
             'label': 'When should the receptionist be answering?',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'default': 'after_hours_only',
             'choices': [
                 ('all_hours', 'All day, every day (24/7)'),
                 ('after_hours_only',
                  'Only outside my business hours, including weekends'),
                 ('business_hours_only',
                  'Only during my business hours'),
             ],
             'help': 'After hours is where the money usually is: a '
                     'no-heat call at 9pm goes to whoever answers.'},
            {'key': 'hours_mon_fri',
             'label': 'Your weekday hours',
             'type': 'text', 'required': True, 'skip_allowed': False,
             'default': '8:00am - 5:00pm',
             'placeholder': '8:00am - 5:00pm',
             'help': 'We need these even if you chose "after hours only" '
                     '— that schedule is worked out from when you are '
                     'open. Put "closed" if you do not work weekdays.'},
            {'key': 'hours_saturday',
             'label': 'Saturday hours',
             'type': 'text', 'required': False, 'skip_allowed': True,
             'default': 'closed',
             'placeholder': 'closed, or 9:00am - 1:00pm'},
            {'key': 'hours_sunday',
             'label': 'Sunday hours',
             'type': 'text', 'required': False, 'skip_allowed': True,
             'default': 'closed',
             'placeholder': 'closed'},
            {'key': 'holiday_handling',
             'label': 'Public holidays',
             'type': 'select', 'required': False, 'skip_allowed': True,
             'default': 'treat_as_closed',
             'choices': [
                 ('treat_as_closed',
                  'Treat them as closed, so it answers all day'),
                 ('treat_as_normal', 'Treat them like a normal day'),
             ]},
        ],
    },
    {
        'key': 'V3',
        'title': 'Who picks up first',
        'intro': 'This one changes what the receptionist can do for you, '
                 'so it is worth thirty seconds of thought.',
        'questions': [
            {'key': 'answer_mode',
             'label': 'Should it answer first, or ring you first?',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'default': 'ai_first',
             'choices': [
                 ('ai_first',
                  'It answers first, and puts real customers through '
                  '(recommended)'),
                 ('ring_you_first',
                  'Ring my phone first, and it picks up if I miss it'),
             ],
             'help': 'Spam screening only works if it answers first — '
                     'that is the only way it can hear a robocall before '
                     'you do. If you choose "ring me first", every '
                     'robocall still rings your phone, and the '
                     'receptionist only sees the calls you already '
                     'missed. Screening is included either way, it just '
                     'cannot do anything in the second mode.'},
            {'key': 'ring_seconds',
             'label': 'How long should your phone ring before it picks up?',
             'type': 'select', 'required': False, 'skip_allowed': True,
             'default': '20',
             'choices': [
                 ('15', '15 seconds (about 3 rings)'),
                 ('20', '20 seconds (about 4 rings)'),
                 ('25', '25 seconds (about 5 rings)'),
                 ('30', '30 seconds (about 6 rings)'),
             ],
             'help': 'Only used if you picked "ring me first", and treat '
                     'it as a target rather than a dial. Your calls reach '
                     'the receptionist by forwarding, and the timer that '
                     'decides when a call counts as missed lives at your '
                     'phone company, not with us. Most carriers let us set '
                     'it, some have it fixed around 20 to 25 seconds, and '
                     'a few will not change it at all. We will tell you '
                     'which one yours is when we set up the forwarding. If '
                     'you move your number over to us instead, we control '
                     'it exactly.'},
            {'key': 'transfer_number',
             'label': 'Where should it put real customers through to?',
             'type': 'text', 'required': True, 'skip_allowed': False,
             'placeholder': '(210) 555-0142',
             'help': 'Usually your cell. This is the number it dials when '
                     'someone needs a human.'},
            {'key': 'transfer_conditions',
             'label': 'When should it put someone through instead of '
                      'taking a message?',
             'type': 'textarea', 'rows': 3,
             'required': False, 'skip_allowed': True,
             'default': 'Anyone with no heat or no cooling. Anyone who '
                        'asks to speak to a person. Anyone calling about '
                        'a job already booked.',
             'help': 'Plain English is fine — this is read as '
                     'instructions, not a form.'},
            {'key': 'after_transfer_fail',
             'label': 'If it tries you and you do not answer?',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'default': 'ai_takes_message',
             'choices': [
                 ('ai_takes_message',
                  'It goes back to the caller and takes the details'),
                 ('voicemail', 'Send them to voicemail'),
                 ('sms_followup', 'Text the caller to say you will ring '
                                  'back'),
             ]},
        ],
    },
    {
        'key': 'V4',
        'title': 'What it says and what it knows',
        'intro': 'The more it knows, the fewer calls it has to hand over. '
                 'You can change any of this later from your portal.',
        'questions': [
            {'key': 'greeting',
             'label': 'How should it answer the phone?',
             'type': 'textarea', 'rows': 2,
             'required': True, 'skip_allowed': False,
             'placeholder': 'Thanks for calling Johnson Heating and '
                            'Cooling, this is the after-hours line. How '
                            'can I help?',
             'help': 'If call recording is on, we add the "this call may '
                     'be recorded" line for you — you do not need to '
                     'write it.'},
            {'key': 'services_offered',
             'label': 'What do you do?',
             'type': 'textarea', 'rows': 3,
             'required': True, 'skip_allowed': False,
             'placeholder': 'AC repair and replacement, heating repair, '
                            'duct cleaning, maintenance plans. '
                            'Residential only.',
             'help': 'Also say what you do NOT do, so it stops wasting '
                     'your time on jobs you would turn down.'},
            {'key': 'service_area',
             'label': 'Where do you work?',
             'type': 'textarea', 'rows': 2,
             'required': True, 'skip_allowed': False,
             'placeholder': 'San Antonio and 30 miles out. Not Austin.'},
            {'key': 'emergency_handling',
             'label': 'What counts as an emergency, and what should it do?',
             'type': 'textarea', 'rows': 3,
             'required': True, 'skip_allowed': False,
             'default': 'No heat in winter or no cooling in summer is an '
                        'emergency. Put those through to me straight '
                        'away, whatever time it is.',
             'help': 'This is the single most valuable answer on the '
                     'form. Be specific about what wakes you up.'},
            {'key': 'faq_text',
             'label': 'Questions you get asked constantly',
             'type': 'textarea', 'rows': 5,
             'required': False, 'skip_allowed': True,
             'placeholder': 'Do you charge for a quote? No, estimates '
                            'are free.\nHow soon can you come out? '
                            'Usually same day.\nDo you take cards? Yes.',
             'help': 'One per line, question then answer. Every one you '
                     'add is a call it can finish without you.'},
            {'key': 'collect_fields',
             'label': 'What should it always get before hanging up?',
             'type': 'select', 'required': True, 'skip_allowed': False,
             'default': 'name_phone_address_issue',
             'choices': [
                 ('name_phone',
                  'Name and phone number'),
                 ('name_phone_issue',
                  'Name, phone and what is wrong'),
                 ('name_phone_address_issue',
                  'Name, phone, address and what is wrong '
                  '(recommended)'),
                 ('name_phone_address_issue_urgency',
                  'All of that plus how urgent it is'),
             ]},
            {'key': 'max_call_minutes',
             'label': 'Cut a call off after how long?',
             'type': 'select', 'required': False, 'skip_allowed': True,
             'default': '10',
             'choices': [
                 ('5', '5 minutes'),
                 ('10', '10 minutes (recommended)'),
                 ('15', '15 minutes'),
                 ('20', '20 minutes'),
             ],
             'help': 'A safety net against a caller who will not hang '
                     'up. Your 300 included minutes are shared across '
                     'all calls in a month.'},
        ],
    },
    {
        'key': 'V5',
        'title': 'Call recording',
        'intro': 'Recording is what gives you the summaries and the '
                 'transcripts in your portal. It also comes with one '
                 'obligation, which we handle for you but you need to '
                 'know about.',
        'questions': [
            {'key': 'recording_enabled',
             'label': 'Record and transcribe calls?',
             'type': 'bool', 'required': True, 'skip_allowed': False,
             'default': 'true',
             'help': 'If you turn this off you still get the receptionist '
                     'and the call summaries, but no transcripts and no '
                     'recordings to go back to.'},
            {'key': 'consent_recording_disclosure',
             'label': 'I understand that callers will be told the call may '
                      'be recorded, and that Aspired Websites adds that '
                      'line to the greeting automatically.',
             'type': 'bool', 'required': True, 'skip_allowed': False,
             'help': 'Texas and Georgia only need one party to consent, '
                     'but your callers may be in states that need both. '
                     'The disclosure in the greeting is what covers you '
                     'for all of them.'},
            {'key': 'consent_numbers_lawful',
             'label': 'I confirm the number being forwarded is my '
                      'business line and I am authorised to route it.',
             'type': 'bool', 'required': True, 'skip_allowed': False},
            {'key': 'attestation_typed_name',
             'label': 'Type your full name',
             'type': 'text', 'required': True, 'skip_allowed': False,
             'help': 'This is your signature on the two statements '
                     'above.'},
            {'key': 'attestation_title',
             'label': 'Your title at the business',
             'type': 'text', 'required': False, 'skip_allowed': True,
             'placeholder': 'Owner'},
        ],
    },
]


ONBOARDING_QUESTIONS = {
    'maintenance':    _MAINTENANCE,
    'social_media':   _social_sections(3),  # default 3 — overridden below
    'website_design': [],
    'review_system':  _REVIEW_SYSTEM,
    'voice_receptionist': _VOICE_RECEPTIONIST,
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
    if q.default_value:
        d['default'] = q.default_value
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
    # Every Review System section applies to every client — the A2P
    # registration needs all of R1, the link needs R2, the trigger needs
    # R3, and R4/R5 are the message and the legal record. Declared
    # explicitly rather than omitted so it is clear this is a decision
    # and not an oversight.
    'review_system': {},
    # Same reasoning for the receptionist: V1 is the number and the
    # timezone, V2/V3 are the whole behaviour, V4 is what it knows and
    # V5 is the recording record. Nothing is tier-dependent because
    # there is only one tier.
    #
    # Note what is deliberately NOT here: V2's hours are required even
    # when coverage_mode is 'all_hours'. They look redundant in that
    # case, but a client who later switches to after-hours would
    # otherwise have no hours on file and the schedule could not be
    # computed — and the switch is a portal toggle, not a new wizard.
    'voice_receptionist': {},
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
