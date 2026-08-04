# Working notes — admin dashboard backend

Carried over from the `admin-dashboard` repository, which now holds only the
React frontend. Everything below covers this backend; the frontend notes stay
because they are the contract it has to keep serving.

This file is only the things that will bite you — mostly places where the
obvious change is wrong. The endpoint list is in the repo-root `README.md` and
in `/docs`.

## Ground rules

- **Live production database.** There is no seed, no fixture, no local copy. A
  write endpoint writes real rows for real people. Read before you write, and
  don't test writes against arbitrary member ids.
- **The login gates the UI only.** `POST /api/login` checks the single operator
  account in `.env` and returns no token; every write endpoint is still open to
  anyone who reaches port 6644. Don't widen exposure without adding real auth,
  and don't mistake the login screen for endpoint protection.
- **Deletes are soft, everywhere.** Nothing in this codebase issues `DELETE
  FROM`. Grants are retired by flipping a flag and revived by flipping it back,
  which is why every mutation is reactivate-or-insert rather than insert.

## Column names that look like typos but aren't

| column | note |
|---|---|
| `activity_member.is_acitve` | Misspelled in the schema. It is the real name — do not "correct" it. |
| `activity_member_access_type.is_active` | Spelled correctly. Both spellings are live, on different tables. |
| `activity_member_access_level.is_active` | Spelled correctly. |
| `activity_member_component.is_valid` | Not `is_active`. Different word for the same idea. |

The API response carries `is_acitve` through to the frontend unchanged;
`adapt.js` is the only place it becomes a friendly `active: boolean`.

## OTP

`login_otp_details` has no expiry column. This console stores the admin-picked
expiry in **`updated_time`**, and `generated_time` stays the creation stamp.
`expires_at` in every OTP response is `updated_time`.

One exception: a brand-new MID created through `POST /api/cadre` gets a fixed
literal `'2026-12-31'` as `generated_time` — a product decision, not a bug, and
it is the column the separate member-facing login flow window-checks. The real
expiry still goes to `updated_time`.

Deactivating a login (`PUT .../active` with `N`, or `DELETE /api/members/{id}`)
also invalidates live OTPs. That is intentional.

## Queries

- `MEMBER_SELECT` picks one access_level grant per login with a `ROW_NUMBER()`
  derived table. **Do not collapse it back to bare `MAX()`.** 99 logins hold
  more than one active scope, and independent `MAX()` per column mixed rows
  together — member 115 holds (ASSEMBLY, 173) and (PARLIAMENT, 510) and was
  reported as (ASSEMBLY, 510), a pairing that exists in no grant row. The
  surviving `MAX()` wrappers only satisfy `GROUP BY`.
- The level/location columns on a member row are a **one-line summary**. The
  full set is `locations`, attached separately by `attach_locations`. Anything
  that needs every scope must read `locations`.
- Joins are `LEFT JOIN` deliberately. Inner joins would drop logins with no
  components or no active role, silently undercounting the Active/Inactive KPIs.
- `activity_member_id = 581` is a reserved placeholder record. It is excluded
  from the duplicate-login check and from the by-mobile join. Keep it excluded.
- `tdp_cadre.mobile_no` is indexed but **not unique** — families share a phone.
  `by-mobile` returns every match. No screen calls it today — the create flow
  writes a new cadre unconditionally — but `api.js` still ships the client, and
  the endpoint is the only way to see the duplicates that leaves behind.
- Route order in `routers/cadre.py`: `/by-mobile/{mobile}` must stay declared
  **before** `/{mid}/access-types`. Both are three-segment paths and FastAPI
  matches in declaration order.

## Connection pool (`db.py`)

Three things there are load-bearing and look like they could be simplified:

- `autocommit=True` on the read pool. With it off, every `SELECT` opens a
  REPEATABLE READ transaction that never commits, and a pooled connection then
  serves the same frozen snapshot for its whole lifetime — writes made
  elsewhere appear to never land.
