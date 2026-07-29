from urllib.parse import quote_plus
from sqlalchemy import create_engine, text

host = "db-projectk-prod.cluster-cxksnp5k9yp8.us-east-1.rds.amazonaws.com"
user = "db_user03"
pwd = "9JKQbp4id!NO9j#485"
url = f"mysql+pymysql://{quote_plus(user)}:{quote_plus(pwd)}@{host}:3306/dakavara_pa?charset=utf8mb4"
eng = create_engine(url)

TABLES = [
    "caste_state", "occupation", "constituency", "address", "tdp_cadre_location",
    "mandal", "village", "tehsil", "membership_renewal", "cadre_renewal",
]

with eng.connect() as c:
    for t in TABLES:
        try:
            cols = c.execute(text(f"SHOW COLUMNS FROM {t}")).fetchall()
            print(f"=== {t} ===")
            for col in cols[:20]:
                print(" ", col[0], col[1])
        except Exception as exc:
            print(f"{t}: {exc}")

    # sample join query test
    mid = "117742"
    row = c.execute(text("""
        SELECT CR.tdp_cadre_id, CR.membership_id, CR.membership_no, CR.first_name, CR.last_name,
               CR.mobile_no, CR.gender, CR.age, CR.date_of_birth, CR.image, CR.caste_state_id,
               CR.designation_name, CR.constituency_id, CR.occupation_id
        FROM tdp_cadre CR
        WHERE CR.membership_id = :mid AND CR.is_deleted = 'N'
        LIMIT 1
    """), {"mid": mid}).mappings().first()
    print("sample:", dict(row) if row else None)
