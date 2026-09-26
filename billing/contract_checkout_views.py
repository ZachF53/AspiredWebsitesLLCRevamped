"""
Pay-at-signing page for current-terms agreements.

    /pay/contract/<token>/            GET   summary + card form
    /pay/contract/<token>/confirm/    POST  JSON {payment_method_id} →
                                            run_contract_checkout
    /pay/contract/<token>/complete/   GET   after SCA: finish any remaining
                                            steps, then on to account setup

Auth is the contract's unguessable token (the same one the emailed signing
link carries), exactly like /pay/<invoice-token>/. The confirm endpoint is
csrf_exempt for the same reason pay_plan_confirm and checkout_confirm are:
it is token-gated, JSON-only, and never acts on an ambient session.
"""

import json
import logging

from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from billing.contract_billing import (
    next_url_after_checkout,
    run_contract_checkout,
    signing_summary,
)
from clients.models import Contract

logger = logging.getLogger(__name__)


def _contract_or_redirect(contract_token):
    contract = get_object_or_404(Contract, contract_token=contract_token)
    if not contract.signed:
        return contract, redirect('clients:contract_sign',
                                  contract_token=contract_token)
    if contract.is_legacy_billing:
        return contract, redirect('clients:contract_pay',
                                  contract_token=contract_token)
    return contract, None


def pay_contract(request, contract_token):
    contract, bounce = _contract_or_redirect(contract_token)
    if bounce is not None:
        return bounce
    if contract.paid_at_signing_at:
        return redirect(next_url_after_checkout(contract))

    owner = contract.account or contract.client
    stripe_config = {
        'publishable_key': getattr(settings, 'STRIPE_PUBLISHABLE_KEY', ''),
        'confirm_url': request.build_absolute_uri(
            f'/pay/contract/{contract_token}/confirm/'),
        'success_url': request.build_absolute_uri(
            f'/pay/contract/{contract_token}/complete/'),
    }
    return render(request, 'billing/pay_contract.html', {
        'contract': contract,
        'summary': signing_summary(contract),
        'client_name': (getattr(owner, 'name', '')
                        or getattr(owner, 'firm_name', '') or ''),
        'stripe_config': stripe_config,
    })


@csrf_exempt
@require_POST
@ratelimit(key='ip', rate='20/h', method='POST', block=True)
def pay_contract_confirm(request, contract_token):
    contract, bounce = _contract_or_redirect(contract_token)
    if bounce is not None:
        return JsonResponse({'error': 'This agreement cannot be paid here.'},
                            status=400)
    try:
        payload = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'bad json'}, status=400)
    pm = (payload.get('payment_method_id') or '').strip()
    if not pm:
        return JsonResponse({'error': 'payment_method_id required'},
                            status=400)
    result = run_contract_checkout(contract, payment_method_id=pm)
    return JsonResponse(result, status=400 if 'error' in result else 200)


def pay_contract_complete(request, contract_token):
    """Browser lands here after the card step (and after any 3-D Secure
    challenge). Re-runs the idempotent checkout with the saved card so
    every remaining subscription is started, then moves on."""
    contract, bounce = _contract_or_redirect(contract_token)
    if bounce is not None:
        return bounce
    if not contract.paid_at_signing_at:
        result = run_contract_checkout(contract)
        if 'error' in result or result.get('requires_action'):
            messages.error(
                request,
                result.get('error')
                or 'Your bank needs one more confirmation — please enter '
                   'your card again.')
            return redirect('pay_contract', contract_token=contract_token)
    return redirect(next_url_after_checkout(contract))
