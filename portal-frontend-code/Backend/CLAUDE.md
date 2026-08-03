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
