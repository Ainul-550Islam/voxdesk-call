# Dashboard metric contract

Every number the dashboard shows, what it counts, and where it comes from.

The rule this document exists to enforce (requirement 12): **a percentage whose
denominator nobody can name is a number nobody can defend.** The pre-STEP-8
dashboard broke that rule with `booking_rate = booked / all calls`, which put
wrong numbers, missed calls and 3-second misdials in the denominator and made a
well-run agent look bad. That defect, and the made-up revenue tile, are recorded
in `docs/DASHBOARD-AUDIT.md` (F1, F10); this file is the definitional half of
the fix, and `app/api/analytics_routes.py` is the code half. The two are meant
to be read together — if a rate's definition changes in code, it changes here
in the same commit.

---

## 1. Where the numbers come from

| Page | Endpoint | Handler |
| --- | --- | --- |
| Overview | `GET /api/analytics/overview?range=` | `app/api/analytics_routes.py::overview` |
| Calls | `GET /api/analytics/calls?range=` | `call_analytics` |
| Analytics | `GET /api/analytics/conversion?range=` | `conversion` |
| Billing | `GET /api/analytics/usage` | `usage_analytics` |

Every figure is a single `SELECT count(...)` / `SELECT sum(...)` scoped by
`tenant_id` and a date window — conditional aggregation in one scan, not eight
round trips and never "fetch the rows and total them in the browser"
(requirement 13). A tenant with 400,000 calls costs the same round trip as a
tenant with four.

Two window rules are load-bearing:

* **The tenant's timezone decides the day boundaries**, not the browser's.
  `Tenant.timezone` is resolved server-side and echoed in the `window` object
  (`start`, `end`, `timezone`), so every client renders the same period.
* **A hard ceiling of 400 days** (`MAX_RANGE_DAYS`) applies to any range, so a
  single request cannot scan a decade.

## 2. Call counters

Source: `Call` rows whose `started_at` falls in the window.

| Field | Counts | Notes |
| --- | --- | --- |
| `total` | every call in the window | the denominator of the two "of every call" rates |
| `answered` | `status = COMPLETED` | |
| `missed` | `status = NO_ANSWER` | rang out |
| `failed` | `status = FAILED` | provider or pipeline failure |
| `booked` | `booked = true` | the tenant's own definition of a booking |
| `transferred` | `escalated = true` | a human took over |
| `inbound` / `outbound` | `direction` | |
| `eligible` | `status = COMPLETED` **and** `duration_seconds >= 10` | see §3 |
| `total_seconds` / `minutes` | `sum(duration_seconds)` | `minutes` is the same figure rounded to 0.1 |

`ELIGIBLE_CALL_SECONDS = 10` (`app/api/analytics_routes.py`). A call that lasted
three seconds because someone misdialled was never a booking opportunity.

## 3. The four rates (`analytics/calls` → `totals`, `analytics/conversion` → `rates`)

| Rate | Numerator | Denominator | Reads as |
| --- | --- | --- | --- |
| `answer_rate` | `answered` | `total` | of every call that arrived, how many did the agent complete? |
| `booking_rate` | `booked` | `eligible` | of the calls that could plausibly have booked, how many did? |
| `transfer_rate` | `transferred` | `answered` | of the answered calls, how many needed a human? |
| `failure_rate` | `failed` | `total` | of every call, how many failed outright? |

`booking_rate` is **not** `booked / total`. That is the F10 defect: it charges
the agent for wrong numbers and hang-ups. The Analytics page prints these
definitions next to the numbers, because an operator has to be able to explain
the rate to their own customer without opening the source.

The conversion endpoint adds three funnel rates:

| Rate | Numerator | Denominator | Notes |
| --- | --- | --- | --- |
| `call_to_lead_rate` | `leads` | `eligible` | capped at 100%: a lead may be created by an import rather than a call |
| `lead_to_appointment_rate` | booked appointments | `leads` | capped at 100% for the same reason |
| `appointment_kept_rate` | `kept` | booked appointments | kept = booked − no-shows |

