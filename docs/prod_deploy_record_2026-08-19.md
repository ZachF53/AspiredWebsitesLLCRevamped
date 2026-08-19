# Production deploy record — `6c37b42` → `4a0071c`

Executed 2026-08-19. Companion to `prod_deploy_plan.md`, which was written
for `6c37b42` → `b44bdbe`. **The range moved before the deploy ran** — the
Prospect agent commit landed on top — so this records what actually
shipped, three corrections to the plan, and the rollback that fits.

---

## 1. What actually shipped

| | Plan said | Actually deployed |
|---|---|---|
| Range | `6c37b42` → `b44bdbe` | `6c37b42` → **`4a0071c`** |
| Commits | 29 | **30** |
| Migrations | 4 | **11** (4 + 7 from `4a0071c`) |
| New deps | `pyflakes` (test-only) | unchanged |
| Static files | 0 | 0 (`collectstatic`: 196 unmodified) |

The 7 extra migrations: `outreach 0010-0013`, `admin_dashboard 0005-0007`.

### Lock analysis for the added migrations

Same treatment the plan gave its original four. Measured on prod first:

| Table | Rows | Size | Operation |
|---|---:|---|---|
| `outreach_emailsent` | 423 | 648 kB | `ADD COLUMN` (nullable FK) + 1 index |
| `outreach_outreachsettings` | 1 | 56 kB | 2 × `ADD COLUMN`, 1 × no-op `AlterField` |
| `outreach_emailtemplatevariant` | 0 (new) | — | `CREATE TABLE` + 2 indexes |
| `admin_dashboard_aiemployee*` | 0 (new) | — | 4 × `CREATE TABLE` |

Verified via `sqlmigrate` before applying. The nullable `ADD COLUMN` takes
no rewrite, and the only index on a populated table covers 423 rows.
**Actual migrate time: 6 seconds** for all 11.

---

## 2. Three corrections to the plan

Found while executing. Each would have caused a real failure.

1. **The database is `aspired_prod`, not `aspired`.** The plan's §3 backup
   command (`pg_dump -Fc aspired`) fails with
   `FATAL: database "aspired" does not exist`. Since that is the *blocking*
   pre-flight step, following it literally means either stopping on a
   confusing error or — worse — believing a backup exists when it does not.

2. **A dump written to `/root` cannot be restored.** `/root` is mode 700,
   so `sudo -u postgres pg_restore` fails with `Permission denied`. The
   plan's own "prove it restores" step is what caught this, which is the
   argument for keeping that step. Backups now live in
   `/var/backups/aspired/` (owned `postgres:postgres`, mode 700 — still
   root/postgres only).

3. **`/admin-dashboard/droplets/metrics/` is not a URL.** The plan's smoke
   test names it; the real route is
   `droplets/<int:droplet_id>/metrics/`. A bare 404 there is correct
   behaviour, not a regression.

The plan's §1 production state was otherwise **exactly right** — commit,
clean tree, 0 unapplied, 70 G free, 4 processes running, 0 open WebSocket
sessions all confirmed unchanged at deploy time.

---

## 3. Pre-flight actually performed

```bash
install -d -o postgres -g postgres -m 700 /var/backups/aspired
sudo -u postgres pg_dump -Fc aspired_prod \
  -f /var/backups/aspired/pre-deploy-2026-08-19-1845.dump   # 33 MB

sudo -u postgres createdb restore_check
sudo -u postgres pg_restore -d restore_check \
  /var/backups/aspired/pre-deploy-2026-08-19-1845.dump
sudo -u postgres psql -d restore_check -c "SELECT count(*) FROM clients_account;"
sudo -u postgres dropdb restore_check
```

Restore proved against live prod, not just against the plan's expected
number:

| | Restored | Live prod |
|---|---:|---:|
| `clients_account` | 10 | 10 |
| `outreach_emailsent` | 423 | 423 |
| `outreach_lead` | 246 | 246 |

Open WebSocket sessions re-checked immediately before the `daphne`
restart: **0**.

---

## 4. Post-deploy verification

