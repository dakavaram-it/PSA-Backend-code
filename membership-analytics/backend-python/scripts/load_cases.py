"""
ETL: xlsx 'Cleaned_Data' -> cases_raw_stg -> cases_fir / cases_section / cases_accused
(all in dakavara_pa). Cadre link resolved via membership_id (MID, '#' stripped),
falling back to last-10-digit mobile_no, picking is_deleted='N' / MAX(tdp_cadre_id).

DRY-RUN by default: computes every count + match rate and prints sample
transformed records, writing NOTHING. Pass --commit to actually load.

Usage:
  python load_cases.py                # dry-run
  python load_cases.py --commit       # load for real (truncates cases_* first)
"""
import sys, re, argparse
import pymysql, openpyxl
from collections import defaultdict, Counter
from parse_sections import parse_sections

XLSX = '/Users/rakesh/Downloads/Total Cases List as on 01-05-2026_split_accused_designation_sheets_mapped (1).xlsx'
DB = dict(host='db-projectk-prod.cluster-cxksnp5k9yp8.us-east-1.rds.amazonaws.com',
          port=3306, user='root', password='w4rT1k5+1arAuFVXEBKR', db='dakavara_pa',
          connect_timeout=20, read_timeout=120, charset='utf8mb4')

NEW_STATUS = {'D', 'PT', 'UI'}
CNC = {'C', 'NC'}
REL_MAP = {'s/o': 'S/o', 'd/o': 'D/o', 'w/o': 'W/o', 'c/o': 'C/o'}


# ---------- cleaning helpers ----------
def s(v):
    if v is None:
        return None
    t = str(v).strip()
    return t or None


def clean_fir(v):
    t = s(v)
    return t.lstrip("'").strip() if t else None


def clean_mid(v):
    t = s(v)
    if not t:
        return None
    t = re.sub(r'[^0-9A-Za-z]', '', t)
    return None if not t or t.upper() == 'NA' else t


def clean_mobile(v):
    t = s(v)
    if not t:
        return None
    d = re.sub(r'\D', '', t)
    return d[-10:] if len(d) >= 10 else None


def clean_age(v):
    t = s(v)
    if not t:
        return None
    m = re.match(r'\d{1,3}', t)
    n = int(m.group(0)) if m else None
    return n if n and 0 < n < 120 else None


def clean_enum(v, allowed):
    t = s(v)
    return t if t in allowed else None


def clean_rel(v):
    t = s(v)
    return REL_MAP.get(t.lower(), None) if t else None


def clean_str(v, n):
    t = s(v)
    if t is None:
        return None
    if t in ('False', 'True', '#REF!', '0', '0.0'):
        return None
    return t[:n]


