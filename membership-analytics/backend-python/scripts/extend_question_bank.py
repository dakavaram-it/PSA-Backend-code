"""Additive expansion of survey24's ivrs_question / ivrs_option banks for the Dhruva rich
batteries (leader satisfaction, development perception, MLA/MP attribute grids, LS vote).

Idempotent (INSERT … ON DUPLICATE KEY UPDATE). New ids only — never renumbers existing
(questions 1–23, options 1–128 are untouched, so the dashboard's option-id contract holds).

    PYTHONPATH=. python3.13 scripts/extend_question_bank.py            # DRY-RUN
    PYTHONPATH=. python3.13 scripts/extend_question_bank.py --commit
"""
import sys
from sqlalchemy import text
from app.database.db import dakavara_session

# new options (id : name) — two new scales; satisfaction reuses the 39–43 opinion scale
OPTIONS = {
    129: "Improved", 130: "Deteriorated", 131: "No Change",
    132: "Completely Agree", 133: "Somewhat Agree",
    134: "Somewhat Disagree", 135: "Completely Disagree",
}
# new questions (id : name)
QUESTIONS = {
    24: "CM Satisfaction", 25: "Dy CM Satisfaction", 26: "MP Satisfaction",
    27: "Nara Lokesh Satisfaction", 28: "Lok Sabha Vote (Past)",
    29: "Development - Law and Order", 30: "Development - Education",
    31: "Development - Employment", 32: "Development - Healthcare",
    33: "Development - Electricity", 34: "Development - Welfare Schemes",
    35: "Development - Roads and Infrastructure", 36: "Development - Public Transport",
    37: "Development - Investments to State", 38: "Development - Inflation",
    39: "Development - Drainage",
    40: "MLA - Hardworking", 41: "MLA - Available", 42: "MLA - Honest",
    43: "MLA - Work and Development", 44: "MLA - Cares for Community",
    45: "MP - Hardworking", 46: "MP - Available", 47: "MP - Honest",
    48: "MP - Work and Development",
}


def main():
    commit = "--commit" in sys.argv
    with dakavara_session() as db:
        qmax = db.execute(text("SELECT MAX(ivrs_question_id) FROM survey24.ivrs_question")).scalar()
        omax = db.execute(text("SELECT MAX(ivrs_option_id) FROM survey24.ivrs_option")).scalar()
        print(f"current max question_id={qmax}  option_id={omax}")
        print(f"adding {len(QUESTIONS)} questions (24–48), {len(OPTIONS)} options (129–135)")
        if min(QUESTIONS) <= qmax or min(OPTIONS) <= omax:
            print("WARNING: id overlap with existing rows — check before commit.")
        if not commit:
            print("\nDRY-RUN — re-run with --commit to insert."); return
        for oid, name in OPTIONS.items():
            db.execute(text("INSERT INTO survey24.ivrs_option (ivrs_option_id, option_name) "
                            "VALUES (:i,:n) ON DUPLICATE KEY UPDATE option_name=VALUES(option_name)"),
                       {"i": oid, "n": name})
        for qid, name in QUESTIONS.items():
            db.execute(text("INSERT INTO survey24.ivrs_question (ivrs_question_id, question_name) "
                            "VALUES (:i,:n) ON DUPLICATE KEY UPDATE question_name=VALUES(question_name)"),
                       {"i": qid, "n": name})
        db.commit()
        print("committed. questions now:",
              db.execute(text("SELECT COUNT(*) FROM survey24.ivrs_question")).scalar(),
              "options now:",
              db.execute(text("SELECT COUNT(*) FROM survey24.ivrs_option")).scalar())


if __name__ == "__main__":
    main()
