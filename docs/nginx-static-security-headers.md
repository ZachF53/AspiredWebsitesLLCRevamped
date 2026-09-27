# Security headers on nginx-served files (/static/, /media/)

Applied to staging and prod on 2026-09-26. The nginx site config lives
on each server, not in this repo, so this file is the record.

## Why

The app sets its security headers in Django (`SecurityMiddleware` +
`core.middleware.SecurityHeadersMiddleware`). Files under `/static/` and
`/media/` never reach Django: nginx serves them directly. And any
`add_header` inside a `location` block (the `/static/` block has one for
`Cache-Control`) stops that location inheriting server-level
`add_header` directives. The result was CSS/JS/images with no `nosniff`,
no HSTS and no frame protection.

## What is on each server

`/etc/nginx/snippets/aspired-static-security.conf`, included as the
first line of both the `location /static/` and `location /media/`
blocks in `/etc/nginx/sites-enabled/<site>`:

```nginx
# prod only (staging deliberately sends no HSTS: not the production host)
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;
# both
add_header X-Content-Type-Options "nosniff" always;
add_header X-Frame-Options "DENY" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
```

```nginx
location /static/ {
    include snippets/aspired-static-security.conf;
    alias /var/www/aspired/static/;
    expires 30d;
    add_header Cache-Control "public, immutable";
}
```

No CSP on `/media/`: a `sandbox` CSP would stop PDFs rendering in
Chrome's built-in viewer.

## New server checklist

1. Create the snippet (drop the HSTS line on non-production hosts).
2. Add the `include` to both locations.
3. `nginx -t && systemctl reload nginx`
4. Verify: `curl -sI https://<host>/static/css/public.css` shows
   `X-Content-Type-Options: nosniff`.

The pre-change config was backed up to `/root/nginx-*.bak.<timestamp>`.
