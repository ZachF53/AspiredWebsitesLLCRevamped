/* Contract signing — enable the Sign button only after the client has
   scrolled to the bottom of the agreement. Degrades gracefully: with JS
   disabled the button stays usable (server still validates name + checkbox). */
(function () {
    'use strict';

    var scroller = document.getElementById('contract-scroll');
    var button = document.getElementById('contract-sign-btn');
    var hint = document.getElementById('contract-scroll-hint');
    if (!scroller || !button) {
        return;
    }

    function unlock() {
        button.disabled = false;
        if (hint) {
            hint.hidden = true;
        }
        scroller.removeEventListener('scroll', onScroll);
    }

    function atBottom() {
        return scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 8;
    }

    function onScroll() {
        if (atBottom()) {
            unlock();
        }
    }

    // Lock the button now that JS is confirmed running.
    button.disabled = true;

    if (scroller.scrollHeight <= scroller.clientHeight + 8) {
        // Agreement is short enough that there is nothing to scroll.
        unlock();
    } else {
        scroller.addEventListener('scroll', onScroll);
    }
})();
