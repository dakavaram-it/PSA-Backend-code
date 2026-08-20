# Working notes — portal backend (local body elections)

Everything here is `main.py` (~800 lines). The endpoint list, the env vars and how to run
it are in `README.md` next to this file; this one is only the things that will bite you —
mostly places where the obvious change is wrong.

The frontend is a **separate repository** (`portal-frontend-code`, React + Vite) that is
built and deployed elsewhere. It reaches these paths through its own `/leapapi` prefix.
Changing a path, a column alias or a status code breaks a bundle you cannot see from here.

## Ground rules

- **Live production database.** No seed, no fixture, no local copy. `assignProposalCandidate` writes a real
  `proposal_candidate` row for a real person. Read before you write.
- **Everything except `login` requires a session.** `PUBLIC_PATHS` is the whole
  allowlist (`/login`, `/docs`, `/redoc`, `/openapi.json`). The cadre endpoints serve
  personal data — names, mobile numbers, voter ids.
- **No authorization, only authentication.** Any valid account can read and write against
  any constituency.
- Only `GET` and `POST` exist. Nothing is deleted; `proposal_candidate.is_active` is the
  flag every read filters on.
- **`proposal_candidate.proposal_status_id` says which kind of row it is** — a lookup into
  `proposal_status` (1 Proposed, 2 Shortlisted, 3 Confirmed). `assignProposalCandidate` takes it in the body and
  defaults it to `PROPOSED_STATUS_ID`, validating it against the table rather than a list
  here, so a new status is a row and not a deploy. It changes no count: the slot arithmetic
  in `getProposalPositionsOverviewByProposalConstituencyId`/`checkProposalPositionAvailability` is over active rows whatever their status, so a shortlisted cadre occupies a
  `max_proposals` slot exactly as a proposed one does. Rows written before the column have
  it NULL, which is why `getProposalCandidatesByProposalPositionId` joins `proposal_status` with a LEFT OUTER JOIN — dropping them
  would desync the list from `getProposalPositionsOverviewByProposalConstituencyId`'s `proposed_cnt`.
- **`removeProposalCandidate` is the only write that undoes `assignProposalCandidate`**, and it is still not a
  delete: `is_active` goes to `'N'`, which every read filters on, so the candidate leaves
  `getProposalCandidatesByProposalPositionId` and `getProposalPositionsOverviewByProposalConstituencyId`'s count and their slot reopens while the row survives. `WHERE … AND
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
- **The session is a JWT and nothing else.** `login` returns `token` in its body —
  `issue_token(user_id)`, `{"sub": "<user_id>", "iat", "exp"}` signed with `JWT_KEY` for
  `SESSION_TTL`. There is **no cookie and no server-side session store**; the one
  transport is `Authorization: Bearer <token>`, read by `bearer_token(request)`.
- **The payload is the Java portal's, deliberately.** It is byte-for-byte the shape
  `mypartydashboard.com/PSA/WebService/User/getToken` produces (`sub` a *string*, not an
  int), signed with the same key, so a token from either side is a session on the other.
  Calling that service instead was tried and reverted: it answers `404` at
  `/PSA/WebService/User/getToken` and at every method, case and sibling path probed —
  a Spring app is mounted at `/PSA` (its `/PSA/error` answers), but no `getToken`
  controller is reachable. Minting locally keeps the tokens interchangeable without
  making a login depend on that service. **Do not "fix" the payload** to carry the user:
  the extra claims would not round-trip through the portal.
- **`JWT_SECRET` is read as base64, not as characters.** jjwt's `signWith(alg, String)`
  treats the secret as base64, so the portal's key is `base64.b64decode(JWT_SECRET)` —
  9 bytes, not the 15 characters. `JWT_KEY` is that decoded value and it is what
  `jwt.decode` gets. Handing PyJWT the raw string verifies against a different key and
  rejects every token the portal issues; that was the bug, not a config mismatch. The
  9-byte key is far under HS512's 64-byte block — fixing that means both sides rotating
  to a longer secret together.
