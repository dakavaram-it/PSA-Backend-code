# Working notes — portal backend (local body elections)

Everything here is `main.py` (~800 lines). The endpoint list, the env vars and how to run
it are in `README.md` next to this file; this one is only the things that will bite you —
mostly places where the obvious change is wrong.

The frontend is a **separate repository** (`portal-frontend-code`, React + Vite) that is
built and deployed elsewhere. It reaches these paths through its own `/leapapi` prefix.
Changing a path, a column alias or a status code breaks a bundle you cannot see from here.

## Ground rules

- **Live production database.** No seed, no fixture, no local copy. `S11` writes a real
  `proposal_candidate` row for a real person. Read before you write.
- **Everything except `S14login` requires a session.** `PUBLIC_PATHS` is the whole
  allowlist (`/S14login`, `/docs`, `/redoc`, `/openapi.json`). The cadre endpoints serve
  personal data — names, mobile numbers, voter ids.
- **No authorization, only authentication.** Any valid account can read and write against
  any constituency.
- Only `GET` and `POST` exist. Nothing is deleted; `proposal_candidate.is_active` is the
  flag every read filters on.
- **`proposal_candidate.proposal_status_id` says which kind of row it is** — a lookup into
  `proposal_status` (1 Proposed, 2 Shortlisted, 3 Confirmed). `S11` takes it in the body and
  defaults it to `PROPOSED_STATUS_ID`, validating it against the table rather than a list
  here, so a new status is a row and not a deploy. It changes no count: the slot arithmetic
  in `S7`/`S10` is over active rows whatever their status, so a shortlisted cadre occupies a
  `max_proposals` slot exactly as a proposed one does. Rows written before the column have
  it NULL, which is why `S13` joins `proposal_status` with a LEFT OUTER JOIN — dropping them
  would desync the list from `S7`'s `proposed_cnt`.
