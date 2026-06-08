# blankdesign.md
# New Client — Build From Scratch
> **Claude Code: Read this entire file before doing anything.**
> This is a brand new website with no existing codebase. There is nothing to scan. Your job is to collect what's in Part A, confirm it with the human, build the project from scratch, then create the memory file so every future session starts instantly.

---

## HOW THIS FILE WORKS

**Part A** — Client intake. The human fills this in before dropping the file into Claude Code. If anything marked REQUIRED is still `[TBD]`, Claude Code stops, lists every missing item, and waits. No code is written until all REQUIRED fields are confirmed.

**Part B** — Design system. The full layout, component, and animation system Claude Code applies to every page it builds.

**Part C** — Build instructions. Project structure, stack rules, build order, quality rules, and memory file creation.

---

# PART A — CLIENT INTAKE
> Human: fill this in before handing to Claude Code.

---

## A1. Business Basics [REQUIRED]

```
Business name:          {{business_name}}
Tagline / one-liner:    {{tagline}}
Primary service:        {{primary_service}}
Target customer:        {{target_customer}}
Contact email:          {{contact_email}}
Phone:                  {{contact_phone}}
Address:                {{contact_address}}
Domain / URL:           {{domain}}
```

---

## A2. Brand Colors [REQUIRED]

> No colors yet? Tell Claude Code the brand personality below and ask it to generate a full palette before proceeding. Do not leave this TBD and expect Claude Code to guess.

```
Primary background (darkest):    {{color_primary_bg}}
Secondary background:            {{color_secondary_bg}}
Card / surface color:            {{color_card}}
Brand accent — buttons, links:   {{color_accent}}
Light tint of accent:            {{color_accent_light}}
Warm secondary — gold or amber:  {{color_warm_secondary}}
Light surface — cream or warm:   {{color_light_surface}}
```

Brand personality / vibe:
```
{{brand_personality}}
```

---

## A3. Typography [REQUIRED]

> Leave TBD and Claude Code uses the default stack below.

```
Heading font:       {{font_heading}}
Body font:          {{font_body}}
Accent/label font:  {{font_accent}}
```

**If TBD — Claude Code asks the human before picking any font.** Do not default to any specific font family. Ask:

> "What fonts do you want to use? If you're not sure, I can suggest options based on your brand personality from A2 — just say the word."

If the human says to just pick something, use these safe neutral defaults:
| Role | Font | Why |
|---|---|---|
| Headings | Merriweather | Readable serif, works across industries |
| Body | Inter | Clean, neutral, highly readable at all sizes |
| Labels | None — use body font at lighter weight | Only add a third font if the client specifically wants it |

---

## A4. Assets [REQUIRED — be specific about what exists vs. what needs to be created]

### Logo
```
Have a logo?               {{has_logo}}
Logo path / upload:        {{logo_path}}
File format:               {{logo_format}}
Works on dark background?  {{logo_dark_compatible}}
```

### Photos / Images
```
Photos of owner or team?   {{photos_team}}
Photos of work/results?    {{photos_work}}
Image style:               {{image_style}}
```

### Decorative Assets
```
Accent / divider images?   {{accent_images}}
```

### Favicon
```
Have one?                  {{favicon_status}}
```

---

## A5. Pages & Navigation [REQUIRED]

List every page. Nav bar will reflect this list exactly.

```
Page name     | URL or Anchor          | What goes on this page
--------------|------------------------|-----------------------------------------------
{{pages_list}}
```

> Note: If this is a single-page site, URL column = anchor IDs (e.g. `#services`). If multi-page, URL column = file paths (e.g. `/about/`). Claude Code builds whichever structure is specified here.

Nav link order:
```
{{nav_link_order}}
```

Primary CTA button in nav:
```
Label: {{cta_button_label}}
Links to: {{cta_button_link}}
```

---

## A6. Services / Packages [REQUIRED — at least 1]

