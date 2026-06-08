# masterdesign.md
> **Claude Code — read this entire file before doing anything else.**
> Your job: scan this existing project, extract everything already here, and redesign the website using what you find. You are not starting from scratch. You are not inventing colors, fonts, or content. You are rebuilding what exists into a better version of itself.

---

## STEP 1 — SCAN THE PROJECT FIRST

Do this before writing a single line of code.

### ⚠️ SCOPE RULE — Every file gets redesigned. No exceptions.

You are redesigning the **entire project** — not just the homepage.

Run this immediately to find every template/markup file in the project:
```bash
find . -name "*.html" | sort
find . -name "*.htm"  | sort
find . -name "*.php"  | sort
find . -name "*.njk"  | sort
find . -name "*.jinja" | sort
```

List every file found. Every single one gets the full redesign treatment — same color system, same layout rhythm, same animations, same nav, same footer. A file in a subfolder like `pages/contact.html` or `templates/services/index.html` gets treated exactly the same as the root `index.html`.

This includes:
- The homepage / root index
- Every page a nav link points to (whether that's a separate file or an anchor section on the same page)
- Every page in subfolders
- Any standalone pages like 404, thank-you, privacy policy, terms
- Any partial or include files that contain nav or footer — update those once and they apply everywhere

**Do not stop after the homepage. Do not finish until every file in the list has been rebuilt.**

---

### 1A. Find and read every CSS file
Look in: `static/`, `assets/`, `css/`, `public/`, or any `<style>` block in HTML files.
Extract:
- Every color value (hex, rgb, hsl) — note which ones appear most
- Every `--css-variable` defined in `:root`
- Font names from `@import`, `@font-face`, or `<link>` tags
- Any existing spacing, border-radius, or shadow values that define the brand feel

### 1B. Find and read every HTML/template file
Look in: `templates/`, `views/`, root-level `.html` files, or whatever template system is in use (Django, Jinja2, plain HTML, etc.)
Extract:
- Business name and tagline
- Navigation links and their order
- Every section that exists (hero, about, services, testimonials, FAQ, contact, footer, etc.)
- All existing copy — headlines, body text, CTAs, button labels
- Contact info: email, phone, address
- Any existing URLs, social links, or third-party embeds

### 1C. Inventory every image and asset
Look in: `static/`, `assets/`, `img/`, `images/`, `public/`
List every file. Note:
- Logo file(s) — name and path
- Hero images or background images
- Team or founder photos
- Service/feature illustrations
- Any decorative or accent images
- Favicon

### 1D. Detect the tech stack
Is this Django, plain HTML, WordPress, another framework? Note it — the redesign output must match the same stack. Do not convert a plain HTML site to Django. Do not convert Django templates to plain HTML.

### 1E. Report your findings before building
Output this summary and wait for confirmation:

```
SCAN COMPLETE
─────────────────────────────────────────
Tech stack:        [what you found]
Primary colors:    [list hex values found, most used first]
Fonts:             [list every font found]
Pages/sections:    [list every page and section found]
Images on disk:    [list every image file with its path]
Business name:     [found or not found]
Tagline:           [found or not found]
Nav links:         [list them in order]
Contact info:      [email / phone / address found]
Missing assets:    [anything referenced in code but not found on disk]
─────────────────────────────────────────
Ready to redesign. Confirm to proceed.
```

Do not proceed until the human confirms.

---

## STEP 2 — REDESIGN RULES

Once the human confirms the scan, rebuild the site using these rules. Every decision uses what you found in Step 1 — nothing is invented.

### Colors
- Use the client's existing colors exactly
- Map them to a role system:
  - **Primary background** — darkest or most dominant background color found
  - **Secondary background** — second most used background (for section alternation)
  - **Card/surface** — slightly lighter than background, used for cards and panels
  - **Brand accent** — the main action color (used for buttons, highlights, links)
  - **Light tint** — lightest version of the accent or a near-white tint
  - **Warm secondary** — any gold, amber, or warm accent found; if none exists, derive one by warming the accent slightly
  - **Muted text** — body text color at reduced opacity on dark, or gray on light
  - **Border** — accent color at 20–25% opacity for subtle dividers and card edges
- Define all of these as CSS custom properties in `:root` — never use raw hex values in the stylesheet or templates

### Fonts
- Use whatever fonts already exist in the project
- If only one font is found, use it for everything with weight variation
- If two or more fonts exist, assign: one for headings (most distinctive), one for body (most readable), optionally one for accent labels if a handwritten or display font is present
- Do not import new fonts unless none exist at all in the project

### Images
- Use only images already on disk — never reference a file that wasn't in the Step 1 inventory
- If no hero image exists, use a CSS radial gradient background derived from the brand colors — no canvas, no JS animation
- If no section images exist, use styled cards and text layouts — do not use placeholder image paths
- Logo goes in nav and footer — use the exact path found in Step 1

---

## STEP 3 — LAYOUT SYSTEM TO APPLY

This is the design pattern to apply to the client's content. It's the same structural rhythm across every site — what changes is the content and colors inside it.

### Page Structure (apply in this order, skip any section the client doesn't need)
```
1. Fixed navigation bar
2. Hero — full viewport height, headline + subtext + 1-2 CTAs
3. Problem / Pain section — what the client's customers struggle with
4. Solution / What you do — how this business solves it
5. Services or Features — cards (3-column grid on desktop)
6. Social proof — testimonials in a scrolling or grid layout
7. FAQ — accordion
8. Final CTA band — strong closing statement + primary button
9. Footer — logo, links, contact
```

### Navigation
```css
nav {
  position: fixed;
  top: 0; left: 0; right: 0;
  z-index: 200;
  padding: 0 48px;
  transition: background 0.4s ease, box-shadow 0.4s ease;
}
nav.scrolled {
  background: [primary background color at 96% opacity];
  backdrop-filter: blur(12px);
  box-shadow: 0 1px 0 [border color];
}
.nav-inner {
  max-width: 1140px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 72px;
}
```
Nav scroll behavior JS:
```javascript
window.addEventListener('scroll', function() {
  document.getElementById('main-nav')
    .classList.toggle('scrolled', window.scrollY > 60);
}, { passive: true });
```

### Hero Section
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
    [lighter shade of primary background] 0%,
    [primary background] 45%,
    [darkest background] 100%);
  overflow: hidden;
}
/* Hero background is a CSS radial gradient only — no canvas, no JS background effects */
.hero-inner {
  position: relative;
  max-width: 860px;
}
```

Hero headline sizing:
```css
h1 {
  font-size: clamp(42px, 7vw, 80px);
  font-weight: 900;
  line-height: 1.1;
  letter-spacing: -1px;
}
h1 em { font-style: italic; color: [light tint color]; }
```

### Section System
```css
section { padding: 96px 48px; }
.section-inner { max-width: 1140px; margin: 0 auto; }

