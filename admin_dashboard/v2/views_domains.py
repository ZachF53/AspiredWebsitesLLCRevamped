"""v2 Domains — read + existing non-destructive actions only.

No transfer-out, no delete in v2 yet — those stay in v1's
admin_domain_detail, which this page links out to.
"""

from django.shortcuts import render

from admin_dashboard.decorators import admin_required


@admin_required
def domains_list(request):
    from domains.models import DomainRegistration

    website_id = request.GET.get('website')

    qs = (DomainRegistration.objects
          .select_related('account_new', 'pointed_at_website')
          .order_by('expires_at'))
    if website_id:
        qs = qs.filter(pointed_at_website_id=website_id)

    return render(request, 'admin_dashboard/v2/domains_list.html', {
        'domains': qs,
    })
