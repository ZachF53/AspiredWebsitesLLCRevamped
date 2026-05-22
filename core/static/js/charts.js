/* Sets bar heights from a data-bar-h attribute (0-100).
   CSP-safe: JS-applied styles are allowed — only inline style attributes
   in the HTML source are blocked by the style-src policy. */
(function () {
    'use strict';
    document.querySelectorAll('[data-bar-h]').forEach(function (el) {
        var h = parseFloat(el.getAttribute('data-bar-h'));
        if (isNaN(h)) { h = 0; }
        el.style.height = Math.max(0, Math.min(100, h)) + '%';
    });
})();