/* Alternate section backgrounds */
section:nth-of-type(odd)  { background: [primary background]; }
section:nth-of-type(even) { background: [secondary background]; }
```

Section heading:
```css
h2 {
  font-size: clamp(30px, 4vw, 48px);
  font-weight: 700;
  line-height: 1.2;
  letter-spacing: -0.5px;
  margin-bottom: 20px;
}
```

Small label above h2 (if a handwritten/display font exists, use it here):
```css
.section-label {
  font-size: 20px;
  font-weight: 600;
  color: [brand accent];
  margin-bottom: 12px;
  display: block;
}
```

### Cards (Services / Features)
```css
.card-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 24px;
}
.card {
  background: [card/surface color];
  border: 1px solid [border color];
  border-radius: 14px;
  padding: 32px 28px;
  transition: transform 0.3s ease, border-color 0.3s ease;
}
.card:hover {
  transform: translateY(-6px);
  border-color: [accent color at 45% opacity];
}
```

### Buttons
```css
.btn {
  display: inline-block;
  font-weight: 600;
  font-size: 14px;
  text-decoration: none;
  border-radius: 6px;
  padding: 11px 24px;
  transition: background 0.22s, transform 0.22s, box-shadow 0.22s;
  white-space: nowrap;
}
.btn-lg { padding: 15px 36px; font-size: 16px; border-radius: 8px; }

