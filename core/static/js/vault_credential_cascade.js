// vault_credential_cascade.js — cascading dropdown for the vault
// credential form. When the user picks a Category, the Type dropdown
// is repopulated with that category's allowed types. When Type is
// "other", the custom-label input is unhidden.
//
// The TYPES_BY_CATEGORY data is embedded in the page as a JSON
// <script id="cred-types-data" type="application/json">…</script>
// tag (CSP-safe — no inline executable JS).
(function () {
    'use strict';

    var dataEl = document.getElementById('cred-types-data');
    var categoryEl = document.querySelector('[data-cred-category]');
    var typeEl = document.querySelector('[data-cred-type]');
    var customGroup = document.getElementById('cred-custom-label-group');
    var customEl = document.querySelector('[data-cred-custom]');
    var helpEl = document.getElementById('cred-type-help');

    if (!dataEl || !categoryEl || !typeEl) return;

    var typesByCategory;
    try {
        typesByCategory = JSON.parse(dataEl.textContent);
    } catch (e) {
        return;
    }

    function populateTypes(category, desiredType) {
        var list = typesByCategory[category] || [];
        // Remember what was selected before we rebuild
        var prior = desiredType || typeEl.value;

        // Clear + rebuild
        while (typeEl.firstChild) typeEl.removeChild(typeEl.firstChild);
        var hit = false;
        list.forEach(function (pair) {
            var opt = document.createElement('option');
            opt.value = pair[0];
            opt.textContent = pair[1];
            if (pair[0] === prior) {
                opt.selected = true;
                hit = true;
            }
            typeEl.appendChild(opt);
        });
        if (!hit && list.length) {
            typeEl.selectedIndex = 0;
        }
        toggleCustom();
    }

    function toggleCustom() {
        if (!customGroup) return;
        var isOther = typeEl.value === 'other';
        customGroup.hidden = !isOther;
        if (customEl) {
            if (isOther) {
                customEl.setAttribute('required', 'required');
            } else {
                customEl.removeAttribute('required');
            }
        }
        if (helpEl) {
            if (isOther) {
                helpEl.textContent =
                    'No matching type — describe what this credential is for ' +
                    'in the field below so you can identify it later.';
            } else {
                helpEl.textContent =
                    'Picking a specific type lets the SetupTodo widget ' +
                    'auto-mark related tasks complete.';
            }
        }
    }

    // Initial paint — server may have selected a category/type via
    // ?category=&type= URL params; preserve the chosen type if it
    // belongs to the chosen category.
    populateTypes(categoryEl.value, typeEl.value);

    categoryEl.addEventListener('change', function () {
        populateTypes(categoryEl.value);
    });
    typeEl.addEventListener('change', toggleCustom);
})();