```
Service 1
  Name:           {{service_1_name}}
  Price:          {{service_1_price}}
  Description:    {{service_1_description}}
  Included:
{{service_1_included}}
  CTA label:      {{service_1_cta_label}}
  CTA link:       {{service_1_cta_link}}

Service 2
  Name:           {{service_2_name}}
  Price:          {{service_2_price}}
  Description:    {{service_2_description}}
  Included:
{{service_2_included}}
  CTA label:      {{service_2_cta_label}}
  CTA link:       {{service_2_cta_link}}

Service 3 (optional)
  Name:           {{service_3_name}}
  Price:          {{service_3_price}}
  Description:    {{service_3_description}}
  Included:
{{service_3_included}}
  CTA label:      {{service_3_cta_label}}
  CTA link:       {{service_3_cta_link}}
```

---

## A7. Testimonials [OPTIONAL — include if you have them]

```
Include testimonials? {{testimonials_include}}

1. Quote:   "{{testimonial_1_quote}}"
   Name:    {{testimonial_1_name}}
   Source:  {{testimonial_1_source}}

2. Quote:   "{{testimonial_2_quote}}"
   Name:    {{testimonial_2_name}}
   Source:  {{testimonial_2_source}}

3. Quote:   "{{testimonial_3_quote}}"
   Name:    {{testimonial_3_name}}
   Source:  {{testimonial_3_source}}
```

---

## A8. Tone & Voice [REQUIRED]

```
Brand personality:
{{tone_personality}}

Messaging angle:
{{messaging_angle}}

What problem do you solve?       {{problem_solved}}
What outcome does the client get? {{outcome}}
One-sentence value statement:     {{value_statement}}
```

---

## A9. Tech Stack [REQUIRED]

```
{{tech_stack}}
```

---

## A10. Integrations [OPTIONAL]

```
Contact form — email to:     {{integration_contact_email}}
Booking / calendar:          {{integration_booking}}
Payment processing:          {{integration_payment}}
Newsletter:                  {{integration_newsletter}}
Analytics:                   {{integration_analytics}}
Blog:                        {{integration_blog}}
Client portal / login:       {{integration_portal}}
Live chat:                   {{integration_chat}}
```

---

## A11. Reference Sites [OPTIONAL]

```
Sites you like and why:
{{reference_sites}}

What you do NOT want:
{{avoid_design}}
```

---

# PART B — DESIGN SYSTEM
> Claude Code: this is the visual system you apply to every page you build. All color values come from Part A. All content comes from Part A. Nothing is invented.

---

## B1. CSS Variables — Define Once, Use Everywhere

Create these in `:root` in the main CSS file using the colors from A2. Never use raw hex anywhere else.

```css
:root {
  --bg-primary:   {{color_primary_bg}};
  --bg-secondary: {{color_secondary_bg}};
  --bg-card:      {{color_card}};
  --accent:       {{color_accent}};
  --accent-light: {{color_accent_light}};
  --accent-warm:  {{color_warm_secondary}};
  --surface:      {{color_light_surface}};
  --text-muted:   rgba([accent-light rgb], 0.65);
  --border:       rgba([accent rgb], 0.25);
  --white:        #ffffff;
}
```

---

## B2. Typography Setup

```html
<!-- In <head> of base template or every HTML file -->
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family={{font_heading}}:wght@700;900&family={{font_body}}:wght@400;500;600&display=swap" rel="stylesheet">
```

Replace with actual fonts from A3 if different. If A3 is TBD, use the defaults above.

```css
/* Type scale */
h1 {
  font-family: {{font_heading}}, serif;
  font-size: clamp(42px, 7vw, 80px);
  font-weight: 900;
  line-height: 1.1;
  letter-spacing: -1px;
  color: var(--white);
}
h1 em { font-style: italic; color: var(--accent-light); }

h2 {
  font-family: {{font_heading}}, serif;
  font-size: clamp(30px, 4vw, 48px);
  font-weight: 700;
  line-height: 1.2;
  letter-spacing: -0.5px;
  color: var(--white);
  margin-bottom: 20px;
}

.section-label {
  font-family: {{font_accent}}, cursive;
  font-size: 20px;
  font-weight: 600;
  color: var(--accent);
  display: block;
  margin-bottom: 12px;
}

body {
  font-family: {{font_body}}, sans-serif;
  font-size: 17px;
  line-height: 1.8;
  color: var(--text-muted);
}
```

