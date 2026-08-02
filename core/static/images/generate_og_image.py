"""
Generate the default Open Graph share card (1200x630).

Master Plan §4.1 brand tokens only — no invented colours, no claims
beyond the master record (§1). Re-run after a brand change:

    python core/static/images/generate_og_image.py

Writes core/static/images/og-default.png. Commit the PNG; this script
exists so the card is reproducible rather than a mystery binary.
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

# §4.1 tokens
NAVY = (12, 17, 33)        # --color-bg        #0C1121
BLUE = (28, 96, 157)       # --color-secondary #1C609D
ORANGE = (231, 101, 10)    # --color-primary   #E7650A
TEAL = (20, 184, 168)      # --color-accent    #14B8A8
WHITE = (251, 252, 250)    # --color-text      #FBFCFA
MUTED = (144, 159, 169)    # --color-muted     #909FA9

W, H = 1200, 630
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'og-default.png')

FONT_DIRS = ['C:/Windows/Fonts', '/usr/share/fonts/truetype/dejavu',
             '/usr/share/fonts/truetype/liberation']
BOLD_NAMES = ['arialbd.ttf', 'DejaVuSans-Bold.ttf',
              'LiberationSans-Bold.ttf']
REG_NAMES = ['arial.ttf', 'DejaVuSans.ttf', 'LiberationSans-Regular.ttf']


def _font(names: list[str], size: int) -> ImageFont.FreeTypeFont:
    for directory in FONT_DIRS:
        for name in names:
            path = os.path.join(directory, name)
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def main() -> None:
    img = Image.new('RGB', (W, H), NAVY)
    draw = ImageDraw.Draw(img)

    # Structural blue wash in the lower-right — §4.3 "dark polished
    # backgrounds", kept subtle so text contrast stays WCAG AA.
    glow = Image.new('RGB', (W, H), NAVY)
    gdraw = ImageDraw.Draw(glow)
    for i in range(28):
        alpha = 1 - (i / 28)
        radius = 420 - i * 14
        colour = (
            int(NAVY[0] + (BLUE[0] - NAVY[0]) * alpha * 0.22),
            int(NAVY[1] + (BLUE[1] - NAVY[1]) * alpha * 0.22),
            int(NAVY[2] + (BLUE[2] - NAVY[2]) * alpha * 0.22),
        )
        gdraw.ellipse(
            [W - 180 - radius, H - 60 - radius,
             W - 180 + radius, H - 60 + radius],
            fill=colour)
    img = Image.blend(img, glow, 0.85)
    draw = ImageDraw.Draw(img)

    # Orange rule down the left edge — the CTA/emphasis colour, used as
    # emphasis only per §4.1.
    draw.rectangle([0, 0, 14, H], fill=ORANGE)

    left = 86
    f_brand = _font(BOLD_NAMES, 40)
    f_head = _font(BOLD_NAMES, 68)
    f_sub = _font(REG_NAMES, 31)
    f_badge = _font(BOLD_NAMES, 22)

    draw.text((left, 88), 'ASPIRED WEBSITES', font=f_brand, fill=WHITE)
    draw.line([(left, 148), (left + 128, 148)], fill=TEAL, width=5)

    for i, line in enumerate(['Custom Web Design', 'Built to Work as',
                              'Hard as You Do']):
        draw.text((left, 206 + i * 80), line, font=f_head, fill=WHITE)

    draw.text((left, 468),
              'Hand-coded, security-first websites for',
              font=f_sub, fill=MUTED)
    draw.text((left, 508), 'law firms & small businesses.',
              font=f_sub, fill=MUTED)

    # Credential chips — factual, straight from the §1 master record.
    x = left
    for label, colour in (('CISSP', TEAL),
                          ('M.S. CYBERSECURITY', TEAL),
                          ('ATLANTA, GA', ORANGE)):
        box = draw.textbbox((0, 0), label, font=f_badge)
        width = box[2] - box[0]
        draw.rounded_rectangle([x, 566, x + width + 34, 606],
                               radius=20, outline=colour, width=2)
        draw.text((x + 17, 576), label, font=f_badge, fill=colour)
        x += width + 34 + 14

    img.save(OUT, 'PNG', optimize=True)
    print(f'wrote {OUT} ({os.path.getsize(OUT):,} bytes, {W}x{H})')


if __name__ == '__main__':
    main()