- **The token carries `sub` and nothing else** — `{"sub": "<user_id>", "iat", "exp"}`,
  `sub` a *string*. So `current_user` decodes it and then reads the identity back out of
  the database (`identity_for`), rather than off the claims. Upside: a changed entitlement
  or constituency takes effect on the next request instead of at the next login. Cost: a
  `user` row plus the four-table entitlements join per request, which is why `current_user`
  caches on `request.state.user` — `guard_response` resolves the identity on the way in and
  `acting_user_id` reads it again inside the endpoint. Any `jwt.InvalidTokenError` —
  expired, tampered with, foreign key, not a JWT — is simply "no session", i.e. a `401`.
- **`SESSION_TTL` caps the portal's window, it does not set it.** getToken signs for 15
  days; `current_user` refuses a token whose `iat` is more than `SESSION_TTL` (8h) old, so
  a session here is 8h regardless. Drop that check to let a token live its full 15 days.
- **`user.state_id` / `district_id` / `constituency_id` are dead columns** — 18 rows out of
  76,785 carry a `constituency_id`. The Java portal records a user's scope as the
  **`access_type` / `access_value`** pair instead and never backfilled them, so reading them
  in `login` answered `null` for all three on every account. `scope_for()` expands the live
  pair, each level filling in the ones above it: `MLA` is an assembly `constituency_id`
  (which carries district and state), `MP` a parliament one (spans districts, so state only),
  `DISTRICT` a `district_id`, `STATE` a `state_id`. `COUNTRY`, `ZONE` and the couple of rows
  holding placeholder text where an id belongs name none of the three.
- **`access_value` is not the same thing as an access grant.** Authorization still comes from
  `user_state_access_info` / `user_constituency_access_info` via `user_access_assemblies()`,
  which covers 1,240 users and disagrees with `access_value` for 38 of them. The login fields
  say where a user sits; they are not what any query may be scoped by.
- **`entitlements` is part of the identity, not a field beside it.** `entitlements_for(user_id)`
  reads the names the user's groups grant (`user_group_relation` → `user_group_entitlement` →
  `group_entitlement_relation` → `entitlement`, `entitlement_type AS entitlement_name`) and the
  list goes **into the `user` dict** that `identity_from_row` builds, which is what both `login`
  and `me` answer with.
- **A token cannot be revoked before it expires.** `logout` is stateless: it answers
  `{"ok": true}` and the client drops its copy, but the token stays valid until `exp` or
  until its `iat` falls outside `SESSION_TTL`. That is the cost of no session store —
  shorten `SESSION_TTL` or add a denylist if it ever stops being acceptable. A token whose
  `sub` names a row that no longer exists is not a session either: `identity_for` answers
  `None` and the guard `401`s. Changing `JWT_SECRET` invalidates every issued token — on
  both sides, since the portal signs with the same value.
- `LOGIN_ATTEMPTS` is a process dict and is not self-cleaning; `sweep_expired` runs on
  the login path. Sessions now survive a restart, since none of one is held in memory.
- **`CORSMiddleware` is registered last on purpose**, at the bottom of the file.
  `add_middleware` prepends and index 0 is outermost, so registering it last is what puts
  it *outside* `guard_response` — otherwise a `401` short-circuits before CORS runs and a
  cross-origin caller sees an opaque failure instead of the `401` the frontend's
  `checkUnauthorized` depends on. `test_auth.py` asserts the order.
- Every response carries `Cache-Control: no-store`. Without it the browser may keep cadre
  data on disk past logout and hand it to whoever signs in next on that machine.

## Eligibility

**The assembly, then the reservation.** A cadre's `user_address.constituency_id` must be
one of the assemblies the proposal constituency sits in, so only cadre from there may be
proposed — but nothing below it is checked, so any mandal, panchayat or town inside is
fine. The reservation itself is `constituency_reservation`: `caste_category_id` when set,
`gender = 'F'` when set. A cadre with no caste category on record compares NULL and so is
ineligible.