---

## B3. Global Resets

```css
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html  { scroll-behavior: smooth; }
body  {
  background: var(--bg-primary);
  -webkit-font-smoothing: antialiased;
  overflow-x: hidden;
}
img   { display: block; max-width: 100%; }
a     { text-decoration: none; }
```

---

## B4. Page Structure — Apply to Every Page

Build every page in this section order. Skip any section the client has no content for — do not use placeholder content.

```
1.  Fixed navigation bar
2.  Hero — full viewport, headline + subheadline + 1-2 CTA buttons
3.  Problem — what the target customer struggles with
4.  Solution — how this business fixes it
5.  Services — card grid (content from A6)
6.  Testimonials — grid layout (content from A7, or skip if none)
7.  FAQ — accordion
8.  Final CTA band — closing statement + primary button
9.  Footer — logo, nav links, email, tagline
```

---

## B5. Navigation

```css
nav {
  position: fixed;
  top: 0; left: 0; right: 0;
  z-index: 200;
  padding: 0 48px;
  transition: background 0.4s ease, box-shadow 0.4s ease;
}
nav.scrolled {
  background: rgba([bg-primary rgb], 0.96);
  backdrop-filter: blur(12px);
  box-shadow: 0 1px 0 var(--border);
}
.nav-inner {
  max-width: 1140px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 72px;
}
.nav-logo-text {
  font-family: {{font_heading}}, serif;
  font-size: 22px;
  font-weight: 700;
  color: var(--white);
}
.nav-links {
  display: flex;
  align-items: center;
  gap: 36px;
  list-style: none;
}
.nav-links a {
  font-size: 14px;
  font-weight: 500;
  color: var(--text-muted);
  transition: color 0.2s;
}
.nav-links a:hover { color: var(--white); }
```

Nav scroll JS:
```javascript
window.addEventListener('scroll', function() {
  document.getElementById('main-nav')
    .classList.toggle('scrolled', window.scrollY > 60);
}, { passive: true });
```

Mobile nav toggle:
```javascript
document.getElementById('nav-toggle').addEventListener('click', function() {
  document.getElementById('nav-links').classList.toggle('open');
});
```

---

## B6. Hero Section

```css
.hero {
  position: relative;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 140px 40px 100px;
  background: radial-gradient(ellipse at 50% 40%,
    [lighter bg-primary] 0%,
    var(--bg-primary) 45%,
    [darkest shade] 100%);
  overflow: hidden;
}
/* Hero background = CSS radial gradient only — no canvas */
.hero-inner { position: relative; max-width: 860px; }

.hero-label {
  font-family: {{font_accent}}, cursive;
  font-size: 22px;
  color: var(--accent);
  display: inline-block;
  margin-bottom: 20px;
}
.hero-sub {
  font-size: 20px;
  color: var(--text-muted);
  max-width: 560px;
  margin: 0 auto 16px;
  line-height: 1.6;
}
.hero-btns {
  display: flex;
  gap: 16px;
  justify-content: center;
  flex-wrap: wrap;
  margin-top: 40px;
}
```

If a hero image/photo exists from A4, position it absolute bottom-right:
```css
.hero-image {
  position: absolute;
  bottom: 0; right: 64px;
  width: 260px;
  border-radius: 20px 20px 0 0;
  overflow: hidden;
  border: 2px solid rgba([accent-warm rgb], 0.45);
  border-bottom: none;
  box-shadow: 0 -8px 48px rgba(0,0,0,0.3);
}
```
If no hero image — use the CSS radial gradient only. Do not use a stock image or placeholder. Do not add a canvas or JS animation.

