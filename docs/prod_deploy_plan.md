# Production deploy plan — `6c37b42` → `b44bdbe`

Written 2026-08-19. Every number below was verified read-only against
production, not inferred. Nothing has been deployed.

---

## 1. Verified production state

| Check | Value |
|---|---|
| Deployed commit | `6c37b42` (2026-08-17) |
| Working tree | clean |
| Unapplied migrations | 0 |
| Disk | 7.0 G used of 77 G — 70 G free |
| supervisor | all 4 RUNNING (gunicorn, celery, celerybeat, daphne) |
| Open WebSocket sessions on :8001 | 0 |
| Settings module | `AspiredWebsitesRevamped.settings` |
| Accounts / Websites / legacy profiles | 10 / 12 / 10 |
| Accounts with **no** legacy mirror | **0** |
| Comped accounts | 3 |
| Vault credentials still server-key encrypted | 4 |
| Scheduled social posts | 0 |

That "0 accounts without a legacy mirror" row is the single most
important fact in this document. See §6.

---

## 2. What ships

29 commits. 146 files. 37 templates. **0 static files.** One new
dependency (`pyflakes`, test-only). Four migrations.

### The migrations

| Migration | Operations | SQL |
|---|---|---|
| `clients/0057` | 3 × AlterField (choices only) | **verified `-- (no-op)`** via `sqlmigrate` |
| `clients/0058` | 6 × AddIndex | 6 × `CREATE INDEX` |
| `domains/0006` | 1 × AddIndex | 1 × `CREATE INDEX` |
| `reporting/0018` | 5 × AddIndex | 5 × `CREATE INDEX` |

A plain `CREATE INDEX` on PostgreSQL takes a lock that blocks **writes**
to that table while it builds. So the only question that matters is table
size. Measured on prod:

| Table | Rows | Size |
|---|---:|---|
| `clients_uptimerecord` | 84,586 | 22 MB |
| `clients_clienthealthscore` | 729 | 336 kB |
| `reporting_sessionrecording` | 710 | 63 MB |
| `reporting_pagesession` | 710 | 1.2 MB |
| `reporting_conversionevent` | 189 | 152 kB |
| `clients_intelligencesuggestion` | 101 | 432 kB |
| everything else | ≤ 27 | ≤ 264 kB |

The largest is 84 k narrow rows. `sessionrecording` is 63 MB but only 710
rows — the bulk is blob columns the index does not touch, and the index
covers three small ones. **Expected total migrate time: low single-digit
seconds. There is no meaningful write-lock window.** No `CONCURRENTLY`
rewrite is warranted at this scale.

---

## 3. Pre-flight — do not skip

1. **Take a backup and prove it restores.** This is the one genuine gap
   and the only step I would call blocking.

   ```bash
   ssh root@161.35.108.209
   sudo -u postgres pg_dump -Fc aspired > /root/pre-deploy-$(date +%F-%H%M).dump
   ls -lh /root/pre-deploy-*.dump
   ```

   A dump that exists is not a backup. Prove it:

   ```bash
   sudo -u postgres createdb restore_check
   sudo -u postgres pg_restore -d restore_check /root/pre-deploy-*.dump
   sudo -u postgres psql -d restore_check -c "SELECT count(*) FROM clients_account;"   # expect 10
   sudo -u postgres dropdb restore_check
   ```

   If that count is not 10, **stop** — do not deploy.

2. Confirm nobody is mid-session in an SSH terminal. Verified 0 open now;
   re-check at deploy time, because the daphne restart in §4 kills them.

3. Deploy at a quiet hour. Nothing here is long, but the gunicorn restart
   drops in-flight requests.

---

## 4. Deploy sequence

Run **as `aspired`**, per CLAUDE.md — pulling as root re-introduces the
ownership breakage fixed on 2026-06-10.

```bash
cd /var/www/aspired/app

sudo -u aspired git pull origin main
sudo -u aspired git rev-parse --short HEAD          # expect b44bdbe

sudo -u aspired /var/www/aspired/venv/bin/pip install -r requirements.txt

# Read the plan before applying it.
sudo -u aspired /var/www/aspired/venv/bin/python manage.py showmigrations --plan | grep '^\[ \]'
sudo -u aspired /var/www/aspired/venv/bin/python manage.py migrate

sudo -u aspired /var/www/aspired/venv/bin/python manage.py collectstatic --noinput

supervisorctl restart aspiredwebsites aspiredwebsites-celery aspiredwebsites-celerybeat
supervisorctl restart aspiredwebsites-daphne     # vault/consumers.py changed
supervisorctl status
```

Three notes on that block:

- **`collectstatic` runs even though 0 static files changed.** It is
  cheap, and skipping it is how the cache-buster bug shipped before.
- **`daphne` must restart** — `vault/consumers.py` is in the diff. It is
  normally skipped because it drops open SSH-terminal sessions; there are
  0 open, so it is free right now.
- **`celerybeat` stays RUNNING on prod.** The "stop celerybeat after
  deploy" habit is *staging-only*. Do not apply it here.