**The reservation hangs off `proposal_position`, not `proposal_consituency`** —
`proposal_position.constituency_reservation_id`. Every position in the database has one and
every `proposal_consituency` row leaves its own column NULL, so reading the body's meant
nothing was ever reserved. It is genuinely per role, not per body: all 825 seeded bodies
with more than one role reserve them differently (a President `GENERAL` beside a
Vice-President `GENERAL WOMEN`). That is why `proposal_context()` and `cadreSearch` are
keyed by `proposal_position_id` rather than `proposal_constituency_id`, why
`getProposalPositionsOverviewByProposalConstituencyId` carries `reservation_type` per row,
and why the body-level `getProposalConstituencyReservation` endpoint is gone. Note what the
lookup table actually says: `GENERAL` is `caste_category_id = 1` (OC), so a GENERAL seat
admits OC cadre only — `gender = 'A'` is the "any" value and only `'F'` constrains.

**Which assemblies those are is not one column** — `assembly_constituency_ids()` resolves
them from the proposal constituency's `user_address`, the same three shapes
`assembly_match()` encodes read the other way round, and `proposal_context()` hands the
list back as `assembly_constituency_ids`:
- mandal- and ward-level rows (MPTC, MPP, ZPTC, Municipal Ward, Corporation Ward) name
  their assembly directly — `[UA.constituency_id]`;
- Municipality / Corporation point at the body's *own* constituency, so the assemblies
  come from `assembly_local_election_body` on `UA.local_election_body` — a town spanning
  two assemblies accepts cadre from either;
- Zilla Parishath has no constituency at all, only `UA.district_id`, so every assembly in
  that district counts.

Reading `UA.constituency_id` as "the assembly" is what used to make every
Municipality/Corporation/ZP search answer "belongs to another assembly" for every cadre,
and every such proposal `409`.

**`eligibility_flag()` returns a SELECT expression, not a WHERE clause** — `… AS eligible`,
`'Y'`/`'N'`. cadreSearch therefore returns every cadre the search matched, ineligible ones
included. That is deliberate: it lets the frontend say
"no cadre has that membership id" and "that cadre is barred by the reservation" differently
instead of showing one blank result. Only `eligible = 'Y'` rows can be staged there.

**The assembly is the other half, and it is a second flag** — `… AS in_assembly`, `'Y'`/`'N'`,
from `UA.constituency_id IN (…)` over the ids `proposal_context()` returned. Flagged rather
than filtered for the same reason the reservation is: the row has to come back for the
frontend to say "that id belongs to another assembly" instead of "no cadre found". Only
rows with both flags `'Y'` can be staged.

**The name search is scoped, not flagged, and runs in two statements.** `CadreName` (and
its older spelling `Name`) is the only substring filter here, so unlike the exact ones it
is bounded: `tdp_cadre_enrollment_year.enrollment_year_id = ENROLLMENT_YEAR_ID` (7),
`UA.constituency_id IN (the assemblies above)`, and `LIMIT NAME_SEARCH_LIMIT` (100).
Selecting the card's columns and applying that filter in one statement makes the
optimizer drive off `user_address` — half a million rows for one assembly — and join
tehsil, panchayat and local_election_body to every one of them before the name is ever
compared (measured: 103s for one search). So the ids are picked first, on their own, and
a second statement decorates exactly those ids: 4s for the same search. Every other
search type is still one statement.

`assignProposalCandidate` re-checks the same rules on write and answers `409` naming the reservation type in
`detail`, which is the text the frontend's error banner shows. Change eligibility in
`proposal_context()` / `eligibility_flag()`, never in one endpoint. `test_eligibility.py`
locks down that the expression never mentions an address column again.

**Some seeded `proposal_candidate` rows would fail the check now.** `getProposalCandidatesByProposalPositionId` still returns
them — it reports what *is* assigned, and filtering it would desync the list from `getProposalPositionsOverviewByProposalConstituencyId`'s
`proposed_cnt`.