---

## B7. Section System

```css
section { padding: 96px 48px; }
.section-inner { max-width: 1140px; margin: 0 auto; }

/* Alternate backgrounds across sections */
.bg-primary   { background: var(--bg-primary); }
.bg-secondary { background: var(--bg-secondary); }

.body-text {
  font-size: 17px;
  color: var(--text-muted);
  line-height: 1.8;
  max-width: 560px;
  margin-bottom: 20px;
}

/* Two column layout — text left, image/card right */
.two-col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 72px;
  align-items: start;
}

/* Thin gradient divider between sections */
.divider {
  width: 100%; height: 1px;
  background: linear-gradient(to right, transparent, var(--border), transparent);
}
```

---

## B8. Buttons

```css
.btn {
  display: inline-block;
  font-weight: 600;
  font-size: 14px;
  border-radius: 6px;
  padding: 11px 24px;
  white-space: nowrap;
  transition: background 0.22s, transform 0.22s, box-shadow 0.22s;
  cursor: pointer;
  border: none;
}
.btn-lg { padding: 15px 36px; font-size: 16px; border-radius: 8px; }

.btn-primary { background: var(--accent); color: var(--white); }
.btn-primary:hover {
  filter: brightness(0.88);
  transform: translateY(-2px);
  box-shadow: 0 8px 24px rgba([accent rgb], 0.4);
}

.btn-ghost {
  background: transparent;
  color: var(--accent-light);
  border: 1.5px solid rgba([accent-light rgb], 0.35);
  padding: 14px 32px;
  font-size: 16px;
  border-radius: 8px;
}
.btn-ghost:hover {
  background: rgba([accent-light rgb], 0.08);
  border-color: var(--accent-light);
  transform: translateY(-2px);
}

.btn-outline {
  background: transparent;
  color: var(--accent);
  border: 1.5px solid var(--accent);
}
.btn-outline:hover {
  background: rgba([accent rgb], 0.12);
  transform: translateY(-2px);
}
```

---

## B9. Service / Feature Cards

```css
.card-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 24px;
}
.card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 32px 28px;
  transition: transform 0.3s ease, border-color 0.3s ease;
  display: flex;
  flex-direction: column;
}
.card:hover {
  transform: translateY(-6px);
  border-color: rgba([accent rgb], 0.45);
}
.card-icon {
  width: 46px; height: 46px;
  border-radius: 12px;
  background: rgba([accent rgb], 0.1);
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 18px;
}
.card h3 {
  font-family: {{font_heading}}, serif;
  font-size: 19px;
  font-weight: 700;
  color: var(--white);
  margin-bottom: 10px;
}
.card p {
  font-size: 15px;
  color: var(--text-muted);
  line-height: 1.75;
  flex: 1;
}

/* Pricing variant */
.card-featured {
  border-color: var(--accent);
  box-shadow: 0 0 40px rgba([accent rgb], 0.12);
}
.price-amount {
  font-family: {{font_heading}}, serif;
  font-size: 48px;
  font-weight: 700;
  color: var(--accent);
  line-height: 1;
}
.price-note { font-size: 14px; color: var(--text-muted); font-style: italic; }
.card-features { list-style: none; margin: 20px 0 28px; flex: 1; }
.card-features li {
  font-size: 15px;
  color: var(--accent-light);
  padding: 8px 0;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 10px;
}
```

---

## B10. Image Frame (when real images exist)

```css
.image-frame {
  background: linear-gradient(135deg, var(--surface) 0%, #fdf8f0 100%);
  border-radius: 20px;
  padding: 20px;
  border: 2px solid rgba([accent-warm rgb], 0.45);
  box-shadow: 0 0 50px rgba([accent rgb], 0.15);
  overflow: hidden;
}
.image-frame img { display: block; width: 100%; border-radius: 10px; }
```

Only use this component if a real image path exists from A4. If no image — use a styled stats/info block or a CSS-only decorative element instead.

