"""Self-check for the S17 score arithmetic. No DB, no test framework:

    cd Backend && python test_score.py

Total Score is half the performance points plus half the feedback points, and must be
None — not 0 — when a cadre has neither, so the wizard can tell "unrated" apart from
"rated badly". The two source tables key the same membership id differently (varchar,
possibly zero-padded, vs INT), which is what mid_key exists to reconcile.
"""

import main


def performance(**points):
    # A real report row carries ~40 columns; only the 11 POINTS ones count.
    row = {"MID": "10000001", "PERFORMANCE SCORE": 99, "REGS": 12}
    row.update(points)
    return row


def feedback(*points):
    return {
        "score": sum(p for p in points if p is not None),
        "answers": {
            str(q): {"option": "x", "points": p}
            for q, p in zip(main.FEEDBACK_QUESTION_IDS, points)
        },
    }


def check():
    # Nothing on record: no score at all, not a zero.
    assert main.total_score(None, None) is None
    assert main.total_score(performance(), None) is None
    assert main.total_score(performance(), feedback()) is None

    # Performance only: half the summed points. Columns outside SCORE_POINT_COLUMNS
    # ("PERFORMANCE SCORE", "REGS") must not be counted.
    only_perf = performance(**{"POINTS (Positions)": 10, "BOOTH 15%": 6})
    assert main.total_score(only_perf, None) == 8, main.total_score(only_perf, None)

    # Feedback only: half the summed answer points.
    assert main.total_score(None, feedback(3, 5)) == 4

    # Both halves, and a missing answer contributes nothing rather than raising.
    assert main.total_score(only_perf, feedback(3, 5, None)) == 12

    # Every points column is summed, so the constant and the report must not drift.
    full = performance(**{column: 2 for column in main.SCORE_POINT_COLUMNS})
    assert main.total_score(full, None) == len(main.SCORE_POINT_COLUMNS)

    # mid_key: the varchar report and the INT leader_feedback agree on one key.
    assert main.mid_key("015067518") == main.mid_key(15067518) == "15067518"
    assert main.mid_key("#15067518") == "15067518"
    assert main.mid_key(None) == ""

    # normalize_mids keeps the caller's order and drops duplicates and blanks.
    assert main.normalize_mids(["#15067518", "20870063", "15067518", "", "  "]) == [
        "15067518",
        "20870063",
    ]

    print("test_score: ok")


if __name__ == "__main__":
    check()
