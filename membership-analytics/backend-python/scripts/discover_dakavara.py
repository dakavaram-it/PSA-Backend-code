from urllib.parse import quote_plus
from sqlalchemy import create_engine, text

host = "db-projectk-prod.cluster-cxksnp5k9yp8.us-east-1.rds.amazonaws.com"
user = "db_user03"
pwd = "9JKQbp4id!NO9j#485"
url = f"mysql+pymysql://{quote_plus(user)}:{quote_plus(pwd)}@{host}:3306/dakavara_pa?charset=utf8mb4"
eng = create_engine(url)
with eng.connect() as c:
    cols = c.execute(text("SHOW COLUMNS FROM tdp_cadre")).fetchall()
    print("=== tdp_cadre ===")
    for col in cols:
        print(col[0], col[1])