## 4. The funnel (`analytics/conversion`)

Five stages, each counted independently over the same window:

| Stage | Counts |
| --- | --- |
| `calls` | every call in the window |
| `eligible` | answered and at least 10 s |
| `leads` | `Lead` rows created in the window |
| `appointments` | `pending` + `confirmed` + `rescheduled` + `no_show` |
| `kept` | booked appointments the customer attended (no-shows removed) |

Two deliberate decisions, both of which were bugs first and comments second:

* **Stages are counted, not followed.** A call in the window can produce an
  appointment outside it; tracing individual records would make this page
  disagree with the Calls page. Each stage is defensible on its own.
* **`booked` includes no-shows.** They *were* booked — dropping out is what the
  next stage measures, not this one. Only `cancelled` and `failed` appointments
  are excluded (a cancelled booking left the funnel earlier; a failed one never
  reached the provider). Subtracting no-shows from a set that had already
  removed them made a tenant with one kept appointment and one no-show report
  zero kept; a test now pins that case.

Because a funnel stage must never be wider than the one above it, the two
lead-derived rates are capped at 100%.

## 5. Operations and integrations tiles (`analytics/overview`)

| Field | Meaning |
| --- | --- |
| `operations.average_call_seconds` | `total_seconds / answered` — 0 when nothing was answered |
| `integrations.crm_sync_failures` | CRM sync failures in the window |
| `integrations.calendar_failures` | calendar provider failures in the window |

A provider error appears on the integrations page and in
`call.crashed` / `channel.agent_*` log events — never folded into the answer
rate, because a provider outage is not the tenant's agent performing badly.

## 6. Usage and overage (`analytics/usage`)

Requires `Permission.BILLING_READ`. The response carries the billing period
(`billing_period`, `period_start`, `period_end`), the plan code, per-metric
used/included quantities, `percent_used`, `estimated_overage_cents` and the
currency. Overage is computed from the plan's own limits
(`app/billing/metering.py`, `app/billing/plans.py`) — it is a price from
configured prices, not a constant.

When the caller may not read billing, `overview.usage` is **absent**, not
zeroed: a zero reads as "you have used nothing" rather than "you may not see
this".

## 7. What is deliberately not shown

* **`estimated_value_usd`.** The old Overview tile rendered
  `booked × $150` — a constant with no relationship to anything the tenant
  sells (audit F1, requirement 33). The field is still returned by the legacy
  `/stats` endpoint so no client breaks, and the dashboard no longer displays
  it: *a number a buyer will read as money has to come from money.*
* **Projections, forecasts and "potential" figures** of any kind. Every tile is
  a measurement of rows that exist.
* **Browser-localised timestamps.** Call times render in the tenant's timezone
  (`Tenant.timezone`), never in whatever locale the laptop happens to have
  (audit F11).

## 8. Checking a change

The definitions above are pinned by tests, in both halves of the stack:

| Test | Holds down |
| --- | --- |
| `tests/test_analytics.py::TestFormulas::test_booking_rate_excludes_calls_that_could_never_book` | the eligible denominator |
| `tests/test_analytics.py::TestFormulas::test_the_eligibility_threshold_is_documented_and_shared` | `ELIGIBLE_CALL_SECONDS` is one constant, not two copies |
| `tests/test_analytics.py::TestFormulas::test_a_rate_cannot_exceed_one_hundred` | the funnel cap |
| `tests/test_analytics.py::TestFormulas::test_no_estimated_revenue_anywhere` | §7, server side |
| `tests/test_analytics.py` (funnel + series classes) | kept-vs-no-show arithmetic, gap-filled series, local-day/local-hour bucketing |
| `dashboard/tests/no-fake-revenue.test.jsx` | §7, client side: the tile is gone and nothing invented a replacement |
| `dashboard/tests/formatting.test.jsx` | tenant-timezone rendering, not browser locale |

When a rate's denominator changes, exactly one of those tests is supposed to
fail — that is the point.