# ---------- load + transform ----------
def build_records():
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb['Cleaned_Data']
    H = [str(ws.cell(1, c).value).replace('\n', ' ').strip() for c in range(1, ws.max_column + 1)]
    idx = {h: i for i, h in enumerate(H)}

    def col(row, name):
        return row[idx[name]] if name in idx else None

    cases = {}          # (ps, fir) -> case dict
    case_order = []
    accused_rows = []   # dicts referencing case key
    for r in range(2, ws.max_row + 1):
        row = [ws.cell(r, c).value for c in range(1, ws.max_column + 1)]
        ps = clean_str(col(row, 'Police Station'), 128)
        fir = clean_fir(col(row, 'FIR No'))
        if not ps or not fir:
            continue
        key = (ps, fir)
        if key not in cases:
            cases[key] = dict(
                source_s_no=clean_age(col(row, 'S. No')) or None,
                range=clean_str(col(row, 'Range'), 128),
                district=clean_str(col(row, 'District'), 128),
                parliament=clean_str(col(row, 'Parliament'), 128),
                constituency=clean_str(col(row, 'Constituency'), 128),
                police_station=ps, fir_no=fir,
                section=s(col(row, 'Section')),
                crime_head=clean_str(col(row, 'Head'), 128),
                new_status=clean_enum(col(row, 'New_Status'), NEW_STATUS),
                c_nc=clean_enum(col(row, 'C/NC'), CNC),
                non_compoundable_sections=s(col(row, 'Non-Compoundable Sections')),
                type_of_disposal=clean_str(col(row, 'Type of Disposal'), 128),
                court_name=s(col(row, 'Court Names')),
                case_status=clean_str(col(row, 'Case Status'), 255),
                status_in_ps=clean_str(col(row, 'status in Police Station'), 255),
                remarks=s(col(row, 'Remarks')),
            )
            case_order.append(key)
        accused_rows.append(dict(
            case_key=key,
            accused_name=clean_str(col(row, 'Accused Name'), 255) or 'UNKNOWN',
            relation_type=clean_rel(col(row, 'Relation Type')),
            relation_name=clean_str(col(row, 'Relation Name'), 255),
            phone_number=clean_mobile(col(row, 'Phone Number')),
            age=clean_age(col(row, 'Age')),
            party_affiliation=clean_str(col(row, 'Party Affiliation'), 64),
            caste=clean_str(col(row, 'Caste'), 64),
            sub_caste=clean_str(col(row, 'Sub caste'), 64),
            occupation=clean_str(col(row, 'Occupation'), 128),
            door_no=clean_str(col(row, 'DNo'), 64),
            address=s(col(row, 'Address')),
            aadhaar_no=clean_str(col(row, 'Aadhaar No'), 16),
            accused_raw_details=s(col(row, 'Accused Raw Details')),
            designation_tags=clean_str(col(row, 'Designation Tags'), 255),
            current_designation=clean_str(col(row, 'Accused Current Designation'), 128),
            raw_mid=s(col(row, 'Accused MID')),
            mid=clean_mid(col(row, 'Accused MID')),
            mobile=clean_mobile(col(row, 'Accused MOBILE NO')),
        ))
    return cases, case_order, accused_rows


