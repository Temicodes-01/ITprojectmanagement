from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from db import connect_db, get_database_path, seed_demo_data


app = Flask(__name__)
app.config.update(SECRET_KEY="dev-only-change-me", DATABASE=get_database_path())


def get_db():
    if "db" not in g:
        g.db = connect_db(app.config["DATABASE"])
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("role") != "Administrator":
            flash("Administrators only.", "warning")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


def ward_availability():
    return get_db().execute(
        """
        SELECT w.*, COUNT(p.id) AS occupied, w.capacity - COUNT(p.id) AS free_beds
        FROM wards w
        LEFT JOIN patients p ON p.ward_id = w.id
        GROUP BY w.id
        ORDER BY w.name
        """
    ).fetchall()


def validate_team_has_grade1(team_id):
    row = get_db().execute(
        "SELECT COUNT(*) AS count FROM doctors WHERE team_id = ? AND grade = 'Grade 1 Junior'",
        (team_id,),
    ).fetchone()
    return row["count"] > 0


def admit_patient(full_name, age, gender, ward_id, team_id):
    db = get_db()
    ward = db.execute(
        """
        SELECT w.*, COUNT(p.id) AS occupied
        FROM wards w LEFT JOIN patients p ON p.ward_id = w.id
        WHERE w.id = ?
        GROUP BY w.id
        """,
        (ward_id,),
    ).fetchone()
    if not ward:
        raise ValueError("Selected ward does not exist.")
    if ward["ward_type"] != gender:
        raise ValueError("Patient gender must match the ward type.")
    if ward["occupied"] >= ward["capacity"]:
        raise ValueError("Selected ward is full.")
    if not validate_team_has_grade1(team_id):
        raise ValueError("Selected team must include at least one grade 1 junior doctor.")
    admitted_at = datetime.now().date().isoformat()
    cur = db.execute(
        "INSERT INTO patients (full_name, age, gender, ward_id, team_id, admitted_at) VALUES (?, ?, ?, ?, ?, ?)",
        (full_name.strip(), int(age), gender, ward_id, team_id, admitted_at),
    )
    db.execute(
        "INSERT INTO occupancy_events (event_type, ward_id, event_date) VALUES ('admission', ?, ?)",
        (ward_id, admitted_at),
    )
    db.commit()
    return cur.lastrowid


def transfer_patient(patient_id, ward_id):
    db = get_db()
    patient = db.execute("SELECT * FROM patients WHERE id = ?", (patient_id,)).fetchone()
    ward = db.execute(
        """
        SELECT w.*, COUNT(p.id) AS occupied
        FROM wards w LEFT JOIN patients p ON p.ward_id = w.id
        WHERE w.id = ?
        GROUP BY w.id
        """,
        (ward_id,),
    ).fetchone()
    if not patient or not ward:
        raise ValueError("Patient or ward not found.")
    if ward["ward_type"] != patient["gender"]:
        raise ValueError("New ward type must match the patient's gender.")
    occupied = ward["occupied"] - (1 if patient["ward_id"] == ward["id"] else 0)
    if occupied >= ward["capacity"]:
        raise ValueError("New ward is full.")
    db.execute("UPDATE patients SET ward_id = ? WHERE id = ?", (ward_id, patient_id))
    db.commit()


def record_treatment(patient_id, doctor_id, treated_at, notes):
    db = get_db()
    row = db.execute(
        """
        SELECT p.team_id AS patient_team_id, d.team_id AS doctor_team_id
        FROM patients p CROSS JOIN doctors d
        WHERE p.id = ? AND d.id = ?
        """,
        (patient_id, doctor_id),
    ).fetchone()
    if not row:
        raise ValueError("Patient or doctor not found.")
    if row["patient_team_id"] != row["doctor_team_id"]:
        raise ValueError("Only doctors in the patient's assigned team may record treatment.")
    db.execute(
        "INSERT INTO treatments (patient_id, doctor_id, treated_at, notes) VALUES (?, ?, ?, ?)",
        (patient_id, doctor_id, treated_at, notes.strip()),
    )
    db.commit()


