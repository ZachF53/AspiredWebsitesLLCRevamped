/**
 * v2 website-create page — "Existing client, no build" preset.
 *
 * External because the site CSP is `script-src 'self'` — no inline.
 *
 * Behaviour:
 *   - Two radios choose a preset: New build (default) or Existing
 *     client — site already live, no build.
 *   - Selecting a preset only changes which <option> is marked selected
 *     on the stage / payment_status / onboarding_status selects — it
 *     never disables them. The admin can still change any of the three
 *     by hand after picking a preset, in either direction.
 *   - This is pure client-side convenience. The server has no notion of
 *     "which preset was used" — it only ever sees whatever values the
 *     three selects were showing at submit time, same as any other
 *     form field.
 */
(function () {
    var buildRadio = document.getElementById('website-type-build');
    var noBuildRadio = document.getElementById('website-type-no-build');
    var stageSelect = document.getElementById('id_stage');
    var paymentSelect = document.getElementById('id_payment_status');
    var onboardingSelect = document.getElementById('id_onboarding_status');

    if (!buildRadio || !noBuildRadio || !stageSelect
            || !paymentSelect || !onboardingSelect) { return; }

    var NO_BUILD_VALUES = {
        stage: 'live',
        payment_status: 'fully_paid',
        onboarding_status: 'intake_complete'
    };

    function applyPreset(values) {
        stageSelect.value = values.stage;
        paymentSelect.value = values.payment_status;
        onboardingSelect.value = values.onboarding_status;
    }

    noBuildRadio.addEventListener('change', function () {
        if (noBuildRadio.checked) {
            applyPreset(NO_BUILD_VALUES);
        }
    });

    buildRadio.addEventListener('change', function () {
        if (buildRadio.checked) {
            applyPreset({ stage: '', payment_status: '', onboarding_status: '' });
        }
    });
})();
