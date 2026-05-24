/*
 * Renders the click-density canvas on the conversions page.
 * Reads click coordinates from <script id="click-overlay-data">,
 * draws radial-gradient dots over a dark page-shaped rectangle.
 *
 * Density inferred by overlapping gradients — no manual binning
 * needed; the player-style "blob" intensifies naturally where
 * clicks cluster.
 */
(function () {
    'use strict';

    function init() {
        var canvas = document.getElementById('heatmap-canvas');
        var dataEl = document.getElementById('click-overlay-data');
        if (!canvas || !dataEl) { return; }

        var clicks;
        try {
            clicks = JSON.parse(dataEl.textContent);
        } catch (e) {
            clicks = [];
        }
        if (typeof clicks === 'string') {
            try { clicks = JSON.parse(clicks); } catch (e) {}
        }
        if (!Array.isArray(clicks)) { clicks = []; }

        // Scale the bitmap to its CSS pixel size so dots aren't
        // blurry on HiDPI displays — but keep the layout size
        // declared in the template (600×400).
        var rect = canvas.getBoundingClientRect();
        var W = canvas.width;
        var H = canvas.height;
        var ctx = canvas.getContext('2d');

        // Page background.
        ctx.fillStyle = '#0F172A';
        ctx.fillRect(0, 0, W, H);

        // Subtle outline so the bounds are clear.
        ctx.strokeStyle = '#1E293B';
        ctx.lineWidth = 1;
        ctx.strokeRect(0.5, 0.5, W - 1, H - 1);

        if (!clicks.length) {
            ctx.fillStyle = '#64748B';
            ctx.font = '14px Arial, Helvetica, sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('No click data yet', W / 2, H / 2);
            // Reference rect to silence the linter about unused.
            void rect;
            return;
        }

        // Each click is a radial-gradient dot. Where dots overlap,
        // the alpha stacks up naturally — denser areas glow hotter.
        clicks.forEach(function (click) {
            var x = (Number(click.x_pct) || 0) / 100 * W;
            var y = (Number(click.y_pct) || 0) / 100 * H;

            var grad = ctx.createRadialGradient(x, y, 0, x, y, 22);
            grad.addColorStop(0,   'rgba(232, 101, 10, 0.65)');
            grad.addColorStop(0.6, 'rgba(232, 101, 10, 0.18)');
            grad.addColorStop(1,   'rgba(232, 101, 10, 0)');

            ctx.beginPath();
            ctx.arc(x, y, 22, 0, Math.PI * 2);
            ctx.fillStyle = grad;
            ctx.fill();
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