def resolve_cadres(conn, accused_rows):
    """Return mid->tdp_cadre_id and mobile->tdp_cadre_id (active, most recent)."""
    mids = {a['mid'] for a in accused_rows if a['mid']}
    mobs = {a['mobile'] for a in accused_rows if a['mobile']}
    cur = conn.cursor()

    def lookup(values, col):
        out = {}
        vals = list(values)
        for i in range(0, len(vals), 800):
            chunk = vals[i:i + 800]
            ph = ','.join(['%s'] * len(chunk))
            cur.execute(
                "SELECT %s AS k, MAX(tdp_cadre_id) AS id FROM tdp_cadre "
                "WHERE is_deleted='N' AND %s IN (%s) GROUP BY %s" % (col, col, ph, col), chunk)
            for row in cur.fetchall():
                out[str(row[0])] = row[1]
        return out

    return lookup(mids, 'membership_id'), lookup(mobs, 'mobile_no')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--commit', action='store_true', help='actually write (default dry-run)')
    args = ap.parse_args()
    DRY = not args.commit

    cases, case_order, accused_rows = build_records()
    conn = pymysql.connect(**DB)
    mid_map, mob_map = resolve_cadres(conn, accused_rows)

    # attach cadre match
    mm = Counter()
    for a in accused_rows:
        cid, method = None, 'none'
        if a['mid'] and a['mid'] in mid_map:
            cid, method = mid_map[a['mid']], 'mid'
        elif a['mobile'] and a['mobile'] in mob_map:
            cid, method = mob_map[a['mobile']], 'mobile'
        a['tdp_cadre_id'], a['match_method'] = cid, method
        mm[method] += 1

    # parse sections per case
    sec_rows = defaultdict(list)
    review_cases = 0
    total_sections = 0
    for key in case_order:
        parsed, nr = parse_sections(cases[key]['section'])
        if nr:
            review_cases += 1
        for p in parsed:
            sec_rows[key].append((p['seq'], p['section_no'][:32], (p['act'] or None), p['kind'], 1 if nr else 0))
            total_sections += 1

    # -------- report --------
    print("=" * 64)
    print("ETL %s" % ("DRY-RUN (no writes)" if DRY else "COMMIT"))
    print("=" * 64)
    print("cases_raw_stg rows (accused rows) : %d" % len(accused_rows))
    print("cases_fir      (distinct FIRs)    : %d" % len(cases))
    print("cases_section  (parsed sections)  : %d  | review-flagged cases: %d (%.1f%%)" % (
        total_sections, review_cases, 100 * review_cases / max(1, len(cases))))
    print("cases_accused  rows               : %d" % len(accused_rows))
    print("\ncadre match: mid=%d  mobile=%d  none=%d  (linked %.1f%%)" % (
        mm['mid'], mm['mobile'], mm['none'],
        100 * (mm['mid'] + mm['mobile']) / max(1, len(accused_rows))))

    print("\n--- sample transformed FIR + accused ---")
    k0 = case_order[0]
    c0 = cases[k0]
    print("FIR:", {x: c0[x] for x in ('police_station', 'fir_no', 'new_status', 'crime_head', 'parliament')})
    print("sections:", sec_rows[k0])
    for a in [x for x in accused_rows if x['case_key'] == k0][:3]:
        print("  accused:", a['accused_name'], "| party:", a['party_affiliation'],
              "| cadre:", a['tdp_cadre_id'], "(%s)" % a['match_method'])

    if DRY:
        print("\nDRY-RUN complete — pass --commit to load.")
        conn.close()
        return

    # -------- commit --------
    cur = conn.cursor()
    print("\nTruncating cases_* and loading...")
    cur.execute("SET FOREIGN_KEY_CHECKS=0")
    for t in ('cases_section', 'cases_accused', 'cases_fir', 'cases_raw_stg'):
        cur.execute("TRUNCATE TABLE %s" % t)
    cur.execute("SET FOREIGN_KEY_CHECKS=1")

    case_id = {}
    fir_cols = ('source_s_no', 'range', 'district', 'parliament', 'constituency', 'police_station',
                'fir_no', 'section', 'crime_head', 'new_status', 'c_nc', 'non_compoundable_sections',
                'type_of_disposal', 'court_name', 'case_status', 'status_in_ps', 'remarks')
    fir_sql = "INSERT INTO cases_fir (`range`,district,parliament,constituency,police_station,fir_no," \
        "section,crime_head,new_status,c_nc,non_compoundable_sections,type_of_disposal,court_name," \
        "case_status,status_in_ps,remarks,source_s_no) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    for key in case_order:
        c = cases[key]
        cur.execute(fir_sql, (c['range'], c['district'], c['parliament'], c['constituency'],
            c['police_station'], c['fir_no'], c['section'], c['crime_head'], c['new_status'],
            c['c_nc'], c['non_compoundable_sections'], c['type_of_disposal'], c['court_name'],
            c['case_status'], c['status_in_ps'], c['remarks'], c['source_s_no']))
        case_id[key] = cur.lastrowid

    sec_sql = "INSERT INTO cases_section (case_id,seq,section_no,act,kind,needs_review) VALUES (%s,%s,%s,%s,%s,%s)"
    sec_batch = [(case_id[k], seq, sn, act, kind, nr) for k in case_order for (seq, sn, act, kind, nr) in sec_rows[k]]
    cur.executemany(sec_sql, sec_batch)

    acc_sql = "INSERT INTO cases_accused (case_id,tdp_cadre_id,match_method,matched_mid,accused_name," \
        "relation_type,relation_name,phone_number,age,party_affiliation,caste,sub_caste,occupation," \
        "door_no,address,aadhaar_no,accused_raw_details,designation_tags,current_designation) " \
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    acc_batch = [(case_id[a['case_key']], a['tdp_cadre_id'], a['match_method'], a['raw_mid'],
        a['accused_name'], a['relation_type'], a['relation_name'], a['phone_number'], a['age'],
        a['party_affiliation'], a['caste'], a['sub_caste'], a['occupation'], a['door_no'],
        a['address'], a['aadhaar_no'], a['accused_raw_details'], a['designation_tags'],
        a['current_designation']) for a in accused_rows]
    cur.executemany(acc_sql, acc_batch)

    conn.commit()
    for t in ('cases_fir', 'cases_section', 'cases_accused'):
        cur.execute("SELECT COUNT(*) FROM %s" % t)
        print("  %-14s -> %d rows" % (t, cur.fetchone()[0]))
    conn.close()
    print("COMMIT complete.")


if __name__ == '__main__':
    main()
