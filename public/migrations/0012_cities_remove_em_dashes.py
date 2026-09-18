# Sept 2026 voice pass: strip em dashes from the City model copy
# (san-antonio, atlanta, warner-robins) so /locations/*/ reads like
# the CISSP engineer who writes it. Punctuation-only rewrite -- no
# facts or claims changed. There is no seed_cities management
# command (0006/0007 set this content directly), so this migration
# is the sole source of truth for the fix.

from django.db import migrations


SAN_ANTONIO_HONESTY_HTML = "<div class=\"cards cards--2col\">\n    <article class=\"card card--accent-left\">\n        <h3>How This Actually Works</h3>\n        <p>\n            We\u2019re based in Georgia and work with San Antonio clients\n            remotely: calls, screen shares, and a staging link you can\n            review from anywhere. Our <strong>(210)</strong> number is a real\n            San Antonio line, not a redirect trick, and it\u2019s where you\n            reach us.\n        </p>\n    </article>\n\n    <article class=\"card card--accent-left\">\n        <h3>Whether That Matters</h3>\n        <p>\n            For a website build, it usually doesn\u2019t. The work is\n            calls, drafts and reviews either way. If you specifically want\n            someone who can sit in your office, hire locally, and we\u2019ll\n            say so on the call rather than talk you out of it.\n        </p>\n    </article>\n</div>"

SAN_ANTONIO_PROOF_SUBHEADING = "Not a stock photo of the Riverwalk. Actual clients you can go and look at."

SAN_ANTONIO_CTA_BODY_HTML = "Call <a href=\"tel:+12108962536\">(210) 896-2536</a> or book a time. We answer within 24 hours, and we\u2019ll tell you honestly if you\u2019d be better served by someone local."

ATLANTA_HONESTY_HTML = "<div class=\"cards cards--2col\">\n    <article class=\"card card--accent-left\">\n        <h3>Where We Actually Are</h3>\n        <p>\n            Warner Robins, in Middle Georgia: about 100 miles\n            south of Atlanta on I-75. Same state, same time zone, and\n            close enough to sit in your office when a project genuinely\n            calls for it. We don\u2019t keep an Atlanta suite, and we\n            won\u2019t list one.\n        </p>\n    </article>\n\n    <article class=\"card card--accent-left\">\n        <h3>How The Work Happens</h3>\n        <p>\n            Most of it remotely, because that\u2019s what a website\n            build actually is: calls, drafts, a staging link you\n            review from anywhere. Kickoff or a launch review in person\n            is a drive, not a flight, so ask if you want it.\n        </p>\n    </article>\n</div>"

ATLANTA_CTA_BODY_HTML = "Call <a href=\"tel:+12108962536\">(210) 896-2536</a> or book a time. We answer within 24 hours, and we\u2019ll tell you honestly if you\u2019d be better served by an agency in the city."

WARNER_ROBINS_HONESTY_HTML = "<div class=\"cards cards--2col\">\n    <article class=\"card card--accent-left\">\n        <h3>You Can Meet Us</h3>\n        <p>\n            Warner Robins is home. Coffee in Houston County, a sit-down\n            in Macon, or a drive out to Perry: that\u2019s a\n            normal week, not a special arrangement. Most of the build\n            still happens over calls and a staging link, because\n            that\u2019s what works, but in person is genuinely on the\n            table.\n        </p>\n    </article>\n\n    <article class=\"card card--accent-left\">\n        <h3>We Work From Here, Not From A Storefront</h3>\n        <p>\n            There\u2019s no walk-in office and we\u2019re not going to\n            invent one. Like most one-engineer studios we run as a\n            service-area business: we come to you, or we work\n            remotely, whichever suits the project.\n        </p>\n        <p>\n            One thing worth explaining: our number is a\n            <strong>210</strong> area code. That\u2019s a San Antonio\n            line from our largest client base, not a hint that\n            we\u2019re in Texas. It rings the same person, here in\n            Warner Robins.\n        </p>\n    </article>\n</div>"

WARNER_ROBINS_CROSS_LINK_HTML = "<p class=\"section-note\">\n    Up the interstate? We work with\n    <a href=\"/locations/atlanta/\">Atlanta businesses</a> too,\n    about 100 miles, and close enough to drive when a project needs\n    it. Outside Georgia, most of our clients are in\n    <a href=\"/locations/san-antonio/\">San Antonio</a>.\n</p>"

WARNER_ROBINS_CTA_BODY_HTML = "Call <a href=\"tel:+12108962536\">(210) 896-2536</a> or book a time. We answer within 24 hours, and if a website isn\u2019t what you need right now, we\u2019ll say so."


def strip_em_dashes(apps, schema_editor):
    City = apps.get_model('public', 'City')
    City.objects.filter(slug='san-antonio').update(
        honesty_html=SAN_ANTONIO_HONESTY_HTML,
        proof_subheading=SAN_ANTONIO_PROOF_SUBHEADING,
        cta_body_html=SAN_ANTONIO_CTA_BODY_HTML,
    )
    City.objects.filter(slug='atlanta').update(
        honesty_html=ATLANTA_HONESTY_HTML,
        cta_body_html=ATLANTA_CTA_BODY_HTML,
    )
    City.objects.filter(slug='warner-robins').update(
        honesty_html=WARNER_ROBINS_HONESTY_HTML,
        cross_link_html=WARNER_ROBINS_CROSS_LINK_HTML,
        cta_body_html=WARNER_ROBINS_CTA_BODY_HTML,
    )


def noop_reverse(apps, schema_editor):
    """Data migration -- nothing to unwind on reverse."""


class Migration(migrations.Migration):

    dependencies = [
        ('public', '0011_articles_remove_em_dashes'),
    ]

    operations = [
        migrations.RunPython(strip_em_dashes, noop_reverse),
    ]
