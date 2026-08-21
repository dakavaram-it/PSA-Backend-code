# CLAUDE.md — portal-frontend-code-2 (Dashboard 2 API)

Guidance for Claude Code working in this sub-project. Read `README.md` first for what the
service is; this file is the traps.

## Commands

```bash
cd Backend
python main.py                 # standalone on 4002, docs at /docs
python test_dashboard2.py      # 11 invariant checks, live DB, no pytest needed
python ../../gateway.py --dump # rebuild the merged spec after adding a route
```

No linter, no formatter. Verification is `test_dashboard2.py` plus reading the JSON.

## What this is not

- **Not a second copy of Dashboard 1's backend.** `../portal-frontend-code` owns the
  nomination *workflow*: login, the wizard, `assignProposalCandidate`, eligibility, the S3
  nomination uploads. This owns one reporting screen. If a rule exists in both, it belongs
  there, and this one should read data rather than re-derive it.
- **Not authenticated.** No token, no middleware, no user id anywhere. See README.
- **Not writable.** Every route is a GET. Adding a write means adding auth first.

## Traps

**A post is a triple, not a role.** `(main_election_type_id, proposal_election_type_id,
proposal_role_id)`. `proposal_role_id = 5` (Corporator) is used by both Municipal Ward
(body 4) and Corporation Ward (body 5) positions, so grouping on the role alone silently
merges two of Dashboard 2's rows. Every endpoint that names one post takes all three.

**A location is one `proposal_position` row.** Its reservation, its names and its stage all
hang off that row. That is what keeps the counters, the location list and the comparison
table agreeing without a second definition.

**Every position is attributed to exactly one assembly, and that is a deliberate corner
cut.** `queries.ASSEMBLY_EXPR` resolves the assembly *from* the position, which is the
inverse of `../portal-frontend-code`'s `assembly_match()`. Mandal- and ward-level rows are
exact. Municipality/Corporation and Zilla Parishath rows genuinely span several assemblies
and are pinned to the lowest-numbered one — ~350 of 43,636 rows. This is what makes the geo
table add back up to the position total, which
`test_geo_halves_add_back_to_the_position_total` guards. Per-assembly precision for those
bodies needs a bridge table, not a second expression.

**Stage 1 is "has an active candidate", not `started_time IS NOT NULL`.** The two disagree
in the live data (MPTC: 11 candidates against 2 stamped times). `started_time` is still
returned per location for anyone who wants the stamp itself.

**`proposal_position.proposal_status_id` is NULL on all 43,636 rows.** The column exists;
nothing populates it. The status that matters is `proposal_candidate.proposal_status_id`.
Do not start reading the position column without checking it has data.

**`proposal_consituency` has one `t`.** So does `proposal_consituency_id`. It is the
column name, not a typo to fix. Same for `conformed_` aliases if you port anything over
from Dashboard 1.

**`enrollment_id = 1` scopes every read**, matching every `proposal_consituency` read in
`../portal-frontend-code`. About 8 active `proposal_candidate` rows point at positions that
do not survive that filter; they are invisible here on purpose.

**Gram Panchayat has no reservations configured.** All 12,420 Sarpanch positions come back
with `reservation_type` NULL. That is the data, not a join bug — `/reservationSummary`
reports the NULL bucket rather than folding it into GENERAL.

**State-wide scope skips the assembly join entirely.** `scope_filter()` returns empty
strings when `scope.state_wide`, which keeps the 43,636-row summary off
`ASSEMBLY_EXPR`'s three correlated subqueries. Do not "simplify" that by always joining.

**Filter fragments are positional.** `queries.py`'s `{position}`, `{scope}`, `{stage}`,
`{reservation}`, `{parliament}` placeholders are substituted in the order their `%s` args
are concatenated in `routers/dashboard.py`. Reordering one without the other silently
shifts every argument.

## Adding a stage

When Door to Door / Result get tables, the change is: one join in `queries.py`, the
counters in `COUNTERS`, and deleting `EMPTY_STAGE_FIELDS` + `STAGES_UNAVAILABLE` from
`routers/dashboard.py`. Nothing between those two files has to be re-derived — that is why
the zeros live in Python rather than in the SQL.