- **`S18removeProposalCandidate` is the only write that undoes `S11`**, and it is still not a
  delete: `is_active` goes to `'N'`, which every read filters on, so the candidate leaves
  `S13` and `S7`'s count and their slot reopens while the row survives. `WHERE … AND
  is_active = 'Y'` makes it idempotent — a second call affects 0 rows and answers `404`
  rather than pretending to remove someone twice.

## Connections are pooled — do not call `pymysql.connect` directly

The database is an RDS cluster in **us-east-1** and this service runs from India: a fresh
`pymysql.connect()` costs **~1.1s** (TCP + MySQL auth handshake) while the query it was
opened for costs ~0.2s. This module used to open one per request, which is what made the
gap between logging in and the first screen filling — 84% of every call was handshake,
discarded immediately after.

`query` / `insert` / `update` (and `ratings_query` / `ratings_call`) now check a connection
out of a `LifoQueue` and hand it back. Every DB access goes through those five, so new code
must too — a direct `pymysql.connect()` reintroduces the whole problem for that endpoint.

- **Reads retry, writes ping.** `_read` runs the statement and, if a *pooled* connection
  raises `OperationalError`/`InterfaceError`, discards it and retries once on a fresh one —
  a connection idle past the server's `wait_timeout` is closed server-side with no notice,
  and pinging to check on every call would cost the round trip the pool exists to remove. A
  brand-new connection failing is a real error and is **not** retried. `_write` cannot
  replay (a write that died mid-flight may already have been applied), so it pings a reused
  connection up front instead, and discards any connection that failed mid-statement rather
  than returning it to the pool.
- `POOL_MAX` (env `DB_POOL_MAX`, default 10) caps each pool; past it a returned connection
  is closed. Sync endpoints run in Starlette's threadpool, so concurrency is what sizes this.
- `ratings_call` must still drain every result set before its connection goes back.
- `test_pool.py` covers reuse, the stale-socket retry, the no-retry-on-new rule, write
  ping/discard, and the cap.

## Names that look like typos but aren't

| name | note |
|---|---|
| `proposal_consituency` (table), `proposal_consituency_id` | Misspelled in the schema. Real name — do not "correct" it. |
| `proposal_position.proposal_constituency_id` | Spelled **correctly**, on the child table. Both spellings are live, one query often uses both. |
| `cadre_performace_report` | Misspelled in the ratings database. Real name. |
| `user.Hash_Key` / `Salt_Key` | Capitalised, unlike everything else. Written by the Java portal that owns the table. |

## Auth

`user.Hash_Key` is not a password hash you would design; it is the Java portal's:

```
digest   = md5(md5(username) + md5(password))          # lowercase hex, concatenated
Hash_Key = hex(PBKDF2-HMAC-SHA1(digest, salt, 1000, 64))
```

`Salt_Key` is hex over the **ASCII** salt that side used (e.g. `'[B@3da6a354'`, a Java
`byte[].toString()`), so it is un-hexed before it goes into PBKDF2. Do not "fix" that to a
raw-bytes salt — every existing row would stop matching.

- **`username` is indexed but not unique.** Login loops every row carrying the name; the
  hash decides which one. A row with an unusable `Salt_Key`/`Hash_Key` is **skipped**, not
  raised on — raising aborted before reaching the row whose password actually matched.
- An unknown username burns one PBKDF2 against `DUMMY_SALT_KEY`, so the 76k-row `user`
  table is not enumerable by timing despite the identical `401`.
- The throttle is **per username, not per IP**: dev and preview both proxy through Vite, so
  every request arrives from `127.0.0.1` and an IP bucket would throttle all users at once.
- **One session, two transports.** `S14login` sets the httpOnly `lbe_session` cookie *and*
  returns the same token in its body as `token`; `session_token(request)` reads
  `Authorization: Bearer <token>` first and falls back to the cookie, so `current_user`,
  the guard and `S16logout` never care which arrived. Bearer wins on purpose — a caller
  that sends one chose it, and a stale cookie in the same browser must not shadow it.
  Nothing else changes: one `SESSIONS` entry, one TTL, one logout. The cookie stays the
  browser's primary path because it is the only one an XSS cannot read; the header exists
  for callers with no cookie jar (another origin, a mobile client, a script).
- `SESSIONS` and `LOGIN_ATTEMPTS` are process dicts and neither is self-cleaning;
  `sweep_expired` runs on the login path. Sessions do not survive a restart — with
  `--reload`, that means every code edit.
- **`CORSMiddleware` is registered last on purpose**, at the bottom of the file.
  `add_middleware` prepends and index 0 is outermost, so registering it last is what puts
  it *outside* `guard_response` — otherwise a `401` short-circuits before CORS runs and a
  cross-origin caller sees an opaque failure instead of the `401` the frontend's
  `checkUnauthorized` depends on. `test_auth.py` asserts the order.
- Every response carries `Cache-Control: no-store`. Without it the browser may keep cadre
  data on disk past logout and hand it to whoever signs in next on that machine.

## Eligibility

**Reservation alone — location is not part of it.** A cadre's `user_address` no longer has
to match the proposal constituency's chain (assembly → mandal → panchayat/town), so cadre
from anywhere may be proposed. What is checked is `constituency_reservation`:
`caste_category_id` when set, `gender = 'F'` when set. A cadre with no caste category on
record compares NULL and so is ineligible.

**`eligibility_flag()` returns a SELECT expression, not a WHERE clause** — `… AS eligible`,
`'Y'`/`'N'`. S12 therefore returns every cadre the search matched, ineligible ones
included. That is deliberate: it lets the frontend say "no cadre has that membership id"
and "that cadre is barred by the reservation" differently instead of showing one blank
result. Only `eligible = 'Y'` rows can be staged there.

`S11` re-checks the same rules on write and answers `409` naming the reservation type in
`detail`, which is the text the frontend's error banner shows. Change eligibility in
`proposal_context()` / `eligibility_flag()`, never in one endpoint. `test_eligibility.py`
locks down that the expression never mentions an address column again.

**Some seeded `proposal_candidate` rows would fail the check now.** `S13` still returns
them — it reports what *is* assigned, and filtering it would desync the list from `S7`'s
`proposed_cnt`.

## Scores (S17) — a second, optional database

`report_ratings` lives on the ratings pipeline's own server and is **optional**: with any
of `REPORT_RATINGS_DB_HOST`/`_USER`/`_PASSWORD` unset, `RATINGS_DB` stays `None` and S17
answers `{"configured": false, "questions": [], "candidates": []}`. The wizard renders
without scores rather than not at all — keep that shape, it is a state the UI draws.

- **Total Score is half of each half**: `(Σ the 11 SCORE_POINT_COLUMNS ÷ 2) + (Σ the
  feedback answer points ÷ 2)`, matching the membership-analytics platform. It is `null` —
  never `0` — when a cadre has neither, so unrated does not sort as the worst candidate.
- **Lookup-first.** A membership id whose `cadre_performace_report` row exists is served
  from the table; `cadre_performance_update` / `cadre_performance_report` (seconds per id)
  run only for the rest. Do not call them unconditionally.
- `ratings_call` must drain every result set (`while cur.nextset()`) before the connection
  can be reused — the procedures emit sets that are of no use here.
- **`mid_key` reconciles two spellings of the same id**: `cadre_performace_report` stores
  it as varchar (possibly zero-padded), `leader_feedback` as an INT. Leading zeros are what
  differ. `normalize_mids` additionally strips a pasted `#`.
- Row keys are the report's **own column names, spaces and all** (`'ACH % (Booth D2D)'`,
  `'BOOTH 15%'`). S17 returns the row unrenamed and the frontend's compare table names the
  same columns — renaming here silently blanks that table.
