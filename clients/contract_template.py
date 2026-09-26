"""
Contract text generator.

generate_contract_text() returns the full website-build agreement as an HTML
string. All pricing and scope numbers are pulled from the billing ServiceTier
row (looked up by slug) — nothing here is hardcoded.

The company location comes from `core.site_facts` for the same reason. Both
contract headers used to read "Aspired Websites LLC — San Antonio, TX &
Atlanta, GA", which is not where the business is based and, unlike the same
error on a marketing page, appeared on a document a client signs. Those are
service markets. The approved statement is the one in the fact matrix.
"""

from decimal import Decimal

from core.site_facts import LOCATION_STATEMENT


def _party_name(client):
    """The organisation name on the contract.

    Accepts an Account (``name``) or a legacy ClientProfile
    (``firm_name``). The party that signs is the business, which is
    account-level: a contract for "Vance Mediation Services" is still
    signed by Vance Family Law the firm.
    """
    return (getattr(client, 'name', '')
            or getattr(client, 'firm_name', '') or '')



def _money(amount):
    """Format a Decimal/number as $X,XXX (no cents when whole)."""
    amount = Decimal(amount)
    if amount == amount.to_integral_value():
        return f'${amount:,.0f}'
    return f'${amount:,.2f}'


def _interval_word(tier):
    """Human billing cadence for a recurring tier, e.g. 'month' -> 'per month'."""
    interval = (getattr(tier, 'billing_interval', '') or '').lower()
    if interval == 'month':
        return 'per month'
    if interval == 'year':
        return 'per year'
    return ''


_ESIGN_BODY = """
  <p>By typing your name and submitting the signature form on the prior page,
  you (the Client) acknowledge and agree that:</p>
  <ul>
    <li>The name you type is your <strong>legal signature</strong> on this
    Agreement, with the same legal force and effect as a handwritten signature.</li>
    <li>You <strong>intend to be bound</strong> by the terms of this Agreement
    when you submit the signature form.</li>
    <li>You consent to transact business <strong>electronically</strong> and to
    receive contracts, invoices, notices, and other records related to this
    Agreement in <strong>electronic form</strong>.</li>
    <li>You acknowledge that we will record and retain, alongside your typed
    name, the <strong>IP address, browser user-agent string, and timestamp</strong>
    at which you submit the signature form, plus a <strong>cryptographic hash</strong>
    of the Agreement text as displayed to you.</li>
    <li>You can request a paper copy at any time, free of charge, by emailing
    <strong>zacherylong@aspiredwebsites.com</strong>.</li>
  </ul>
  <p>This Agreement is intended to satisfy the federal <strong>ESIGN Act</strong>
  and the <strong>Uniform Electronic Transactions Act (UETA)</strong> as adopted
  in Texas and Georgia.</p>"""


def _infer_payment_option(build_svc):
    """The build's payment option: explicit on the service dict, else
    inferred from the tier slug. Never 'deposit' — there is no deposit
    option for a new agreement."""
    explicit = (build_svc or {}).get('payment_option')
    if explicit in ('pay_in_full', 'installment'):
        return explicit
    tier = (build_svc or {}).get('tier')
    from clients.contract_options import BUILD_INSTALLMENT_SLUG
    if getattr(tier, 'slug', '') == BUILD_INSTALLMENT_SLUG:
        return 'installment'
    return 'pay_in_full'


def _build_scope(build_svc, build, weeks, start_phrase):
    """Scope paragraph for the build line."""
    if build_svc.get('platform') == 'wordpress':
        # No page counts: a WordPress engagement is scoped by agreement,
        # and the site runs on the Client's own hosting, so the
        # hand-coded/Droplet language does not apply.
        return (
            '<p>A WordPress website, mobile-responsive and '
            'security-hardened, built on hosting the <strong>Client '
            'provides and controls</strong>, in an estimated '
            f'<strong>{weeks} weeks</strong> from {start_phrase} and all '
            'required Client assets. Hosting, domain, and platform fees '
            'remain the Client&rsquo;s responsibility.</p>')
    pages = getattr(build, 'pages_included', None) if build is not None else None
    practice = (getattr(build, 'practice_areas_included', None)
                if build is not None else None)
    if pages:
        extra = ''
        if practice:
            extra = (f' (including up to <strong>{practice} practice area '
                     'pages</strong>)')
        return (
            '<p>A hand-coded, mobile-responsive, security-hardened '
            f'website of up to <strong>{pages} pages</strong>{extra}, '
            f'built in an estimated <strong>{weeks} weeks</strong> from '
            f'{start_phrase} and all required Client assets.</p>')
    return (
        '<p>A hand-coded, mobile-responsive, security-hardened '
        'website, scoped as agreed in writing between the parties, '
        f'built in an estimated <strong>{weeks} weeks</strong> from '
        f'{start_phrase} and all required Client assets.</p>')


