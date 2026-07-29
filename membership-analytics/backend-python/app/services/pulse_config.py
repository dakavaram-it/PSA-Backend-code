"""Config for the Pulse Trend computations — edit here (or via env) to change
which surveys/options/dimensions feed the dashboard, then trigger a refresh.
No code changes needed for routine survey/wave updates."""
import os, json

# survey24.ivrs_option ids
OPT_TDP, OPT_YSRCP, OPT_OTHERS, OPT_NDA = 14, 15, 16, 28
TDP_NDA_OPTS = [OPT_TDP, OPT_NDA]
PARTY_OPTS = [OPT_TDP, OPT_YSRCP, OPT_OTHERS, OPT_NDA]

# IVRS waves shown on the bars (label -> survey ids in survey24.ivrs_survey)
IVRS_WAVES = [
    {"label": "Apr–May ’24", "sids": [19, 20, 21, 24, 25, 26]},
    {"label": "Dec ’25",     "sids": [28]},
    {"label": "Jun ’26",     "sids": [31]},
]

# Decline comparison: "before" wave vs "after" wave (survey id sets).
# Compares the two most recent IVRS waves (Dec ’25 → Jun ’26).
DECLINE_BEFORE_SIDS = [28]
DECLINE_AFTER_SIDS = [31]
DECLINE_LABEL = "Dec ’25 → Jun ’26"
DECLINE_BEFORE_LABEL = "Dec ’25"
DECLINE_AFTER_LABEL = "Jun ’26"
# dimension -> (ivrs_mobiles column, top-N declines to keep, min sample per wave)
DECLINE_DIMS = {
    "constituency":   {"col": "constituency_name", "top": 12, "min": 1200},
    "caste":          {"col": "caste_name",        "top": 12, "min": 4000},
    "age":            {"col": "age_range",         "top": None, "min": 2000},
    "gender":         {"col": "gender",            "top": None, "min": 2000},
}
# caste category via dakavara cross-join
CASTE_CATEGORY_MIN = 2000

# constituency drill-down: booth-level + sub-dimensions scoped to one constituency.
# Samples are far smaller here (a booth IVRS sample is tens), so mins are lower.
# mins are UNIQUE VOTERS per wave (deduped), so lower than response-count thresholds
CONSTITUENCY_DIMS = {
    "booth":          {"col": "part_no",    "top": 15,   "min": 25},
    "caste":          {"col": "caste_name", "top": 12,   "min": 60},
    "caste_category": {"col": "cat",        "top": None, "min": 80},
    "age":            {"col": "age_range",  "top": None, "min": 100},
    "gender":         {"col": "gender",     "top": None, "min": 100},
}

# CATI continuous tracker (survey24.ivrs_survey id) + start month
CATI_SURVEY_ID = 30
CATI_FROM = "2025-04-01"

# allow an env JSON override, e.g. PULSE_CONFIG_JSON='{"CATI_SURVEY_ID":31}'
def _apply_env_overrides():
    raw = os.getenv("PULSE_CONFIG_JSON")
    if not raw:
        return
    try:
        over = json.loads(raw)
        g = globals()
        for k, v in over.items():
            if k in g:
                g[k] = v
    except Exception:
        pass

_apply_env_overrides()

# refresh endpoint auth (optional). If unset, refresh is open (dev).
REFRESH_TOKEN = os.getenv("PULSE_REFRESH_TOKEN")