---

## B11. Testimonials

Only build this section if A7 has real quotes. Do not fabricate testimonials.

```css
.testimonials-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 20px;
}
.testimonial-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 28px;
}
.stars       { color: var(--accent-warm); font-size: 15px; margin-bottom: 14px; }
.quote       { color: var(--accent-light); font-size: 15px; line-height: 1.75; font-style: italic; margin-bottom: 20px; }
.author-name { font-weight: 600; color: var(--white); font-size: 14px; display: block; }
.author-src  { color: var(--text-muted); font-size: 13px; }
```

---

## B12. FAQ Accordion

```html
<div class="faq-item">
  <button class="faq-q">Question? <span class="faq-icon">+</span></button>
  <div class="faq-a">Answer text.</div>
</div>
```

```css
.faq-q {
  width: 100%; background: none; border: none; cursor: pointer;
  color: var(--white); font-size: 17px; font-weight: 600;
  display: flex; justify-content: space-between; align-items: center;
  padding: 20px 0; text-align: left;
  border-bottom: 1px solid var(--border);
}
.faq-a {
  display: none; padding: 16px 0 20px;
  color: var(--text-muted); font-size: 15px; line-height: 1.75;
}
.faq-item.open .faq-a    { display: block; }
.faq-item.open .faq-icon { transform: rotate(45deg); }
.faq-icon { transition: transform 0.25s ease; display: inline-block; }
```

```javascript
document.querySelectorAll('.faq-q').forEach(function(btn) {
  btn.addEventListener('click', function() {
    var item = btn.closest('.faq-item');
    var wasOpen = item.classList.contains('open');
    document.querySelectorAll('.faq-item').forEach(el => el.classList.remove('open'));
    if (!wasOpen) item.classList.add('open');
  });
});
```

---

## B13. Final CTA Band

```css
.cta-band {
  position: relative;
  padding: 120px 48px;
  text-align: center;
  background: radial-gradient(ellipse at 50% 60%,
    [lighter bg-primary] 0%,
    var(--bg-primary) 60%,
    [darkest] 100%);
  overflow: hidden;
}
/* CTA band background = CSS gradient only */
.cta-inner { position: relative; max-width: 720px; margin: 0 auto; }
.cta-label { font-family: {{font_accent}}, cursive; font-size: 20px; color: var(--accent); margin-bottom: 16px; }
.cta-btns  { display: flex; gap: 16px; justify-content: center; flex-wrap: wrap; margin-top: 36px; }
```

---

## B14. Footer

```css
.footer {
  background: var(--bg-primary);
  border-top: 1px solid var(--border);
  padding: 48px;
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 20px;
}
.footer-links { display: flex; gap: 28px; list-style: none; }
.footer-links a { font-size: 14px; color: var(--text-muted); }
.footer-links a:hover { color: var(--white); }
.footer-tagline { font-size: 14px; color: var(--text-muted); font-style: italic; }
.footer-email a { font-size: 14px; color: var(--accent); }
```

---

## B15. Hero & CTA Band Background

Both the hero section and the CTA band use a **CSS radial gradient only** — no canvas element, no JavaScript animation, no sparkle or starfield effect. That style is specific to individual client brands that request it; it is not a default.

```css
/* Hero */
.hero {
  background: radial-gradient(ellipse at 50% 40%,
    [lighter bg-primary] 0%,
    var(--bg-primary) 45%,
    [darkest shade] 100%);
}
/* CTA Band */
.cta-band {
  background: radial-gradient(ellipse at 50% 60%,
    [lighter bg-primary] 0%,
    var(--bg-primary) 60%,
    [darkest] 100%);
}
```

If the client specifically requests a particle, sparkle, or animated background effect — ask for confirmation before adding it. Never add it by default.

## B16. Scroll Reveal Animation — Apply to Every Page

