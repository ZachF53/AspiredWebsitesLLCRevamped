// schedule_call.js — load available slots, let user pick, hold,
// then submit the form to confirm.
(function () {
    'use strict';

    var slotList = document.getElementById('slot-list');
    var form = document.getElementById('schedule-form');
    var submitBtn = document.getElementById('schedule-submit');
    var chosenLine = document.getElementById('s-chosen-time');
    var selectedSlotInput = document.getElementById('s-selected-slot');
    var errorEl = document.getElementById('schedule-error');
    var heldCallId = null;

    function getCsrf() {
        var m = document.cookie.match(/csrftoken=([^;]+)/);
        return m ? m[1] : '';
    }

    function fetchSlots() {
        fetch('/schedule/slots.json', { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(renderSlots)
            .catch(function () {
                slotList.textContent = 'Could not load slots — refresh to retry.';
            });
    }

    function renderSlots(data) {
        slotList.innerHTML = '';
        if (!data.slots || !data.slots.length) {
            slotList.textContent = 'No slots available in the next 3 weeks — please email us.';
            return;
        }
        // Group by date
        var byDate = {};
        data.slots.forEach(function (s) {
            var d = s.start.slice(0, 10);
            if (!byDate[d]) byDate[d] = [];
            byDate[d].push(s);
        });
        Object.keys(byDate).forEach(function (d) {
            var group = document.createElement('div');
            group.className = 'schedule-day';
            var heading = document.createElement('div');
            heading.className = 'schedule-day__heading';
            heading.textContent = new Date(byDate[d][0].start)
                .toLocaleDateString(undefined, {
                    weekday: 'long', month: 'short', day: 'numeric'
                });
            group.appendChild(heading);
            byDate[d].forEach(function (s) {
                var btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'schedule-slot';
                btn.textContent = s.label.split(' ').slice(3).join(' ');
                btn.addEventListener('click', function () {
                    pickSlot(s);
                });
                group.appendChild(btn);
            });
            slotList.appendChild(group);
        });
    }

    function pickSlot(s) {
        // Try to hold the slot
        fetch('/schedule/hold/', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCsrf(),
            },
            body: JSON.stringify({ starts_at: s.start }),
        }).then(function (r) {
            if (r.status === 409) {
                showError('That slot just got taken — pick another.');
                fetchSlots();
                return;
            }
            return r.json();
        }).then(function (data) {
            if (!data || !data.ok) return;
            heldCallId = data.call_id;
            selectedSlotInput.value = s.start;
            chosenLine.textContent = '✓ Holding ' + s.label + ' for 15 minutes';
            submitBtn.disabled = false;
            submitBtn.textContent = 'Confirm + submit';
            // Highlight the picked button
            document.querySelectorAll('.schedule-slot--picked').forEach(
                function (el) { el.classList.remove('schedule-slot--picked'); });
            event.target.classList.add('schedule-slot--picked');
        });
    }

    function showError(msg) {
        errorEl.textContent = msg;
        errorEl.hidden = false;
    }

    form.addEventListener('submit', function (e) {
        e.preventDefault();
        errorEl.hidden = true;
        if (!heldCallId) {
            showError('Please pick a time first.');
            return;
        }
        var addons = [];
        form.querySelectorAll('input[name="addons"]:checked').forEach(
            function (c) { addons.push(c.value); });
        var payload = {
            call_id: heldCallId,
            name: form.elements['name'].value.trim(),
            email: form.elements['email'].value.trim(),
            phone: form.elements['phone'].value.trim(),
            business: form.elements['business'].value.trim(),
            website: form.elements['website'].value.trim(),
            build_type: form.elements['build_type'].value,
            inquiry: form.elements['inquiry'].value.trim(),
            addons: addons,
        };
        fetch('/schedule/confirm/', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCsrf(),
            },
            body: JSON.stringify(payload),
        }).then(function (r) { return r.json(); })
          .then(function (data) {
              if (data.error) { showError(data.error); return; }
              if (data.ok) {
                  document.querySelector('.schedule-shell').innerHTML =
                      '<div class="checkout-success">' +
                      '<h1>Booked!</h1>' +
                      '<p class="checkout-success__lead">' +
                      'Your call is confirmed. You will receive an email confirmation shortly.' +
                      '</p></div>';
              }
          });
    });

    fetchSlots();
})();
