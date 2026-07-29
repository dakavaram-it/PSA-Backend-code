-- =====================================================================
-- TDP Cases Register schema for `dakavara_pa` (MySQL 8)
-- Source: "Total Cases List ... Cleaned_Data" (12,626 cases_accused rows /
-- 2,802 FIR cases). Grain of source sheet = one row per cases_accused person.
-- =====================================================================
SET NAMES utf8mb4;

-- ---------------------------------------------------------------------
-- 0. STAGING — raw 1:1 load of Cleaned_Data, all TEXT, no constraints.
--    Lets ETL be idempotent: TRUNCATE + reload + transform.
-- ---------------------------------------------------------------------
CREATE TABLE cases_raw_stg (
  row_id              INT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  s_no                VARCHAR(32),
  `range`             VARCHAR(128),
  district            VARCHAR(128),
  parliament          VARCHAR(128),
  constituency        VARCHAR(128),
  police_station      VARCHAR(128),
  fir_no              VARCHAR(64),
  section             TEXT,
  accused_name        VARCHAR(255),
  new_status          VARCHAR(16),
  remarks             TEXT,
  type_of_disposal    VARCHAR(128),
  c_nc                VARCHAR(8),
  non_compoundable    TEXT,
  court_names         TEXT,
  head                VARCHAR(128),
  relation_type       VARCHAR(32),
  relation_name       VARCHAR(255),
  phone_number        VARCHAR(32),
  age                 VARCHAR(16),
  party_affiliation   VARCHAR(64),
  caste               VARCHAR(64),
  sub_caste           VARCHAR(64),
  occupation          VARCHAR(128),
  dno                 VARCHAR(64),
  address             TEXT,
  aadhaar_no          VARCHAR(32),
  accused_raw_details TEXT,
  designation_tags    VARCHAR(255),
  m_name              VARCHAR(255),
  m_relative_name     VARCHAR(255),
  m_relation          VARCHAR(64),
  m_mobile_no         VARCHAR(32),
  m_mid               VARCHAR(32),
  m_current_designation VARCHAR(128),
  m_relative_mobile   VARCHAR(32),
  m_relative_mid      VARCHAR(32),
  m_case_status       VARCHAR(255),
  status_police_station VARCHAR(255),
  assembly            VARCHAR(128),
  mandal              VARCHAR(128),
  town                VARCHAR(128),
  village             VARCHAR(128),
  booth               VARCHAR(32)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------
-- EXISTING dakavara_pa TABLES (CONFIRMED via DESCRIBE on prod) — REUSE.
--   tdp_cadre  (117 cols) key fields:
--       tdp_cadre_id      BIGINT PK
--       membership_id     VARCHAR(10)  nullable, NON-UNIQUE (indexed) <- MID, no '#'
--       mobile_no         VARCHAR(15)  indexed                        <- fallback match
--       first_name/last_name VARCHAR(200), relative_name VARCHAR(200),
--       relative_type VARCHAR(50), gender ENUM('M','F'), age INT,
--       date_of_birth DATE, aadhar_no VARCHAR(100),
--       occupation_id/caste_state_id/address_id BIGINT, constituency_id INT,
--       tehsil_id INT, designation_id INT, designation_name VARCHAR(50),
--       is_deleted ENUM('Y','N','H','NA','AR','T','MD','O','A','I','P')  <- filter 'N'
--   constituency (21 cols):
--       constituency_id BIGINT PK, name VARCHAR(50), constituency_no INT <- PC/AC code,
--       election_scope_id BIGINT  <- level discriminator (parliament/assembly/...),
--       parliament_id / assembly_constituency_id / district_id / state_id /
--       tehsil_id  all BIGINT (self/parent links)
--   occupation, caste_state->caste->caste_category_group, address(mandal,address1/2),
--   tehsil(tehsil_name), state, district ... (see database_table_mapping.md)
-- MATCH STRATEGY (validated against prod): strip '#' from sheet MID -> match
--   membership_id (~70% hit); fall back to last-10-digits mobile_no (~23%).
--   membership_id is non-unique -> resolve to is_deleted='N', MAX(tdp_cadre_id).
-- ---------------------------------------------------------------------
-- 1. MASTERS — REUSE EXISTING dakavara_pa TABLES. Do NOT recreate.
-- ---------------------------------------------------------------------
--   * Geography  -> existing `constituency` (self-referential hierarchy:
--       constituency_type_id discriminator + parliament_id /
--       assembly_constituency_id / district_id parent links) + `district`,
--       `state`. The TABLE sheet (PC/AC code -> parliament/assembly) is just
--       a re-statement of rows already present here; use it only to RESOLVE
--       the sheet's text names to constituency_id during ETL.
--   * Members    -> existing `tdp_cadre` (membership_id [bare, no '#'],
--       mobile_no, constituency_id, mandal/booth/village/town, is_deleted).
--       The sheet's enrichment columns (MID/mobile/assembly/mandal/booth/
--       designation) are ALREADY here — link, don't copy.
--   * Designation (Drop_Down, 98 party titles): NO confirmed existing home
--       (`position`/`board` are govt nominated-post masters, not party-org
--       titles). Create the small lookup below only if confirmed absent.
-- (optional) party_designation lookup — create ONLY if not already in DB:
-- CREATE TABLE party_designation (
--   designation_id SMALLINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
--   name           VARCHAR(128) NOT NULL UNIQUE,
--   is_ex          TINYINT(1) NOT NULL DEFAULT 0   -- "EX ..." former-role flag
-- ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------
-- 2. CASE  (one per FIR; ~2,802 rows). UNIQUE on (police_station, fir_no)
-- ---------------------------------------------------------------------
CREATE TABLE cases_fir (
  case_id            INT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  source_s_no        INT UNSIGNED,
  `range`            VARCHAR(128),
  district           VARCHAR(128),
  parliament         VARCHAR(128),
  constituency       VARCHAR(128),
  police_station     VARCHAR(128) NOT NULL,
  fir_no             VARCHAR(64)  NOT NULL,        -- apostrophe stripped
  section            TEXT,                          -- IPC sections (free text)
  crime_head         VARCHAR(128),                  -- Head: Hurt / SC-ST / 307 ...
  new_status         ENUM('D','PT','UI') NULL,      -- Disposed / Pending Trial / Under Investigation
  c_nc               ENUM('C','NC') NULL,           -- Compoundable / Non-compoundable
  non_compoundable_sections TEXT,
  type_of_disposal   VARCHAR(128),
  court_name         TEXT,
  case_status        VARCHAR(255),                  -- free text (Pursuing/Closed/...)
  status_in_ps       VARCHAR(255),
  remarks            TEXT,
  created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_fir (police_station, fir_no),
  KEY ix_parliament (parliament),
  KEY ix_constituency (constituency),
  KEY ix_status (new_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------
-- 2b. CASE_SECTION  (expansion of cases_fir.section; ~12,384 rows).
--     Derived from the raw Section cell by parse_sections.py. The raw text
--     stays on cases_fir.section as source-of-truth; this is the queryable
--     breakdown. needs_review flags cells the parser could not fully trust.
-- ---------------------------------------------------------------------
CREATE TABLE cases_section (
  case_section_id INT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  case_id      INT UNSIGNED NOT NULL,
  seq          TINYINT UNSIGNED NOT NULL,          -- order within the FIR
  section_no   VARCHAR(32)  NOT NULL,              -- '152-A', '505(2)', '3(2)(va)'
  act          VARCHAR(32),                         -- IPC / SC/ST POA Act / NDPS Act / ...
  kind         ENUM('primary','read_with') NOT NULL DEFAULT 'primary',
  needs_review TINYINT(1) NOT NULL DEFAULT 0,       -- set on whole case when parse was ambiguous
  CONSTRAINT fk_cs_case FOREIGN KEY (case_id) REFERENCES cases_fir(case_id),
  KEY ix_section (section_no, act),
  KEY ix_case (case_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- (Section 3 "party_member" removed — members live in existing tdp_cadre.)

-- ---------------------------------------------------------------------
-- 4. ACCUSED  (one per source row; ~12,626). The fact table.
--    tdp_cadre_id links the cases_accused to the matched party cadre (nullable —
--    only ~23% of rows matched). Resolved during ETL: strip '#' from the
--    sheet MID and match tdp_cadre.membership_id; fall back to mobile_no.
--    Member name/mobile/mandal/booth/designation are NOT stored here — join
--    tdp_cadre. matched_mid keeps the raw sheet value for audit.
-- ---------------------------------------------------------------------
CREATE TABLE cases_accused (
  accused_id        INT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
  case_id           INT UNSIGNED NOT NULL,
  tdp_cadre_id      BIGINT NULL,                   -- SOFT ref -> tdp_cadre.tdp_cadre_id
                                                   -- (no hard FK: prod table, non-unique MID)
  match_method      ENUM('mid','mobile','none') NOT NULL DEFAULT 'none',
  matched_mid       VARCHAR(32) NULL,              -- raw sheet MID (e.g. '#21958158'), audit
  accused_name      VARCHAR(255) NOT NULL,
  relation_type     ENUM('S/o','D/o','W/o','C/o') NULL,  -- normalized casing
  relation_name     VARCHAR(255),
  phone_number      VARCHAR(16),
  age               TINYINT UNSIGNED,
  party_affiliation VARCHAR(64),                    -- TDP / YSRCP / JSP / BJP ...
  caste             VARCHAR(64),
  sub_caste         VARCHAR(64),
  occupation        VARCHAR(128),
  door_no           VARCHAR(64),
  address           TEXT,
  aadhaar_no        VARCHAR(16),
  accused_raw_details TEXT,                         -- original unparsed blob
  designation_tags  VARCHAR(255),                   -- C29 'Designation Tags' (multi-value)
  current_designation VARCHAR(128),                 -- C35 analyst category (free text,
                                                    -- NOT a FK: own vocabulary, see notes)
  CONSTRAINT fk_acc_case  FOREIGN KEY (case_id)      REFERENCES cases_fir(case_id),
  -- no hard FK to tdp_cadre (see note above); indexed soft reference instead
  KEY ix_party (party_affiliation),
  KEY ix_caste (caste),
  KEY ix_cadre (tdp_cadre_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ---------------------------------------------------------------------
-- 5. REPORTING VIEWS  (replace PC_Abstract / AC_Abstract pivots)
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW cases_v_pc_abstract AS
SELECT c.parliament,
       COUNT(*)                                             AS total_accused,
       SUM(a.party_affiliation='TDP')                       AS tdp,
       SUM(a.party_affiliation='YSRCP')                     AS ysrcp,
       SUM(a.party_affiliation='JSP')                       AS jsp,
       SUM(a.tdp_cadre_id IS NOT NULL)                         AS matched_members
FROM cases_accused a JOIN cases_fir c ON c.case_id=a.case_id
GROUP BY c.parliament;

CREATE OR REPLACE VIEW cases_v_ac_abstract AS
SELECT c.parliament, c.constituency,
       COUNT(*) AS total_accused,
       SUM(a.party_affiliation='TDP')   AS tdp,
       SUM(a.party_affiliation='YSRCP') AS ysrcp,
       SUM(a.tdp_cadre_id IS NOT NULL)     AS matched_members
FROM cases_accused a JOIN cases_fir c ON c.case_id=a.case_id
GROUP BY c.parliament, c.constituency;
