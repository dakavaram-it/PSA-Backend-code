from urllib.parse import quote_plus
from sqlalchemy import create_engine, text

host = "db-projectk-prod.cluster-cxksnp5k9yp8.us-east-1.rds.amazonaws.com"
user = "root"
pwd = "w4rT1k5+1arAuFVXEBKR"
url = f"mysql+pymysql://{quote_plus(user)}:{quote_plus(pwd)}@{host}:3306/report_ratings?charset=utf8mb4"
eng = create_engine(url)

with eng.connect() as c:
    cnt1 = c.execute(text("SELECT COUNT(*) FROM cadre_details")).scalar()
    cnt2 = c.execute(text("SELECT COUNT(*) FROM cadre_performace_report")).scalar()
    print("cadre_details count:", cnt1)
    print("performance count:", cnt2)
    row = c.execute(text("SELECT membership_id FROM cadre_details LIMIT 1")).first()
    print("sample membership:", row)
    if row:
        mid = row[0]
        perf = c.execute(text("SELECT * FROM cadre_performace_report WHERE MID = :mid"), {"mid": mid}).mappings().first()
        if perf:
            for k, v in dict(perf).items():
                print(f"{k}: {v}")
