import os
import sqlite3
from datetime import date, datetime, timedelta

from werkzeug.security import generate_password_hash


DATABASE = os.environ.get("HMS_DATABASE", "hms.sqlite3")


def get_database_path(app=None):
    if app is not None and app.config.get("DATABASE"):
        return app.config["DATABASE"]
    return os.environ.get("HMS_DATABASE", DATABASE)


def connect_db(path=None):
    conn = sqlite3.connect(path or get_database_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


SCHEMA = """
-- Users are deliberately simple for the prototype. Passwords are stored
-- as Werkzeug hashes and role checks are enforced in Flask routes.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('Administrator', 'Staff'))
);

-- Wards are gender-specific and capacity-limited. Occupancy is derived
-- from current patients, which keeps capacity checks transparent.
CREATE TABLE IF NOT EXISTS wards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    ward_type TEXT NOT NULL CHECK (ward_type IN ('Male', 'Female')),
    capacity INTEGER NOT NULL CHECK (capacity > 0)
);

-- Every team has one consultant. The seed data and validation ensure
-- each team also has at least one grade 1 junior doctor.
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    consultant_doctor_id INTEGER
);

CREATE TABLE IF NOT EXISTS doctors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    grade TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    is_consultant INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS nurses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    age INTEGER NOT NULL CHECK (age BETWEEN 0 AND 130),
    gender TEXT NOT NULL CHECK (gender IN ('Male', 'Female')),
    ward_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    admitted_at TEXT NOT NULL,
    FOREIGN KEY (ward_id) REFERENCES wards(id),
    FOREIGN KEY (team_id) REFERENCES teams(id)
);

CREATE TABLE IF NOT EXISTS treatments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id INTEGER NOT NULL,
    doctor_id INTEGER NOT NULL,
    treated_at TEXT NOT NULL,
    notes TEXT,
    FOREIGN KEY (patient_id) REFERENCES patients(id) ON DELETE CASCADE,
    FOREIGN KEY (doctor_id) REFERENCES doctors(id)
);

CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_type TEXT NOT NULL CHECK (staff_type IN ('Doctor', 'Nurse')),
    staff_id INTEGER NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    role_note TEXT
);

-- Anonymous operational events support monthly reporting after patient
-- discharge deletes the clinical/admin patient record.
CREATE TABLE IF NOT EXISTS occupancy_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL CHECK (event_type IN ('admission', 'discharge')),
    ward_id INTEGER NOT NULL,
    event_date TEXT NOT NULL,
    FOREIGN KEY (ward_id) REFERENCES wards(id)
);
"""


def init_db(path=None):
    conn = connect_db(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def seed_demo_data(path=None):
    conn = init_db(path)
    existing = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()["count"]
    if existing:
        conn.close()
        return

    conn.executemany(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
        [
            ("admin", generate_password_hash("admin123"), "Administrator"),
            ("staff", generate_password_hash("staff123"), "Staff"),
        ],
    )
    conn.executemany(
        "INSERT INTO wards (name, ward_type, capacity) VALUES (?, ?, ?)",
        [
            ("Male Surgical", "Male", 3),
            ("Female Medical", "Female", 2),
            ("Male Orthopaedics", "Male", 2),
            ("Female Paediatrics", "Female", 3),
        ],
    )
    conn.executemany(
        "INSERT INTO teams (code, name) VALUES (?, ?)",
        [
            ("ORTH-A", "Orthopaedics A"),
            ("PAEDS", "Paediatrics"),
            ("MED-B", "General Medicine B"),
        ],
    )
    teams = {r["code"]: r["id"] for r in conn.execute("SELECT id, code FROM teams")}
    doctors = [
        ("Dr Amina Cole", "Consultant", teams["ORTH-A"], 1),
        ("Dr Peter James", "Grade 1 Junior", teams["ORTH-A"], 0),
        ("Dr Lara Singh", "Grade 2 Junior", teams["ORTH-A"], 0),
        ("Dr Sofia Grant", "Consultant", teams["PAEDS"], 1),
        ("Dr Musa Bello", "Grade 1 Junior", teams["PAEDS"], 0),
        ("Dr Helen Okafor", "Consultant", teams["MED-B"], 1),
        ("Dr Noah Reed", "Grade 1 Junior", teams["MED-B"], 0),
    ]
    conn.executemany(
        "INSERT INTO doctors (full_name, grade, team_id, is_consultant) VALUES (?, ?, ?, ?)",
        doctors,
    )
    for row in conn.execute("SELECT id, team_id FROM doctors WHERE is_consultant = 1"):
        conn.execute("UPDATE teams SET consultant_doctor_id = ? WHERE id = ?", (row["id"], row["team_id"]))
    conn.executemany("INSERT INTO nurses (full_name) VALUES (?)", [("Nurse Ada Stone",), ("Nurse Ben Cross",)])

    wards = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM wards")}
    today = date.today().isoformat()

    # Patients spread across all four wards. Male Surgical and Female Medical
    # are filled to capacity on purpose, so you can demo the "ward full"
    # rejection live. Male Orthopaedics and Female Paediatrics are left with
    # one free bed each, so you can demo a *successful* live admission too.
    patients = [
        ("John Carter", 45, "Male", wards["Male Surgical"], teams["ORTH-A"], today),
        ("David Okoro", 52, "Male", wards["Male Surgical"], teams["MED-B"], today),
        ("Ibrahim Khan", 61, "Male", wards["Male Surgical"], teams["ORTH-A"], today),
        ("Mary Evans", 32, "Female", wards["Female Medical"], teams["MED-B"], today),
        ("Grace Adebayo", 29, "Female", wards["Female Medical"], teams["MED-B"], today),
        ("Tunde Bakare", 38, "Male", wards["Male Orthopaedics"], teams["ORTH-A"], today),
        ("Amara Chukwu", 7, "Female", wards["Female Paediatrics"], teams["PAEDS"], today),
        ("Zainab Musa", 5, "Female", wards["Female Paediatrics"], teams["PAEDS"], today),
    ]
    conn.executemany(
        "INSERT INTO patients (full_name, age, gender, ward_id, team_id, admitted_at) VALUES (?, ?, ?, ?, ?, ?)",
        patients,
    )

    # Log an admission event for every seeded patient so ward occupancy
    # figures and the monthly report agree with each other from first run.
    for p in patients:
        conn.execute(
            "INSERT INTO occupancy_events (event_type, ward_id, event_date) VALUES ('admission', ?, ?)",
            (p[3], today),
        )

    # Two synthetic discharge events earlier this month, so the monthly
    # report shows non-zero discharge figures without you having to
    # discharge any of the seeded (still-present) patients yourself.
    earlier_this_month = date.today().replace(day=max(1, date.today().day - 5)).isoformat()
    conn.executemany(
        "INSERT INTO occupancy_events (event_type, ward_id, event_date) VALUES ('discharge', ?, ?)",
        [
            (wards["Male Surgical"], earlier_this_month),
            (wards["Female Paediatrics"], earlier_this_month),
        ],
    )

    doctors_by_name = {r["full_name"]: r["id"] for r in conn.execute("SELECT id, full_name FROM doctors")}
    patients_by_name = {r["full_name"]: r["id"] for r in conn.execute("SELECT id, full_name FROM patients")}

    treatments = [
        ("John Carter", "Dr Amina Cole", "09:00"),
        ("John Carter", "Dr Peter James", "14:30"),
        ("Ibrahim Khan", "Dr Amina Cole", "10:15"),
        ("Tunde Bakare", "Dr Lara Singh", "11:00"),
        ("David Okoro", "Dr Helen Okafor", "09:45"),
        ("Mary Evans", "Dr Helen Okafor", "10:30"),
        ("Grace Adebayo", "Dr Noah Reed", "13:00"),
        ("Amara Chukwu", "Dr Sofia Grant", "09:30"),
        ("Zainab Musa", "Dr Musa Bello", "15:00"),
    ]
    conn.executemany(
        "INSERT INTO treatments (patient_id, doctor_id, treated_at, notes) VALUES (?, ?, ?, ?)",
        [
            (patients_by_name[pname], doctors_by_name[dname], f"{today}T{time}", "Routine review")
            for pname, dname, time in treatments
        ],
    )

    # A few clean, non-overlapping roster shifts for background realism.
    # Leave the overlap/over-hours warning to be demonstrated live.
    tomorrow = datetime.combine(date.today() + timedelta(days=1), datetime.min.time())
    day_after = datetime.combine(date.today() + timedelta(days=2), datetime.min.time())
    shifts = [
        ("Doctor", doctors_by_name["Dr Amina Cole"], tomorrow.replace(hour=8), tomorrow.replace(hour=16), "Ward round"),
        ("Doctor", doctors_by_name["Dr Peter James"], tomorrow.replace(hour=8), tomorrow.replace(hour=16), "Ward round"),
        ("Doctor", doctors_by_name["Dr Helen Okafor"], day_after.replace(hour=9), day_after.replace(hour=17), "Clinic cover"),
        ("Nurse", 1, tomorrow.replace(hour=7), tomorrow.replace(hour=15), "Early shift"),
        ("Nurse", 2, tomorrow.replace(hour=15), tomorrow.replace(hour=23), "Late shift"),
    ]
    conn.executemany(
        "INSERT INTO shifts (staff_type, staff_id, starts_at, ends_at, role_note) VALUES (?, ?, ?, ?, ?)",
        [(st, sid, start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes"), note)
         for st, sid, start, end, note in shifts],
    )

    conn.commit()
    conn.close()