- `conn.ping(reconnect=False)`. A reconnect would start a new session without
  the `SET SESSION TRANSACTION READ ONLY` pragma, silently dropping the
  read-only guarantee. Dead connections are discarded and rebuilt instead.
- The `BoundedSemaphore`. It caps concurrent connections against a shared RDS
  box; removing it lets a burst open unbounded connections.

Statement count is what costs here, not row count — each round trip to RDS is
~220 ms. That is why `apply_locations` and `grant_components` look up all
candidates in one query rather than one per item.

## Static values written on create

`POST /api/cadre` and `POST /api/members` stamp constants that match what the
bulk of existing rows carry. They are product decisions, not derived values —
don't compute them from the current date:

- cadre: `enrollment_year = 2014`, `is_synced = 'Y'`, `data_source_type =
  'WEB'`, `image = 'human.jpg'` (no photo upload in this console)
- enrollment: one `tdp_cadre_enrollment_year` row each for ids **6 and 7**
  (2022-2024 and 2024-2026), not just the current cycle
- login: `updated_by = 1`, `state_id = 1`, `activity_member_enrollment_id = 2`

The membership ID is **not** admin-chosen. The cadre row is inserted with
`membership_id` NULL, then set to `str(tdp_cadre_id)` in the same transaction —
uniqueness for free, no collision retry.

## Scope levels

Only three are ever written: **5 ASSEMBLY, 4 PARLIAMENT, 2 STATE** — served as
`used_level_ids` from `/api/lookups/user-levels`. Level 2 is state-wide and
carries **no** `location_value`; `MEMBER_SELECT` resolves it to `'AP'`
regardless, so its grant row is stored with `location_value = NULL`.

Other levels exist in `user_level` and appear on legacy rows (you will see
`DISTRICT` in the data), but nothing resolves their location to a name and this
console does not offer them.

`apply_locations` compares with `<=>` (NULL-safe equals) precisely because
STATE grants carry NULL — plain `=` would never match one.

## Frontend

- **Component identity is the numeric `component_id`, never the label.** The
  catalog reuses "DOWNLOAD CENTER" across seven ids and "CADRE WELFARE" across
  two. `adapt.js` exposes `{ id, label, key }`; `label` is for display, `key` is
  `component.name`, `id` is what every write endpoint wants.
- **`adapt.js` derives everything.** Totals, roles, ranked components and the
  per-role rollups are all recomputed from the member list, so the KPIs cannot
  drift from the rows on screen. There is no separate counts endpoint and no
  stored aggregate — if a number looks wrong, the member list is wrong.
- **`ctx.components` is a slice, `ctx.componentsAll` is the catalog.** The
  dashboard panel shows granted-only, top 14; the Total Components section (and
  anything counting modules) must read `componentsAll`, which keeps
  never-granted entries at `granted: 0`. `adapt.test.mjs` pins both.
- **Every write endpoint returns the updated member row.** `App.jsx` swaps it in
  place; nothing re-fetches after a save. Preserve that contract if you add an
  endpoint.
- **The Detail screen stages a draft and sends one request.** It used to fire
  one request per changed field plus one per toggled component (~59 s for a
  member with five components). Don't reintroduce per-field writes.
- **Mobile number is read-only** on the Detail screen. It lives on `tdp_cadre`
  and there is no write endpoint for that table — an editable field there would
  silently discard the edit.
- **Create New User is one page and two writes.** `POST /api/cadre` then `POST
  /api/members`, gated by a single `validateNewUser` pass that also feeds the
  progress rail — one validator, so the rail and Save can't disagree. The
  screen holds the created cadre in state: a failed second write retries
  against that cadre instead of writing a duplicate person. Don't split it back
  into a wizard and don't validate per step.
- **"Grant to a role" is a loop, not a bulk endpoint.** There is no
  role→component table; it issues one `POST` per active login in the role that
  doesn't already hold the component. Slow by nature — keep the busy state.

