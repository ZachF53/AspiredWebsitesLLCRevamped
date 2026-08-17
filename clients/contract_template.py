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


def generate_combined_contract_text(client, services):
    """
    Build a combined services-agreement HTML for any mix of website
    development, maintenance, and social media.

    Args:
        client:   ClientProfile the contract is for.
        services: list of dicts ``{'service_type': 'build'|'maintenance'|'social',
                  'tier': <ServiceTier>}`` — one per selected service.

    A single signature covers everything selected. The build line is billed
    one-time (50% deposit / 50% on delivery); maintenance and social are
    recurring and month-to-month.
    """
    from billing.pricing_models import AddonPricing

    hourly = AddonPricing.objects.filter(slug='addon-hourly').first()
    hourly_rate = f'${hourly.price_min:,.0f}' if hourly else '$85'

    client_name = client.contact_name or client.firm_name
    firm = client.firm_name

    by_type = {s['service_type']: s['tier'] for s in services}
    build = by_type.get('build')
    maintenance = by_type.get('maintenance')
    social = by_type.get('social')

    # ── Section 2 — Services & Pricing (one block per selected service) ──
    service_blocks = []
    n = 0
    if build is not None:
        n += 1
        price = Decimal(build.price)
        deposit = (price / 2).quantize(Decimal('0.01'))
        final = price - deposit
        pages = build.pages_included or 0
        practice = build.practice_areas_included or 0
        weeks = build.timeline_weeks or 0
        service_blocks.append(f"""
  <h3>2.{n} Website Development &mdash; {build.name}</h3>
  <p>A hand-coded, mobile-responsive, security-hardened website of up to
  <strong>{pages} pages</strong> (including up to <strong>{practice} practice
  area pages</strong>), built in an estimated <strong>{weeks} weeks</strong>
  from receipt of the deposit and all required Client assets.</p>
  <p>One-time price: <strong>{_money(price)}</strong>, payable
  <strong>{_money(deposit)}</strong> (50%) before work begins and
  <strong>{_money(final)}</strong> (50%) on delivery, before launch. Includes
  two (2) major revisions and two (2) weeks of post-launch support.</p>""")
    if maintenance is not None:
        n += 1
        service_blocks.append(f"""
  <h3>2.{n} Website Maintenance &mdash; {maintenance.name}</h3>
  <p>Ongoing maintenance, monitoring, and support under the
  <strong>{maintenance.name}</strong> plan at
  <strong>{_money(maintenance.price)} {_interval_word(maintenance)}</strong>.
  Billed monthly via Stripe, month-to-month, cancellable any time with 30
  days&rsquo; written notice.</p>""")
    if social is not None:
        n += 1
        service_blocks.append(f"""
  <h3>2.{n} Social Media Marketing &mdash; {social.name}</h3>
  <p>Social media management under the <strong>{social.name}</strong> plan at
  <strong>{_money(social.price)} {_interval_word(social)}</strong>. Billed
  monthly via Stripe, month-to-month, cancellable any time with 30 days&rsquo;
  written notice.</p>""")

    services_section = '\n'.join(service_blocks)
    has_recurring = maintenance is not None or social is not None

    # ── Conditional clauses ──
    ownership = ''
    revisions = ''
    if build is not None:
        ownership = """
  <h2>4. Ownership</h2>
  <p>All build work product, including the website and its source code, remains
  the property of Aspired Websites LLC until the final build payment has cleared
  in full, at which point ownership transfers to the Client. The Client owns
  their domain name at all times.</p>"""
        revisions = f"""
  <h2>5. Revisions &amp; Out-of-Scope Work</h2>
  <p>The build includes <strong>two (2) major revisions</strong>. Additional
  major revisions, post-launch changes, and any work outside the scope above
  are billed at <strong>{hourly_rate} per hour</strong>, quoted and invoiced
  before that work begins.</p>"""

    recurring_clause = ''
    if has_recurring:
        recurring_clause = """
  <h2>6. Recurring Services</h2>
  <p>Maintenance and social media plans are <strong>month-to-month</strong>,
  billed monthly through Stripe, and may be cancelled at any time with
  <strong>30 days&rsquo; written notice</strong>. There are no annual contracts
  and no long-term lock-in.</p>"""

    return f"""
<div class="contract-doc">
  <h1>Services Agreement</h1>
  <p class="contract-doc__meta">Aspired Websites LLC &mdash; {LOCATION_STATEMENT}</p>

  <h2>1. Parties</h2>
  <p>This Services Agreement (the &ldquo;Agreement&rdquo;) is entered into
  between <strong>Aspired Websites LLC</strong> (&ldquo;Aspired Websites,&rdquo;
  &ldquo;we,&rdquo; &ldquo;us&rdquo;) and <strong>{firm}</strong>
  (&ldquo;Client,&rdquo; &ldquo;you&rdquo;), represented by {client_name}.</p>

  <h2>2. Services &amp; Pricing</h2>
  <p>Aspired Websites will provide the following service(s) to the Client:</p>
{services_section}

  <h2>3. Payment</h2>
  <p>All invoices are issued and paid through Stripe. One-time build work does
  not begin until the deposit has cleared. Recurring plans begin on activation
  and bill on a monthly cycle.</p>
{ownership}
{revisions}
{recurring_clause}

  <h2>7. 30-Day Money-Back Guarantee (Build)</h2>
  <p>If the Client is not satisfied with a website build, they may request a
  full refund of the build fee within <strong>30 days</strong> of signing this
  Agreement.</p>

  <h2>8. Governing Law</h2>
  <p>This Agreement is governed by and construed in accordance with the laws of
  the <strong>State of Georgia</strong>.</p>

  <h2>9. Electronic Signature Consent (ESIGN / UETA)</h2>
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
  in Texas and Georgia.</p>

  <h2>10. Signatures</h2>
  <p>By signing below, the Client acknowledges they have read, understood, and
  agreed to all terms of this Agreement, including the Electronic Signature
  Consent in Section 9.</p>
  <div class="contract-doc__sigblock">
    <p><strong>Aspired Websites LLC</strong><br>Zachery Long, Owner</p>
    <p><strong>Client:</strong> {firm}<br>Signed electronically &mdash; see signature record below.</p>
  </div>
</div>
""".strip()


