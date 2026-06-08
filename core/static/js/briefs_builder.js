// briefs_builder.js — live preview + client-side download for the
// blank brief builder page.
//
// The page embeds the raw blankdesign.md text in a hidden <textarea>
// (#brief-template-source). Every form input has data-field="<key>"
// matching a {{key}} placeholder in the template. On any input we
// re-render the preview by find-replacing every {{key}}; empty inputs
// leave the {{key}} marker visible so the operator sees what's still
// missing.
//
// Download is a Blob — no server roundtrip needed; the preview text
// is exactly what gets saved.
(function () {
    'use strict';

    var sourceEl = document.getElementById('brief-template-source');
    var previewEl = document.getElementById('brief-preview');
    var progressEl = document.getElementById('brief-progress');
    var form = document.getElementById('brief-form');
    var downloadBtnTop = document.getElementById('brief-download');
    var downloadBtnBottom = document.getElementById('brief-download-bottom');
    if (!sourceEl || !previewEl || !form) return;

    var templateText = sourceEl.value;
    var totalFields = form.querySelectorAll('[data-field]').length;

    // Indent a multiline value so each line gets a leading "    - "
    // prefix — used for service "Included" lists where the template
    // formatting expects an indented bullet list.
    function indentBullets(value) {
        if (!value) return '';
        return value
            .split('\n')
            .map(function (line) {
                line = line.trim();
                if (!line) return '';
                // If user already started with "-", just indent
                if (line.charAt(0) === '-') return '    ' + line;
                return '    - ' + line;
            })
            .filter(function (l) { return l; })
            .join('\n');
    }

    // Some fields want special rendering when stitched into the
    // preview — e.g. "Included" lists need indentation. Default is
    // to use the raw value.
    function renderValue(key, value) {
        if (!value) return '';
        if (/_included$/.test(key)) return indentBullets(value);
        return value;
    }

    function escapeRegex(s) {
        return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    }

    function rebuildPreview() {
        var text = templateText;
        var filled = 0;
        form.querySelectorAll('[data-field]').forEach(function (el) {
            var key = el.getAttribute('data-field');
            var value = (el.value || '').trim();
            if (value) filled++;
            var rendered = renderValue(key, value);
            if (rendered) {
                var pattern = new RegExp(
                    '\\{\\{' + escapeRegex(key) + '\\}\\}', 'g');
                text = text.replace(pattern, rendered);
            }
        });
        previewEl.textContent = text;
        if (progressEl) {
            progressEl.textContent =
                filled + ' / ' + totalFields + ' fields filled';
        }
    }

    function slugify(s) {
        return (s || '')
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, '-')
            .replace(/^-+|-+$/g, '')
            .slice(0, 60) || 'brief';
    }

    function download() {
        var business = document.querySelector('[data-field="business_name"]');
        var name = slugify(business ? business.value : '') + '-brief.md';
        var blob = new Blob([previewEl.textContent],
            { type: 'text/markdown;charset=utf-8' });
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    }

    // Initial render so the preview shows the unfilled template
    rebuildPreview();

    form.addEventListener('input', rebuildPreview);
    form.addEventListener('change', rebuildPreview);
    if (downloadBtnTop) downloadBtnTop.addEventListener('click', download);
    if (downloadBtnBottom) downloadBtnBottom.addEventListener('click', download);
})();