## Sign-in

The console's one operator account is `LOGIN_USERNAME` / `LOGIN_PASSWORD` in
`.env`, compared in `routers/auth.py` with `compare_digest` on both halves and
a non-short-circuiting `&`. Nothing else is an allow-list: there is no users
table, no fallback pair, no dev bypass.

- The check must stay server-side. A `VITE_*` credential gets inlined into the
  bundle, so `Frontend/dist` would then ship the password in plaintext.
- `LoginScreen` gates the frontend; `App.jsx` fetches nothing until it passes.
  That is UI-level only — the API endpoints are still unauthenticated.
- The 401 body says "Incorrect username or password" for a wrong username too.
  Keep it identical, or the valid username becomes enumerable.
- `config.py` reads both with `os.environ[...]`. Don't switch to `.get()` — a
  missing key must stop the app, not become an empty credential.

The screen itself is a copy of `mypartydashboard.com/login.action` — Montserrat,
`#fbc928` inputs, green SIGN IN, and `public/login-bg.svg` (that page's own
artwork, captured verbatim). It is the one screen that ignores the app's Inter +
slate tokens. Two things there look like tidying but break it:

- `.cycle` needs **`overflow: clip`**, not `hidden`. The oversized artwork gives
  it ~1278px of scrollable overflow, and `hidden` boxes still scroll
  programmatically: focusing the autofocused username field scrolled the column
  462px out of view, leaving only the copyright line on screen.
- `#circle-image` needs an explicit **`width: 100%`**. Upstream renders that SVG
  inline, so its box is the full body width and the square drawing letterboxes
  inside it. As an `<img>` it would take the drawing's own 1:1 ratio instead,
  and the `scale(2.4)` then crops from the wrong origin.

There is no OTP step. Upstream sends an OTP after a correct pair; the admin
account has none, so an OTP field would have nothing to check.

## Running and deploying

- **Two ways to run, and they load `.env` differently.** Standalone is `cd
  Backend && python main.py` (port 4000), where `config.py` `load_dotenv()`s
  `../.env` off its own file path. Under the repo-root `gateway.py` it is
  imported and mounted at `/admin-dashboard`, and the gateway must
  `load_dotenv(..., override=True)` first — `config.py`'s own call will not
  replace variables the portal's `.env` already put in `os.environ`, so without
  the override this backend silently talks to the portal's database.
- **`Backend/` is a flat, package-less tree** (`from config import ...`, `from
  routers import ...`), which only resolves with `Backend/` on `sys.path`.
  Standalone gets that for free from the working directory; `gateway.py` inserts
  it explicitly in `_load_admin()`. Don't remove that line.
- **The frontend is a separate deploy** (`admin-dashboard` repo, React + Vite).
  It reaches this backend through `/api` — either `VITE_API_BASE` pointed
  straight at it or Vite's proxy via `VITE_API_PROXY`. Changing a path or a
  response shape breaks a bundle that ships from another repository.
- **Port 6644** is what the frontend's proxy falls back to for a local backend,
  and what `gateway.py`, `deploy.sh` and `ecosystem.config.js` all serve on. This
  backend's own standalone default is still 4000 (`Backend/main.py`), so the two
  don't collide on one host. A frontend pointed at 6644 reaches this backend
  through `/admin-dashboard/api/...`; a standalone run serves `/api/...` bare.
- **No reverse proxy config in this repo.** TLS and the public hostname belong to
  something in front (nginx/Caddy/ALB). A request that hangs ~15 s before failing
  is PyMySQL's `connect_timeout`, i.e. the server's outbound IP is not in the RDS
  security group.

## Verify

```bash
python test_gateway.py                     # from the repo root
cd admin-dashboard/Backend && python -c "import main; print(len(main.app.routes))" && python test_auth.py
```

The route count should be 26 (22 API routes plus FastAPI's four docs routes).
A failing assertion means stop, not retry.