## Scores (getCadreScores) — a second, optional database

`report_ratings` lives on the ratings pipeline's own server and is **optional**: with any
of `REPORT_RATINGS_DB_HOST`/`_USER`/`_PASSWORD` unset, `RATINGS_DB` stays `None` and getCadreScores
answers `{"configured": false, "candidates": []}`. The wizard renders
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
  `'BOOTH 15%'`). getCadreScores returns the row unrenamed and the frontend's member card reads
  `'YEAR'` and `'NO OF TIME'` off it by those names — renaming here silently blanks those fields.
- **`leader_feedback` is read for `total_score` alone.** The per-answer detail and the
  question labels are no longer returned: the compare table that rendered them is gone.
- `jsonable()` exists because DictCursor hands back `Decimal` and `date`, neither of which
  json encodes.
- `test_score.py` covers the arithmetic and the key matching.

## What the data actually contains

Only one path through the frontend wizard reaches live rows:

- One `proposal_consituency`, reachable via **ACHANTA (`constituency_id` 181) → Achanta
  mandal (`tehsil_id` 658)** → panchayat **VALLURU** (`constituency_id` 58153,
  `election_scope_id` 33). Every other assembly/mandal ends at an empty getProposalConstituenciesByTehsilId result.
- It has no `local_election_body`, so the towns half (getTownsInAConstituency/getProposalConstituenciesByTownId) yields nothing for it.
- Every seeded row is `proposal_election_type_id` 8 = **Panchayat**. Row 8 was originally
  `is_active = NULL, order_no = NULL`, which made getProposalElectionTypes hide the one type the data used; it
  has since been activated. If Panchayat ever disappears from getProposalElectionTypes, check those two columns.
- Its positions: `President` (`max_proposals` 3, already full — assignProposalCandidate answers `409`) and
  `Vice-President` (open).
- Reservation is `BC-GENERAL`, so only cadre with `caste_category_id = 2` can be assigned.

## Which assembly a proposal constituency sits in is three cases, not one — `assembly_match()`

`proposal_consituency.address_id → user_address` names the assembly directly **only** for
the mandal- and ward-level rows (MPTC, MPP, ZPTC, Municipal Ward, Corporation Ward). The
bodies above that level are seeded differently:

| Level | `UA.constituency_id` | Resolve the assembly through |
|---|---|---|
| Mandal / ward | the parent assembly | itself |
| Municipality / Corporation | the body's **own** constituency (self-reference to `PCon.constituency_id`) | `assembly_local_election_body` on `UA.local_election_body` |
| Zilla Parishath | `NULL` (a ZP is a district) | `constituency.district_id` matching `UA.district_id` |

`assembly_match(ua, pc)` is that rule as one SQL fragment, taking the assembly's
`constituency_id` **three times**, once per branch. `getDashboardPositionsByConstituencyId`
and `getDashboardCandidatesByStatus` both use it — the second is the drill-down behind the
first's tiles, so a body one can reach and the other cannot makes a tile open an empty list.
Matching on `UA.constituency_id` alone (what both did before) silently dropped every
whole-body and district row, which is how a Municipality or Zilla Parishath that *has*
`proposal_position` rows still read as **"Not configured"** on the Dashboard.

`getProposalConstituenciesByTownId` carries the town half of the same rule inline, gated on
the self-reference on purpose: applying the town→assembly map to ward rows as well would
list one ward under every assembly its town touches.

**One call site still assumes the one-column version** and will need this rule when a
whole-body proposal is actually worked:
- `getProposalPositionsWithCandidates` scopes with `JOIN constituency AC ON UA.constituency_id = AC.constituency_id` +
  `AC.constituency_id IN (access)` — a Municipality/Corporation/ZP proposal never reaches
  the Candidates screen. Fixing it means deciding whether a body spanning several
  assemblies should appear once per assembly.