def discharge_patient(patient_id):
    db = get_db()
    patient = db.execute("SELECT ward_id FROM patients WHERE id = ?", (patient_id,)).fetchone()
    if not patient:
        raise ValueError("Patient not found.")
    today = datetime.now().date().isoformat()
    db.execute(
        "INSERT INTO occupancy_events (event_type, ward_id, event_date) VALUES ('discharge', ?, ?)",
        (patient["ward_id"], today),
    )
    db.execute("DELETE FROM patients WHERE id = ?", (patient_id,))
    db.commit()


def roster_warnings(staff_type, staff_id, starts_at, ends_at, ignore_shift_id=None):
    db = get_db()
    warnings = []
    params = [staff_type, staff_id, ends_at, starts_at]
    extra = ""
    if ignore_shift_id:
        extra = "AND id != ?"
        params.append(ignore_shift_id)
    overlap = db.execute(
        f"""
        SELECT COUNT(*) AS count FROM shifts
        WHERE staff_type = ? AND staff_id = ? AND starts_at < ? AND ends_at > ? {extra}
        """,
        params,
    ).fetchone()["count"]
    if overlap:
        warnings.append("This shift overlaps an existing shift for the same staff member.")

    start_dt = datetime.fromisoformat(starts_at)
    end_dt = datetime.fromisoformat(ends_at)
    new_hours = (end_dt - start_dt).total_seconds() / 3600
    day_start = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=day_start.weekday())
    for label, begin, limit in (("daily", day_start, 12), ("weekly", week_start, 48)):
        period_end = begin + (timedelta(days=1) if label == "daily" else timedelta(days=7))
        rows = db.execute(
            f"""
            SELECT starts_at, ends_at FROM shifts
            WHERE staff_type = ? AND staff_id = ? AND starts_at < ? AND ends_at > ? {extra}
            """,
            [staff_type, staff_id, period_end.isoformat(timespec="minutes"), begin.isoformat(timespec="minutes")] + ([ignore_shift_id] if ignore_shift_id else []),
        ).fetchall()
        total = new_hours + sum((datetime.fromisoformat(r["ends_at"]) - datetime.fromisoformat(r["starts_at"])).total_seconds() / 3600 for r in rows)
        if total > limit:
            warnings.append(f"This shift pushes the staff member over the {limit}-hour {label} limit.")
    return warnings


@app.context_processor
def inject_common():
    return {"current_user": session, "active_endpoint": request.endpoint}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = get_db().execute("SELECT * FROM users WHERE username = ?", (request.form["username"],)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form["password"]):
            session.clear()
            session.update(user_id=user["id"], username=user["username"], role=user["role"])
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    db = get_db()
    patients = db.execute(
        """
        SELECT p.id, p.full_name, p.age, p.gender, w.name AS ward_name, t.code AS team_code
        FROM patients p
        JOIN wards w ON w.id = p.ward_id
        JOIN teams t ON t.id = p.team_id
        ORDER BY p.full_name
        """
    ).fetchall()
    teams = db.execute("SELECT id, code, name FROM teams ORDER BY code").fetchall()
    wards = ward_availability()
    stats = {
        "patients": len(patients),
        "wards": len(wards),
        "free_beds": sum(row["free_beds"] for row in wards),
        "occupied_beds": sum(row["occupied"] for row in wards),
        "teams": len(teams),
    }
    return render_template("dashboard.html", wards=wards, patients=patients, teams=teams, stats=stats)


@app.route("/admit", methods=["GET", "POST"])
@login_required
@admin_required
def admit():
    if request.method == "POST":
        try:
            if not request.form["full_name"].strip():
                raise ValueError("Patient name is required.")
            admit_patient(request.form["full_name"], request.form["age"], request.form["gender"], int(request.form["ward_id"]), int(request.form["team_id"]))
            flash("Patient admitted.", "success")
            return redirect(url_for("dashboard"))
        except (ValueError, KeyError) as exc:
            flash(str(exc), "danger")
    db = get_db()
    return render_template("admit.html", wards=ward_availability(), teams=db.execute("SELECT * FROM teams ORDER BY code").fetchall())


@app.route("/transfer", methods=["GET", "POST"])
@login_required
@admin_required
def transfer():
    if request.method == "POST":
        try:
            transfer_patient(int(request.form["patient_id"]), int(request.form["ward_id"]))
            flash("Patient transferred.", "success")
            return redirect(url_for("dashboard"))
        except ValueError as exc:
            flash(str(exc), "danger")
    return render_template("transfer.html", patients=get_patient_options(), wards=ward_availability())