- Feedback question labels come from `members_track.question`, a **different database on
  the same server**, read once into `FEEDBACK_QUESTIONS`. A failure there is cosmetic (the
  answers still render, keyed by question id) and must not take the response down.
- `jsonable()` exists because DictCursor hands back `Decimal` and `date`, neither of which
  json encodes.
- `test_score.py` covers the arithmetic and the key matching.

## What the data actually contains

Only one path through the frontend wizard reaches live rows:

- One `proposal_consituency`, reachable via **ACHANTA (`constituency_id` 181) → Achanta
  mandal (`tehsil_id` 658)** → panchayat **VALLURU** (`constituency_id` 58153,
  `election_scope_id` 33). Every other assembly/mandal ends at an empty S5 result.
- It has no `local_election_body`, so the towns half (S4/S6) yields nothing for it.
- Every seeded row is `proposal_election_type_id` 8 = **Panchayat**. Row 8 was originally
  `is_active = NULL, order_no = NULL`, which made S1 hide the one type the data used; it
  has since been activated. If Panchayat ever disappears from S1, check those two columns.
- Its positions: `President` (`max_proposals` 3, already full — S11 answers `409`) and
  `Vice-President` (open).
- Reservation is `BC-GENERAL`, so only cadre with `caste_category_id = 2` can be assigned.

## `S19` — the one endpoint that is not keyed by a drilled-down id

Every other read here takes an id the wizard walked down to (`proposal_constituency_id`,
`proposal_position_id`). `S19getProposalPositionsWithCandidates` takes nothing: it serves
the frontend's Candidates screen, which lists positions **across all constituencies** and
so has no id to key off.

- The join to `proposal_candidate` is **inner** — a position nobody was proposed for has
  nothing to show and must not appear. That is the endpoint's whole filter.
- It returns both constituencies a position sits under: the **local body**
  (`PCon.constituency_id`, a panchayat/ward-level `constituency` row) and the **assembly**
  (through `PCon.address_id → user_address.constituency_id`, the way `S5`/`S6` resolve it).
  The screen filters on the assembly while naming the local body, so dropping either
  breaks it.
- The per-status counts `COALESCE` a NULL `proposal_status_id` to Proposed, matching the
  LEFT OUTER JOIN in `S13`, and are `CAST(… AS UNSIGNED)` because `SUM()` is DECIMAL in
  MySQL and would otherwise reach the browser as a float next to `proposed_cnt`'s integer.
- **No query parameters, deliberately.** The caller filters in the browser and derives its
  Election Type / Assembly / Role dropdown options from these same rows — narrowing them
  server-side would empty the dropdowns the filter was picked from.

## Who wrote a row comes from the session, never the body

`proposal_candidate.inserted_user_id` (written by `S11`) and `updated_user_id` (written by
`S20`) both come from `acting_user_id(request)`, i.e. `current_user(request)["user_id"]` —
the same identity `S14` put in `SESSIONS`. **Never accept a user id in a request body for
these**: they are an audit trail, and a browser can put any number in a payload. Any future
write that stamps a user follows the same route. `guard_response` has already rejected
callers without a live session before a handler runs, so `current_user` is never `None`
outside `PUBLIC_PATHS`, which is why there is no None check there.

`S18` does **not** stamp `updated_user_id` today — it flips `is_active` and `updated_time`
only.

## `S20` — the only in-place edit of a `proposal_candidate`

`S11` creates the row and `S18` deactivates it; `S20updateProposalCandidateStatus` is the
only write that changes one that already exists, and it touches `proposal_status_id`
alone.

- **No slot or eligibility re-check.** All three statuses are live rows counted by `S7`'s
  `proposed_cnt`, so moving between them frees nothing and consumes nothing; re-running
  `S11`'s checks here would refuse a candidate already sitting in the slot.
- `is_active = 'Y'` is in the WHERE — a removed candidate is on no screen to restatus.
- MySQL reports 0 affected rows both for "no such row" and for "already that status", so
  the handler re-checks existence before answering `404`: re-saving an unmoved status is
  not an error the screen should show.

## Frontend contract worth keeping

- `img_url` is built in SQL as an S3 URL and is `''`, not NULL, when the cadre has no
  image — the card falls back to initials on `''`.
- `S7` carries the role names and both counts, which is why `S8` and `S10` are unused by
  the frontend. `S10` is still called internally by `S11`.
- A `429` (login throttle) and a `500` must not read as "session over"; the frontend only
  logs out on `401`, and only outside `S14`/`S15`/`S16`.
- Under the gateway, `StripPrefix` removes the `/portal-frontend-code` mount before this
  app sees the request. Without it `PUBLIC_PATHS` stops matching and login is unreachable;
  `../../test_gateway.py` locks that down.
