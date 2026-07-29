"""Discover dakavara_pa and report_ratings schemas."""
from urllib.parse import quote_plus

from sqlalchemy import create_engine, text

from app.core.config import get_settings


def main():
    s = get_settings()
    print("dakavara:", s.dakavara_db_name)
    eng = create_engine(s.dakavara_url)
    with eng.connect() as c:
        cols = c.execute(text("SHOW COLUMNS FROM tdp_cadre")).fetchall()
        print("=== tdp_cadre ===")
        for col in cols:
            print(col[0])

    if not s.report_ratings_configured:
        print("report_ratings not configured")
        return

    rr = create_engine(s.report_ratings_url)
    with rr.connect() as c:
        tables = c.execute(text("SHOW TABLES")).fetchall()
        print("=== report_ratings tables ===")
        for t in tables:
            print(t[0])
        for tname in ["cadre_details", "cadre_performance_report", "cadre_performace_report"]:
            try:
                cols = c.execute(text(f"DESCRIBE {tname}")).fetchall()
                print(f"=== {tname} ===")
                for col in cols:
                    print(col[0], col[1])
            except Exception as exc:
                print(f"{tname}: {exc}")
        procs = c.execute(
            text("SHOW PROCEDURE STATUS WHERE Db = :db"),
            {"db": s.report_ratings_db_name},
        ).fetchall()
        print("=== procedures ===")
        for p in procs:
            print(p[1])


if __name__ == "__main__":
    main()
