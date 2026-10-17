"""
FAQ lists for the service pages. Each list is rendered as visible cards
AND as the page's FAQPage JSON-LD (via pricing_content.faq_schema), so
the structured data can never describe answers that aren't on the page.

Questions are phrased the way contractors actually search ("how much
does an HVAC website cost"), and every price is read from the live
ServiceTier / AddonPricing rows — never hardcoded (CLAUDE.md).
"""
from core.site_facts import BUILD_TIMELINE, GUARANTEE_DAYS, GUARANTEE_REFUND_PERCENT
from public.pricing_content import money


def web_design_faqs(build_full=None, build_installment=None):
    full = money(build_full.price) if build_full else ''
    inst = money(build_installment.price) if build_installment else ''
    if full and inst:
        cost = (f'{full} paid in full, or {inst} a month for 24 months. That '
                'is the published price for a custom-coded build: four main '
                'pages plus up to six service pages, with the copy written '
                'for you. No quote-on-request, no setup fees.')
    else:
        cost = ('One flat, published price for a custom-coded build of four '
                'main pages plus up to six service pages, with the copy '
                'written for you. See the pricing page for the current number.')
    return [
        ('How much does a website for an HVAC company cost?', cost),
        ('How long does it take to build an HVAC website?',
         f'{BUILD_TIMELINE[0].upper()}{BUILD_TIMELINE[1:]} from kickoff to launch, '
         'depending mostly on how quickly photos and feedback come back. '
         'You see a working staging site before anything goes live.'),
        ('What should an HVAC contractor website have?',
         'A phone number you can tap from the top of every page, a page for '
         'each service you want calls for, the areas you serve, recent '
         'reviews, and fast load times on a phone. Emergency customers decide '
         'in seconds, so everything that slows them down costs calls.'),
        ('Do I need a website if I already have a Google Business Profile?',
         'The profile gets you into the map results; the website is where '
         'people go to decide. A strong profile linking to a slow or dated '
         'site loses the call to the competitor whose site answers the '
         'question. Brand-new with no reviews yet? Set up the profile first.'),
        ('Is a custom website better than Wix or Squarespace for HVAC?',
         'For a single page with your hours, a builder is cheaper and does the '
         'job. A custom build wins on speed, on owning the code outright, and '
         'on service pages built around your trades instead of a template '
         'someone else also bought.'),
        ('Will I own my website?',
         'Yes. Your domain is registered in your name from day one, and the '
         'site and its code are yours once the build is paid in full. If you '
         'ever leave, you take every file with you.'),
        ('What if I am not happy with it?',
         'Two rounds of revisions are included. Within '
         f'{GUARANTEE_DAYS} days of signing you can cancel for any reason '
         f'and get {GUARANTEE_REFUND_PERCENT}% of everything paid back.'),
    ]