```css
.rv {
  opacity: 0;
  transform: translateY(24px);
  transition: opacity 0.65s ease, transform 0.65s ease;
}
.rv.on { opacity: 1; transform: none; }
.d1 { transition-delay: 0.1s; }
.d2 { transition-delay: 0.2s; }
.d3 { transition-delay: 0.3s; }
.d4 { transition-delay: 0.4s; }
```

```javascript
if ('IntersectionObserver' in window) {
  var io = new IntersectionObserver(function(entries) {
    entries.forEach(function(e) {
      if (e.isIntersecting) { e.target.classList.add('on'); io.unobserve(e.target); }
    });
  }, { threshold: 0.1 });
  document.querySelectorAll('.rv').forEach(function(el) { io.observe(el); });
} else {
  document.querySelectorAll('.rv').forEach(function(el) { el.classList.add('on'); });
}
```

Apply `.rv` to every section heading, card, image, and CTA element. Use `.d1` through `.d4` for staggered entrance within a section.

---

## B17. Mobile Responsive — Required on Every Page

```css
@media (max-width: 900px) {
  .card-grid         { grid-template-columns: 1fr; }
  .testimonials-grid { grid-template-columns: 1fr; }
  .two-col           { grid-template-columns: 1fr; gap: 40px; }
  .nav-links         {
    display: none;
    position: absolute;
    top: 72px; left: 0; right: 0;
    background: rgba([bg-primary rgb], 0.98);
    flex-direction: column;
    padding: 24px 32px;
    gap: 20px;
    border-bottom: 1px solid var(--border);
  }
  .nav-links.open { display: flex; }
  .nav-toggle     { display: block; }
}
@media (max-width: 600px) {
  section        { padding: 64px 24px; }
  nav            { padding: 0 20px; }
  .hero-btns     { flex-direction: column; align-items: center; }
  .hero-image    { display: none; }
  .cta-btns      { flex-direction: column; align-items: center; }
}
```

---

# PART C — BUILD INSTRUCTIONS

---

## C1. Pre-Build Checklist

Claude Code runs this before writing any file. If any REQUIRED item is unchecked, stop and ask.

```
[ ] Business name confirmed (A1)
[ ] All 7 colors filled in (A2)
[ ] Font choices confirmed or defaulted (A3)
[ ] Logo status confirmed — exists or text-only acknowledged (A4)
[ ] All pages listed with URLs or anchors (A5)
[ ] Nav link order confirmed (A5)
[ ] At least 1 service with real content (A6)
[ ] Tech stack selected (A9)
[ ] Contact email confirmed (A1)
```

---

## C2. Project Structure by Stack

### Plain HTML
```
[client-slug]/
├── index.html
├── [page].html          ← one file per page from A5
├── css/
│   └── main.css
├── js/
│   └── main.js
└── assets/
    ├── logo.png
    ├── [images from A4]
    └── favicon.ico
```

### Django
```
[client-slug]/
├── manage.py
├── config/
│   ├── settings/
│   │   ├── base.py
│   │   ├── local.py
│   │   └── production.py
│   ├── urls.py
│   └── wsgi.py
├── apps/
│   ├── core/
│   │   ├── views.py
│   │   ├── urls.py
│   │   └── models.py
│   └── [feature_app]/
│       ├── views.py
│       ├── urls.py
│       ├── models.py
│       └── admin.py
├── templates/
│   ├── base.html
│   ├── partials/
│   │   ├── nav.html
│   │   └── footer.html
│   └── [app]/
│       └── [page].html
├── static/
│   ├── css/main.css
│   ├── js/main.js
│   └── assets/
└── requirements.txt
```

Django template rules:
- `{% load static %}` at top of every template
- All asset paths via `{% static 'path' %}`
- All internal links via `{% url 'app:name' %}`
- Every `{% for %}` includes `{% empty %}`
- Every `{% if %}` closed with `{% endif %}`

### WordPress
- Keep standard theme structure: `style.css`, `functions.php`, `index.php`, `page.php`, `header.php`, `footer.php`
- Assets via `get_template_directory_uri()`
- Links via `home_url()` and `get_permalink()`