## `getProposalPositionsWithCandidates` — the one endpoint that is not keyed by a drilled-down id

Every other read here takes an id the wizard walked down to (`proposal_constituency_id`,
`proposal_position_id`). `getProposalPositionsWithCandidates` takes nothing: it serves
the frontend's Candidates screen, which lists positions **across all constituencies** and
so has no id to key off.

- The join to `proposal_candidate` is **inner** — a position nobody was proposed for has
  nothing to show and must not appear. That is the endpoint's whole filter.
- It returns both constituencies a position sits under: the **local body**
  (`PCon.constituency_id`, a panchayat/ward-level `constituency` row) and the **assembly**
  (through `PCon.address_id → user_address.constituency_id`, the way `getProposalConstituenciesByTehsilId`/`getProposalConstituenciesByTownId` resolve it).
  The screen filters on the assembly while naming the local body, so dropping either
  breaks it.
- The per-status counts `COALESCE` a NULL `proposal_status_id` to Proposed, matching the
  LEFT OUTER JOIN in `getProposalCandidatesByProposalPositionId`, and are `CAST(… AS UNSIGNED)` because `SUM()` is DECIMAL in
  MySQL and would otherwise reach the browser as a float next to `proposed_cnt`'s integer.
- **No query parameters, deliberately.** The caller filters in the browser and derives its
  Election Type / Assembly / Role dropdown options from these same rows — narrowing them
  server-side would empty the dropdowns the filter was picked from.

## Who wrote a row comes from the session, never the body

`proposal_candidate.inserted_user_id` (written by `assignProposalCandidate`) and `updated_user_id` (written by
`updateProposalCandidateStatus`) both come from `acting_user_id(request)`, i.e. `current_user(request)["user_id"]` —
the same identity `login` signed into the token. **Never accept a user id in a request body for
these**: they are an audit trail, and a browser can put any number in a payload. Any future
write that stamps a user follows the same route. `guard_response` has already rejected
callers without a live session before a handler runs, so `current_user` is never `None`
outside `PUBLIC_PATHS`, which is why there is no None check there.

`removeProposalCandidate` does **not** stamp `updated_user_id` today — it flips `is_active` and `updated_time`
only.

## `updateProposalCandidateStatus` — the only in-place edit of a `proposal_candidate`

`assignProposalCandidate` creates the row and `removeProposalCandidate` deactivates it; `updateProposalCandidateStatus` is the
only write that changes one that already exists, and it touches `proposal_status_id`
alone.

- **No slot or eligibility re-check.** All three statuses are live rows counted by `getProposalPositionsOverviewByProposalConstituencyId`'s
  `proposed_cnt`, so moving between them frees nothing and consumes nothing; re-running
  `assignProposalCandidate`'s checks here would refuse a candidate already sitting in the slot.
- `is_active = 'Y'` is in the WHERE — a removed candidate is on no screen to restatus.
- MySQL reports 0 affected rows both for "no such row" and for "already that status", so
  the handler re-checks existence before answering `404`: re-saving an unmoved status is
  not an error the screen should show.

## Frontend contract worth keeping

- `img_url` is built in SQL as an S3 URL and is `''`, not NULL, when the cadre has no
  image — the card falls back to initials on `''`.
- `getProposalPositionsOverviewByProposalConstituencyId` carries the role names and both counts, which is why `getProposalPositionsByProposalConstituencyId` and `checkProposalPositionAvailability` are unused by
  the frontend. `checkProposalPositionAvailability` is still called internally by `assignProposalCandidate`.
- A `429` (login throttle) and a `500` must not read as "session over"; the frontend only
  logs out on `401`, and only outside `login`/`me`/`logout`.
- Under the gateway, `StripPrefix` removes the `/portal-frontend-code` mount before this
  app sees the request. Without it `PUBLIC_PATHS` stops matching and login is unreachable;
  `../../test_gateway.py` locks that down.