def review_automation_faqs(review_system=None, plan_paid_in_full=None):
    """``review_system`` is the standalone hvac-review-system tier (Oct
    2026). Its price is read live, never hardcoded, and every sentence
    that quotes it degrades to a price-free version if the row is
    missing — a half-rendered "on its own for /month" would be worse
    than a vaguer true sentence."""
    rev = money(review_system.price) if review_system else ''
    ppf = money(plan_paid_in_full.price) if plan_paid_in_full else ''
    return [
        ('How much does the review system cost?',
         (f'{rev}/month on its own, month-to-month, cancel anytime. That '
          'is the whole price: no setup fee and no contract. It is also '
          'included at no extra cost in the Full Plan'
          + (f' ({ppf}/month), ' if ppf else ', ')
          + 'which adds the website, hosting and unlimited content '
          'updates on top.') if rev else
         ('Sold on its own month-to-month, and included at no extra cost '
          'in the Full Plan. The pricing page has the current number.')),
        ('Do I need a website from you to use it?',
         'No. It runs on whatever website you already have, and it works '
         'with no website at all: the request sends your customer straight '
         'to your Google Business Profile, which is where the review gets '
         'left either way. Plenty of good contractors have a dated site or '
         'nothing but a Google listing. This still works for them.'),
        ('How do HVAC companies get more Google reviews?',
         'By asking every customer, every time, while the job is fresh, '
         'which is the step busy crews forget. Automating the request '
         'off the completed job turns asking into a habit that runs itself, '
         'and a steady flow of recent reviews is what homeowners and Google '
         'both weigh most.'),
        ('How does automated review generation work?',
         'When a job is marked complete in your system, the customer '
         'automatically gets a text and email asking for a Google review, '
         'with a direct link to your profile. No one on your team has to '
         'remember to ask.'),
        ('When does the review request go out?',
         'One to two hours after the job is marked complete, while the work '
         'is still fresh. Jobs finished in the evening wait until 8:00 AM the '
         'customer’s local time the next morning, so nobody gets a review '
         'request at nine at night.'),
        ('Which job systems does it connect to?',
         'Any job or scheduling system that can send a webhook when a job '
         'closes. That covers ServiceTitan, Housecall Pro, Jobber and most '
         'others, either directly or through Zapier. The automation is set up '
         'inside your system during onboarding, so marking the job complete is '
         'the only thing your team ever does. Run something unusual, or '
         'nothing at all? The exact trigger is confirmed on the call before '
         'you sign.'),
        ('Does this violate Google’s review policies?',
         'No. It asks every customer the same way after a real completed job, '
         'with no incentive and no filtering for positive reviews only. '
         'Selective or paid-for asking is what gets profiles penalized; asking '
         'everyone consistently is what keeps you inside the rules.'),
        ('Is review automation sold on its own?',
         (f'Yes. {rev}/month, month-to-month, with no website build '
          'required. It is also bundled into the Full Plan at no extra '
          'cost, so if you buy a build later you stop paying for it '
          'separately. The pricing page has the full breakdown.') if rev else
         ('Yes, month-to-month, with no website build required. It is '
          'also bundled into the Full Plan at no extra cost. The pricing '
          'page has the full breakdown.')),
        ('What do you need from me to get it running?',
         'Access to your Google Business Profile so the request can link '
         'to the right place, and one trigger wired into the job or '
         'scheduling software you already use, which we set up with you in '
         'the first week. After that, marking a job complete is the only '
         'thing your team ever does.'),
        ('Can I cancel?',
         'Any time, month-to-month, 30 days’ notice. There is no contract '
         'and no setup fee to write off, so there is nothing to argue '
         'about on the way out.'),
    ]


def hosting_faqs(hourly_display=''):
    edit = (f'{hourly_display}/hour, quoted and approved before any work starts.'
            if hourly_display else 'Quoted and approved before any work starts.')
    return [
        ('Am I locked in?',
         'No. Both plans are month-to-month with 30 days’ notice. Once your '
         'build is paid off, the files are yours, and leaving is a support '
         'ticket, not a dispute.'),
        ('What happens if I cancel?',
         'Once the build is paid off, the site keeps working; the plan buys '
         'updates, monitoring and support, not the right to keep your own '
         'website online. Standard code on standard hosting means any '
         'competent developer can take it over.'),
        ('Who does the work?',
         'The engineer who built the platform, a CISSP-certified security '
         'professional. The About page lists the credentials and how to '
         'verify them.'),
        ('What does an edit cost on Hosting + Security?',
         f'{edit} On the Full Plan, routine content updates are unlimited and '
         'included.'),
    ]


