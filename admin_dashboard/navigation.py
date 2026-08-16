"""
Admin sidebar navigation, defined as data.

The sidebar was ~200 lines of hand-written markup: 41 links, each repeating
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
active-state rule is written once instead of 41 times.

Grouping follows the owner's actual workflows rather than the apps the
code happens to live in — the operator is one person moving through *find
work -> deliver it -> get paid -> grow the account*, and the sidebar should
read in that order.

Each item is (label, url_name, match_prefix, badge_context_key).
`match_prefix` decides the active state; when None the item is active only
on an exact path match, which is what "Dashboard" needs so it does not
light up on every child page.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class NavItem:
    label: str
    url_name: str
    match: str | None = None
    badge: str | None = None
    exact: bool = False


@dataclass(frozen=True)
class NavGroup:
    label: str | None
    items: tuple


# The order here is the order on screen.
NAVIGATION = (
    # Unlabelled first block: what the operator opens the admin to check.
    NavGroup(None, (
        NavItem('Dashboard', 'admin_dashboard:home',
                '/admin-dashboard/', exact=True),
        NavItem('Needs You', 'admin_dashboard:needs_you',
                '/admin-dashboard/needs-you/', badge='needs_you_count'),
        NavItem('Approvals', 'admin_dashboard:outreach_approvals',
                '/admin-dashboard/outreach/approvals/',
                badge='approvals_count'),
        NavItem('Calls', 'admin_dashboard:schedule_calls',
                '/admin-dashboard/schedule/calls/'),
    )),

    # Finding and winning work. Everything here happens before someone is
    # a client, which is why leads and scraping no longer sit under
    # "Clients".
    NavGroup('Pipeline', (
        NavItem('Leads', 'admin_dashboard:leads_table',
                '/admin-dashboard/leads/table/'),
        NavItem('Lead Board', 'admin_dashboard:leads_kanban',
                '/admin-dashboard/leads/kanban/'),
        NavItem('Find Leads', 'admin_dashboard:scrape',
                '/admin-dashboard/scrape/'),
        NavItem('Scrape Jobs', 'admin_dashboard:scrape_jobs',
                '/admin-dashboard/scrape-jobs/'),
        NavItem('Enrichment', 'admin_dashboard:enrichment_status',
                '/admin-dashboard/enrichment/'),
        NavItem('Outreach Sent', 'admin_dashboard:outreach_sent',
                '/admin-dashboard/outreach/sent/'),
        NavItem('Proposals', 'admin_dashboard:proposals_list',
                '/admin-dashboard/proposals/'),
        NavItem('Referrals', 'admin_dashboard:referrals_list',
                '/admin-dashboard/referrals/'),
    )),

    # Delivering the work, in roughly the order a build moves.
    NavGroup('Delivery', (
        NavItem('Accounts', 'admin_dashboard:accounts_list',
                '/admin-dashboard/accounts/'),
        NavItem('Websites', 'admin_dashboard:websites_list',
                '/admin-dashboard/websites/'),
        NavItem('Send Onboarding', 'admin_dashboard:send_onboarding',
                '/admin-dashboard/billing/send-onboarding/'),
        NavItem('Intake Questions', 'admin_dashboard:onboarding_questions',
                '/admin-dashboard/onboarding-questions/'),
        NavItem('Domains', 'admin_dashboard:admin_domain_list',
                '/admin-dashboard/domains/'),
        NavItem('Deploy', 'admin_dashboard:deploy_home',
                '/admin-dashboard/deploy/'),
        NavItem('Droplets', 'admin_dashboard:droplet_list',
                '/admin-dashboard/droplets/'),
    )),

    NavGroup('Money', (
        NavItem('Billing', 'admin_dashboard:billing_list',
                '/admin-dashboard/billing/'),
        NavItem('New Invoice', 'admin_dashboard:new_invoice',
                '/admin-dashboard/billing/new-invoice/'),
        NavItem('Pricing', 'admin_dashboard:pricing_list',
                '/admin-dashboard/pricing/'),
    )),

    # Work that grows an existing account.
    NavGroup('Growth', (
        NavItem('Intelligence', 'admin_dashboard:intelligence_dashboard',
                '/admin-dashboard/intelligence/'),
        NavItem('Competitor Gaps', 'admin_dashboard:competitor_gaps_list',
                '/admin-dashboard/competitor-gaps/'),
        NavItem('Blog', 'admin_dashboard:blog_list',
                '/admin-dashboard/blog/'),
        NavItem('Social', 'social:channels_list', '/social/'),
        NavItem('Google Business', 'gbp:dashboard', '/gbp/'),
        NavItem('Monthly Reports', 'admin_dashboard:reports_list',
                '/admin-dashboard/reports/'),
        NavItem('Annual Reports', 'admin_dashboard:annual_reports_list',
                '/admin-dashboard/annual-reports/'),
        NavItem('NPS', 'admin_dashboard:nps_list',
                '/admin-dashboard/nps/'),
        NavItem('Case Studies', 'admin_dashboard:case_studies_list',
                '/admin-dashboard/case-studies/'),
        NavItem('Changelog', 'admin_dashboard:changelog_list',
                '/admin-dashboard/changelog/'),
    )),

    # Running the business itself.
    NavGroup('System', (
        NavItem('Alerts', 'admin_dashboard:system_alerts',
                '/admin-dashboard/system-alerts/'),
        NavItem('Security Scans', 'admin_dashboard:scans_list',
                '/admin-dashboard/scans/'),
        NavItem('Vault', 'vault:home', '/admin-dashboard/vault/'),
        NavItem('Ops Sessions', 'vault:ops_sessions_list',
                '/admin-dashboard/vault/ops-sessions/'),
        NavItem('Redis', 'admin_dashboard:redis_monitor',
                '/admin-dashboard/redis/'),
        NavItem('DMARC', 'admin_dashboard:dmarc_dashboard',
                '/admin-dashboard/dmarc/'),
        NavItem('Availability', 'admin_dashboard:schedule_availability',
                '/admin-dashboard/schedule/availability/'),
        NavItem('Calendar Connect', 'admin_dashboard:schedule_connect',
                '/admin-dashboard/schedule/connect/'),
        NavItem('Briefs', 'admin_dashboard:briefs_home',
                '/admin-dashboard/briefs/'),
        NavItem('AI Assistant', 'admin_dashboard:ai_assistant',
                '/admin-dashboard/ai-assistant/'),
        NavItem('Settings', 'admin_dashboard:settings',
                '/admin-dashboard/settings/'),
    )),
)


def is_active(item, path):
    """Whether `item` should render as the current page."""
    if not item.match:
        return False
    if item.exact:
        return path == item.match
    return item.match in path


def all_items():
    for group in NAVIGATION:
        yield from group.items


def navigation(request):
    """Context processor: the sidebar, resolved for this request."""
    path = getattr(request, 'path', '') or ''
    groups = []
    for group in NAVIGATION:
        groups.append({
            'label': group.label,
            'items': [
                {
                    'label': item.label,
                    'url_name': item.url_name,
                    'badge': item.badge,
                    'active': is_active(item, path),
                }
                for item in group.items
            ],
        })
    return {'admin_nav': groups}
