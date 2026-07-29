from urllib.parse import quote_plus
from sqlalchemy import create_engine, text

host = "db-projectk-prod.cluster-cxksnp5k9yp8.us-east-1.rds.amazonaws.com"
user = "db_user03"
pwd = "9JKQbp4id!NO9j#485"
url = f"mysql+pymysql://{quote_plus(user)}:{quote_plus(pwd)}@{host}:3306/dakavara_pa?charset=utf8mb4"
eng = create_engine(url)

with eng.connect() as c:
    tables = c.execute(text("SHOW TABLES LIKE '%renew%'")).fetchall()
    print("renew tables:", [t[0] for t in tables])
    tables2 = c.execute(text("SHOW TABLES LIKE '%membership%'")).fetchall()
    print("membership tables:", [t[0] for t in tables2[:20]])