def ai_receptionist_faqs(voice_receptionist=None, plan_paid_in_full=None):
    """``voice_receptionist`` is the standalone hvac-voice-receptionist
    tier (Oct 2026). Price read live, never hardcoded, and every sentence
    quoting it degrades to a price-free version if the row is missing.

    Note the asymmetry with ``review_automation_faqs``: that one can say
    "included at no extra cost in the Full Plan". This one must say the
    opposite, because the receptionist bills real per-minute vendor cost
    on every call and is never bundled. The "is it included in the Full
    Plan?" question is answered honestly and up front rather than left
    for the buyer to discover on an invoice.
    """
    price = money(voice_receptionist.price) if voice_receptionist else ''
    plan = money(plan_paid_in_full.price) if plan_paid_in_full else ''
    return [
        ('How much does the AI receptionist cost?',
         (f'{price}/month, month-to-month, cancel anytime. That includes '
          '300 minutes of talk time a month, which covers most shops. '
          'Past that it is 25 cents a minute and it keeps answering: '
          'we do not cut your phone off mid-season.'
          if price else
          'One monthly price, month-to-month, cancel anytime. It '
          'includes 300 minutes of talk time a month, then 25 cents a '
          'minute after that.')),
        ('Is it included in the Full Plan?',
         ('No, and that is deliberate. Everything else in the plan '
          'costs us the same whether you use it or not, so we can bundle '
          'it. Every call the receptionist answers has a real cost '
          'attached, so it is a separate add-on. You can have it with a '
          'plan, with just hosting, or on its own with no website from '
          'us at all.')),
        ('Do I need a website from you to use it?',
         ('No. It answers your phone, so it works whether your website '
          'came from us, from someone else, or does not exist. If you '
          'later want a site, the receptionist carries over unchanged.'
          + (f' A full website plan is {plan}/month on top.'
             if plan else ''))),
        ('Can it just cover after hours?',
         ('Yes, and that is what most shops start with. You tell us your '
          'hours and it only answers outside them, including weekends. '
          'Plenty of owners move it to all day once they hear the first '
          'few calls back.')),
        ('Will it stop the robocalls?',
         ('If it answers first, yes: it screens the call before your '
          'phone ever rings, and that is included at no extra charge. If '
          'you choose to have your own phone ring first and the '
          'receptionist only pick up what you miss, then the robocall '
          'has already reached you before it gets involved, so there is '
          'nothing for it to screen. Both setups are available; only the '
          'first one can filter spam.')),
        ('Does it put real customers through to me?',
         ('Yes. You tell it what counts as urgent, in your words, and it '
          'transfers those straight to your cell with no phone tree. If '
          'you do not pick up, it goes back to the caller and takes the '
          'details rather than dumping them in voicemail.')),
        ('What happens on a no-heat call at 9pm?',
         ('Whatever you tell it to do. Most shops have it transfer those '
          'immediately, whatever the hour, because a no-heat call in '
          'January is the most valuable call of the week. If you would '
          'rather it take the details and text you, it will do that '
          'instead.')),
        ('Does it sound like a robot?',
         ('It sounds like a calm receptionist reading from your notes. '
          'It is not pretending to be a person and it will say it is an '
          'assistant if asked. What callers actually notice is that '
          'somebody picked up on the second ring at nine at night.')),
        ('Are calls recorded?',
         ('By default yes, because the recording is what gives you the '
          'summary and the transcript in your portal. Callers are told '
          'at the start of the call, which is what keeps you right in '
          'states that require it. You can switch recording off and keep '
          'everything else.')),
        ('What if it cannot answer a question?',
         ('It takes the details and hands the call to you rather than '
          'guessing. You can add the questions you get asked constantly '
          'in your portal any time, and each one you add is a call it '
          'can finish without you.')),
        ('Can I keep my current phone number?',
         ('Yes. The simplest setup is forwarding: you keep your number '
          'and your carrier, and you can turn the forwarding off '
          'whenever you want. We can also move the number over to us '
          'completely if you prefer, which takes about two weeks.')),
        ('How do I know what it actually did?',
         ('Every call shows up in your portal with a summary, who '
           'called, how long it took and whether it put them through, '
           'plus the full transcript if recording is on. You also get a '
           'summary emailed to you as each call ends.')),
    ]