`git pull` will also bring `settings_production.py`, `settings_test.py`,
`settings_development.py` and `settings_rehearsal.py`, which are newly
tracked. Prod runs `AspiredWebsitesRevamped.settings`, so these arrive
**inert**. No `.env` change is required — no new environment variable is
read by any of the 29 commits (verified against the `settings.py` diff).

---

## 5. Smoke tests

In order, immediately after restart:

```bash
# 1. Nothing is on fire.
curl -sSI https://aspiredwebsites.com/ | head -1
sudo -u aspired /var/www/aspired/venv/bin/python manage.py check --deploy 2>&1 | tail -5
tail -40 /var/www/aspired/logs/gunicorn-error.log

# 2. Parity is still clean.
sudo -u aspired /var/www/aspired/venv/bin/python manage.py audit_account_website_parity
```

Then in a browser, logged in as admin:

| Page | Why |
|---|---|
| `/admin-dashboard/droplets/metrics/` | **the actual fix** — the "Open the vault" link on the banner. 4 credentials are still server-key encrypted, so the banner renders. Click the link; it must reach the vault, not a 404. |
| `/admin-dashboard/clients/` | the biggest template diff |
| `/admin-dashboard/needs-you/` | had a `FieldError` earlier in the cutover |
| One client portal, as that client | the portal templates changed |
| A **comped** client's portal → subscriptions | 3 comped accounts exist; comp handling changed |

---

## 6. Corrections to what I told you earlier

I checked my own claims against prod before writing this, and **three of
them were wrong.** Stating them plainly because they change the risk
picture in your favour:

1. **It is 29 commits behind, not 15.** I was quoting a stale number.

2. **The three "dead" beat jobs are not dead on prod.** All 10 accounts
   have a legacy mirror, so the legacy walk still resolves — there are 729
   health scores, 63 written in the last 7 days. They are dead only for
   *mirror-less* accounts, which is the post-cutover shape my fixtures
   modelled, not today's production data. **Consequence: no burst of
   catch-up email on deploy night.** `churn_risk` rows in the last 7 days:
   0. (And the churn alert mails `LEAD_NOTIFICATION_EMAIL` — you — never
   clients.)

3. **The comped-client portal 500 is not on prod.** Prod's
   `portal_subscriptions` (`clients/views.py:2410`) imports
   `ClientProfile` locally and never references `Account`. That
   `NameError` lives on `main`'s newer code and was fixed before shipping.

4. **The social publish crash is real but latent** — prod has 0 scheduled
   posts, so nothing is being dropped today. It would bite the first time
   a post fails.

5. **The vault 404 is confirmed live.** 4 credentials are still
   server-key encrypted, so that banner is rendering right now with a link
   that 404s. This is the one user-visible bug the deploy actually fixes
   today.

So: the deploy is **lower-risk and lower-urgency than I implied.** It is
correctness and cutover-readiness work, not a fire.

---

## 7. Rollback

**Revert code only. Leave the migrations applied.**

`0057` is a verified no-op, and the 12 indexes are purely additive —
nothing the old code does is affected by an index existing. Old code runs
correctly against the new schema, so there is no reason to take on the
risk of reversing DDL.

```bash
cd /var/www/aspired/app
sudo -u aspired git reset --hard 6c37b42
sudo -u aspired /var/www/aspired/venv/bin/pip install -r requirements.txt
sudo -u aspired /var/www/aspired/venv/bin/python manage.py collectstatic --noinput
supervisorctl restart aspiredwebsites aspiredwebsites-celery aspiredwebsites-celerybeat aspiredwebsites-daphne
```

Django will list 4 applied migrations with no file on disk. That is
harmless — it only matters if you then run `makemigrations`, which you
would not during a rollback.

Reversing the migrations too (only if you truly need the old schema):
order matters, because `domains/0006` and `reporting/0018` both depend on
`clients/0058`.

```bash
manage.py migrate domains 0005
manage.py migrate reporting 0017
manage.py migrate clients 0056
```

Restore from the §1 dump only if data is wrong — not for a code problem.

---

## 8. What this deploy does NOT do

- **It does not drop anything.** All 51 legacy FK columns, `ClientProfile`
  and `Project` all survive. The 9 `.planned` migrations stay unpromoted.
- **It does not require the PG→PG rehearsal.** That gates the *teardown*,
  not this deploy.
- It does not change pricing, Stripe, or any external integration.
- It does not touch `.env`.

---

## 9. Post-deploy watch

- **Tonight:** `grep -i error /var/www/aspired/logs/celery-worker.log` after
  the nightly beat jobs run.
- **Sept 1, 09:00:** `check_annual_report_schedule` fires for the first
  time on fixed code. It queues reports for sites whose launch anniversary
  falls in September. Worth watching that run specifically.
- Unrelated, whenever convenient: `seed_pricing` reports blank Stripe
  price IDs for the social, hosting and domain tiers. Not a blocker and
  not part of this deploy, but those tiers cannot be sold through checkout
  until the IDs are filled in.