def generate_combined_contract_text(client, services, payment_option=None,
                                    legacy_deposit=False):
    """
    Build the services-agreement HTML for a website build and/or a
    recurring plan.

    Args:
        client:   Account (or legacy ClientProfile) the contract is for.
        services: list of dicts ``{'service_type': 'build'|'maintenance'|
                  'hosting'|'social', 'tier': <ServiceTier or None>, ...}``.
                  The build dict may carry overrides — ``price``, ``name``,
                  ``platform``, ``weeks`` — and ``payment_option``.
        payment_option: 'pay_in_full' or 'installment' for the build.
                  Inferred from the build dict / tier slug when omitted.
        legacy_deposit: ONLY for re-rendering a legacy contract that was
                  created under the old 50% deposit / 50% final terms
                  (Contract.payment_option blank). No new agreement is
                  ever generated with it.

    A single signature covers everything selected. The build is paid in
    full at signing or in 24 monthly installments (the first at signing);
    the Full Plan / Hosting + Security bill monthly from signing.
    """
    from billing.pricing_models import AddonPricing
    from clients.contract_options import (
        FULL_PLAN_SLUG, GUARANTEE_DAYS, INSTALLMENT_COUNT,
        PLAN_PAID_IN_FULL_SLUG, installment_total)

    hourly = AddonPricing.objects.filter(slug='addon-hourly').first()
    hourly_rate = f'${hourly.price_min:,.0f}' if hourly else '$85'

    firm = _party_name(client)
    client_name = getattr(client, 'contact_name', '') or firm

    svc_by_type = {s['service_type']: s for s in services}
    build_svc = svc_by_type.get('build')
    maint_svc = svc_by_type.get('maintenance')
    hosting_svc = svc_by_type.get('hosting')
    social_svc = svc_by_type.get('social')
    build = build_svc.get('tier') if build_svc else None
    maintenance = maint_svc.get('tier') if maint_svc else None
    hosting = hosting_svc.get('tier') if hosting_svc else None
    social = social_svc.get('tier') if social_svc else None

    if build_svc is not None and not legacy_deposit:
        option = payment_option or _infer_payment_option(build_svc)
    else:
        option = None
    installment = option == 'installment'
    # Installment build + Full Plan: the $105 installment is billed INSIDE
    # the $250 Full Plan payment — one charge, never two.
    combined_full_plan = (
        installment and maintenance is not None
        and getattr(maintenance, 'slug', '') == FULL_PLAN_SLUG)

    # Price of the plan the Full Plan steps down to after month 24.
    step_down_price = None
    if combined_full_plan:
        from billing.pricing_models import ServiceTier
        step = ServiceTier.objects.filter(slug=PLAN_PAID_IN_FULL_SLUG).first()
        step_down_price = step.price if step is not None else None

    # ── Section 2 — Services & Pricing ──
    service_blocks = []
    n = 0
    due_at_signing = []
    if build_svc is not None:
        n += 1
        override = build_svc.get('price')
        price = Decimal(override if override is not None
                        else getattr(build, 'price', 0) or 0)
        label = (build_svc.get('name')
                 or (build.name if build is not None else 'Website Build'))
        weeks = build_svc.get('weeks') or (
            getattr(build, 'timeline_weeks', 0) if build is not None
            else 0) or 4

        if legacy_deposit:
            deposit = (price / 2).quantize(Decimal('0.01'))
            final = price - deposit
            scope = _build_scope(build_svc, build, weeks,
                                 'receipt of the deposit')
            payment_line = (
                f'One-time price: <strong>{_money(price)}</strong>, payable '
                f'<strong>{_money(deposit)}</strong> (50%) before work begins '
                f'and <strong>{_money(final)}</strong> (50%) on delivery, '
                'before launch.')
        elif installment:
            total = installment_total(price)
            scope = _build_scope(build_svc, build, weeks,
                                 'the first installment')
            payment_line = (
                f'Price: <strong>{INSTALLMENT_COUNT} monthly payments of '
                f'{_money(price)} ({_money(total)} total)</strong>, the first '
                'charged at signing and each of the remaining '
                f'{INSTALLMENT_COUNT - 1} on the same day of each following '
                'month.')
            if combined_full_plan:
                payment_line += (
                    ' Each installment is billed as part of the Full Plan '
                    'payment described below &mdash; it is not charged '
                    'separately.')
            else:
                due_at_signing.append(
                    f'the first build installment of {_money(price)}')
        else:
            scope = _build_scope(build_svc, build, weeks,
                                 'payment at signing')
            payment_line = (
                f'Price: <strong>{_money(price)}, paid in full at '
                'signing</strong>.')
            due_at_signing.append(f'the build price of {_money(price)}')

        service_blocks.append(f"""
  <h3>2.{n} Website Development &mdash; {label}</h3>
  {scope}
  <p>{payment_line} Includes two (2) rounds of revisions and two (2) weeks
  of post-launch support.</p>""")

    if maintenance is not None:
        n += 1
        plan_price = Decimal(maint_svc.get('price') or maintenance.price)
        if combined_full_plan:
            build_part = Decimal(build_svc.get('price') or 0)
            plan_part = plan_price - build_part
            after_price = step_down_price or plan_part
            body = (
                'Hosting, maintenance, unlimited content updates, security '
                'patching, and the automated review system at '
                f'<strong>{_money(plan_price)} per month</strong> for the '
                f'first {INSTALLMENT_COUNT} months &mdash; '
                f'{_money(build_part)} of which is the monthly build '
                f'installment and {_money(plan_part)} the plan. After the '
                f'{INSTALLMENT_COUNT}th payment the build is paid in full '
                'and the rate drops automatically to '
                f'<strong>{_money(after_price)} per month</strong>, '
                'month-to-month. The first payment is charged at signing.')
            due_at_signing.append(
                f'the first Full Plan payment of {_money(plan_price)}')
        elif getattr(maintenance, 'slug', '') in (
                FULL_PLAN_SLUG, PLAN_PAID_IN_FULL_SLUG):
            body = (
                'Hosting, maintenance, unlimited content updates, security '
                'patching, and the automated review system at '
                f'<strong>{_money(plan_price)} per month</strong>, billed '
                'monthly from signing (the first payment is charged at '
                'signing), month-to-month.')
            due_at_signing.append(
                f'the first Full Plan payment of {_money(plan_price)}')
        else:
            # A legacy maintenance tier, re-rendered on an old contract.
            body = (
                'Ongoing maintenance, monitoring, and support under the '
                f'<strong>{maintenance.name}</strong> plan at '
                f'<strong>{_money(plan_price)} '
                f'{_interval_word(maintenance)}</strong>. Billed monthly '
                'via Stripe, month-to-month.')
        is_hvac_plan = getattr(maintenance, 'slug', '') in (
            FULL_PLAN_SLUG, PLAN_PAID_IN_FULL_SLUG)
        heading = (maintenance.name if is_hvac_plan
                   else 'Website Maintenance &mdash; ' + maintenance.name)
        service_blocks.append(f"""
  <h3>2.{n} {heading}</h3>
  <p>{body}</p>""")

    if hosting is not None:
        n += 1
        host_price = Decimal(hosting_svc.get('price') or hosting.price)
        service_blocks.append(f"""
  <h3>2.{n} {hosting.name}</h3>
  <p>Server and operating-system patching, SSL certificate renewal, and
  application security updates, with the site&rsquo;s forms kept live, at
  <strong>{_money(host_price)} per month</strong>, billed monthly from signing
  (the first payment is charged at signing), month-to-month. Content edits,
  code changes, and the review system are not included.</p>""")
        due_at_signing.append(
            f'the first Hosting + Security payment of {_money(host_price)}')

    if social is not None:
        # Legacy rows only — social media is no longer sold.
        n += 1
        service_blocks.append(f"""
  <h3>2.{n} Social Media Marketing &mdash; {social.name}</h3>
  <p>Social media management under the <strong>{social.name}</strong> plan at
  <strong>{_money(social.price)} {_interval_word(social)}</strong>. Billed
  monthly via Stripe, month-to-month.</p>""")

    services_section = '\n'.join(service_blocks)
    has_plan = maintenance is not None or hosting is not None
    has_recurring = has_plan or social is not None or installment

    # ── Payment ──
    if legacy_deposit:
        payment = """
  <p>All invoices are issued and paid through Stripe. One-time build work does
  not begin until the deposit has cleared. Recurring plans begin on activation
  and bill on a monthly cycle.</p>"""
    else:
        due = ''
        if due_at_signing:
            due = ('\n  <p>Charged at signing, to the card the Client '
                   f'provides: {"; ".join(due_at_signing)}. Work begins once '
                   'these payments clear.</p>')
        build_terms = ''
        if build_svc is not None:
            build_terms = (
                ' There is no deposit: the build is paid either in full at '
                f'signing or in {INSTALLMENT_COUNT} monthly installments, the '
                'first charged at signing.')
        payment = f"""
  <p>All payments are processed by Stripe.{build_terms}</p>{due}
  <p>Recurring payments are charged automatically to the card on file on the
  same day of each month as the first charge.</p>"""

    # ── Conditional clauses ──
    #
    # Section numbers are assigned at render time, not written into the
    # headings, so an agreement without a build or a plan still numbers
    # 1, 2, 3... Keyed on whether a BUILD WAS SOLD, not on whether a
    # ServiceTier object exists: a custom-priced build has no tier and
    # still needs the ownership clause, the revision limit and the
    # out-of-scope rate.
    ownership = ''
    revisions = ''
    responsiveness = ''
    if build_svc is not None:
        if legacy_deposit:
            ownership = """
  <p>All build work product, including the website and its source code, remains
  the property of Aspired Websites LLC until the final build payment has cleared
  in full, at which point ownership transfers to the Client. The Client owns
  their domain name at all times.</p>"""
        else:
            when_paid = (
                f'the {INSTALLMENT_COUNT}th and final installment has been '
                'paid' if installment
                else 'the build price has been paid in full')
            installment_terms = ''
            if installment:
                installment_terms = """
  <p>The website stays live and in the Client&rsquo;s use throughout the
  installment term. If an installment payment is missed, Aspired Websites will
  contact the Client first, and will give at least <strong>fourteen (14)
  days&rsquo; written notice</strong> before suspending the website for
  non-payment.</p>"""
            ownership = f"""
  <p>All build work product, including the website and its source code, remains
  the property of Aspired Websites LLC until the build has been paid in full
  &mdash; that is, until {when_paid}. Ownership of the completed website then
  transfers to the Client. The Client owns their domain name at all times.</p>
  <p><strong>Continuity.</strong> If Aspired Websites LLC ceases operating
  before the build has been paid in full, ownership of the website, its source
  code, and all related files transfers to the Client immediately and at no
  further cost, and any remaining build payments are cancelled.</p>{installment_terms}"""
        revisions = f"""
  <p>The build includes <strong>two (2) rounds of revisions</strong>. Additional
  revision rounds, post-launch changes, and any work outside the scope above
  are billed at <strong>{hourly_rate} per hour</strong>, quoted and invoiced
  before that work begins.</p>"""
        if not legacy_deposit:
            responsiveness = """
  <p>The Client agrees to provide content, assets, feedback, and approvals in a
  timely manner. If the Client does not respond to a request from Aspired
  Websites for <strong>thirty (30) consecutive days</strong>, the project is
  paused. Restarting a paused project carries a <strong>restart fee of 25% of
  the build price</strong>.</p>"""

    recurring_clause = ''
    if has_recurring and legacy_deposit:
        recurring_clause = """
  <p>Maintenance and social media plans are <strong>month-to-month</strong>,
  billed monthly through Stripe, and may be cancelled at any time with
  <strong>30 days&rsquo; written notice</strong>. There are no annual contracts
  and no long-term lock-in.</p>"""
    elif has_recurring:
        if has_plan:
            recurring_clause += """
  <p>The Full Plan and Hosting + Security plans are billed <strong>monthly from
  the date of signing</strong>, are <strong>month-to-month</strong>, and may be
  cancelled at any time with <strong>30 days&rsquo; written notice</strong>.
  There are no annual contracts.</p>"""
        if installment:
            recurring_clause += f"""
  <p>A {INSTALLMENT_COUNT}-month installment build is a fixed
  {INSTALLMENT_COUNT}-month payment term for the build, not a month-to-month
  plan: cancelling a plan does not cancel the remaining build installments
  (except under the {GUARANTEE_DAYS}-Day Guarantee below).</p>"""

    if legacy_deposit:
        guarantee_heading = '30-Day Money-Back Guarantee (Build)'
        guarantee = """
  <p>If the Client is not satisfied with a website build, they may request a
  full refund of the build fee within <strong>30 days</strong> of signing this
  Agreement.</p>""" if build_svc is not None else ''
    else:
        guarantee_heading = f'{GUARANTEE_DAYS}-Day Guarantee'
        guarantee = f"""
  <p>The Client may cancel this Agreement by written notice within
  <strong>{GUARANTEE_DAYS} days of signing</strong>. On a cancellation under
  this guarantee, Aspired Websites refunds <strong>75% of all amounts the
  Client has paid under this Agreement</strong> and retains the remaining
  <strong>25%</strong> for work performed and costs incurred, and
  <strong>all remaining payments &mdash; build installments and plan
  subscriptions &mdash; are cancelled immediately</strong>, with no further
  charges. After {GUARANTEE_DAYS} days, the cancellation terms above
  apply.</p>"""

    liability = '' if legacy_deposit else """
  <p>To the fullest extent permitted by law, Aspired Websites&rsquo; total
  liability arising out of or relating to this Agreement is limited to the
  amounts paid by the Client under this Agreement in the <strong>three (3)
  months</strong> preceding the event giving rise to the claim.</p>"""

    # (heading, body) in document order. A body of '' drops the section
    # entirely, and numbering closes up behind it.
    sections = [
        ('Parties', f"""
  <p>This Services Agreement (the &ldquo;Agreement&rdquo;) is entered into
  between <strong>Aspired Websites LLC</strong> (&ldquo;Aspired Websites,&rdquo;
  &ldquo;we,&rdquo; &ldquo;us&rdquo;) and <strong>{firm}</strong>
  (&ldquo;Client,&rdquo; &ldquo;you&rdquo;), represented by {client_name}.</p>"""),
        ('Services &amp; Pricing', f"""
  <p>Aspired Websites will provide the following service(s) to the Client:</p>
{services_section}"""),
        ('Payment', payment),
        ('Ownership', ownership),
        ('Revisions &amp; Out-of-Scope Work', revisions),
        ('Client Responsiveness', responsiveness),
        ('Recurring Services &amp; Cancellation', recurring_clause),
        (guarantee_heading, guarantee),
        ('Limitation of Liability', liability),
        ('Governing Law', """
  <p>This Agreement is governed by and construed in accordance with the laws of
  the <strong>State of Georgia</strong>.</p>"""),
        ('Electronic Signature Consent (ESIGN / UETA)', _ESIGN_BODY),
    ]

    rendered = []
    number = 0
    esign_number = 0
    for heading, body in sections:
        if not (body or '').strip():
            continue
        number += 1
        if heading.startswith('Electronic Signature Consent'):
            esign_number = number
        rendered.append(f'  <h2>{number}. {heading}</h2>{body}')

    # Signatures always closes the document, and its cross-reference has
    # to track whatever number the ESIGN section actually landed on.
    number += 1
    esign_ref = (f', including the Electronic Signature Consent in Section '
                 f'{esign_number}' if esign_number else '')
    rendered.append(f"""  <h2>{number}. Signatures</h2>
  <p>By signing below, the Client acknowledges they have read, understood, and
  agreed to all terms of this Agreement{esign_ref}.</p>
  <div class="contract-doc__sigblock">
    <p><strong>Aspired Websites LLC</strong><br>Zachery Long, Owner</p>
    <p><strong>Client:</strong> {firm}<br>Signed electronically &mdash; see signature record below.</p>
  </div>""")

    body_html = '\n\n'.join(rendered)
    return f"""
<div class="contract-doc">
  <h1>Services Agreement</h1>
  <p class="contract-doc__meta">Aspired Websites LLC &mdash; {LOCATION_STATEMENT}</p>

{body_html}
</div>
""".strip()


def generate_contract_text(client, package_slug, payment_option=None):
    """
    Build-only agreement for a client and a website-build ServiceTier.

    Kept for the Django-admin "Generate contract" action. It used to carry
    its own copy of the old 50% deposit / 50% final wording; it now renders
    the same agreement as generate_combined_contract_text — paid in full at
    signing, or in 24 installments when the tier is the installment build.

    Args:
        client:       Account / ClientProfile-like object (name, contact).
        package_slug: slug of the billing ServiceTier.
    """
    from billing.pricing_models import ServiceTier

    tier = ServiceTier.objects.get(slug=package_slug)
    return generate_combined_contract_text(
        client,
        [{'service_type': 'build', 'tier': tier,
          'price': Decimal(tier.price), 'name': tier.name,
          'weeks': tier.timeline_weeks or 4}],
        payment_option=payment_option)