/* Primary */
.btn-primary { background: [accent]; color: #fff; }
.btn-primary:hover {
  filter: brightness(0.9);
  transform: translateY(-2px);
  box-shadow: 0 8px 24px [accent at 40% opacity];
}

/* Ghost */
.btn-ghost {
  background: transparent;
  color: [light tint];
  border: 1.5px solid [light tint at 35% opacity];
}
.btn-ghost:hover {
  background: [light tint at 8% opacity];
  border-color: [light tint];
  transform: translateY(-2px);
}
```

### Testimonials
```css
.testimonials-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 20px;
}
.testimonial-card {
  background: [card color];
  border: 1px solid [border color];
  border-radius: 14px;
  padding: 28px;
}
.stars { color: [warm secondary / gold]; margin-bottom: 12px; font-size: 15px; }
.quote { color: [light tint]; font-size: 15px; line-height: 1.75; font-style: italic; }
.author-name { font-weight: 600; color: [white]; font-size: 14px; }
.author-detail { color: [muted]; font-size: 13px; }
```

### FAQ Accordion
```html
<div class="faq-item">
  <button class="faq-q">Question text? <span class="faq-icon">+</span></button>
  <div class="faq-a">Answer text.</div>
</div>
```
```css
.faq-q {
  width: 100%; background: none; border: none;
  color: [white]; font-size: 17px; font-weight: 600;
  display: flex; justify-content: space-between; align-items: center;
  padding: 20px 0; cursor: pointer; text-align: left;
  border-bottom: 1px solid [border color];
}
.faq-a { display: none; padding: 16px 0 20px; color: [muted]; font-size: 15px; line-height: 1.75; }
.faq-item.open .faq-a { display: block; }
.faq-item.open .faq-icon { transform: rotate(45deg); }
.faq-icon { transition: transform 0.25s; }
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

### CTA Band (final section before footer)
```css
.cta-band {
  position: relative;
  padding: 120px 48px;
  text-align: center;
  background: radial-gradient(ellipse at 50% 60%,
    [lighter primary] 0%,
    [primary background] 60%,
    [darkest] 100%);
  overflow: hidden;
}
/* CTA band background is CSS gradient only */
.cta-inner { position: relative; max-width: 720px; margin: 0 auto; }
```

### Hero & CTA Band Background
Use a CSS radial gradient only. No canvas, no JS animation. The gradient is derived from the brand colors extracted in Step 1.

### Scroll Reveal Animation
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

### Mobile Responsive (always include)
```css
@media (max-width: 900px) {
  .card-grid          { grid-template-columns: 1fr; }
  .testimonials-grid  { grid-template-columns: 1fr; }
  .two-col            { grid-template-columns: 1fr; gap: 40px; }
}
@media (max-width: 600px) {
  section { padding: 64px 24px; }
  nav     { padding: 0 20px; }
  h1      { font-size: clamp(32px, 10vw, 52px); }
  .hero-btns { flex-direction: column; align-items: center; }
}
/* Mobile nav */
.nav-links { display: flex; gap: 32px; }
@media (max-width: 768px) {
  .nav-links {
    display: none;
    position: absolute;
    top: 72px; left: 0; right: 0;
    background: [primary background at 98% opacity];
    flex-direction: column;
    padding: 24px 32px;
    gap: 20px;
    border-bottom: 1px solid [border color];
  }
  .nav-links.open { display: flex; }
  .nav-toggle { display: block; }
}
```
Mobile nav toggle JS:
```javascript
document.getElementById('nav-toggle').addEventListener('click', function() {
  document.getElementById('nav-links').classList.toggle('open');
});
```

### Global Resets
```css
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { scroll-behavior: smooth; }
body {
  font-family: [body font from scan], sans-serif;
  background: var(--bg-primary);
  color: var(--white);
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
  overflow-x: hidden;
}
img { display: block; max-width: 100%; }
a { text-decoration: none; }
```

---

## STEP 4 — TECH STACK OUTPUT RULES

Build the output to match whatever stack was found in Step 1D.

### If plain HTML:
- Single file or multi-file HTML with `<link>` to external CSS and `<script>` at bottom of body
- Images referenced as relative paths from what was found on disk
- No build tools, no frameworks

### If Django templates:
- Use `{% load static %}` at top of every template
- All image/CSS/JS paths use `{% static 'path/to/file' %}`
- Internal links use `{% url 'app:view' %}`
- Loops use `{% for item in items %}...{% empty %}...{% endfor %}`
- Conditionals use `{% if %}...{% elif %}...{% else %}...{% endif %}`
- Keep existing app/template structure — do not reorganize the project

### If WordPress (PHP):
- Use `get_template_part()` for partials
- Images use `get_template_directory_uri()` for theme assets
- Links use `home_url()` or `get_permalink()`
- Keep existing theme structure

### Any other stack:
- Match it. Do not convert. Ask if unclear.

---

## STEP 5 — WHAT YOU ARE NOT ALLOWED TO DO