@app.route("/treatment", methods=["GET", "POST"])
@login_required
def treatment():
    if request.method == "POST":
        try:
            record_treatment(int(request.form["patient_id"]), int(request.form["doctor_id"]), request.form["treated_at"], request.form.get("notes", ""))
            flash("Treatment recorded.", "success")
            return redirect(url_for("dashboard"))
        except ValueError as exc:
            flash(str(exc), "danger")
    db = get_db()
    return render_template("treatment.html", patients=get_patient_options(), doctors=db.execute("SELECT * FROM doctors ORDER BY full_name").fetchall(), now=datetime.now().isoformat(timespec="minutes"))


@app.post("/discharge/<int:patient_id>")
@login_required
@admin_required
def discharge(patient_id):
    try:
        discharge_patient(patient_id)
        flash("Patient discharged and removed from the system.", "success")
    except ValueError as exc:
        flash(str(exc), "danger")
    return redirect(url_for("dashboard"))


def get_patient_options():
    return get_db().execute(
        """
        SELECT p.*, w.name AS ward_name, t.code AS team_code
        FROM patients p JOIN wards w ON w.id = p.ward_id JOIN teams t ON t.id = p.team_id
        ORDER BY p.full_name
        """
    ).fetchall()


@app.route("/wards/<int:ward_id>")
@login_required
def ward_patients(ward_id):
    ward = get_db().execute("SELECT * FROM wards WHERE id = ?", (ward_id,)).fetchone()
    patients = get_db().execute("SELECT full_name, age FROM patients WHERE ward_id = ? ORDER BY full_name", (ward_id,)).fetchall()
    return render_template("ward_patients.html", ward=ward, patients=patients)


@app.route("/teams/<int:team_id>")
@login_required
def team_patients(team_id):
    team = get_db().execute(
        """
        SELECT t.*, c.full_name AS consultant
        FROM teams t LEFT JOIN doctors c ON c.id = t.consultant_doctor_id
        WHERE t.id = ?
        """,
        (team_id,),
    ).fetchone()
    patients = get_db().execute(
        """
        SELECT p.id, p.full_name, w.name AS ward_name, t.code AS team_code, c.full_name AS consultant
        FROM patients p
        JOIN wards w ON w.id = p.ward_id
        JOIN teams t ON t.id = p.team_id
        LEFT JOIN doctors c ON c.id = t.consultant_doctor_id
        WHERE p.team_id = ?
        ORDER BY p.full_name
        """,
        (team_id,),
    ).fetchall()
    treatments = {}
    for row in get_db().execute(
        """
        SELECT tr.patient_id, d.full_name, d.grade, tr.treated_at
        FROM treatments tr JOIN doctors d ON d.id = tr.doctor_id
        WHERE tr.patient_id IN (SELECT id FROM patients WHERE team_id = ?)
        ORDER BY tr.treated_at DESC
        """,
        (team_id,),
    ):
        treatments.setdefault(row["patient_id"], []).append(row)
    return render_template("team_patients.html", team=team, patients=patients, treatments=treatments)


@app.route("/roster", methods=["GET", "POST"])
@login_required
@admin_required
def roster():
    db = get_db()
    if request.method == "POST":
        try:
            starts_at = request.form["starts_at"]
            ends_at = request.form["ends_at"]
            if datetime.fromisoformat(ends_at) <= datetime.fromisoformat(starts_at):
                raise ValueError("Shift end must be after shift start.")
            warnings = roster_warnings(request.form["staff_type"], int(request.form["staff_id"]), starts_at, ends_at)
            db.execute(
                "INSERT INTO shifts (staff_type, staff_id, starts_at, ends_at, role_note) VALUES (?, ?, ?, ?, ?)",
                (request.form["staff_type"], int(request.form["staff_id"]), starts_at, ends_at, request.form.get("role_note", "")),
            )
            db.commit()
            flash("Shift saved." + (" Warning: " + " ".join(warnings) if warnings else ""), "warning" if warnings else "success")
            return redirect(url_for("roster"))
        except ValueError as exc:
            flash(str(exc), "danger")
    shifts = db.execute(
        """
        SELECT s.*, COALESCE(d.full_name, n.full_name) AS staff_name
        FROM shifts s
        LEFT JOIN doctors d ON s.staff_type = 'Doctor' AND s.staff_id = d.id
        LEFT JOIN nurses n ON s.staff_type = 'Nurse' AND s.staff_id = n.id
        ORDER BY s.starts_at DESC
        """
    ).fetchall()
    return render_template("roster.html", doctors=db.execute("SELECT * FROM doctors ORDER BY full_name").fetchall(), nurses=db.execute("SELECT * FROM nurses ORDER BY full_name").fetchall(), shifts=shifts)


