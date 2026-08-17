"""
Admin sidebar navigation, defined as data.

The sidebar was ~200 lines of hand-written markup: 43 links, each repeating
the same anchor structure and each carrying its own bespoke
`{% if '...' in request.path %}active{% endif %}` test. Two consequences
followed from that, both visible in the markup this replaces:

- `schedule_connect` was linked twice under Operations, because nothing
  could notice a duplicate in 200 lines of near-identical HTML.
- The groups had stopped describing the work. Lead scraping and enrichment
  sat under "Clients" though they run before anyone is a client; "Content
  & Settings" had become a ten-item drawer holding pricing, domains, the AI
  assistant and Google Business Profile.

Defining it here makes the nav countable, testable and regroupable, and the
active-state rule is written once instead of 43 times.

Grouping follows the owner's actual workflows rather than the apps the
code happens to live in — the operator is one person moving through *find
work -> deliver it -> get paid -> grow the account*, and the sidebar should
read in that order.

An item is just a label and a url name. The path it highlights on is
resolved from the URL conf, not written down beside it — hand-written
prefixes drifted from the routes immediately: six of the first set were
already wrong (`system_alerts` is `/alerts/`, not `/system-alerts/`; the
scraping tools live under `/leads/`). A prefix that has to be kept in sync
by hand is the same defect as the markup this module replaces.

The current item is the one whose URL is the *longest* prefix of the
request path. That handles nesting without any per-item exceptions:
`/admin-dashboard/billing/new-invoice/` lights New Invoice rather than
Billing, `/admin-dashboard/vault/ops/sessions/` lights Ops Sessions rather
than Vault, and Dashboard (`/admin-dashboard/`, the shortest prefix of
all) only wins on its own page.
"""

from dataclasses import dataclass

from django.urls import NoReverseMatch, reverse


@dataclass(frozen=True)
class NavItem:
    label: str
    url_name: str
    badge: str | None = None

    @property
    def path(self):
        """The URL this item points at, or '' if it cannot be resolved."""
        try:
            return reverse(self.url_name)
        except NoReverseMatch:
            return ''


@dataclass(frozen=True)
class NavGroup:
    label: str | None
    items: tuple


# The order here is the order on screen.
NAVIGATION = (
    # Unlabelled first block: what the operator opens the admin to check.
    NavGroup(None, (
        NavItem('Dashboard', 'admin_dashboard:home'),
        NavItem('Needs You', 'admin_dashboard:needs_you', badge='needs_you_count'),
        NavItem('Approvals', 'admin_dashboard:outreach_approvals',
                badge='approvals_count'),
        NavItem('Calls', 'admin_dashboard:schedule_calls'),
    )),

    # Finding and winning work. Everything here happens before someone is
    # a client, which is why leads and scraping no longer sit under
    # "Clients".
    NavGroup('Pipeline', (
        NavItem('Leads', 'admin_dashboard:leads_table'),
        NavItem('Lead Board', 'admin_dashboard:leads_kanban'),
        NavItem('Find Leads', 'admin_dashboard:scrape'),
        NavItem('Scrape Jobs', 'admin_dashboard:scrape_jobs'),
        NavItem('Enrichment', 'admin_dashboard:enrichment_status'),
        NavItem('Outreach Sent', 'admin_dashboard:outreach_sent'),
        NavItem('Proposals', 'admin_dashboard:proposals_list'),
        NavItem('Referrals', 'admin_dashboard:referrals_list'),
    )),

    # Delivering the work, in roughly the order a build moves.
    NavGroup('Delivery', (
        NavItem('Accounts', 'admin_dashboard:accounts_list'),
        NavItem('Websites', 'admin_dashboard:websites_list'),
        NavItem('Send Onboarding', 'admin_dashboard:send_onboarding'),
        NavItem('Intake Questions', 'admin_dashboard:onboarding_questions'),
        NavItem('Domains', 'admin_dashboard:admin_domain_list'),
        NavItem('Deploy', 'admin_dashboard:deploy_home'),
        NavItem('Droplets', 'admin_dashboard:droplet_list'),
    )),

    NavGroup('Money', (
        NavItem('Billing', 'admin_dashboard:billing_list'),
        NavItem('New Invoice', 'admin_dashboard:new_invoice'),
        NavItem('Pricing', 'admin_dashboard:pricing_list'),
    )),

    # Work that grows an existing account.
    NavGroup('Growth', (
        NavItem('Intelligence', 'admin_dashboard:intelligence_dashboard'),
        NavItem('Competitor Gaps', 'admin_dashboard:competitor_gaps_list'),
        NavItem('Blog', 'admin_dashboard:blog_list'),
        NavItem('Social', 'social:channels_list'),
        NavItem('Google Business', 'gbp:dashboard'),
        NavItem('Monthly Reports', 'admin_dashboard:reports_list'),
        NavItem('Annual Reports', 'admin_dashboard:annual_reports_list'),
        NavItem('NPS', 'admin_dashboard:nps_list'),
        NavItem('Case Studies', 'admin_dashboard:case_studies_list'),
        NavItem('Changelog', 'admin_dashboard:changelog_list'),
    )),

    # Running the business itself.
    NavGroup('System', (
        NavItem('Data Health', 'admin_dashboard:data_health'),
        NavItem('Alerts', 'admin_dashboard:system_alerts'),
        NavItem('Security Scans', 'admin_dashboard:scans_list'),
        NavItem('Vault', 'vault:home'),
        NavItem('Ops Sessions', 'vault:ops_sessions_list'),
        NavItem('Redis', 'admin_dashboard:redis_monitor'),
        NavItem('DMARC', 'admin_dashboard:dmarc_dashboard'),
        NavItem('Availability', 'admin_dashboard:schedule_availability'),
        NavItem('Calendar Connect', 'admin_dashboard:schedule_connect'),
        NavItem('Briefs', 'admin_dashboard:briefs_home'),
        NavItem('AI Assistant', 'admin_dashboard:ai_assistant'),
        NavItem('Settings', 'admin_dashboard:settings'),
    )),
)


def all_items():
    for group in NAVIGATION:
        yield from group.items


def active_item(path):
    """The item whose URL is the longest prefix of `path`, or None.

    Longest-prefix rather than "first match" is what makes nesting work
    without per-item exceptions — see the module docstring.
    """
    best = None
    best_length = 0
    for item in all_items():
        item_path = item.path
        if not item_path or not path.startswith(item_path):
            continue
        if len(item_path) > best_length:
            best, best_length = item, len(item_path)
    return best


def is_active(item, path):
    """Whether `item` should render as the current page."""
    return active_item(path) is item


def navigation(request):
    """Context processor: the sidebar, resolved for this request."""
    path = getattr(request, 'path', '') or ''
    current = active_item(path)
    groups = []
    for group in NAVIGATION:
        groups.append({
            'label': group.label,
            'items': [
                {
                    'label': item.label,
                    'url_name': item.url_name,
                    'badge': item.badge,
                    'active': item is current,
                }
                for item in group.items
            ],
        })
    return {'admin_nav': groups}