- Do not invent colors that weren't in the existing project
- Do not import fonts that weren't already in the project
- Do not reference image paths that weren't in the Step 1 inventory
- Do not restructure the project's folder layout or tech stack
- Do not remove existing content — reformat it, don't delete it
- Do not add placeholder text like "Lorem ipsum" or "[Insert content here]"
- Do not skip mobile responsiveness
- Do not skip the scroll reveal animations
- Do not add canvas animations or sparkle/starfield effects — hero and CTA band backgrounds are CSS gradients only
- Do not stop after the homepage — every HTML/template file found in Step 1 must be fully redesigned
- Do not skip a file because it is in a subfolder, is a secondary page, or seems minor (404, thank-you, privacy, terms — all of them)
- Do not apply the new design to some pages and leave others looking like the old site
- Do not use a different nav or footer design on different pages — nav and footer must be identical across every single file
- Do not finish and report complete until every file from the Step 1 file list has been rebuilt and confirmed


---

## STEP 6 — CREATE PROJECT MEMORY FILE

After the scan in Step 1 is complete and confirmed, before writing any redesign code, create this file at the project root:

```
.claude/project-memory.md
```

Create the `.claude/` folder if it doesn't exist. This file is Claude Code's persistent memory for this project. Every future session should read this file first before doing anything else — it eliminates the need to re-scan the entire project from scratch.

Populate it with exactly this structure, filled in with what you found in Step 1:

---

```markdown
# Project Memory
# Auto-generated by Claude Code — do not edit manually unless you know what you're changing.
# Claude Code: READ THIS FIRST before scanning or building anything in this project.

## Last Updated
[date and time generated]

## Project Overview
- Business name:    [extracted]
- Tagline:          [extracted or none]
- Contact email:    [extracted or none]
- Phone:            [extracted or none]
- Address:          [extracted or none]
- Domain:           [extracted or none]
- Tech stack:       [plain HTML / Django / WordPress / other]

## Color System
/* These are the extracted brand colors mapped to roles — use these, not raw hex */
--bg-primary:    [hex];   /* darkest background */
--bg-secondary:  [hex];   /* second background, alternating sections */
--bg-card:       [hex];   /* card and surface color */
--accent:        [hex];   /* primary brand action color */
--accent-light:  [hex];   /* light tint of accent */
--accent-warm:   [hex];   /* gold/amber secondary accent */
--text-muted:    [rgba or hex]; /* subdued body text */
--border:        [rgba or hex]; /* subtle card/section borders */
--white:         #ffffff;

## Typography
- Heading font:   [name and weights]
- Body font:      [name and weights]
- Accent font:    [name and weights, or "none"]
- Google Fonts import URL: [full URL or "none — local fonts"]

## File Map

### All HTML/Template Files (every file that needs redesigning)
[list every file path found, one per line]

### CSS Files
[list every CSS file path]

### JavaScript Files
[list every JS file path]

### Images & Assets
#### Logo
- [exact path to logo file]
#### Hero Images
- [exact path]
#### Section / Feature Images
- [exact path per image]
#### Decorative / Accent Images
- [exact path per image]
#### Other Assets
- [any remaining image or media files]

### Partials / Includes / Base Templates
[list any shared nav, footer, base, or include files]

## Nav Structure
Links in order:
1. [label] — [href or anchor]
2. [label] — [href or anchor]
3. [label] — [href or anchor]
[continue for all nav items]
Primary CTA button: [label] — [href]

## Pages / Sections Inventory
[For each HTML file or major page section, one line summary:]
- index.html — Hero, About, Services, Testimonials, FAQ, CTA, Footer
- about.html — [sections found]
- contact.html — [sections found]
[etc.]

## Redesign Status
[Claude Code updates this section as it completes each file]
- [ ] index.html
- [ ] about.html
- [ ] contact.html
[one checkbox per file — check it off as each is completed]

## Notes
[Anything unusual found during scan — missing images, broken links, inconsistent colors, etc.]
```

---

### Memory File Rules

- **Every future Claude Code session in this project must read `.claude/project-memory.md` first** before scanning or building anything
- If the memory file exists and is less than 30 days old, use it as the source of truth — skip the full re-scan
- If the memory file is missing or outdated, run Step 1 again and regenerate it
- Update the **Redesign Status** checkboxes as each file is completed
- Update **Last Updated** any time the file is modified
- If new files are added to the project later, append them to the File Map and Redesign Status sections — do not regenerate the whole file