| Check | Result |
|---|---|
| `https://aspiredwebsites.com/` | 200 |
| `manage.py check --deploy` | no issues |
| `audit_account_website_parity` | **0 findings** (plan noted 1 outstanding warning) |
| supervisor | all 4 RUNNING, celerybeat **RUNNING** (prod, not staging) |
| gunicorn error log since restart | empty |
| **Vault 404 fix** | all **4** server-key-encrypted credentials resolve to a real vault URL |
| Prospect registered | `active=False`, effort `medium` |
| Baseline variants | 4/4 active |
| Spend caps | LLM $10.00/day · Apify 3 runs / 100 results |
| Pricing guardrail | blocks invented figures and discounts, passes clean copy |
| Existing data | `EmailSent` 423 preserved |
| Frozen sequence rows | 0 |

`outreach_active` is **False** on prod, so the cold sender ships dormant.

---

## 5. What the deploy fixed that nobody predicted

The celery log showed this repeating **every 30 minutes**:

```
ValueError: Invalid address; only getfit@apexfitnesssa.com&quot could be
parsed from "getfit@apexfitnesssa.com&quot;,"
```

Two shipped bugs in one line, and **4 rows** were stuck in it:

- The **enricher** scraped an HTML-entity-mangled address (`&quot;` inside
  it). Now fixed by `html.unescape()` before extraction plus an
  `EMAIL_RE.fullmatch()` guard on the winning candidate.
- The **dispatcher** retried it forever, because every failure left the row
  on `approved`. Now `ValueError` classifies as permanent, so the row is
  rejected and the address suppressed on the next tick.

These 4 rows will clear themselves. Worth confirming
`EmailSent.objects.filter(status='approved').count()` drops from 4 to 0.

---

## 6. Rollback

**Revert code only. Leave all 11 migrations applied.** This holds for the
7 new ones as well, and it was checked rather than assumed:

| Column | Nullable | DB default | Safe for old code? |
|---|---|---|---|
| `outreach_emailsent.template_variant_id` | YES | — | yes — old inserts omit it |
| `outreach_outreachsettings.daily_ai_spend_cap_usd` | NO | none | yes, see below |
| `outreach_outreachsettings.apify_max_runs_per_day` | NO | none | yes, see below |
| `outreach_outreachsettings.apify_max_results_per_run` | NO | none | yes, see below |

Django adds a default, backfills, then **drops the default**. A NOT NULL
column with no default would break an old-code `INSERT` — except
`OutreachSettings` is a singleton that already exists (1 row) and
`load()` only ever `get_or_create(pk=1)`, which resolves to a GET. The
new `admin_dashboard_aiemployee*` and `outreach_emailtemplatevariant`
tables are invisible to old code entirely.

The one edge: if that singleton row were deleted while running old code,
`load()` would attempt an INSERT and fail. Do not delete it.

```bash
cd /var/www/aspired/app
sudo -u aspired git reset --hard 6c37b42
sudo -u aspired /var/www/aspired/venv/bin/pip install -r requirements.txt
sudo -u aspired /var/www/aspired/venv/bin/python manage.py collectstatic --noinput
supervisorctl restart aspiredwebsites aspiredwebsites-celery \
                      aspiredwebsites-celerybeat aspiredwebsites-daphne
```

Django will list 11 applied migrations with no file on disk. Harmless —
it only matters if you then run `makemigrations`, which you would not do
mid-rollback.

**Partial rollback** (keep the cutover work, drop only Prospect):
`git reset --hard b44bdbe` instead. Same reasoning applies.

**Restore from the dump only if data is wrong, never for a code problem:**

```bash
supervisorctl stop all
sudo -u postgres dropdb aspired_prod && sudo -u postgres createdb -O aspired aspired_prod
sudo -u postgres pg_restore -d aspired_prod \
  /var/backups/aspired/pre-deploy-2026-08-19-1845.dump
supervisorctl start all
```

---

## 7. Watch list

- **Next dispatcher tick:** the 4 stuck `approved` rows should reject and
  suppress. If they stay at 4, `_is_permanent_failure` is not catching the
  real exception type.
- **Tonight:** `grep -i error /var/www/aspired/logs/celery-worker.log`
  after the nightly beat jobs.
- **`MODEL_CONTENT` is now `claude-sonnet-5`** — live for blog generation,
  the chatbot, case studies, social copy and intelligence analysis. Watch
  the AI Usage widget for a cost-shape change; the new tokenizer produces
  ~30% more tokens for the same text.
- **2026-09-01:** re-check Sonnet 5 pricing (`$2/$10` is encoded as
  standard); and `check_annual_report_schedule` fires for the first time
  on fixed code.
- Backups in `/var/backups/aspired/` have no rotation. Prune manually or
  add one before it accumulates.
