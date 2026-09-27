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
         'For a single page with your hours, a builder is honestly cheaper and '
         'fine. A custom build wins on speed, on owning the code outright, and '
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


def review_automation_faqs():
    return [
        ('How do HVAC companies get more Google reviews?',
         'By asking every customer, every time, while the job is fresh — '
         'which is exactly the step busy crews forget. Automating the request '
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
         'closes — that covers ServiceTitan, Housecall Pro, Jobber and most '
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
         'No. It is included in the Full Plan alongside hosting, maintenance '
         'and unlimited content updates. The pricing page has the full '
         'breakdown.'),
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
        ('Who actually does the work?',
         'The engineer who built the platform, a CISSP-certified security '
         'professional. The About page lists the credentials and how to '
         'verify them.'),
        ('What does an edit cost on Hosting + Security?',
         f'{edit} On the Full Plan, routine content updates are unlimited and '
         'included.'),
    ]