@app.route("/roster/<int:shift_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def edit_shift(shift_id):
    db = get_db()
    shift = db.execute("SELECT * FROM shifts WHERE id = ?", (shift_id,)).fetchone()
    if not shift:
        flash("Shift not found.", "danger")
        return redirect(url_for("roster"))
    if request.method == "POST":
        try:
            starts_at = request.form["starts_at"]
            ends_at = request.form["ends_at"]
            if datetime.fromisoformat(ends_at) <= datetime.fromisoformat(starts_at):
                raise ValueError("Shift end must be after shift start.")
            staff_type = request.form["staff_type"]
            staff_id = int(request.form["staff_id"])
            warnings = roster_warnings(staff_type, staff_id, starts_at, ends_at, ignore_shift_id=shift_id)
            db.execute(
                """
                UPDATE shifts
                SET staff_type = ?, staff_id = ?, starts_at = ?, ends_at = ?, role_note = ?
                WHERE id = ?
                """,
                (staff_type, staff_id, starts_at, ends_at, request.form.get("role_note", ""), shift_id),
            )
            db.commit()
            flash("Shift updated." + (" Warning: " + " ".join(warnings) if warnings else ""), "warning" if warnings else "success")
            return redirect(url_for("roster"))
        except ValueError as exc:
            flash(str(exc), "danger")
    return render_template(
        "shift_edit.html",
        shift=shift,
        doctors=db.execute("SELECT * FROM doctors ORDER BY full_name").fetchall(),
        nurses=db.execute("SELECT * FROM nurses ORDER BY full_name").fetchall(),
    )


@app.route("/reports")
@login_required
@admin_required
def reports():
    month = request.args.get("month") or datetime.now().strftime("%Y-%m")
    start = f"{month}-01"
    end = (datetime.strptime(start, "%Y-%m-%d").replace(day=28) + timedelta(days=4)).replace(day=1).date().isoformat()
    summary = get_db().execute(
        """
        SELECT w.name,
               SUM(CASE WHEN e.event_type = 'admission' THEN 1 ELSE 0 END) AS admitted,
               SUM(CASE WHEN e.event_type = 'discharge' THEN 1 ELSE 0 END) AS discharged,
               (SELECT COUNT(*) FROM patients p WHERE p.ward_id = w.id) AS current_occupancy,
               w.capacity
        FROM wards w
        LEFT JOIN occupancy_events e ON e.ward_id = w.id AND e.event_date >= ? AND e.event_date < ?
        GROUP BY w.id
        ORDER BY w.name
        """,
        (start, end),
    ).fetchall()
    return render_template("reports.html", month=month, summary=summary)


@app.route("/users", methods=["GET", "POST"])
@login_required
@admin_required
def users():
    db = get_db()
    if request.method == "POST":
        action = request.form["action"]
        if action == "add":
            db.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (request.form["username"].strip(), generate_password_hash(request.form["password"]), request.form["role"]),
            )
            flash("User added.", "success")
        elif action == "delete" and int(request.form["user_id"]) != session["user_id"]:
            db.execute("DELETE FROM users WHERE id = ?", (int(request.form["user_id"]),))
            flash("User removed.", "success")
        db.commit()
        return redirect(url_for("users"))
    return render_template("users.html", users=db.execute("SELECT id, username, role FROM users ORDER BY username").fetchall())


@app.route("/api/availability")
@login_required
def api_availability():
    return {"wards": [dict(row) for row in ward_availability()]}


if __name__ == "__main__":
    seed_demo_data(app.config["DATABASE"])
    app.run(debug=True)
