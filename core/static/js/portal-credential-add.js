// portal-credential-add.js — cascading category → type dropdown for the
// client "add a credential" form. CSP-safe: external file, reads the type
// vocabulary from a non-executable <script type="application/json"> block.
(function () {
    'use strict';
    var catSel = document.getElementById('pcred-category');
    var typeSel = document.getElementById('pcred-type');
    var dataEl = document.getElementById('pcred-types-data');
    if (!catSel || !typeSel || !dataEl) return;

    var types = {};
    try {
        types = JSON.parse(dataEl.textContent || '{}');
    } catch (e) {
        return;
    }

    function populate() {
        var list = types[catSel.value] || [];
        typeSel.innerHTML = '';
        list.forEach(function (pair) {
            var o = document.createElement('option');
            o.value = pair[0];
            o.textContent = pair[1];
            typeSel.appendChild(o);
        });
    }

    catSel.addEventListener('change', populate);
    populate();
})();
