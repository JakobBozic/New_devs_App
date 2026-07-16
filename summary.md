# Summary — Property Revenue Dashboard bug fixes

This summarizes the single commit on `main` (`fix: resolve Client A revenue
accuracy and Client B tenant isolation`) against the three symptoms reported in
`ASSIGNMENT.md`. Full investigation write-ups live in `client_A.md` (accuracy)
and `client_B.md` (tenant isolation).

## 1. "Revenue numbers don't match our records" / "different totals for March" (Client A)

**Root cause 1 — dashboard never read the real database.**
`services/reservations.py` routed revenue through `core/database_pool.py`, which
built its connection string from `settings.supabase_db_user/_password/_host/_port/_name`
— fields that don't exist on `Settings`. Every call threw, was swallowed, and
`calculate_total_revenue` silently returned hard-coded mock totals keyed by
`property_id` (e.g. `prop-001 → $1,000.00 / 3`), regardless of what was actually
in Postgres (real total: `$2,250.00 / 4`).
**Fix:** `reservations.py` was rewritten to query `settings.database_url` (the
real seeded Postgres) directly via a plain SQLAlchemy engine, and the mock
fallback was removed entirely — a DB failure now raises instead of fabricating
a number.

**Root cause 2 — monthly bucketing ignored property timezone.**
`calculate_monthly_revenue` compared naive `datetime(year, month, 1)` boundaries
against the `TIMESTAMP WITH TIME ZONE` `check_in_date` column with no timezone
conversion. A Sunset booking at `2024-02-29 23:30 UTC` is `2024-03-01 00:30` in
`Europe/Paris` (all Sunset properties' timezone) — the client's records count it
as March, the dashboard counted it as February.
**Fix:** `_month_window()` now looks up the property's `timezone` column and
builds month boundaries as timezone-aware `datetime`s in that zone before
querying, so boundary-straddling bookings land in the month the client expects.

## 2. "Off by a few cents" (finance team)

**Root cause.** `total_amount` is `NUMERIC(10,3)` (sub-cent precision), and the
API layer summed/rounded through `float`, which loses precision on aggregation
(classic `0.1 + 0.2` drift) and rounded row-by-row instead of once.
**Fix:** all money math uses `Decimal` end-to-end; the SQL aggregate is summed
in Postgres, then quantized to cents exactly once with `ROUND_HALF_UP`
(`reservations.py`). `dashboard.py` parses the already-cents-exact string back
through `Decimal(...)` rather than `float(str(...))` before it hits the JSON
response, so no precision is lost at the API boundary either.

## 3. "Sometimes we see another company's revenue" (Client B, privacy)

**Root cause — tenant-blind Redis cache key.**
`services/cache.py` cached revenue under `revenue:{property_id}` only.
`property_id` is unique **per tenant**, not globally (`properties`/`reservations`
use a composite `(id, tenant_id)` key), and the seed data has both Sunset and
Ocean owning a `prop-001`. Whichever tenant loaded it first won the shared cache
slot for the 5-minute TTL, and the other tenant read back the first tenant's
revenue on refresh — intermittent by construction, matching "sometimes."
**Fix:** cache key is now `revenue:{property_id}:tenant:{tenant_id}`, matching
the `{resource}:{id}:tenant:{tenant_id}` convention already used elsewhere in
the codebase. Every revenue query in `reservations.py` is also explicitly
scoped by both `property_id` and `tenant_id` (it already was in the SQL; only
the cache sat in front of it un-scoped).

**Second leak vector — hard-coded, tenant-agnostic property list.**
`frontend/src/components/Dashboard.tsx` rendered a static `PROPERTIES` array
(`prop-001` … `prop-005`) regardless of which client was logged in, so every
tenant's dropdown listed every other tenant's properties by name and ID.
**Fix:** added `GET /api/v1/dashboard/properties` (`dashboard.py` →
`reservations.list_properties(tenant_id)`), scoped by the authenticated user's
`tenant_id`, and `Dashboard.tsx` now fetches this list via a new
`SecureAPI.getDashboardProperties()` (`secureApi.ts`) instead of using the
hard-coded array.

**Third leak vector — auth tenant resolution defaulted to Client A.**
`core/auth.py`'s `authenticate_request` (and the WebSocket equivalent
`verify_token_ws`) resolved `tenant_id` via `TenantResolver.resolve_tenant_id`,
which special-cases three emails and then **defaults every other user to
`"tenant-a"`** — i.e. any authenticated user not on that short hard-coded list
would silently be treated as Sunset Properties for every tenant-scoped query.
**Fix:** both functions now resolve `tenant_id` from the authenticated user's
own JWT claims (`app_metadata`/`raw_app_metadata.tenant_id`, as actually issued
by `login.py`) first, falling back to the already-queried `user_tenants` DB
rows second. If neither source yields a tenant, the request is refused
(`401 Unauthorized` over HTTP, connection dropped over WebSocket) instead of
silently defaulting to `tenant-a`.

## Files changed

| File | Change |
|---|---|
| `backend/app/services/reservations.py` | Query real DB via `settings.database_url`; remove mock fallback; timezone-aware monthly bucketing; exact `Decimal` rounding; new `list_properties()` |
| `backend/app/services/cache.py` | Tenant-scoped the revenue cache key |
| `backend/app/api/v1/dashboard.py` | `Decimal`-safe response parsing; new `GET /dashboard/properties` endpoint |
| `backend/app/core/auth.py` | Tenant resolution now sourced from the user's own claims/DB records, not a hard-coded email map defaulting to `tenant-a`; refuses auth instead of guessing |
| `frontend/src/components/Dashboard.tsx` | Loads properties from the backend (tenant-scoped) instead of a hard-coded list |
| `frontend/src/lib/secureApi.ts` | Adds `getDashboardProperties()` client method |
| `client_A.md`, `client_B.md` | Detailed investigation notes for each client's report |

## Noted but out of scope

`backend/app/core/database_pool.py` is now unused (its only consumer was the
old, broken revenue path) and still references non-existent `settings.supabase_db_*`
fields — harmless while dead, but a landmine if reconnected. Left as a follow-up.
