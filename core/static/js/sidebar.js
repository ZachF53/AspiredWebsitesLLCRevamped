/* Admin sidebar — mobile open/close + close-on-navigate. */
(function () {
    'use strict';

    const sidebar = document.getElementById('admin-sidebar');
    const overlay = document.getElementById('sidebar-overlay');
    const hamburger = document.getElementById('hamburger-btn');
    const closeBtn = document.getElementById('sidebar-close');

    // Bail quietly on non-admin pages where the sidebar isn't present.
    if (!sidebar || !overlay || !hamburger) return;

    function openSidebar() {
        sidebar.classList.add('open');
        overlay.classList.add('active');
        document.body.style.overflow = 'hidden';
    }

    function closeSidebar() {
        sidebar.classList.remove('open');
        overlay.classList.remove('active');
        document.body.style.overflow = '';
    }

    hamburger.addEventListener('click', openSidebar);
    overlay.addEventListener('click', closeSidebar);
    if (closeBtn) closeBtn.addEventListener('click', closeSidebar);

    // Close on link tap so a single tap navigates + dismisses.
    sidebar.querySelectorAll('a').forEach(function (link) {
        link.addEventListener('click', function () {
            if (window.innerWidth < 768) closeSidebar();
        });
    });

    // Reset state if the viewport grows back to desktop while open.
    let lastWidth = window.innerWidth;
    window.addEventListener('resize', function () {
        const w = window.innerWidth;
        if (lastWidth < 768 && w >= 768) closeSidebar();
        lastWidth = w;
    });
})();
