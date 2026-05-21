"""
Contract text generator.

generate_contract_text() returns the full website-build agreement as an HTML
string. The same HTML is shown on the signing page and rendered to the signed
PDF, so it must be self-contained (no external CSS dependencies).
"""

from decimal import Decimal


# Per-package scope. Page counts are the build scope written into the contract.
PACKAGE_SCOPE = {
    'essential_build': {
        'name': 'Essential Website Build',
        'pages': 5,
        'practice_area_pages': 2,
    },
    'premium_build': {
        'name': 'Premium Website Build',
        'pages': 8,
        'practice_area_pages': 4,
    },
}


def _money(amount):
    """Format a Decimal/number as $X,XXX (no cents when whole)."""
    amount = Decimal(amount)
    if amount == amount.to_integral_value():
        return f'${amount:,.0f}'
    return f'${amount:,.2f}'


def generate_contract_text(client, package, price, timeline):
    """
    Build the full contract HTML.

    Args:
        client:   ClientProfile the contract is for.
        package:  'essential_build' or 'premium_build'.
        price:    total build price (Decimal/number).
        timeline: build timeline in weeks (int).
    """
    scope = PACKAGE_SCOPE.get(package, PACKAGE_SCOPE['essential_build'])
    price = Decimal(price)
    deposit = (price / 2).quantize(Decimal('0.01'))
    final = price - deposit

    client_name = client.contact_name or client.firm_name
    firm = client.firm_name

    return f"""
<div class="contract-doc">
  <h1>Website Design &amp; Development Agreement</h1>
  <p class="contract-doc__meta">Aspired Websites LLC &mdash; San Antonio, TX &amp; Atlanta, GA</p>

  <h2>1. Parties</h2>
  <p>This Website Design &amp; Development Agreement (the &ldquo;Agreement&rdquo;) is entered
  into between <strong>Aspired Websites LLC</strong> (&ldquo;Aspired Websites,&rdquo; &ldquo;we,&rdquo;
  &ldquo;us&rdquo;) and <strong>{firm}</strong> (&ldquo;Client,&rdquo; &ldquo;you&rdquo;), represented by
  {client_name}.</p>

  <h2>2. Scope of Work</h2>
  <p>Aspired Websites will design and develop a <strong>{scope['name']}</strong> for the
  Client, consisting of up to <strong>{scope['pages']} pages</strong>, including up to
  <strong>{scope['practice_area_pages']} practice area pages</strong>. The website will be
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
  and any minor changes requested after launch, are billed at <strong>$85 per hour</strong>.</p>

  <h2>7. Client Assets</h2>
  <p>The Client agrees to provide all required content and assets (text, images, logos,
  brand materials) in a timely manner. If the Client delays asset delivery, the project
  clock pauses and placeholder content may be used in the interim. The build timeline in
  Section 3 is extended by the length of any such delay.</p>

  <h2>8. Scope Creep / Out-of-Scope Work</h2>
  <p>Any work requested outside the scope defined in Section 2 is billed at
  <strong>$85 per hour</strong>. Out-of-scope work will be quoted and invoiced before that
  work begins, and is not started until the corresponding invoice is paid.</p>

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

  <h2>13. Signatures</h2>
  <p>By signing below, the Client acknowledges they have read, understood, and agreed to
  all terms of this Agreement.</p>
  <div class="contract-doc__sigblock">
    <p><strong>Aspired Websites LLC</strong><br>Zachery Long, Owner</p>
    <p><strong>Client:</strong> {firm}<br>Signed electronically &mdash; see signature record below.</p>
  </div>
</div>
""".strip()