def generate_contract_text(client, package_slug):
    """
    Build the full contract HTML for a client and a website-build tier.

    Args:
        client:       ClientProfile the contract is for.
        package_slug: slug of the billing ServiceTier (e.g. 'website-essential').
    """
    from billing.pricing_models import AddonPricing, ServiceTier

    tier = ServiceTier.objects.get(slug=package_slug)
    price = Decimal(tier.price)
    deposit = (price / 2).quantize(Decimal('0.01'))
    final = price - deposit
    pages = tier.pages_included or 0
    practice_pages = tier.practice_areas_included or 0
    timeline = tier.timeline_weeks or 0

    hourly = AddonPricing.objects.filter(slug='addon-hourly').first()
    hourly_rate = f'${hourly.price_min:,.0f}' if hourly else '$85'

    client_name = client.contact_name or client.firm_name
    firm = client.firm_name

    return f"""
<div class="contract-doc">
  <h1>Website Design &amp; Development Agreement</h1>
  <p class="contract-doc__meta">Aspired Websites LLC &mdash; {LOCATION_STATEMENT}</p>

  <h2>1. Parties</h2>
  <p>This Website Design &amp; Development Agreement (the &ldquo;Agreement&rdquo;) is entered
  into between <strong>Aspired Websites LLC</strong> (&ldquo;Aspired Websites,&rdquo; &ldquo;we,&rdquo;
  &ldquo;us&rdquo;) and <strong>{firm}</strong> (&ldquo;Client,&rdquo; &ldquo;you&rdquo;), represented by
  {client_name}.</p>

  <h2>2. Scope of Work</h2>
  <p>Aspired Websites will design and develop a <strong>{tier.name}</strong> for the
  Client, consisting of up to <strong>{pages} pages</strong>, including up to
  <strong>{practice_pages} practice area pages</strong>. The website will be
  hand-coded, mobile-responsive, and security-hardened. Any work beyond this scope is
  governed by Section 8.</p>

  <h2>3. Timeline</h2>
  <p>The estimated build timeline is <strong>{timeline} weeks</strong> from the date the
  deposit payment is received and all required Client assets have been delivered. The
  timeline is an estimate made in good faith; see Section 7 regarding asset delays.</p>

  <h2>4. Payment</h2>
  <p>The total price for the build is <strong>{_money(price)}</strong>, payable as follows:</p>
  <ul>
    <li><strong>{_money(deposit)}</strong> (50%) due upfront, before work begins.</li>
    <li><strong>{_money(final)}</strong> (50%) due on delivery, before the site is launched.</li>
  </ul>
  <p>Invoices are issued and paid through Stripe. Work does not begin until the deposit
  has cleared.</p>

  <h2>5. Ownership</h2>
  <p>All work product, including the website and its source code, remains the property of
  Aspired Websites LLC until the final payment has cleared in full. Upon receipt of final
  payment, ownership of the completed website transfers to the Client. The Client owns
  their domain name at all times.</p>

  <h2>6. Revisions</h2>
  <p>This Agreement includes <strong>two (2) major revisions</strong>. A major revision is a
  substantive change to layout, structure, or design direction. Additional major revisions,
  and any minor changes requested after launch, are billed at
  <strong>{hourly_rate} per hour</strong>.</p>

  <h2>7. Client Assets</h2>
  <p>The Client agrees to provide all required content and assets (text, images, logos,
  brand materials) in a timely manner. If the Client delays asset delivery, the project
  clock pauses and placeholder content may be used in the interim. The build timeline in
  Section 3 is extended by the length of any such delay.</p>

  <h2>8. Scope Creep / Out-of-Scope Work</h2>
  <p>Any work requested outside the scope defined in Section 2 is billed at
  <strong>{hourly_rate} per hour</strong>. Out-of-scope work will be quoted and invoiced
  before that work begins, and is not started until the corresponding invoice is paid.</p>

  <h2>9. Post-Launch Support</h2>
  <p>The build includes <strong>two (2) weeks of free support</strong> beginning on the
  launch date. After that window, continued maintenance, updates, and support require an
  active monthly maintenance plan.</p>

  <h2>10. 30-Day Money-Back Guarantee</h2>
  <p>If the Client is not satisfied, they may request a full refund of the build fee within
  <strong>30 days</strong> of signing this Agreement.</p>

  <h2>11. Cancellation</h2>
  <p>Monthly maintenance plans are month-to-month and may be cancelled at any time with
  <strong>30 days&rsquo; written notice</strong>. There are no annual contracts and no
  long-term lock-in.</p>

  <h2>12. Governing Law</h2>
  <p>This Agreement is governed by and construed in accordance with the laws of the
  <strong>State of Georgia</strong>.</p>

  <h2>13. Electronic Signature Consent (ESIGN / UETA)</h2>
  <p>By typing your name and submitting the signature form on the prior page, you (the
  Client) acknowledge and agree that:</p>
  <ul>
    <li>The name you type is your <strong>legal signature</strong> on this Agreement,
    with the same legal force and effect as a handwritten signature.</li>
    <li>You <strong>intend to be bound</strong> by the terms of this Agreement when you
    submit the signature form.</li>
    <li>You consent to transact business <strong>electronically</strong> and to receive
    contracts, invoices, notices, and other records related to this Agreement in
    <strong>electronic form</strong> (typically by email and through the client portal).</li>
    <li>You acknowledge that we will record and retain, alongside your typed name, the
    <strong>IP address, browser user-agent string, and timestamp</strong> at which you
    submit the signature form, plus a <strong>cryptographic hash</strong> of the
    Agreement text as displayed to you, to serve as evidence of what was agreed to and
    when.</li>
    <li>You can request a paper copy of this Agreement at any time by emailing
    <strong>zacherylong@aspiredwebsites.com</strong>. There is no fee for paper copies.</li>
    <li>You can withdraw your consent to transact electronically by emailing the address
    above; doing so does not invalidate this Agreement once it has been signed, but
    governs how we communicate with you going forward.</li>
  </ul>
  <p>This Agreement is intended to satisfy the requirements of the federal
  <strong>Electronic Signatures in Global and National Commerce Act (ESIGN)</strong> and
  the <strong>Uniform Electronic Transactions Act (UETA)</strong> as adopted in
  Texas and Georgia.</p>

  <h2>14. Signatures</h2>
  <p>By signing below, the Client acknowledges they have read, understood, and agreed to
  all terms of this Agreement, including the Electronic Signature Consent in Section 13.</p>
  <div class="contract-doc__sigblock">
    <p><strong>Aspired Websites LLC</strong><br>Zachery Long, Owner</p>
    <p><strong>Client:</strong> {firm}<br>Signed electronically &mdash; see signature record below.</p>
  </div>
</div>
""".strip()
