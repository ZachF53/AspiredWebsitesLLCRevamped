// schedule_call.js — month calendar + per-day slot picker.
//
// Customer flow:
//   1. Page loads → fetch all available slots (next 60 days).
//   2. Render a month grid. Days with availability are clickable,
//      days without are dimmed.
//   3. Auto-select the soonest available day on load.
//   4. Clicking a day fills the "times" panel beneath the calendar
//      with that day's 30-min slot buttons (visitor-local time).
//   5. Picking a time POSTs /schedule/hold/, then the form below
//      handles the confirm.
(function () {
    'use strict';

    var slotList = document.getElementById('slot-list');
    var form = document.getElementById('schedule-form');
    var submitBtn = document.getElementById('schedule-submit');
    var chosenLine = document.getElementById('s-chosen-time');
    var selectedSlotInput = document.getElementById('s-selected-slot');
    var errorEl = document.getElementById('schedule-error');
    var heldCallId = null;

    // Slots from the server, indexed by visitor-local YYYY-MM-DD
    var slotsByDay = {};
    // Calendar state
    var viewYear = 0;
    var viewMonth = 0;          // 0-11 like JS Date
    var selectedDayKey = null;  // 'YYYY-MM-DD' currently picked, or null
    var pickedBtn = null;       // the highlighted time button

    function getCsrf() {
        var m = document.cookie.match(/csrftoken=([^;]+)/);
        return m ? m[1] : '';
    }

    function showError(msg) {
        errorEl.textContent = msg;
        errorEl.hidden = false;
    }
    function clearError() {
        errorEl.hidden = true;
    }

    function localDateKey(iso) {
        var d = new Date(iso);
        var y = d.getFullYear();
        var m = String(d.getMonth() + 1).padStart(2, '0');
        var day = String(d.getDate()).padStart(2, '0');
        return y + '-' + m + '-' + day;
    }
    function keyFromYmd(y, m, day) {
        return y + '-' + String(m + 1).padStart(2, '0') + '-' +
               String(day).padStart(2, '0');
    }
    function formatTime(iso) {
        return new Date(iso).toLocaleTimeString(undefined, {
            hour: 'numeric', minute: '2-digit'
        });
    }
    function formatLongDate(key) {
        // key = 'YYYY-MM-DD' (local). Build a local Date at noon to
        // dodge DST edge cases.
        var p = key.split('-');
        var d = new Date(parseInt(p[0], 10), parseInt(p[1], 10) - 1,
                         parseInt(p[2], 10), 12, 0, 0);
        return d.toLocaleDateString(undefined, {
            weekday: 'long', month: 'long', day: 'numeric', year: 'numeric'
        });
    }
    function fullLabel(iso) {
        var d = new Date(iso);
        return d.toLocaleDateString(undefined, {
            weekday: 'long', month: 'short', day: 'numeric'
        }) + ' at ' + formatTime(iso);
    }

    function fetchSlots() {
        return fetch('/schedule/slots.json?days=60',
                     { credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                slotsByDay = {};
                (data.slots || []).forEach(function (s) {
                    var key = localDateKey(s.start);
                    if (!slotsByDay[key]) slotsByDay[key] = [];
                    slotsByDay[key].push(s);
                });
            })
            .catch(function () {
                slotList.innerHTML =
                    '<p class="cal-error">Could not load availability — refresh to retry.</p>';
            });
    }

    function buildShell() {
        slotList.innerHTML = '' +
          '<div class="cal">' +
            '<div class="cal__header">' +
              '<button type="button" class="cal__nav" id="cal-prev" aria-label="Previous month">&lsaquo;</button>' +
              '<span class="cal__month-label" id="cal-month-label"></span>' +
              '<button type="button" class="cal__nav" id="cal-next" aria-label="Next month">&rsaquo;</button>' +
            '</div>' +
            '<div class="cal__weekdays">' +
              ['Sun','Mon','Tue','Wed','Thu','Fri','Sat']
                .map(function (d) { return '<span class="cal__weekday">' + d + '</span>'; }).join('') +
            '</div>' +
            '<div class="cal__grid" id="cal-grid"></div>' +
          '</div>' +
          '<div class="cal-slots" id="cal-slots">' +
            '<p class="cal-slots__empty">Pick a date to see open times.</p>' +
          '</div>';

        document.getElementById('cal-prev').addEventListener('click', function () {
            viewMonth -= 1;
            if (viewMonth < 0) { viewMonth = 11; viewYear -= 1; }
            renderCalendar();
        });
        document.getElementById('cal-next').addEventListener('click', function () {
            viewMonth += 1;
            if (viewMonth > 11) { viewMonth = 0; viewYear += 1; }
            renderCalendar();
        });
    }

    function renderCalendar() {
        var label = document.getElementById('cal-month-label');
        var grid = document.getElementById('cal-grid');
        var prev = document.getElementById('cal-prev');

        var first = new Date(viewYear, viewMonth, 1);
        label.textContent = first.toLocaleDateString(undefined, {
            month: 'long', year: 'numeric'
        });

        // Disable "prev" when we're at the visitor-local current month.
        var todayLocal = new Date();
        var atOrBeforeNow = (viewYear < todayLocal.getFullYear()) ||
            (viewYear === todayLocal.getFullYear() &&
             viewMonth <= todayLocal.getMonth());
        prev.disabled = atOrBeforeNow;

        grid.innerHTML = '';
        // Leading blanks: Sun=0..Sat=6 — first.getDay() works directly
        var leading = first.getDay();
        for (var i = 0; i < leading; i++) {
            var pad = document.createElement('span');
            pad.className = 'cal__cell cal__cell--pad';
            grid.appendChild(pad);
        }
        // Days of the month
        var daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();
        var todayKey = keyFromYmd(
            todayLocal.getFullYear(), todayLocal.getMonth(),
            todayLocal.getDate());
        for (var day = 1; day <= daysInMonth; day++) {
            var cell = document.createElement('button');
            cell.type = 'button';
            cell.className = 'cal__cell';
            var key = keyFromYmd(viewYear, viewMonth, day);
            cell.dataset.key = key;
            cell.textContent = day;

            if (key < todayKey) {
                cell.classList.add('cal__cell--past');
                cell.disabled = true;
            } else if (slotsByDay[key] && slotsByDay[key].length) {
                cell.classList.add('cal__cell--open');
            } else {
                cell.classList.add('cal__cell--closed');
                cell.disabled = true;
            }
            if (key === todayKey) {
                cell.classList.add('cal__cell--today');
            }
            if (key === selectedDayKey) {
                cell.classList.add('cal__cell--selected');
            }
            cell.addEventListener('click', function (ev) {
                selectDay(ev.currentTarget.dataset.key);
            });
            grid.appendChild(cell);
        }
    }

    function selectDay(key) {
        selectedDayKey = key;
        // Re-render selection highlight without rebuilding the grid
        var cells = document.querySelectorAll('.cal__cell--selected');
        cells.forEach(function (c) { c.classList.remove('cal__cell--selected'); });
        var newSel = document.querySelector('.cal__cell[data-key="' + key + '"]');
        if (newSel) newSel.classList.add('cal__cell--selected');
        renderSlots(key);
    }

    function renderSlots(key) {
        var panel = document.getElementById('cal-slots');
        var slots = slotsByDay[key] || [];
        if (!slots.length) {
            panel.innerHTML = '<p class="cal-slots__empty">No open times that day.</p>';
            return;
        }
        var heading = document.createElement('div');
        heading.className = 'cal-slots__heading';
        heading.textContent = formatLongDate(key);

        var grid = document.createElement('div');
        grid.className = 'cal-slots__grid';
        slots.forEach(function (s) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'cal-slot';
            btn.textContent = formatTime(s.start);
            btn.addEventListener('click', function (ev) {
                pickSlot(s, ev.currentTarget);
            });
            grid.appendChild(btn);
        });

        panel.innerHTML = '';
        panel.appendChild(heading);
        panel.appendChild(grid);
    }

    function autoSelectFirstAvailable() {
        // Find the soonest day that has slots and focus the calendar there.
        var keys = Object.keys(slotsByDay).sort();
        if (!keys.length) {
            var panel = document.getElementById('cal-slots');
            panel.innerHTML =
                '<p class="cal-slots__empty">' +
                'No availability in the next 60 days — please email us.' +
                '</p>';
            return;
        }
        var first = keys[0];
        var p = first.split('-');
        viewYear = parseInt(p[0], 10);
        viewMonth = parseInt(p[1], 10) - 1;
        selectedDayKey = first;
        renderCalendar();
        renderSlots(first);
    }

    function pickSlot(s, btnEl) {
        clearError();
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
                return r.json().then(function (data) {
                    showError(
                        (data && data.error) === 'too close to start time'
                            ? 'That time is too close to now — pick a later slot.'
                            : 'That slot just got taken — pick another.');
                    return fetchSlots().then(function () {
                        renderCalendar();
                        if (selectedDayKey) renderSlots(selectedDayKey);
                    });
                });
            }
            return r.json();
        }).then(function (data) {
            if (!data || !data.ok) return;
            heldCallId = data.call_id;
            selectedSlotInput.value = s.start;
            chosenLine.textContent =
                '✓ Holding ' + fullLabel(s.start) + ' for 15 minutes';
            submitBtn.disabled = false;
            submitBtn.textContent = 'Confirm + submit';
            if (pickedBtn) pickedBtn.classList.remove('cal-slot--picked');
            if (btnEl) {
                btnEl.classList.add('cal-slot--picked');
                pickedBtn = btnEl;
            }
        });
    }

    form.addEventListener('submit', function (e) {
        e.preventDefault();
        clearError();
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
            // build_type is service-specific — only on web-design form.
            build_type: form.elements['build_type']
                ? form.elements['build_type'].value : '',
            // service: 'web_design' | 'social_media' | 'seo' — hidden input
            // set per-page by the scheduler view; falls back to web_design.
            service: form.elements['service']
                ? form.elements['service'].value : 'web_design',
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
                  document.querySelector('.contact-grid').innerHTML =
                      '<div class="checkout-success">' +
                      '<h1>Booked!</h1>' +
                      '<p class="checkout-success__lead">' +
                      'Your call is confirmed. You will receive an email confirmation shortly.' +
                      '</p></div>';
              }
          });
    });

    // Boot
    buildShell();
    fetchSlots().then(autoSelectFirstAvailable);
})();
