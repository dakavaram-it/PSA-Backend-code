from urllib.parse import quote_plus
from sqlalchemy import create_engine, text

host = "db-projectk-prod.cluster-cxksnp5k9yp8.us-east-1.rds.amazonaws.com"
user = "db_user03"
pwd = "9JKQbp4id!NO9j#485"
url = f"mysql+pymysql://{quote_plus(user)}:{quote_plus(pwd)}@{host}:3306/dakavara_pa?charset=utf8mb4"
eng = create_engine(url)

with eng.connect() as c:
    row = c.execute(text("""
        SELECT membership_id FROM tdp_cadre
        WHERE is_deleted='N' AND membership_id IS NOT NULL AND membership_id != ''
        LIMIT 1
    """)).first()
    mid = row[0] if row else None
    print("mid:", mid)

    for t in ["caste", "caste_category_group"]:
        try:
            cols = c.execute(text(f"SHOW COLUMNS FROM {t}")).fetchall()
            print(t, [col[0] for col in cols[:8]])
        except Exception as exc:
            print(t, exc)

    if mid:
        sql = text("""
            SELECT
                CR.tdp_cadre_id AS tdpCadreId,
                CR.membership_id AS membershipId,
                CR.membership_no AS membershipNo,
                TRIM(CONCAT(COALESCE(CR.first_name,''),' ',COALESCE(CR.last_name,''))) AS candidateName,
                CR.mobile_no AS mobileNo,
                CR.gender AS gender,
                CR.age AS age,
                CR.date_of_birth AS dob,
                OCC.occupation AS occupation,
                CR.designation_name AS designation,
                CR.caste_state_id AS casteStateId,
                C.caste_name AS casteName,
                CCG.caste_category_group_name AS castCategory,
                CON.address1 AS village,
                CON.mandal AS mandal,
                AC.name AS assembly,
                PC.name AS parliament,
                CR.image AS photo
            FROM tdp_cadre CR
            LEFT JOIN occupation OCC ON CR.occupation_id = OCC.occupation_id
            LEFT JOIN caste_state CS ON CR.caste_state_id = CS.caste_state_id
            LEFT JOIN caste C ON CS.caste_id = C.caste_id
            LEFT JOIN caste_category_group CCG ON CS.caste_category_group_id = CCG.caste_category_group_id
            LEFT JOIN address CON ON CR.address_id = CON.address_id
            LEFT JOIN constituency CONS ON CR.constituency_id = CONS.constituency_id
            LEFT JOIN constituency AC ON CONS.assembly_constituency_id = AC.constituency_id
            LEFT JOIN constituency PC ON CONS.parliament_id = PC.constituency_id
            WHERE CR.is_deleted = 'N' AND CR.membership_id = :mid
            LIMIT 1
        """)
        sample = c.execute(sql, {"mid": mid}).mappings().first()
        print("profile:", dict(sample) if sample else None)

        # renewal count - search tables
        renew = c.execute(text("""
            SELECT COUNT(*) FROM tdp_cadre_renewal TR
            WHERE TR.tdp_cadre_id = (SELECT tdp_cadre_id FROM tdp_cadre WHERE membership_id=:mid LIMIT 1)
        """), {"mid": mid}).scalar()
        print("renewals:", renew)
