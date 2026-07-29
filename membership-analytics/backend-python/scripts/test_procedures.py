from urllib.parse import quote_plus
from sqlalchemy import create_engine, text

host = "db-projectk-prod.cluster-cxksnp5k9yp8.us-east-1.rds.amazonaws.com"
user = "root"
pwd = "w4rT1k5+1arAuFVXEBKR"
url = f"mysql+pymysql://{quote_plus(user)}:{quote_plus(pwd)}@{host}:3306/report_ratings?charset=utf8mb4"
eng = create_engine(url)

mid = "36967181"
with eng.connect() as c:
    conn = c.connection
    cur = conn.cursor()
    cur.execute("CALL cadre_performance_update(%s)", (mid,))
    while cur.nextset():
        pass
    cur.execute("CALL cadre_performance_report(%s)", (mid,))
    while cur.nextset():
        pass
    cur.close()

    details = c.execute(text("""
        SELECT * FROM cadre_details WHERE membership_id = :mid LIMIT 1
    """), {"mid": mid}).mappings().first()
    perf = c.execute(text("""
        SELECT * FROM cadre_performace_report WHERE MID = :mid LIMIT 1
    """), {"mid": mid}).mappings().first()
    print("cadre_details:", dict(details) if details else None)
    if perf:
        d = dict(perf)
        print("PERFORMANCE SCORE:", d.get("PERFORMANCE SCORE"))
        print("NO OF TIME:", d.get("NO OF TIME"))
        print("MANDAL/TOWN 7.5%:", d.get("MANDAL/TOWN 7.5%"))
