"""
Section parser for the TDP Cases Register.

Strategy (dictionary-anchored, raw-preserving, confidence-flagged):
  - Scan left-to-right. Accumulate section tokens until an Act keyword is hit,
    then assign the accumulated tokens to that Act (Indian notation puts the
    Act *after* its sections: "342, 365 r/w 34 IPC").
  - "r/w" / "R/W" marks the tokens that follow as read_with connectors.
  - Anything left unattributed or matching a garbage pattern -> needs_review.
  - The raw cell is never discarded; this only produces a derived expansion.
"""
import re

# Act aliases -> canonical name. Order matters: match longer/more-specific first.
ACT_ALIASES = [
    (r'SC\s*/?\s*ST(?:\s*\(?POA\)?)?(?:\s*ATROCITIES)?\s*(?:POA\s*)?ACT', 'SC/ST POA Act'),
    (r'\bSCST\s*POA\s*ACT', 'SC/ST POA Act'),
    (r'\bPOCSO\s*ACT', 'POCSO Act'),
    (r'\bNDPS\s*ACT', 'NDPS Act'),
    (r'\bARMS\s*ACT', 'Arms Act'),
    (r'\bEXPLOSIVES?\s*ACT', 'Explosives Act'),
    (r'\bEXCISE\s*ACT', 'Excise Act'),
    (r'\bGAMING\s*ACT', 'Gaming Act'),
    (r'\bI\.?T\.?\s*ACT', 'IT Act'),
    (r'\bM\.?V\.?\s*ACT|MOTOR\s*VEHICLES?\s*ACT', 'MV Act'),
    (r'\bP\.?D\.?\s*ACT|PROHIBITION\s*ACT', 'PD Act'),
    (r'\bCR\.?\s*P\.?\s*C', 'CrPC'),
    (r'\bB\.?N\.?S\.?S', 'BNSS'),
    (r'\bB\.?N\.?S\b', 'BNS'),
    (r'\bI\.?P\.?C\b', 'IPC'),
]
ACT_RE = re.compile('|'.join('(%s)' % p for p, _ in ACT_ALIASES), re.I)

# A section token: number, optional letter suffix (152-A), optional sub-clauses 505(2), 3(2)(va)
SECTION_RE = re.compile(r'\d+\s*[-–]?\s*[A-Za-z]?(?:\s*\([0-9A-Za-z]+\))*')

GARBAGE_RE = re.compile(r'add\s+section|section\s+add|not\s+available|n/?a\b', re.I)


def _canon_act(fragment):
    m = ACT_RE.search(fragment)
    if not m:
        return None
    matched = m.group(0)
    for pat, name in ACT_ALIASES:
        if re.fullmatch(pat, matched, re.I) or re.search(pat, matched, re.I):
            return name
    return None


def parse_sections(raw):
    """Return (list_of_dicts, needs_review_bool)."""
    if raw is None:
        return [], False
    s = re.sub(r'\s+', ' ', str(raw)).strip()
    needs_review = bool(GARBAGE_RE.search(s))

    # Drop leading "U/S", "Sec", "Section", "of" noise (but keep r/w + act words)
    work = re.sub(r'(?i)\bU\s*/?\s*S\b|\bSec(?:tion)?\.?\b|\bof\b', ' ', s)

    # Single forward pass: each section token binds to the NEXT Act keyword
    # ahead of it ("341, 506 r/w 34 IPC" -> 341/506/34 all IPC). An "r/w" toggle
    # flips subsequent tokens to read_with. When an Act is hit, flush the buffer.
    # Build an ordered event stream of: section tokens, 'RW' markers, Act spans.
    events = []
    for m in re.finditer(r'(?i)(\br/?w\b)|(' + '|'.join(p for p, _ in ACT_ALIASES) + r')', work):
        events.append((m.start(), m.end(), 'RW' if m.group(1) else 'ACT', m.group(0)))
    events.sort()

    out, buffer = [], []
    rw_mode = False
    cursor = 0

    def grab_sections(seg, rw):
        for sm in SECTION_RE.finditer(seg):
            tok = re.sub(r'\s+', '', sm.group(0)).strip('-–')
            if tok:
                buffer.append({'section_no': tok, 'kind': 'read_with' if rw else 'primary'})

    for start, end, kind, text in events:
        grab_sections(work[cursor:start], rw_mode)
        cursor = end
        if kind == 'RW':
            rw_mode = True
        else:  # ACT -> flush everything buffered so far to this act
            act = _canon_act(text)
            for b in buffer:
                b['act'] = act
            out.extend(buffer)
            buffer = []
    grab_sections(work[cursor:], rw_mode)

    # Any leftover buffer had no Act ahead of it -> resolve to the only/last act seen
    last_act = next((r['act'] for r in reversed(out) if r.get('act')), None)
    for b in buffer:
        b['act'] = last_act
        if last_act is None:
            needs_review = True
        out.append(b)

    # de-dup (section_no, act, kind) preserving order
    seen, dedup = set(), []
    for r in out:
        k = (r['section_no'], r['act'], r['kind'])
        if k not in seen:
            seen.add(k); dedup.append(r)
    for seq, r in enumerate(dedup, 1):
        r['seq'] = seq
    if not dedup:
        needs_review = True
    return dedup, needs_review


if __name__ == '__main__':
    import sys, openpyxl
    from collections import Counter
    f = sys.argv[1] if len(sys.argv) > 1 else \
        '/Users/rakesh/Downloads/Total Cases List as on 01-05-2026_split_accused_designation_sheets_mapped (1).xlsx'
    wb = openpyxl.load_workbook(f, data_only=True)
    ws = wb['Cleaned_Data']
    H = [str(ws.cell(1, c).value).replace('\n', ' ') for c in range(1, ws.max_column + 1)]
    ci_ps, ci_fir, ci_sec = H.index('Police Station') + 1, H.index('FIR No') + 1, H.index('Section') + 1

    seen, cases = set(), 0
    review, total_sections, act_counter = 0, 0, Counter()
    samples = []
    for r in range(2, ws.max_row + 1):
        key = (str(ws.cell(r, ci_ps).value).strip(),
               str(ws.cell(r, ci_fir).value).strip("'").strip())
        if key in seen:
            continue
        seen.add(key)
        sec = ws.cell(r, ci_sec).value
        if sec is None or not str(sec).strip():
            continue
        cases += 1
        rows, nr = parse_sections(sec)
        total_sections += len(rows)
        review += int(nr)
        for x in rows:
            act_counter[x['act'] or '(unresolved)'] += 1
        if len(samples) < 6:
            samples.append((key, str(sec).replace('\n', ' '), rows, nr))

    print('Cases with a Section value : %d' % cases)
    print('Total expanded case_section rows: %d (avg %.2f/case)' % (total_sections, total_sections / cases))
    print('Cases flagged needs_review : %d (%.1f%%)' % (review, 100 * review / cases))
    print('\nAct distribution:')
    for a, n in act_counter.most_common():
        print('   %5d  %s' % (n, a))
    print('\nSamples:')
    for key, raw, rows, nr in samples:
        print('  %s @ %s  review=%s' % (key[1], key[0], nr))
        print('     raw: %s' % raw)
        print('     ->  ' + '  '.join('[%s/%s/%s]' % (x['section_no'], x['act'], x['kind']) for x in rows))