---

## C3. Build Order

Follow this sequence exactly. Do not skip steps.

```
1.  Create project folder structure from C2
2.  Create main CSS file — define :root variables from A2 first
3.  Add Google Fonts import from A3 (or defaults)
4.  Build base template / shared HTML structure
5.  Build nav partial — use A5 link order, A1 business name, A4 logo path
6.  Build footer partial — use A1 contact info, A5 links
7.  Build every page listed in A5 — one at a time, in nav order
    For each page:
      a. Apply correct section structure from B4
      b. Fill with real content from Part A — no placeholders
      c. Apply .rv and .d1–.d4 to animatable elements
      d. Hero and CTA band use CSS gradient background — no canvas needed
      e. Confirm mobile breakpoints are in CSS
8.  Wire up all internal links — confirm every nav link resolves correctly
9.  Add FAQ accordion JS
10. Add scroll reveal JS
12. Add nav scroll JS + mobile toggle JS
13. Create .claude/project-memory.md (see C4)
14. Final check — open every page, confirm nav works, confirm no broken image paths
```

---

## C4. Create Project Memory File

After the project is built, create this at the project root:

```
.claude/project-memory.md
```

Populate it with:

```markdown
# Project Memory
# Claude Code: READ THIS FIRST at the start of every session in this project.
# Do not re-ask the human for information already recorded here.

## Last Updated
[date and time]

## Project Overview
- Business name:    {{business_name}}
- Tagline:          {{tagline}}
- Contact email:    {{contact_email}}
- Phone:            {{contact_phone}}
- Address:          {{contact_address}}
- Domain:           {{domain}}
- Tech stack:       {{tech_stack}}

## Color System
--bg-primary:   {{color_primary_bg}};
--bg-secondary: {{color_secondary_bg}};
--bg-card:      {{color_card}};
--accent:       {{color_accent}};
--accent-light: {{color_accent_light}};
--accent-warm:  {{color_warm_secondary}};
--surface:      {{color_light_surface}};
--text-muted:   [rgba];
--border:       [rgba];

## Typography
- Heading font:  {{font_heading}}
- Body font:     {{font_body}}
- Accent font:   {{font_accent}}
- Google Fonts:  [full import URL]

## File Map

### Pages
[list every page file with its path]

### CSS
[list every CSS file]

### JavaScript
[list every JS file]

### Assets
- Logo:    {{logo_path}}
- Favicon: [exact path]
- Images:  [list every image path]

### Partials / Base Templates
[list shared templates]

## Nav Structure
[list every nav link in order: label — href]
CTA button: {{cta_button_label}} — {{cta_button_link}}

## Page Inventory
[one line per page: filename — sections it contains]

## Build Status
[one checkbox per page — check when complete and confirmed]
- [ ] index.html / home
- [ ] [page 2]
- [ ] [page 3]
[etc.]

## Notes
[anything unusual, decisions made, things to revisit]
```

### Memory File Rules
- Every future Claude Code session reads `.claude/project-memory.md` first
- If it exists and is current — use it, skip re-asking the human
- Update **Build Status** checkboxes as work is completed
- Update **Last Updated** on every write
- If new pages, images, or colors are added — update the relevant sections, do not regenerate the whole file

---

## C5. Quality Rules — Non-Negotiable

- No placeholder text anywhere — if content is missing, ask the human, do not invent it
- No stock image paths or `placeholder.com` URLs — if no image exists, use CSS/gradient only
- No raw hex values outside of `:root` definition
- No inline styles except dynamically generated ones
- Mobile responsive on every single page — not just the homepage
- Scroll reveal on every page — not just the homepage
- Hero and CTA band backgrounds are CSS gradients — do not add canvas, sparkle, or starfield effects unless the client explicitly requests it
- Nav and footer identical across every page
- Every internal link tested and resolving before reporting complete
- Do not report the project as finished until every page in the Build Status checklist is checked off
