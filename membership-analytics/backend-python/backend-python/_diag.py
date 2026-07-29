import time, pymysql
conn = pymysql.connect(
    host="db-projectk-prod.cluster-cxksnp5k9yp8.us-east-1.rds.amazonaws.com",
    port=3306, user="db_user03", password="9JKQbp4id!NO9j#485",
    database="dakavara_pa", connect_timeout=15, read_timeout=180)
cur = conn.cursor()

def t(label, sql, args=None):
    s = time.time()
    cur.execute(sql, args or ())
    rows = cur.fetchall()
    print(f"{label}: {time.time()-s:.2f}s  rows={len(rows)}  sample={rows[:2]}", flush=True)
    return rows

t("count tdp_committee", "SELECT COUNT(*) FROM tdp_committee")
t("count tdp_committee_role", "SELECT COUNT(*) FROM tdp_committee_role")
t("count tdp_committee_member", "SELECT COUNT(*) FROM tdp_committee_member")
t("count tdp_committee_member active", "SELECT COUNT(*) FROM tdp_committee_member WHERE is_active='Y'")
conn.close()
