"""
app.py
ระบบเช็คงานค้าง — ติดตามสถานะการส่งงานของนักเรียน (ไม่มีระบบคะแนน/เกรด)

รันด้วย:  python app.py
เปิดเบราว์เซอร์ที่ http://127.0.0.1:5000
"""
import os
import secrets
import time
import calendar
import csv
import io
import random
import re
from datetime import datetime, date, timedelta
from functools import wraps
from urllib.parse import urlsplit

from flask import (
    Flask, render_template, request, redirect, url_for, session, flash, abort, jsonify
)

import openpyxl
import holidays as holiday_calendar_lib
from holidays.constants import GOVERNMENT, PUBLIC
from sqlalchemy import text as sql_text

from models import (
    db, Teacher, Student, Subject, SubjectClass, Enrollment, Assignment, Group, Submission,
    ScheduleSlot, TermSetting, Holiday, HolidaySeed, DAYS_TH, ASSIGNMENT_TYPES,
    GRADE_LEVELS, SECTIONS, STATUS_META,
    get_status, subject_status_summary,
    build_schedule_grid, schedule_slots_for_class, status_label, status_color, status_icon,
    format_due_countdown, thai_now, build_period_lookup, period_box_state
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
# ตั้งค่า SECRET_KEY ผ่านตัวแปรสภาพแวดล้อมเมื่อใช้งานจริง (ถ้าไม่ตั้ง จะสุ่มใหม่ทุกครั้งที่เปิดเซิร์ฟเวอร์ ผู้ใช้ต้องล็อกอินใหม่)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "homework_tracker.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

#ให้เทมเพลตเรียกใช้ helper เกี่ยวกับสถานะได้โดยตรง
app.jinja_env.globals.update(
    status_label=status_label,
    status_color=status_color,
    status_icon=status_icon,
    format_due_countdown=format_due_countdown,
    STATUS_META=STATUS_META,
)


# ============================================================================
# Auth helpers
# ============================================================================

def login_required_student(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("role") != "student":
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapper


def login_required_teacher(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("role") != "teacher":
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapper


def login_required_admin(f):
    """ต้องล็อกอินเป็นครู และมีสิทธิ์ is_admin เท่านั้น"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("role") != "teacher" or not session.get("is_admin"):
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapper


def current_student():
    sid = session.get("user_id")
    return db.session.get(Student, sid) if sid else None


def current_teacher():
    tid = session.get("user_id")
    return db.session.get(Teacher, tid) if tid else None


# ============================================================================
# หน้า Login
# ============================================================================

@app.route("/")
def index():
    if session.get("role") == "student":
        return redirect(url_for("student_dashboard"))
    if session.get("role") == "teacher":
        return redirect(url_for("teacher_dashboard"))
    return render_template("login.html")


@app.route("/login/student", methods=["POST"])
def login_student():
    student_id = request.form.get("student_id", "").strip()
    student = Student.query.filter_by(student_id=student_id).first()
    if not student:
        flash("ไม่พบเลขประจำตัวนักเรียนนี้ในระบบ", "error")
        return redirect(url_for("index"))
    session.clear()
    session["role"] = "student"
    session["user_id"] = student.id
    return redirect(url_for("student_dashboard"))


LOGIN_FAILURES = {}          # (ip, ชื่อผู้ใช้) -> [เวลาที่ล็อกอินผิด, ...]
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 300
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # ตัดตัวที่อ่านสับสน
MIN_PASSWORD_LENGTH = 8


def login_blocked(key):
    """ล็อกอินผิดครบ 5 ครั้งใน 5 นาที จะถูกบล็อกชั่วคราว"""
    cutoff = time.time() - LOGIN_WINDOW_SECONDS
    recent = [t for t in LOGIN_FAILURES.get(key, []) if t > cutoff]
    LOGIN_FAILURES[key] = recent
    return len(recent) >= LOGIN_MAX_FAILURES


def record_login_failure(key):
    LOGIN_FAILURES.setdefault(key, []).append(time.time())


def generate_password(length=10):
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


@app.route("/login/teacher", methods=["POST"])
def login_teacher():
    username = request.form.get("teacher_name", "").strip()
    password = request.form.get("password", "").strip()
    teacher = Teacher.query.filter_by(username=username).first()
    # รองรับชื่อผู้ใช้เดิมที่มีช่องว่าง/ตัวพิมพ์เล็กใหญ่ต่างกัน
    if not teacher and username:
        teacher = next((t for t in Teacher.query.all()
                        if (t.username or "").strip().casefold() == username.casefold()), None)
    throttle_key = (request.remote_addr, username.casefold())
    if login_blocked(throttle_key):
        flash("ลองเข้าสู่ระบบผิดหลายครั้งเกินไป กรุณารอสักครู่แล้วลองใหม่", "error")
        return redirect(url_for("index"))
    if not (teacher and teacher.check_password(password)):
        record_login_failure(throttle_key)
        flash("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง กรุณาตรวจชื่อผู้ใช้ในหน้าแอดมิน", "error")
        return redirect(url_for("index"))
    LOGIN_FAILURES.pop(throttle_key, None)
    session.clear()
    session["role"] = "teacher"
    session["user_id"] = teacher.id
    session["is_admin"] = teacher.is_admin
    return redirect(url_for("teacher_dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ============================================================================
# ฝั่งนักเรียน
# ============================================================================

def period_box_for(slots, label_fn):
    """ข้อมูลกล่อง "คาบตอนนี้ / คาบต่อไป" จากรายการคาบสอน"""
    holiday_map = {h.holiday_date: h.description for h in Holiday.query.all()}
    return period_box_state(build_period_lookup(slots, label_fn), holiday_map,
                            TermSetting.query.first())


@app.route("/period-box")
def period_box():
    """ส่วนของกล่องคาบ (ให้หน้าเว็บดึงมาอัปเดตทุก 30 วินาที)"""
    if session.get("role") == "student":
        student = current_student()
        slots = schedule_slots_for_class(student.grade_level, student.class_room)
        label_fn = lambda sl: f"{sl.subject.name} ({sl.subject.teacher_names()})"
    elif session.get("role") == "teacher":
        slots = ScheduleSlot.query.filter_by(teacher_id=current_teacher().id).all()
        label_fn = lambda sl: f"{sl.subject.name} ({sl.class_label()})"
    else:
        abort(403)
    return render_template("_period_box.html", period_box=period_box_for(slots, label_fn))


@app.route("/student/dashboard")
@login_required_student
def student_dashboard():
    ensure_default_holidays()
    student = current_student()
    enrollments = Enrollment.query.filter_by(student_id=student.id).all()

    subject_rows = []
    total_pending = 0
    total_closed = 0
    total_assignments = 0
    total_submitted = 0
    for e in enrollments:
        subject = e.subject
        counts, pending = subject_status_summary(student.id, subject.id)
        total_pending += len(pending)
        total_closed += counts["late_closed"]
        total_assignments += sum(counts.values())
        total_submitted += counts["submitted"]
        # สถานะที่เร่งด่วนที่สุดในวิชานี้ ใช้กำหนดสีการ์ด
        if counts["late_accepting"]:
            worst = "late_accepting"
        elif counts["due_soon"]:
            worst = "due_soon"
        elif counts["not_submitted"]:
            worst = "not_submitted"
        elif counts["late_closed"]:
            worst = None  # มีงานที่ยังไม่ส่ง แต่ปิดรับแล้ว: ไม่ไฮไลท์ว่าเสร็จหรือค้าง
        else:
            worst = "submitted" if counts["submitted"] else None
        subject_rows.append({
            "subject": subject,
            "counts": counts,
            "pending_count": len(pending),
            "worst": worst,
        })

    # เรียงวิชาที่ค้างเยอะ/เร่งด่วนขึ้นก่อน
    order = {"late_accepting": 0, "due_soon": 1, "not_submitted": 2, None: 3, "submitted": 4}
    subject_rows.sort(key=lambda r: (order.get(r["worst"], 9), -r["pending_count"]))

    # ตารางเรียนของห้องนักเรียน — ดึงจากตารางสอนที่ครูบันทึกไว้โดยตรง
    class_slots = schedule_slots_for_class(student.grade_level, student.class_room)
    morning, afternoon, schedule_rows = build_schedule_grid(class_slots)
    return render_template("student_dashboard.html", student=student,
                            rows=subject_rows, total_pending=total_pending,
                            total_closed=total_closed,
                            total_assignments=total_assignments, total_submitted=total_submitted,
                            morning=morning, afternoon=afternoon, schedule_rows=schedule_rows,
                            days=DAYS_TH, has_schedule=bool(class_slots),
                            period_box=period_box_for(class_slots,
                                                      lambda sl: f"{sl.subject.name} ({sl.subject.teacher_names()})"))


def example_url(example_text):
    """แปลงตัวอย่างงานที่เป็น URL ทั้งช่องให้เป็นลิงก์ที่เปิดได้"""
    value = (example_text or "").strip()
    if not value or any(char.isspace() for char in value):
        return None
    url = f"https://{value}" if value.lower().startswith("www.") else value
    try:
        parsed = urlsplit(url)
        return url if parsed.scheme.lower() in ("http", "https") and parsed.netloc else None
    except ValueError:
        return None


@app.route("/student/subject/<int:subject_id>")
@login_required_student
def student_subject(subject_id):
    student = current_student()
    subject = db.session.get(Subject, subject_id) or abort(404)

    enrolled = Enrollment.query.filter_by(student_id=student.id, subject_id=subject_id).first()
    if not enrolled:
        abort(403)

    counts, pending = subject_status_summary(student.id, subject.id)

    all_assignments = Assignment.query.filter_by(subject_id=subject_id).order_by(
        Assignment.due_date).all()
    assignment_rows = []
    for a in all_assignments:
        s = Submission.query.filter_by(assignment_id=a.id, student_id=student.id).first()
        members = ([m.student.name for m in Submission.query.filter_by(group_id=s.group_id).all()]
                   if s and s.group_id else [])
        assignment_rows.append({
            "assignment": a,
            "status": get_status(a, s),
            "group_label": s.group.label if (s and s.group) else None,
            "group_members": members,
            "note": s.note if s else "",
            "example_url": example_url(a.example_text),
        })

    rows_by_id = {row["assignment"].id: row for row in assignment_rows}
    pending_rows = [rows_by_id[a.id] for a, _ in pending]
    pending_ids = {row["assignment"].id for row in pending_rows}
    other_rows = [row for row in assignment_rows if row["assignment"].id not in pending_ids]

    return render_template(
        "student_subject.html",
        student=student, subject=subject, pending_rows=pending_rows,
        other_rows=other_rows, counts=counts, assignment_rows=assignment_rows,
    )


@app.route("/student/assignment/<int:assignment_id>")
@login_required_student
def student_assignment(assignment_id):
    """หน้ารายละเอียดงานแบบเต็ม — เห็นรายละเอียดชัดเจน พร้อมลิงก์แนบ"""
    student = current_student()
    a = db.session.get(Assignment, assignment_id) or abort(404)

    enrolled = Enrollment.query.filter_by(student_id=student.id, subject_id=a.subject_id).first()
    if not enrolled:
        abort(403)

    s = Submission.query.filter_by(assignment_id=a.id, student_id=student.id).first()
    members = []
    if s and s.group:
        members = [m.student.name for m in Submission.query.filter_by(group_id=s.group_id).all()]

    return render_template(
        "assignment_detail.html",
        assignment=a, status=get_status(a, s),
        group_label=s.group.label if (s and s.group) else None,
        group_members=members, note=s.note if s else "",
        back_url=url_for("student_subject", subject_id=a.subject_id),
    )


# ============================================================================
# ฝั่งครู/อาจารย์
# ============================================================================

def count_remaining_periods_by_subject_class(slots, term_end_date, holiday_dates,
                                              term_start_date=None):
    """นับคาบตั้งแต่วันเปิดเทอมหรือวันนี้ถึงวันสิ้นสุดเทอม โดยข้ามวันหยุด"""
    today = thai_now().date()
    keys = {(s.subject_id, s.grade_level, s.section) for s in slots if s.grade_level}
    result = {key: 0 for key in keys}
    start = max(today, term_start_date) if term_start_date else today
    if term_end_date < start:
        return result
    d = start
    while d <= term_end_date:
        if d not in holiday_dates:
            weekday = d.weekday()
            for s in slots:
                key = (s.subject_id, s.grade_level, s.section)
                if s.day_of_week == weekday and key in result:
                    result[key] += s.duration
        d += timedelta(days=1)
    return result


def clamp_duration(period, duration):
    """คาบต่อเนื่องห้ามคร่อมพักเที่ยง (คาบ 4 -> 5) และห้ามเกินคาบ 8"""
    if duration > 1 and (period == 4 or period + duration - 1 > 8):
        return 1
    return duration


def find_schedule_conflict(day_of_week, period_list, grade_level, section, exclude_id=None):
    """หาคาบสอนอื่นที่ชนกัน (วัน+ช่วงคาบ+ชั้น/ห้องเดียวกัน)"""
    if not grade_level:
        return None
    q = ScheduleSlot.query.filter_by(
        day_of_week=day_of_week, grade_level=grade_level, section=section
    )
    if exclude_id:
        q = q.filter(ScheduleSlot.id != exclude_id)
    for other in q.all():
        if set(other.period_range()) & set(period_list):
            return other
    return None


def respond_action(ok, message, **extra):
    """ตอบกลับการกระทำของแอดมิน: ถ้าเรียกมาจากหน้าเว็บแบบไม่รีโหลด (fetch) ตอบเป็น JSON
    ไม่เช่นนั้น flash แล้ว redirect กลับแผงควบคุมเหมือนเดิม"""
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=ok, message=message, **extra), (200 if ok else 400)
    flash(message, "success" if ok else "error")
    return redirect(url_for("admin_dashboard"))


def sync_subject_enrollments(subject):
    """ทำให้รายชื่อนักเรียนของวิชาตรงกับชั้น/ห้องที่วิชานั้นสอน
    - เพิ่มนักเรียนทุกคนในห้องที่ยังไม่อยู่ในวิชา
    - นำคนที่ย้ายออกจากห้องนั้นออก (พร้อมสถานะส่งงานของวิชานี้)
    วิชาที่ไม่ได้ผูกชั้น/ห้อง (เช่น วิชาเสริม) จะไม่ถูกแตะต้อง"""
    if not subject.uses_classes():
        return
    wanted_classes = {(c.grade_level, str(c.section)) for c in subject.classes}
    wanted = {st.id for st in Student.query.all()
              if (st.grade_level, (st.class_room or "").strip()) in wanted_classes}
    existing = {e.student_id: e for e in Enrollment.query.filter_by(subject_id=subject.id).all()}
    for sid in wanted - existing.keys():
        db.session.add(Enrollment(student_id=sid, subject_id=subject.id))
    for sid in existing.keys() - wanted:
        for sub in Submission.query.join(Assignment).filter(
                Assignment.subject_id == subject.id, Submission.student_id == sid).all():
            db.session.delete(sub)
        db.session.delete(existing[sid])


def sync_all_class_subjects():
    """เรียกหลังข้อมูลนักเรียนเปลี่ยน (เพิ่ม/ย้ายห้อง/นำเข้า) เพื่ออัปเดตรายชื่อทุกวิชาที่ผูกห้อง"""
    for subject in Subject.query.all():
        sync_subject_enrollments(subject)
    db.session.commit()


def parse_class_selection(form):
    """อ่านชั้น + ห้อง (1 วิชาต่อ 1 ห้อง) จากฟอร์ม คืนค่า [(ชั้น, ห้อง)] หรือ None ถ้าไม่ได้เลือก"""
    grade = form.get("grade_level", "").strip()
    room = form.get("class_room", type=int)
    if grade not in GRADE_LEVELS or room not in SECTIONS:
        return None
    return [(grade, room)]


def set_subject_classes(subject, selected):
    """ปรับชั้น/ห้องของวิชาให้ตรงกับที่เลือก โดยคงแถวเดิมไว้ (ไม่ลบแล้วสร้างซ้ำ)"""
    wanted = set(selected)
    for c in list(subject.classes):
        if (c.grade_level, c.section) not in wanted:
            subject.classes.remove(c)
    have = {(c.grade_level, c.section) for c in subject.classes}
    for g, r in sorted(wanted - have):
        subject.classes.append(SubjectClass(grade_level=g, section=r))


@app.route("/teacher/dashboard")
@login_required_teacher
def teacher_dashboard():
    ensure_default_holidays()
    teacher = current_teacher()
    slots = ScheduleSlot.query.filter_by(teacher_id=teacher.id).order_by(
        ScheduleSlot.day_of_week, ScheduleSlot.period).all()

    # ตาราง [วัน][คาบ] — คาบอยู่ด้านบน วันอยู่ด้านซ้าย
    morning, afternoon, schedule_rows = build_schedule_grid(slots)
    term = TermSetting.query.first()
    holiday_dates = {h.holiday_date for h in Holiday.query.all()}
    subjects = Subject.query.filter(Subject.teachers.any(id=teacher.id)).all()
    subject_by_id = {subject.id: subject for subject in subjects}
    remaining_by_subject_class = []
    if term:
        raw = count_remaining_periods_by_subject_class(
            slots, term.term_end_date, holiday_dates, term.term_start_date)
        remaining_by_subject_class = [
            {"subject_name": subject_by_id[k[0]].name,
             "subject_code": subject_by_id[k[0]].code,
             "class_label": f"{k[1]}/{k[2]}", "remaining": remaining}
            for k, remaining in raw.items() if k[0] in subject_by_id
        ]
        remaining_by_subject_class.sort(key=lambda row: (
            row["subject_name"], row["class_label"]))

    # ข้อมูลสำหรับป็อปอัพเลือกวิชาในตารางสอน
    slot_subjects = [{
        "id": s.id, "name": s.name, "elective": bool(s.is_elective),
        "classes": [{"g": c.grade_level, "s": c.section} for c in s.classes],
    } for s in sorted(subjects, key=lambda x: x.name)]
    slot_occupancy = {}
    for s in slots:
        for p in s.period_range():
            slot_occupancy.setdefault(s.day_of_week, {})[p] = s.id

    return render_template(
        "teacher_dashboard.html",
        slot_subjects=slot_subjects, slot_occupancy=slot_occupancy,
        period_box=period_box_for(slots, lambda sl: f"{sl.subject.name} ({sl.class_label()})"),
        teacher=teacher, morning=morning, afternoon=afternoon,
        days=DAYS_TH, schedule_rows=schedule_rows,
        remaining_by_subject_class=remaining_by_subject_class, term=term, subjects=subjects,
        grade_levels=GRADE_LEVELS, sections=SECTIONS,
    )


@app.route("/teacher/subject/new", methods=["POST"])
@login_required_teacher
def new_subject():
    teacher = current_teacher()
    name = request.form.get("name", "").strip()
    code = request.form.get("code", "").strip()
    room = request.form.get("room", "").strip()

    if not name:
        flash("กรุณากรอกชื่อวิชา", "error")
        return redirect(url_for("teacher_dashboard"))
    selected = parse_class_selection(request.form)
    if not selected:
        flash("กรุณาเลือกระดับชั้นและห้องที่สอน", "error")
        return redirect(url_for("teacher_dashboard"))

    subject = Subject(name=name, code=code, room=room, teacher_id=teacher.id)
    subject.teachers.append(teacher)
    subject.classes = [SubjectClass(grade_level=g, section=r) for g, r in selected]
    db.session.add(subject)
    db.session.flush()
    sync_subject_enrollments(subject)
    db.session.commit()
    flash(f"เพิ่มวิชาเรียบร้อยแล้ว — นักเรียนทุกคนใน {subject.class_labels()} ได้รับงานของวิชานี้อัตโนมัติ", "success")
    return redirect(url_for("teacher_dashboard"))


@app.route("/teacher/subject/<int:subject_id>/edit", methods=["POST"])
@login_required_teacher
def edit_subject(subject_id):
    teacher = current_teacher()
    subject = db.session.get(Subject, subject_id) or abort(404)
    if teacher not in subject.teachers or subject.is_elective:
        abort(403)  # วิชาเสริมแก้ไขได้เฉพาะแอดมิน
    subject.name = request.form.get("name", subject.name).strip()
    subject.code = request.form.get("code", subject.code).strip()
    subject.room = request.form.get("room", subject.room).strip()
    selected = parse_class_selection(request.form)
    if selected:
        set_subject_classes(subject, selected)
        db.session.flush()
        sync_subject_enrollments(subject)
    elif subject.uses_classes() and "grade_level" in request.form:
        db.session.rollback()
        flash("กรุณาเลือกระดับชั้นและห้องที่สอน", "error")
        return redirect(url_for("teacher_dashboard"))
    db.session.commit()
    flash("แก้ไขข้อมูลวิชาเรียบร้อยแล้ว", "success")
    return redirect(url_for("teacher_dashboard"))


@app.route("/teacher/subject/<int:subject_id>/delete", methods=["POST"])
@login_required_teacher
def delete_subject(subject_id):
    teacher = current_teacher()
    subject = db.session.get(Subject, subject_id) or abort(404)
    if teacher not in subject.teachers or subject.is_elective:
        abort(403)  # วิชาเสริมลบได้เฉพาะแอดมิน
    db.session.delete(subject)
    db.session.commit()
    flash("ลบวิชาเรียบร้อยแล้ว", "success")
    return redirect(url_for("teacher_dashboard"))


# --- ตารางสอน --------------------------------------------------------------

def read_slot_form(teacher, existing_slot=None):
    """อ่านและตรวจข้อมูลคาบสอนจากฟอร์ม: วิชาต้องเป็นของครูคนนี้ และถ้าวิชาผูกชั้น/ห้องไว้ ต้องเป็นห้องใดห้องหนึ่งของวิชานั้น
    คืนค่า (subject, day_of_week, period, duration, grade_level, section)"""
    try:
        subject = db.session.get(Subject, int(request.form.get("subject_id"))) or abort(404)
        day_of_week = int(request.form.get("day_of_week"))
        period = int(request.form.get("period"))
        duration = clamp_duration(period, int(request.form.get("duration", 1)))
        grade_level = request.form.get("grade_level", "").strip()
        section = int(request.form.get("section", 1))
    except (TypeError, ValueError):
        abort(400)
    if teacher not in subject.teachers:
        abort(403)
    if not (0 <= day_of_week < 5 and 1 <= period <= 8) or grade_level not in GRADE_LEVELS or section not in SECTIONS:
        abort(400)
    allowed = {(c.grade_level, c.section) for c in subject.classes}
    if existing_slot is not None:      # คาบเดิมที่ผูกห้องไว้ก่อนแล้ว บันทึกต่อได้โดยไม่ต้องย้ายห้อง
        allowed.add((existing_slot.grade_level, existing_slot.section))
    if subject.uses_classes() and (grade_level, section) not in allowed:
        abort(400)
    return subject, day_of_week, period, duration, grade_level, section


@app.route("/teacher/schedule/new", methods=["POST"])
@login_required_teacher
def new_schedule_slot():
    teacher = current_teacher()
    subject, day_of_week, period, duration, grade_level, section = read_slot_form(teacher)
    subject_id = subject.id
    period_list = list(range(period, period + duration))

    conflict = find_schedule_conflict(day_of_week, period_list, grade_level, section)
    if conflict:
        flash(f"❌ ไม่สามารถเพิ่มคาบสอนได้ เนื่องจากมีวิชา {conflict.subject.name} "
              f"ของครู {conflict.teacher.name} อยู่แล้วในคาบนี้", "error")
        return redirect(url_for("teacher_dashboard"))

    slot = ScheduleSlot(
        teacher_id=teacher.id, subject_id=subject_id,
        day_of_week=day_of_week, period=period, duration=duration,
        grade_level=grade_level, section=section,
    )
    db.session.add(slot)
    db.session.commit()
    flash("เพิ่มคาบสอนเรียบร้อยแล้ว", "success")
    return redirect(url_for("teacher_dashboard"))


@app.route("/teacher/schedule/<int:slot_id>/edit", methods=["POST"])
@login_required_teacher
def edit_schedule_slot(slot_id):
    teacher = current_teacher()
    slot = db.session.get(ScheduleSlot, slot_id) or abort(404)
    if slot.teacher_id != teacher.id:
        abort(403)

    subject, day_of_week, period, duration, grade_level, section = read_slot_form(teacher, existing_slot=slot)
    period_list = list(range(period, period + duration))

    conflict = find_schedule_conflict(day_of_week, period_list, grade_level, section,
                                       exclude_id=slot.id)
    if conflict:
        flash(f"❌ ไม่สามารถแก้ไขคาบสอนนี้ได้ เนื่องจากมีวิชา {conflict.subject.name} "
              f"ของครู {conflict.teacher.name} อยู่แล้วในคาบนี้", "error")
        return redirect(url_for("teacher_dashboard"))

    slot.subject_id = subject.id
    slot.day_of_week = day_of_week
    slot.period = period
    slot.duration = duration
    slot.grade_level = grade_level
    slot.section = section
    db.session.commit()
    flash("แก้ไขคาบสอนเรียบร้อยแล้ว", "success")
    return redirect(url_for("teacher_dashboard"))

@app.route("/teacher/schedule/<int:slot_id>/delete", methods=["POST"])
@login_required_teacher
def delete_schedule_slot(slot_id):
    teacher = current_teacher()
    slot = db.session.get(ScheduleSlot, slot_id) or abort(404)
    if slot.teacher_id != teacher.id:
        abort(403)
    db.session.delete(slot)
    db.session.commit()
    flash("ลบคาบสอนเรียบร้อยแล้ว", "success")
    return redirect(url_for("teacher_dashboard"))

# --- จัดการวิชา / งาน / สถานะการส่ง --------------------------------------------

@app.route("/teacher/subject/<int:subject_id>")
@login_required_teacher
def teacher_subject(subject_id):
    teacher = current_teacher()
    subject = db.session.get(Subject, subject_id) or abort(404)
    if teacher not in subject.teachers:
        abort(403)

    if subject.uses_classes():
        sync_subject_enrollments(subject)
        db.session.commit()
    all_assignments = Assignment.query.filter_by(subject_id=subject_id).order_by(
        Assignment.due_date).all()
    students = [e.student for e in Enrollment.query.filter_by(subject_id=subject_id).all()]

    # นักเรียนที่ยังไม่ได้อยู่ในวิชานี้ (ให้ครูเลือกเพิ่มเอง)
    enrolled_ids = {st.id for st in students}
    available_students = [
        st for st in Student.query.order_by(
            Student.grade_level, Student.class_room, Student.roll_number, Student.student_id).all()
        if st.id not in enrolled_ids
    ]
    students.sort(key=lambda st: (st.grade_level or "", st.class_room or "",
                                  st.roll_number or 0, st.student_id))

    # ตารางสถานะ [นักเรียน][งาน]
    status_table = {}
    for st in students:
        row = {}
        for a in all_assignments:
            s = Submission.query.filter_by(assignment_id=a.id, student_id=st.id).first()
            row[a.id] = {"submission": s, "status": get_status(a, s)}
        status_table[st.id] = row

    # นักเรียนที่ส่งงานครบทุกชิ้นแล้วเรียงไปอยู่ล่างสุดของตารางเช็คสถานะ (ลำดับเดิมตามเลขที่ไว้ในแต่ละกลุ่ม)
    done_ids = {
        st.id for st in students
        if all_assignments and all(
            status_table[st.id][a.id]["submission"] is not None
            and status_table[st.id][a.id]["submission"].submitted
            for a in all_assignments)
    }
    status_students = ([st for st in students if st.id not in done_ids]
                       + [st for st in students if st.id in done_ids])

    return render_template(
        "teacher_subject.html",
        teacher=teacher, subject=subject, assignments=all_assignments,
        students=students, status_table=status_table,
        status_students=status_students, done_ids=done_ids,
        assignment_types=ASSIGNMENT_TYPES,
        available_students=available_students,
        grade_levels=GRADE_LEVELS, sections=SECTIONS,
    )


# --- ครูผู้สอนจัดการนักเรียนในวิชาของตัวเอง ----------------------------------

def teacher_subject_or_403(subject_id):
    teacher = current_teacher()
    subject = db.session.get(Subject, subject_id) or abort(404)
    if teacher not in subject.teachers:
        abort(403)
    return subject


def class_subject_guard(subject):
    """วิชาที่ผูกชั้น/ห้องใช้รายชื่ออัตโนมัติ จึงเพิ่ม/นำออกเองไม่ได้"""
    if subject.uses_classes():
        flash(f"วิชานี้ผูกกับ {subject.class_labels()} — นักเรียนในห้องได้รับงานอัตโนมัติ "
              "ถ้านักเรียนย้ายห้อง รายชื่อจะปรับให้เอง", "error")
        return redirect(url_for("teacher_subject", subject_id=subject.id))
    return None


@app.route("/teacher/subject/<int:subject_id>/enroll", methods=["POST"])
@login_required_teacher
def teacher_enroll_students(subject_id):
    """ครูผู้สอนเลือกนักเรียน (หลายคน) เข้าวิชา"""
    subject = teacher_subject_or_403(subject_id)
    blocked = class_subject_guard(subject)
    if blocked:
        return blocked
    student_ids = request.form.getlist("student_ids", type=int)
    if not student_ids:
        flash("กรุณาเลือกนักเรียนอย่างน้อย 1 คน", "error")
        return redirect(url_for("teacher_subject", subject_id=subject.id) + "#studentManager")
    existing = {e.student_id for e in Enrollment.query.filter_by(subject_id=subject.id).all()}
    added = 0
    for st in Student.query.filter(Student.id.in_(student_ids)).all():
        if st.id not in existing:
            db.session.add(Enrollment(student_id=st.id, subject_id=subject.id))
            added += 1
    db.session.commit()
    flash(f"เพิ่มนักเรียนเข้าวิชาแล้ว {added} คน", "success")
    return redirect(url_for("teacher_subject", subject_id=subject.id) + "#studentManager")


@app.route("/teacher/subject/<int:subject_id>/enroll_class", methods=["POST"])
@login_required_teacher
def teacher_enroll_class(subject_id):
    """ครูผู้สอนเพิ่มนักเรียนทั้งห้อง (เช่น ม.2/1) เข้าวิชา"""
    subject = teacher_subject_or_403(subject_id)
    blocked = class_subject_guard(subject)
    if blocked:
        return blocked
    grade = request.form.get("grade_level", "").strip()
    room = request.form.get("class_room", "").strip()
    if grade not in GRADE_LEVELS or not room:
        flash("กรุณาเลือกระดับชั้นและห้อง", "error")
        return redirect(url_for("teacher_subject", subject_id=subject.id) + "#studentManager")
    existing = {e.student_id for e in Enrollment.query.filter_by(subject_id=subject.id).all()}
    class_students = Student.query.filter_by(grade_level=grade, class_room=room).all()
    added = 0
    for st in class_students:
        if st.id not in existing:
            db.session.add(Enrollment(student_id=st.id, subject_id=subject.id))
            added += 1
    db.session.commit()
    if not class_students:
        flash(f"ไม่พบนักเรียนในห้อง {grade}/{room}", "error")
    else:
        flash(f"เพิ่มนักเรียนห้อง {grade}/{room} เข้าวิชาแล้ว {added} คน", "success")
    return redirect(url_for("teacher_subject", subject_id=subject.id) + "#studentManager")


@app.route("/teacher/subject/<int:subject_id>/unenroll", methods=["POST"])
@login_required_teacher
def teacher_unenroll_students(subject_id):
    """ครูผู้สอนนำนักเรียน (หลายคน) ออกจากวิชา"""
    subject = teacher_subject_or_403(subject_id)
    blocked = class_subject_guard(subject)
    if blocked:
        return blocked
    student_ids = request.form.getlist("student_ids", type=int)
    if not student_ids:
        flash("กรุณาเลือกนักเรียนที่ต้องการนำออก", "error")
        return redirect(url_for("teacher_subject", subject_id=subject.id) + "#studentManager")
    removed = 0
    for e in Enrollment.query.filter(Enrollment.subject_id == subject.id,
                                     Enrollment.student_id.in_(student_ids)).all():
        # ลบสถานะการส่งงานของนักเรียนคนนั้นในวิชานี้ด้วย เพื่อไม่ให้มีข้อมูลค้าง
        for sub in Submission.query.join(Assignment).filter(
                Assignment.subject_id == subject.id,
                Submission.student_id == e.student_id).all():
            db.session.delete(sub)
        db.session.delete(e)
        removed += 1
    db.session.commit()
    flash(f"นำนักเรียนออกจากวิชาแล้ว {removed} คน", "success")
    return redirect(url_for("teacher_subject", subject_id=subject.id) + "#studentManager")


@app.route("/teacher/assignment/<int:assignment_id>")
@login_required_teacher
def teacher_assignment(assignment_id):
    """หน้ารายละเอียดงานฝั่งครู พร้อมรายชื่อคนที่ยังไม่ส่ง"""
    teacher = current_teacher()
    a = db.session.get(Assignment, assignment_id) or abort(404)
    if teacher not in a.subject.teachers:
        abort(403)

    students = [e.student for e in a.subject.enrollments]
    rows = []
    for st in students:
        s = Submission.query.filter_by(assignment_id=a.id, student_id=st.id).first()
        rows.append({"student": st, "status": get_status(a, s),
                      "group_label": s.group.label if (s and s.group) else None})
    order = {"late_closed": 0, "late_accepting": 1, "due_soon": 2, "not_submitted": 3, "submitted": 4}
    rows.sort(key=lambda r: order.get(r["status"], 9))

    group_rows = []
    if a.assignment_type == "group":
        for group in sorted(a.groups, key=lambda item: item.id):
            members = Submission.query.filter_by(group_id=group.id).all()
            group_rows.append({"group": group, "names": [m.student.name for m in members]})

    return render_template("teacher_assignment.html", assignment=a, rows=rows,
                           group_rows=group_rows)


def parse_day_month_year(value):
    """อ่านวันที่ วว/ดด/ปปปป (ค.ศ.) และตรวจว่าเป็นวันที่จริง"""
    if not isinstance(value, str):
        raise ValueError("invalid date")
    value = value.strip()
    parsed = datetime.strptime(value, "%d/%m/%Y").date()
    if parsed.strftime("%d/%m/%Y") != value:
        raise ValueError("invalid date format")
    return parsed


def parse_assignment_due_date(form):
    """อ่านวันที่ วว/ดด/ปปปป และเวลาไทยแบบ 24 ชั่วโมงจากฟอร์ม"""
    try:
        if "due_date_day" in form or "due_time_hm" in form:
            day = parse_day_month_year(form.get("due_date_day", ""))
            due_time = datetime.strptime(form.get("due_time_hm", ""), "%H:%M").time()
            return datetime.combine(day, due_time)
        # รองรับฟอร์มเดิมที่ส่งค่า datetime-local มาในช่องเดียว
        return datetime.strptime(form.get("due_date", ""), "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError):
        return None


@app.route("/teacher/subject/<int:subject_id>/assignment/new", methods=["POST"])
@login_required_teacher
def new_assignment(subject_id):
    teacher = current_teacher()
    subject = db.session.get(Subject, subject_id) or abort(404)
    if teacher not in subject.teachers:
        abort(403)

    title = request.form.get("title", "").strip()
    if not title:
        flash("กรุณากรอกชื่องาน", "error")
        return redirect(url_for("teacher_subject", subject_id=subject_id))

    due_date = parse_assignment_due_date(request.form)
    if due_date is None:
        flash("กรุณาระบุวันที่แบบวัน/เดือน/ปีและเวลา 24 ชั่วโมง เช่น 14/10/2026 14:30", "error")
        return redirect(url_for("teacher_subject", subject_id=subject_id))
    assignment_type = request.form.get("assignment_type", "")
    if assignment_type not in dict(ASSIGNMENT_TYPES):
        flash("กรุณาเลือกประเภทงาน", "error")
        return redirect(url_for("teacher_subject", subject_id=subject_id))

    group_size = None
    group_method = request.form.get("group_method", "random")
    group_numbers = {}
    students = []
    if assignment_type == "group":
        if group_method not in ("random", "manual"):
            flash("กรุณาเลือกวิธีจัดกลุ่ม", "error")
            return redirect(url_for("teacher_subject", subject_id=subject_id))
        students = [e.student for e in subject.enrollments]
        if not students:
            flash("ยังไม่มีนักเรียนลงทะเบียนวิชานี้ จึงจัดกลุ่มไม่ได้", "error")
            return redirect(url_for("teacher_subject", subject_id=subject_id))
        if group_method == "random":
            try:
                group_size = int(request.form.get("group_size", ""))
                if group_size < 1:
                    raise ValueError
            except ValueError:
                flash("กรุณากรอกจำนวนคนต่อกลุ่มให้ถูกต้อง", "error")
                return redirect(url_for("teacher_subject", subject_id=subject_id))
        else:
            try:
                total_groups = int(request.form.get("group_count", ""))
                if not 1 <= total_groups <= len(students):
                    raise ValueError
                for student in students:
                    number = int(request.form.get(f"group_{student.id}", ""))
                    if not 1 <= number <= total_groups:
                        raise ValueError
                    group_numbers[student.id] = number
            except ValueError:
                flash("กรุณาเลือกจำนวนกลุ่ม (ไม่เกินจำนวนนักเรียน) และเลือกกลุ่มให้นักเรียนทุกคน", "error")
                return redirect(url_for("teacher_subject", subject_id=subject_id))
            empty = [str(n) for n in range(1, total_groups + 1) if n not in set(group_numbers.values())]
            if empty:
                flash(f"กลุ่ม {', '.join(empty)} ยังไม่มีสมาชิก — ลดจำนวนกลุ่มหรือย้ายนักเรียนเข้ากลุ่มนั้น", "error")
                return redirect(url_for("teacher_subject", subject_id=subject_id))

    a = Assignment(
        subject_id=subject_id,
        title=title,
        description=request.form.get("description", "").strip(),
        example_text=request.form.get("example_text", "").strip(),
        due_date=due_date,
        assignment_type=assignment_type,
        group_size=group_size,
        link_url=request.form.get("link_url", "").strip(),
        link_label=request.form.get("link_label", "").strip(),
        accept_late="accept_late" in request.form,
    )
    db.session.add(a)
    if assignment_type == "group":
        db.session.flush()
        group_count = (add_random_groups(a, students) if group_method == "random"
                       else add_manual_groups(a, students, group_numbers))
    db.session.commit()
    flash(f"เพิ่มงานและจัดกลุ่มเรียบร้อยแล้ว ({group_count} กลุ่ม)" if assignment_type == "group"
          else "เพิ่มงานเรียบร้อยแล้ว", "success")
    return redirect(url_for("teacher_subject", subject_id=subject_id))


@app.route("/teacher/assignment/<int:assignment_id>/edit", methods=["POST"])
@login_required_teacher
def edit_assignment(assignment_id):
    teacher = current_teacher()
    a = db.session.get(Assignment, assignment_id) or abort(404)
    if teacher not in a.subject.teachers:
        abort(403)

    due_date = parse_assignment_due_date(request.form)
    if due_date is None:
        flash("กรุณาระบุวันที่แบบวัน/เดือน/ปีและเวลา 24 ชั่วโมง เช่น 14/10/2026 14:30", "error")
        return redirect(url_for("teacher_subject", subject_id=a.subject_id))

    a.title = request.form.get("title", a.title).strip()
    a.description = request.form.get("description", a.description).strip()
    a.example_text = request.form.get("example_text", a.example_text).strip()
    a.link_url = request.form.get("link_url", a.link_url).strip()
    a.link_label = request.form.get("link_label", a.link_label).strip()
    a.accept_late = "accept_late" in request.form
    a.due_date = due_date
    group_size = request.form.get("group_size")
    if a.assignment_type == "group" and group_size:
        a.group_size = int(group_size)

    db.session.commit()
    flash("แก้ไขงานเรียบร้อยแล้ว", "success")
    return redirect(url_for("teacher_subject", subject_id=a.subject_id))


@app.route("/teacher/assignment/<int:assignment_id>/delete", methods=["POST"])
@login_required_teacher
def delete_assignment(assignment_id):
    teacher = current_teacher()
    a = db.session.get(Assignment, assignment_id) or abort(404)
    if teacher not in a.subject.teachers:
        abort(403)
    subject_id = a.subject_id
    db.session.delete(a)
    db.session.commit()
    flash("ลบงานเรียบร้อยแล้ว", "success")
    return redirect(url_for("teacher_subject", subject_id=subject_id))


@app.route("/teacher/assignment/<int:assignment_id>/toggle_late", methods=["POST"])
@login_required_teacher
def toggle_accept_late(assignment_id):
    """สลับสถานะ 'ยังรับงานสายอยู่' / 'ไม่รับแล้ว' อย่างรวดเร็ว"""
    teacher = current_teacher()
    a = db.session.get(Assignment, assignment_id) or abort(404)
    if teacher not in a.subject.teachers:
        abort(403)
    a.accept_late = not a.accept_late
    db.session.commit()
    flash(f"เปลี่ยนเป็น: {'ยังรับงานสายอยู่' if a.accept_late else 'ปิดรับงานแล้ว'}", "success")
    return redirect(request.referrer or url_for("teacher_subject", subject_id=a.subject_id))


@app.route("/teacher/subject/<int:subject_id>/update_status", methods=["POST"])
@login_required_teacher
def update_status(subject_id):
    """บันทึกสถานะ 'ส่งแล้ว/ยังไม่ส่ง' ของนักเรียนหลายคนพร้อมกัน"""
    teacher = current_teacher()
    subject = db.session.get(Subject, subject_id) or abort(404)
    if teacher not in subject.teachers:
        abort(403)
    if "row_all_marker" not in request.form:
        abort(400)

    students = [e.student for e in subject.enrollments]
    assignments = Assignment.query.filter_by(subject_id=subject_id).all()

    for a in assignments:
        for st in students:
            field = f"submitted_{a.id}_{st.id}"
            s = Submission.query.filter_by(assignment_id=a.id, student_id=st.id).first()
            if not s:
                s = Submission(assignment_id=a.id, student_id=st.id)
                db.session.add(s)
            was_submitted = s.submitted
            s.submitted = field in request.form
            if s.submitted and not was_submitted:
                s.submitted_at = datetime.utcnow()
            elif not s.submitted:
                s.submitted_at = None

    db.session.commit()
    flash("บันทึกสถานะการส่งงานเรียบร้อยแล้ว", "success")
    return redirect(url_for("teacher_subject", subject_id=subject_id))

# --- งานกลุ่ม ---------------------------------------------------------------

def add_random_groups(assignment, students):
    """สร้างกลุ่มและสมาชิกใน transaction ปัจจุบัน (ยังไม่ commit)"""
    shuffled = list(students)
    random.shuffle(shuffled)
    size = assignment.group_size or 4
    chunks = [shuffled[i:i + size] for i in range(0, len(shuffled), size)]
    for idx, chunk in enumerate(chunks, start=1):
        grp = Group(assignment_id=assignment.id, label=f"กลุ่ม {idx}")
        db.session.add(grp)
        db.session.flush()
        for student in chunk:
            db.session.add(Submission(assignment_id=assignment.id,
                                      student_id=student.id, group_id=grp.id))
    return len(chunks)


def add_manual_groups(assignment, students, group_numbers):
    """สร้างกลุ่มตามหมายเลขที่ครูเลือกใน transaction ปัจจุบัน"""
    groups = {}
    for number in sorted(set(group_numbers.values())):
        group = Group(assignment_id=assignment.id, label=f"กลุ่ม {number}")
        db.session.add(group)
        db.session.flush()
        groups[number] = group
    for student in students:
        db.session.add(Submission(assignment_id=assignment.id,
                                  student_id=student.id,
                                  group_id=groups[group_numbers[student.id]].id))
    return len(groups)

@app.route("/teacher/assignment/<int:assignment_id>/randomize_groups", methods=["POST"])
@login_required_teacher
def randomize_groups(assignment_id):
    teacher = current_teacher()
    a = db.session.get(Assignment, assignment_id) or abort(404)
    if teacher not in a.subject.teachers:
        abort(403)
    if a.assignment_type != "group":
        abort(400)

    students = [e.student for e in Enrollment.query.filter_by(subject_id=a.subject_id).all()]
    if not students:
        flash("ยังไม่มีนักเรียนลงทะเบียนวิชานี้ ไม่สามารถแบ่งกลุ่มได้", "error")
        return redirect(url_for("teacher_subject", subject_id=a.subject_id))

    Submission.query.filter(Submission.assignment_id == a.id).delete(synchronize_session=False)
    Group.query.filter(Group.assignment_id == a.id).delete(synchronize_session=False)
    db.session.flush()
    group_count = add_random_groups(a, students)
    db.session.commit()
    flash(f"สุ่มแบ่งกลุ่มเรียบร้อยแล้ว ({group_count} กลุ่ม)", "success")
    return redirect(url_for("teacher_subject", subject_id=a.subject_id))


@app.route("/teacher/assignment/<int:assignment_id>/manual_groups", methods=["POST"])
@login_required_teacher
def manual_groups(assignment_id):
    teacher = current_teacher()
    a = db.session.get(Assignment, assignment_id) or abort(404)
    if teacher not in a.subject.teachers:
        abort(403)
    if a.assignment_type != "group":
        abort(400)

    Submission.query.filter(Submission.assignment_id == a.id).delete(synchronize_session=False)
    Group.query.filter(Group.assignment_id == a.id).delete(synchronize_session=False)
    db.session.commit()

    students = [e.student for e in Enrollment.query.filter_by(subject_id=a.subject_id).all()]
    group_map = {}
    for st in students:
        val = request.form.get(f"group_{st.id}", "").strip()
        if not val:
            continue
        if val not in group_map:
            grp = Group(assignment_id=a.id, label=f"กลุ่ม {val}")
            db.session.add(grp)
            db.session.flush()
            group_map[val] = grp
        db.session.add(Submission(assignment_id=a.id, student_id=st.id, group_id=group_map[val].id))

    db.session.commit()
    flash(f"จัดกลุ่มด้วยตนเองเรียบร้อยแล้ว ({len(group_map)} กลุ่ม)", "success")
    return redirect(url_for("teacher_subject", subject_id=a.subject_id))


@app.route("/teacher/group/<int:group_id>/mark", methods=["POST"])
@login_required_teacher
def mark_group_submitted(group_id):
    """ทำเครื่องหมายว่าทั้งกลุ่มส่งงานแล้ว/ยังไม่ส่ง"""
    teacher = current_teacher()
    grp = db.session.get(Group, group_id) or abort(404)
    a = grp.assignment
    if teacher not in a.subject.teachers:
        abort(403)

    mark_as = request.form.get("mark_as") == "submitted"
    for m in Submission.query.filter_by(group_id=grp.id).all():
        m.submitted = mark_as
        m.submitted_at = datetime.utcnow() if mark_as else None
    db.session.commit()
    flash(f"บันทึก {grp.label}: {'ส่งแล้ว' if mark_as else 'ยังไม่ส่ง'}", "success")
    return redirect(url_for("teacher_assignment", assignment_id=a.id))


# ============================================================================
# ฝั่งผู้ดูแลระบบ
# ============================================================================

def read_uploaded_rows(file):
    """อ่าน CSV/XLSX โดยค้นหาแถวหัวตารางและรองรับหัวคอลัมน์ไทย/อังกฤษ"""
    filename = (file.filename or "").lower()
    raw = file.read()
    if not raw:
        raise ValueError("ไฟล์ว่าง")

    if filename.endswith(".xlsx"):
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        ws = wb.active
        matrix = list(ws.iter_rows(values_only=True))
    elif filename.endswith(".csv"):
        decoded = None
        for encoding in ("utf-8-sig", "cp874", "tis-620", "latin-1"):
            try:
                decoded = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        matrix = list(csv.reader(io.StringIO(decoded or "")))
    else:
        raise ValueError("รองรับเฉพาะ .xlsx และ .csv")

    def norm(value):
        return re.sub(r"[\s_\-–—./()]+", "", str(value or "").strip().lower())

    aliases = {
        "student_id": {"student_id", "studentid", "รหัสนักเรียน", "เลขประจำตัวนักเรียน", "เลขประจำตัว", "รหัสประจำตัว"},
        "name": {"name", "fullname", "studentname", "ชื่อ", "ชื่อ-นามสกุล", "ชื่อนามสกุล", "ชื่อสกุล", "ชื่อ–สกุล", "ชื่อ—สกุล"},
        "grade_level": {"grade_level", "grade", "ชั้น", "ระดับชั้น", "ชั้นเรียน"},
        "class_room": {"class_room", "classroom", "room", "ห้อง", "ห้องเรียน"},
        "roll_number": {"roll_number", "rollnumber", "number", "เลขที่", "ลำดับที่", "ลำดับ"},
        "username": {"username", "user", "ชื่อผู้ใช้", "บัญชีผู้ใช้"},
        "name_en": {"name_en", "nameen", "englishname", "enname", "ชื่ออังกฤษ", "ชื่อภาษาอังกฤษ",
                    "ชื่อนามสกุลอังกฤษ", "ชื่อนามสกุลภาษาอังกฤษ", "ชื่อสกุลอังกฤษ"},
        "is_admin": {"is_admin", "admin", "ผู้ดูแลระบบ"},
    }
    reverse = {norm(alias): key for key, vals in aliases.items() for alias in vals}
    # บางไฟล์มีชื่อโรงเรียน/หัวเรื่องอยู่ก่อนหัวคอลัมน์ จึงค้นหาแถวหัวตารางใน 15 แถวแรก
    header_index = None
    headers = []
    for i, row in enumerate(matrix[:15]):
        mapped = [reverse.get(norm(v), "") for v in row]
        if "student_id" in mapped and "name" in mapped:
            header_index, headers = i, mapped
            break
        if ("username" in mapped or "name_en" in mapped) and "name" in mapped:
            header_index, headers = i, mapped
            break
    if header_index is None:
        raise ValueError("ไม่พบหัวคอลัมน์ชื่อและเลขประจำตัว/ชื่อผู้ใช้")

    results = []
    for row in matrix[header_index + 1:]:
        if row is None or all(v is None or str(v).strip() == "" for v in row):
            continue
        item = {}
        for key, value in zip(headers, row):
            if not key:
                continue
            if value is None:
                item[key] = ""
            elif isinstance(value, float) and value.is_integer():
                item[key] = str(int(value))
            else:
                item[key] = str(value).strip()
        results.append(item)
    return results


MONTHS_TH = ["มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
             "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"]
WEEKDAYS_SUN_FIRST_TH = ["อา", "จ", "อ", "พ", "พฤ", "ศ", "ส"]


def parse_month(value):
    """'2026-10' -> (2026, 10) หรือ None ถ้ารูปแบบไม่ถูกต้อง"""
    match = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", value or "")
    if not match or int(match.group(1)) < 1:
        return None
    return int(match.group(1)), int(match.group(2))


def build_holiday_calendar(holidays, term, draft):
    """ข้อมูลปฏิทินวันหยุดของเดือนที่เลือก (วันที่ติ๊กเลือกไว้ส่งมาใน query ชื่อ sel)"""
    year, month = (parse_month(request.args.get("goto"))
                   or parse_month(request.args.get("calendar_month"))
                   or parse_month(request.args.get("month"))
                   or (thai_now().year, thai_now().month))
    by_date = {h.holiday_date: h for h in holidays}
    selected = set()
    for value in request.args.getlist("sel"):
        try:
            picked = date.fromisoformat(value)
        except ValueError:
            continue
        if picked not in by_date:
            selected.add(picked)

    active = next((h for h in holidays if str(h.id) == request.args.get("holiday")), None)
    term_start = term.term_start_date if term else None
    weeks = calendar.Calendar(firstweekday=6).monthdatescalendar(year, month)
    rows = [[None if d.month != month else {
                "date": d, "iso": d.isoformat(), "holiday": by_date.get(d),
                "term_start": d == term_start,
                "active": by_date.get(d) is active and active is not None,
                "selected": d in selected}
             for d in week] for week in weeks]

    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    editor = None
    if active:
        mine = draft.get("holiday_id") == active.id
        editor = {"holiday": active,
                  "date_text": draft["holiday_date"] if mine else f"{active.holiday_date:%d/%m/%Y}",
                  "description": draft["description"] if mine else active.description,
                  "title": f"แก้ไขวันหยุด {active.holiday_date:%d/%m/%Y}"}
    return {
        "month_value": f"{year}-{month:02d}", "label": f"{MONTHS_TH[month - 1]} {year + 543}",
        "prev_month": f"{prev_year}-{prev_month:02d}", "next_month": f"{next_year}-{next_month:02d}",
        "weekdays": WEEKDAYS_SUN_FIRST_TH, "rows": rows, "editor": editor,
        # วันที่ติ๊กไว้ในเดือนอื่น ส่งต่อเป็น hidden เพื่อไม่ให้หายตอนเปลี่ยนเดือน
        "other_selected": sorted(d.isoformat() for d in selected if (d.year, d.month) != (year, month)),
    }


@app.route("/admin/dashboard")
@login_required_admin
def admin_dashboard():
    ensure_default_holidays()
    term_form = session.pop("term_form_draft", {})
    holiday_form_draft = session.pop("holiday_form_draft", {})
    teachers = Teacher.query.order_by(Teacher.name).all()
    students = Student.query.order_by(Student.student_id).all()
    subjects = Subject.query.filter_by(is_elective=True).order_by(Subject.name).all()
    term = TermSetting.query.first()
    holidays = Holiday.query.order_by(Holiday.holiday_date).all()

    students_by_grade = {g: [] for g in GRADE_LEVELS}
    students_by_grade["ไม่ระบุชั้น"] = []
    for st in students:
        key = st.grade_level if st.grade_level in GRADE_LEVELS else "ไม่ระบุชั้น"
        students_by_grade[key].append(st)

    return render_template(
        "admin_dashboard.html",
        teachers=teachers, students=students, subjects=subjects,
        term=term, term_form=term_form, holidays=holidays,
        cal=build_holiday_calendar(holidays, term, holiday_form_draft),
        grade_levels=GRADE_LEVELS, sections=SECTIONS, students_by_grade=students_by_grade,
    )


# --- ครู ---------------------------------------------------------------

def parse_english_name(name_en):
    """แยกชื่อ-นามสกุลภาษาอังกฤษ คืน (ชื่อจริง, นามสกุล) หรือ None ถ้ารูปแบบไม่ถูกต้อง
    ใช้คำแรกเป็นชื่อจริง และคำสุดท้ายเป็นนามสกุล"""
    tokens = (name_en or "").split()
    if len(tokens) < 2:
        return None
    first = re.sub(r"[^A-Za-z]", "", tokens[0])
    last = re.sub(r"[^A-Za-z]", "", tokens[-1])
    if not first or not last:
        return None
    return first, last


def make_teacher_username(name_en, exclude_id=None):
    """ชื่อผู้ใช้ = ชื่อจริงภาษาอังกฤษ + อักษรแรกของนามสกุล (เช่น Somchai Boonmee → SomchaiB)
    ถ้าซ้ำกับครูท่านอื่นจะต่อเลข 2, 3, ... ให้อัตโนมัติ"""
    parts = parse_english_name(name_en)
    if not parts:
        return None
    base = parts[0].capitalize() + parts[1][0].upper()
    taken = {(t.username or "").casefold() for t in Teacher.query.all() if t.id != exclude_id}
    candidate, n = base, 2
    while candidate.casefold() in taken:
        candidate = f"{base}{n}"
        n += 1
    return candidate


@app.route("/admin/teacher/new", methods=["POST"])
@login_required_admin
def admin_new_teacher():
    name = request.form.get("name", "").strip()
    name_en = " ".join(request.form.get("name_en", "").split())
    if not name:
        flash("กรุณากรอกชื่อ-นามสกุลครู (ภาษาไทย)", "error")
        return redirect(url_for("admin_dashboard"))
    username = make_teacher_username(name_en)
    if not username:
        flash("กรุณากรอกชื่อ-นามสกุลภาษาอังกฤษ (ชื่อจริงและนามสกุล เช่น Somchai Boonmee)", "error")
        return redirect(url_for("admin_dashboard"))
    if Teacher.query.filter_by(name=name).first():
        flash("ชื่อครูนี้มีอยู่ในระบบแล้ว", "error")
        return redirect(url_for("admin_dashboard"))
    # ครูที่เพิ่มใหม่เป็นครูทั่วไปเสมอ; รหัสผ่านเริ่มต้นสุ่มให้และแสดงครั้งเดียว; วิชาครูสร้างเองหรือแอดมินกำหนดที่วิชาเสริม
    password = generate_password()
    teacher = Teacher(name=name, name_en=name_en, username=username, is_admin=False)
    teacher.set_password(password)
    db.session.add(teacher)
    db.session.commit()
    flash(f"เพิ่มครู {name} เรียบร้อยแล้ว — ชื่อผู้ใช้: {username} · รหัสผ่านเริ่มต้น: {password} "
          "(แสดงครั้งนี้ครั้งเดียว กรุณาจดและแจ้งครู ครูเปลี่ยนรหัสผ่านเองได้หลังเข้าสู่ระบบ)", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/teacher/import", methods=["POST"])
@login_required_admin
def admin_import_teachers():
    file = request.files.get("file")
    if not file or file.filename == "":
        flash("กรุณาเลือกไฟล์ก่อนอัปโหลด", "error")
        return redirect(url_for("admin_dashboard"))
    try:
        rows = read_uploaded_rows(file)
    except Exception:
        flash("ไม่สามารถอ่านไฟล์นี้ได้ กรุณาตรวจสอบว่าเป็นไฟล์ .csv หรือ .xlsx ที่ถูกต้อง", "error")
        return redirect(url_for("admin_dashboard"))

    added, skipped, new_logins = 0, 0, []
    for row in rows:
        name = (row.get("name") or "").strip()
        name_en = " ".join((row.get("name_en") or "").split())
        username = (row.get("username") or "").strip()
        if not username and name_en:
            username = make_teacher_username(name_en) or ""
        if not name or not username:
            skipped += 1
            continue
        if Teacher.query.filter((Teacher.name == name) | (Teacher.username == username)).first():
            skipped += 1
            continue
        is_admin_val = (row.get("is_admin") or "").strip().lower() in ("1", "true", "yes", "y", "ใช่")
        password = (row.get("password") or "").strip()
        generated = not password
        if generated:
            password = generate_password()
        teacher = Teacher(name=name, name_en=name_en or None, username=username, is_admin=is_admin_val)
        teacher.set_password(password)
        db.session.add(teacher)
        db.session.commit()  # commit ทีละคน เพื่อให้ชื่อผู้ใช้ที่สร้างอัตโนมัติไม่ซ้ำกันในไฟล์เดียวกัน
        added += 1
        if generated:
            new_logins.append(f"{username}: {password}")
    message = f"นำเข้าครูสำเร็จ {added} คน (ข้าม {skipped} คนที่ชื่อซ้ำหรือข้อมูลไม่ครบ)"
    if new_logins:
        message += " — รหัสผ่านเริ่มต้นที่ระบบสุ่มให้ (แสดงครั้งเดียว กรุณาจดไว้) → " + " | ".join(new_logins)
    flash(message, "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/teacher/<int:teacher_id>/edit", methods=["POST"])
@login_required_admin
def admin_edit_teacher(teacher_id):
    t = db.session.get(Teacher, teacher_id) or abort(404)
    name = request.form.get("name", t.name).strip()
    name_en = " ".join(request.form.get("name_en", "").split())
    if not name:
        flash("กรุณากรอกชื่อ-นามสกุลครู (ภาษาไทย)", "error")
        return redirect(url_for("admin_dashboard"))
    if Teacher.query.filter(Teacher.name == name, Teacher.id != t.id).first():
        flash("ชื่อครูนี้มีอยู่ในระบบแล้ว", "error")
        return redirect(url_for("admin_dashboard"))
    msg = "แก้ไขข้อมูลครูเรียบร้อยแล้ว"
    if name_en:
        if not parse_english_name(name_en):
            flash("ชื่อภาษาอังกฤษต้องมีชื่อจริงและนามสกุล เช่น Somchai Boonmee", "error")
            return redirect(url_for("admin_dashboard"))
        if name_en != (t.name_en or ""):
            t.username = make_teacher_username(name_en, exclude_id=t.id)
            msg += f" — ชื่อผู้ใช้ใหม่: {t.username}"
        t.name_en = name_en
    # เว้นชื่อภาษาอังกฤษว่าง = ใช้ชื่อผู้ใช้เดิมต่อไป
    t.name = name
    t.is_admin = "is_admin" in request.form
    db.session.commit()
    flash(msg, "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/teacher/<int:teacher_id>/reset_password", methods=["POST"])
@login_required_admin
def admin_reset_teacher_password(teacher_id):
    t = db.session.get(Teacher, teacher_id) or abort(404)
    password = generate_password()
    t.set_password(password)
    db.session.commit()
    flash(f"รีเซ็ตรหัสผ่านของ {t.name} แล้ว — รหัสผ่านใหม่: {password} "
          "(แสดงครั้งนี้ครั้งเดียว กรุณาจดและแจ้งครู)", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/teacher/password", methods=["GET", "POST"])
@login_required_teacher
def change_password():
    """ครูเปลี่ยนรหัสผ่านของตัวเอง"""
    teacher = current_teacher()
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not teacher.check_password(current):
            flash("รหัสผ่านปัจจุบันไม่ถูกต้อง", "error")
        elif len(new) < MIN_PASSWORD_LENGTH:
            flash(f"รหัสผ่านใหม่ต้องยาวอย่างน้อย {MIN_PASSWORD_LENGTH} ตัวอักษร", "error")
        elif new != confirm:
            flash("รหัสผ่านใหม่ทั้งสองช่องไม่ตรงกัน", "error")
        elif new == current:
            flash("รหัสผ่านใหม่ต้องไม่ซ้ำกับรหัสเดิม", "error")
        else:
            teacher.set_password(new)
            db.session.commit()
            flash("เปลี่ยนรหัสผ่านเรียบร้อยแล้ว", "success")
            return redirect(url_for("teacher_dashboard"))
    return render_template("change_password.html", min_length=MIN_PASSWORD_LENGTH)


@app.route("/admin/teacher/<int:teacher_id>/delete", methods=["POST"])
@login_required_admin
def admin_delete_teacher(teacher_id):
    t = db.session.get(Teacher, teacher_id) or abort(404)
    if t.id == session.get("user_id"):
        return respond_action(False, "ไม่สามารถลบบัญชีที่กำลังใช้งานอยู่ได้")
    # วิชาที่ครูคนนี้สอนอยู่คนเดียวถูกลบ (ไม่ทิ้งวิชาที่ไม่มีครู) ส่วนวิชาที่มีครูท่านอื่นสอนร่วมยังอยู่
    mine = set(t.taught_subjects) | set(Subject.query.filter_by(teacher_id=t.id).all())
    deleted_subjects = []
    for subj in mine:
        others = [x for x in subj.teachers if x.id != t.id]
        if others:
            if subj.teacher_id == t.id:
                subj.teacher_id = others[0].id
        else:
            deleted_subjects.append(subj.id)
            db.session.delete(subj)
    name = t.name
    db.session.delete(t)
    db.session.commit()
    return respond_action(True, f"ลบครู {name} เรียบร้อยแล้ว", teacher_id=teacher_id,
                          deleted_subject_ids=deleted_subjects)


# --- นักเรียน -----------------------------------------------------------

@app.route("/admin/student/new", methods=["POST"])
@login_required_admin
def admin_new_student():
    student_id = request.form.get("student_id", "").strip()
    name = request.form.get("name", "").strip()
    grade_level = request.form.get("grade_level", "").strip()
    class_room = request.form.get("class_room", "").strip()
    roll_raw = request.form.get("roll_number", "").strip()
    roll_number = int(roll_raw) if roll_raw.isdigit() else None
    if student_id and name:
        db.session.add(Student(student_id=student_id, name=name,
                                grade_level=grade_level, class_room=class_room, roll_number=roll_number))
        db.session.commit()
        sync_all_class_subjects()
        flash("เพิ่มนักเรียนเรียบร้อยแล้ว", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/student/import", methods=["POST"])
@login_required_admin
def admin_import_students():
    file = request.files.get("file")
    if not file or file.filename == "":
        flash("กรุณาเลือกไฟล์ก่อนอัปโหลด", "error")
        return redirect(url_for("admin_dashboard"))
    try:
        rows = read_uploaded_rows(file)
    except Exception as exc:
        flash(f"อ่านไฟล์ไม่ได้: {exc}", "error")
        return redirect(url_for("admin_dashboard"))

    # ระบุชั้น/ห้องสำรองจากชื่อไฟล์ เช่น ม.2-1.xlsx, 2-1.xlsx หรือ ม2_1.xlsx
    filename_hint = file.filename or ""
    class_match = re.search(r"(?:ม\.?\s*)?([1-6])\s*[-_ ]\s*([12])(?:\s*\([^)]*\))?\s*(?:\.[^.]+)?$", filename_hint)
    filename_grade = f"ม.{class_match.group(1)}" if class_match else ""
    filename_room = class_match.group(2) if class_match else ""

    # อนุญาตเฉพาะ 5 ช่องข้อมูลนักเรียนที่ระบบใช้จริง คอลัมน์อื่นในไฟล์จะถูกละทิ้ง
    allowed_fields = ("student_id", "name", "grade_level", "class_room", "roll_number")
    added, skipped = 0, 0
    for source_row in rows:
        row = {key: (source_row.get(key) or "").strip() for key in allowed_fields}
        # รับชั้น/ห้องที่อยู่รวมในคอลัมน์ เช่น 2/1, ม.2/1, ม.2 ห้อง 1
        combined = " ".join(str(source_row.get(k) or "") for k in
                            ("grade_level", "class_room", "class", "class_label", "ระดับชั้น/ห้อง"))
        combined_match = re.search(r"(?:ม\.?\s*)?([1-6])\s*(?:/|ห้อง\s*)\s*([12])(?:\D|$)", combined, re.IGNORECASE)
        if combined_match:
            row["grade_level"] = f"ม.{combined_match.group(1)}"
            row["class_room"] = combined_match.group(2)
        else:
            # ค่าชั้นล้วนจากคอลัมน์ชั้น และห้องจากคอลัมน์ห้อง
            grade_match = re.search(r"(?:ม\.?\s*)?([1-6])(?:\D|$)", row["grade_level"])
            if grade_match:
                row["grade_level"] = f"ม.{grade_match.group(1)}"
            if not row["class_room"].strip() and filename_room:
                row["class_room"] = filename_room
            if not row["grade_level"].strip() and filename_grade:
                row["grade_level"] = filename_grade
        student_id = row["student_id"]
        name = row["name"]
        if not student_id or not name:
            continue
        if Student.query.filter_by(student_id=student_id).first():
            skipped += 1
            continue
        db.session.add(Student(
            student_id=student_id,
            name=name,
            grade_level=row["grade_level"],
            class_room=row["class_room"],
            roll_number=int(row["roll_number"]) if row["roll_number"].isdigit() else None,
        ))
        added += 1
    db.session.commit()
    sync_all_class_subjects()
    flash(f"นำเข้านักเรียนสำเร็จ {added} คน (ข้าม {skipped} คนที่มีเลขประจำตัวซ้ำอยู่แล้ว)", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/student/bulk-edit", methods=["POST"])
@login_required_admin
def admin_bulk_edit_students():
    raw_ids = request.form.getlist("student_ids")
    ids = []
    for raw in raw_ids:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    chosen = Student.query.filter(Student.id.in_(ids)).all() if ids else []
    grade = request.form.get("bulk_grade", "").strip()
    room = request.form.get("bulk_room", "").strip()
    roll_raw = request.form.get("bulk_roll_start", "").strip()
    roll_start = int(roll_raw) if roll_raw.isdigit() and int(roll_raw) > 0 else None
    if not chosen:
        flash("กรุณาเลือกนักเรียนอย่างน้อย 1 คน", "error")
        return redirect(url_for("admin_dashboard"))
    if not grade and not room and roll_start is None:
        flash("กรุณากำหนดข้อมูลที่ต้องการแก้ไขอย่างน้อยหนึ่งรายการ", "error")
        return redirect(url_for("admin_dashboard"))
    # deterministic roll order: current roll then student ID
    chosen.sort(key=lambda st: (st.roll_number is None, st.roll_number or 0, st.student_id))
    for offset, st in enumerate(chosen):
        if grade:
            st.grade_level = grade
        if room:
            st.class_room = room
        if roll_start is not None:
            st.roll_number = roll_start + offset
    db.session.commit()
    sync_all_class_subjects()
    flash(f"แก้ไขข้อมูลนักเรียน {len(chosen)} คนเรียบร้อยแล้ว", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/student/<int:student_id>/edit", methods=["POST"])
@login_required_admin
def admin_edit_student(student_id):
    st = db.session.get(Student, student_id) or abort(404)
    st.student_id = request.form.get("student_id", st.student_id).strip()
    st.name = request.form.get("name", st.name).strip()
    st.grade_level = request.form.get("grade_level", st.grade_level).strip()
    st.class_room = request.form.get("class_room", st.class_room).strip()
    roll_raw = request.form.get("roll_number", "").strip()
    st.roll_number = int(roll_raw) if roll_raw.isdigit() else None
    db.session.commit()
    sync_all_class_subjects()
    flash("แก้ไขข้อมูลนักเรียนเรียบร้อยแล้ว", "success")
    return redirect(url_for("admin_dashboard"))


def delete_students_and_related(student_pks):
    """ลบนักเรียนพร้อมสถานะการส่งงานของนักเรียนเหล่านั้น (กันข้อมูลค้างที่อ้างถึงนักเรียนที่ไม่มีแล้ว)"""
    for sub in Submission.query.filter(Submission.student_id.in_(student_pks)).all():
        db.session.delete(sub)
    count = 0
    for st in Student.query.filter(Student.id.in_(student_pks)).all():
        db.session.delete(st)  # รายการลงทะเบียนเรียนถูกลบตาม (cascade)
        count += 1
    db.session.commit()
    return count


@app.route("/admin/student/<int:student_id>/delete", methods=["POST"])
@login_required_admin
def admin_delete_student(student_id):
    db.session.get(Student, student_id) or abort(404)
    delete_students_and_related([student_id])
    flash("ลบนักเรียนเรียบร้อยแล้ว", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/student/bulk-delete", methods=["POST"])
@login_required_admin
def admin_bulk_delete_students():
    """ลบนักเรียนที่เลือกหลายคนพร้อมกัน"""
    ids = request.form.getlist("student_ids", type=int)
    if not ids:
        flash("กรุณาเลือกนักเรียนที่ต้องการลบอย่างน้อย 1 คน", "error")
        return redirect(url_for("admin_dashboard"))
    count = delete_students_and_related(ids)
    flash(f"ลบนักเรียน {count} คนเรียบร้อยแล้ว", "success")
    return redirect(url_for("admin_dashboard"))


# --- วิชาเสริม ------------------------------------------------------------
# แอดมินสร้างวิชาเสริมและกำหนดครูผู้สอน (หลายคนได้) เท่านั้น
# ส่วนการเพิ่มนักเรียนเข้าวิชา ครูผู้สอนของวิชานั้นทำเอง (ดู teacher_enroll_students)

def elective_or_404(subject_id):
    subj = db.session.get(Subject, subject_id) or abort(404)
    if not subj.is_elective:
        abort(404)
    return subj


@app.route("/admin/subject/new", methods=["POST"])
@login_required_admin
def admin_new_subject():
    """แอดมินสร้างวิชาเสริมใหม่ พร้อมเลือกครูผู้สอนได้หลายคน"""
    name = request.form.get("name", "").strip()
    teacher_ids = request.form.getlist("teacher_ids", type=int)

    if not name:
        flash("กรุณากรอกชื่อวิชาเสริม", "error")
        return redirect(url_for("admin_dashboard"))
    teachers = Teacher.query.filter(Teacher.id.in_(teacher_ids)).all() if teacher_ids else []
    if not teachers:
        flash("กรุณาเลือกครูผู้สอนอย่างน้อย 1 คน", "error")
        return redirect(url_for("admin_dashboard"))

    subj = Subject(name=name,
                   code=request.form.get("code", "").strip(),
                   room=request.form.get("room", "").strip(),
                   teacher_id=teachers[0].id,
                   is_elective=True)
    subj.teachers = teachers
    db.session.add(subj)
    db.session.commit()
    flash(f"สร้างวิชาเสริม '{name}' เรียบร้อยแล้ว (ครูผู้สอน {len(teachers)} คน) "
          "— ครูผู้สอนเพิ่มนักเรียนเองได้ที่หน้าวิชา", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/subject/<int:subject_id>/edit", methods=["POST"])
@login_required_admin
def admin_edit_subject(subject_id):
    subj = elective_or_404(subject_id)
    name = request.form.get("name", "").strip()
    if not name:
        flash("กรุณากรอกชื่อวิชาเสริม", "error")
        return redirect(url_for("admin_dashboard"))
    teacher_ids = request.form.getlist("teacher_ids", type=int)
    teachers = Teacher.query.filter(Teacher.id.in_(teacher_ids)).all() if teacher_ids else []
    if not teachers:
        flash("วิชาเสริมต้องมีครูผู้สอนอย่างน้อย 1 คน", "error")
        return redirect(url_for("admin_dashboard"))
    subj.name = name
    subj.code = request.form.get("code", subj.code).strip()
    subj.room = request.form.get("room", subj.room).strip()
    subj.teachers = teachers
    subj.teacher_id = teachers[0].id
    db.session.commit()
    flash("แก้ไขวิชาเสริมเรียบร้อยแล้ว", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/subject/<int:subject_id>/delete", methods=["POST"])
@login_required_admin
def admin_delete_subject(subject_id):
    subj = elective_or_404(subject_id)
    name = subj.name
    db.session.delete(subj)
    db.session.commit()
    return respond_action(True, f"ลบวิชาเสริม {name} เรียบร้อยแล้ว", subject_id=subject_id)


# --- เทอม / วันหยุด -------------------------------------------------------

def redirect_holiday_calendar(month=None, holiday_id=None):
    """กลับไปยังเดือนที่เลือกในปฏิทิน หลังเพิ่ม/แก้ไข/ลบวันหยุด"""
    if month is None:
        month = request.form.get("calendar_month")
    try:
        if len(month) != 7:
            raise ValueError
        date.fromisoformat(f"{month}-01")
    except (TypeError, ValueError):
        month = None
    return redirect(url_for("admin_dashboard", month=month, holiday=holiday_id,
                            _anchor="holidayCalendar"))

@app.route("/admin/term/edit", methods=["POST"])
@login_required_admin
def admin_edit_term():
    submitted_dates = {
        "term_start_date": (request.form.get("term_start_date") or "").strip(),
        "term_end_date": (request.form.get("term_end_date") or "").strip(),
    }
    try:
        start_date = parse_day_month_year(submitted_dates["term_start_date"])
        end_date = parse_day_month_year(submitted_dates["term_end_date"])
    except ValueError:
        session["term_form_draft"] = submitted_dates
        flash("กรุณาระบุวันเปิดเทอมและวันสิ้นสุดเทอมแบบวัน/เดือน/ปี เช่น 14/10/2026", "error")
        return redirect(url_for("admin_dashboard"))
    if start_date >= end_date:
        session["term_form_draft"] = submitted_dates
        flash(
            f"วันเปิดเทอม {start_date:%d/%m/%Y} ต้องมาก่อนวันสิ้นสุดเทอม "
            f"{end_date:%d/%m/%Y} หากเป็นเทอมใหม่ให้เปลี่ยนวันสิ้นสุดเทอมด้วย",
            "error",
        )
        return redirect(url_for("admin_dashboard"))

    term = TermSetting.query.first()
    if not term:
        term = TermSetting()
        db.session.add(term)
    term.term_start_date = start_date
    term.term_end_date = end_date
    db.session.commit()
    ensure_default_holidays()
    flash("ตั้งค่าวันเปิดเทอมและวันสิ้นสุดเทอมเรียบร้อยแล้ว", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/holiday/bulk_new", methods=["POST"])
@login_required_admin
def admin_bulk_new_holiday():
    """เพิ่มวันหยุดหลายวันพร้อมกัน (จากปฏิทินที่จิ้มเลือกหลายวัน)"""
    description = request.form.get("description", "").strip()
    date_list = [d.strip() for d in request.form.getlist("sel") if d.strip()]

    if not date_list:
        flash("กรุณาเลือกอย่างน้อย 1 วันก่อนบันทึก", "error")
        return redirect_holiday_calendar()

    added, skipped = 0, 0
    for d in date_list:
        try:
            hd = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            continue
        if Holiday.query.filter_by(holiday_date=hd).first():
            skipped += 1
            continue
        db.session.add(Holiday(holiday_date=hd, description=description))
        added += 1
    db.session.commit()
    flash(f"เพิ่มวันหยุดสำเร็จ {added} วัน (ข้าม {skipped} วันที่มีอยู่แล้ว)", "success")
    return redirect_holiday_calendar()


@app.route("/admin/holiday/<int:holiday_id>/delete", methods=["POST"])
@login_required_admin
def admin_delete_holiday(holiday_id):
    h = db.session.get(Holiday, holiday_id) or abort(404)
    db.session.delete(h)
    db.session.commit()
    flash("ลบวันหยุดเรียบร้อยแล้ว", "success")
    return redirect_holiday_calendar()


@app.route("/admin/holiday/<int:holiday_id>/edit", methods=["POST"])
@login_required_admin
def admin_edit_holiday(holiday_id):
    holiday = db.session.get(Holiday, holiday_id) or abort(404)
    submitted_date = (request.form.get("holiday_date") or "").strip()
    description = (request.form.get("description") or "").strip()
    try:
        new_date = parse_day_month_year(submitted_date)
    except ValueError:
        session["holiday_form_draft"] = {"holiday_id": holiday_id,
                                          "holiday_date": submitted_date,
                                          "description": description[:200]}
        flash("กรุณาระบุวันที่แบบวัน/เดือน/ปี เช่น 14/10/2026", "error")
        return redirect_holiday_calendar(holiday_id=holiday_id)

    duplicate = Holiday.query.filter_by(holiday_date=new_date).first()
    if duplicate and duplicate.id != holiday.id:
        session["holiday_form_draft"] = {"holiday_id": holiday_id,
                                          "holiday_date": submitted_date,
                                          "description": description[:200]}
        flash("มีวันหยุดวันที่นี้อยู่แล้ว", "error")
        return redirect_holiday_calendar(holiday_id=holiday_id)

    holiday.holiday_date = new_date
    holiday.description = description
    db.session.commit()
    flash("แก้ไขวันหยุดเรียบร้อยแล้ว", "success")
    return redirect_holiday_calendar(month=new_date.strftime("%Y-%m"))


# ============================================================================
# เริ่มต้นฐานข้อมูล
# ============================================================================

# วันหยุดราชการทั่วประเทศ พ.ศ. 2569 (ค.ศ. 2026) และวันหยุดชดเชย
# อ้างอิง: สำนักเลขาธิการคณะรัฐมนตรี https://www.soc.go.th/?p=33672
# และรายการวันหยุดราชการของ ม.สงขลานครินทร์ https://hr.psu.ac.th/component/sppagebuilder/?id=169&view=page
# ไม่รวมวันแรงงาน (ไม่ใช่วันหยุดราชการ) และวันหยุดพิเศษเฉพาะพื้นที่
PUBLIC_HOLIDAYS_2026 = (
    ("2026-01-01", "วันขึ้นปีใหม่"),
    ("2026-01-02", "วันหยุดราชการกรณีพิเศษ"),
    ("2026-03-03", "วันมาฆบูชา"),
    ("2026-04-06", "วันจักรี"),
    ("2026-04-13", "วันสงกรานต์"),
    ("2026-04-14", "วันสงกรานต์"),
    ("2026-04-15", "วันสงกรานต์"),
    ("2026-05-04", "วันฉัตรมงคล"),
    ("2026-05-13", "วันพืชมงคล"),
    ("2026-05-31", "วันวิสาขบูชา"),
    ("2026-06-01", "วันหยุดชดเชยวันวิสาขบูชา"),
    ("2026-06-03", "วันเฉลิมพระชนมพรรษาสมเด็จพระนางเจ้าฯ พระบรมราชินี"),
    ("2026-07-28", "วันเฉลิมพระชนมพรรษาพระบาทสมเด็จพระเจ้าอยู่หัว"),
    ("2026-07-29", "วันอาสาฬหบูชา"),
    ("2026-07-30", "วันเข้าพรรษา"),
    ("2026-08-12", "วันแม่แห่งชาติ และวันเฉลิมพระชนมพรรษาพระบรมราชชนนีพันปีหลวง"),
    ("2026-10-13", "วันนวมินทรมหาราช"),
    ("2026-10-23", "วันปิยมหาราช"),
    ("2026-12-05", "วันชาติ และวันพ่อแห่งชาติ"),
    ("2026-12-07", "วันหยุดชดเชยวันชาติและวันพ่อแห่งชาติ"),
    ("2026-12-10", "วันรัฐธรรมนูญ"),
    ("2026-12-31", "วันสิ้นปี"),
)


def public_holidays_for_year(year):
    """วันหยุดราชการทั่วประเทศ: ปี 2569 ยึดวันประกาศ ส่วนปีอื่นใช้ปฏิทินไทย"""
    if year == 2026:
        return [(date.fromisoformat(day), name) for day, name in PUBLIC_HOLIDAYS_2026]
    calendar = holiday_calendar_lib.country_holidays(
        "TH", years=year, categories=(PUBLIC, GOVERNMENT), language="th"
    )
    # วันเด็กเป็นวันเสาร์; วันแรงงาน/วันชดเชยเป็นวันหยุดแรงงาน ไม่ใช่วันหยุดราชการ
    return sorted((day, name[:200]) for day, name in calendar.items()
                  if "เด็กแห่งชาติ" not in name and "แรงงาน" not in name)


def seed_public_holidays(year):
    """เพิ่มค่าเริ่มต้นของปีครั้งเดียว โดยไม่ทับวันหยุดที่ผู้ดูแลเคยแก้หรือลบ"""
    if db.session.get(HolidaySeed, year):
        return
    existing_dates = {h.holiday_date for h in Holiday.query.filter(
        Holiday.holiday_date.between(date(year, 1, 1), date(year, 12, 31))
    ).all()}
    for holiday_date, description in public_holidays_for_year(year):
        if holiday_date not in existing_dates:
            db.session.add(Holiday(holiday_date=holiday_date, description=description))
    db.session.add(HolidaySeed(year=year))
    db.session.commit()


def ensure_default_holidays():
    """เมื่อเปิดระบบ/หน้าหลัก เติมปีปัจจุบันและปีถัดไป (ตามเวลาไทย)"""
    this_year = thai_now().year
    for year in sorted({2026, this_year, this_year + 1}):
        seed_public_holidays(year)


def migrate_term_start_date():
    """เพิ่มวันเปิดเทอมใน SQLite เดิมโดยไม่ลบวันปิดเทอมหรือข้อมูลอื่น"""
    columns = db.session.execute(sql_text("PRAGMA table_info(term_settings)")).all()
    if "term_start_date" not in {column[1] for column in columns}:
        db.session.execute(sql_text(
            "ALTER TABLE term_settings ADD COLUMN term_start_date DATE"
        ))
        db.session.commit()


def migrate_subject_elective():
    """เพิ่มคอลัมน์ is_elective ให้ฐานข้อมูลเก่า
    วิชาเดิมที่มีครูสอนตั้งแต่ 2 คนขึ้นไป (วิชาที่แอดมินเคยสร้าง เช่น ชุมนุม) จะถูกมองเป็นวิชาเสริม"""
    cols = {c[1] for c in db.session.execute(sql_text("PRAGMA table_info(subjects)")).all()}
    if "is_elective" not in cols:
        db.session.execute(sql_text(
            "ALTER TABLE subjects ADD COLUMN is_elective BOOLEAN NOT NULL DEFAULT 0"))
        db.session.execute(sql_text(
            "UPDATE subjects SET is_elective = 1 WHERE id IN "
            "(SELECT subject_id FROM subject_teachers GROUP BY subject_id HAVING COUNT(*) >= 2)"))
        db.session.commit()


def migrate_teacher_student_fields():
    teacher_cols = {c[1] for c in db.session.execute(sql_text("PRAGMA table_info(teachers)")).all()}
    if "username" not in teacher_cols:
        db.session.execute(sql_text("ALTER TABLE teachers ADD COLUMN username VARCHAR(120) NOT NULL DEFAULT ''"))
    if "name_en" not in teacher_cols:
        db.session.execute(sql_text("ALTER TABLE teachers ADD COLUMN name_en VARCHAR(120)"))
    student_cols = {c[1] for c in db.session.execute(sql_text("PRAGMA table_info(students)")).all()}
    if "roll_number" not in student_cols:
        db.session.execute(sql_text("ALTER TABLE students ADD COLUMN roll_number INTEGER"))
    db.session.commit()
    # Fill legacy teacher usernames from existing names, then make them editable by admin.
    for t in Teacher.query.all():
        if not t.username:
            safe = ''.join(ch for ch in (t.name or '').lower() if ch.isascii() and ch.isalnum()) or f"teacher{t.id}"
            candidate = safe
            suffix = 2
            while Teacher.query.filter(Teacher.username == candidate, Teacher.id != t.id).first():
                candidate = f"{safe}{suffix}"; suffix += 1
            t.username = candidate
        # ฐานข้อมูลรุ่นเก่าเก็บรหัสผ่านเป็นข้อความธรรมดา: แปลงเป็นแฮชโดยคงรหัสเดิมไว้
        if t.password and not t.password_is_hashed:
            t.set_password(t.password)
    db.session.commit()


DB_PATH = os.path.join(BASE_DIR, "homework_tracker.db")
DEMO_PASSWORD = "demo1234"


def init_db_with_demo_data():
    # ข้อมูลตัวอย่างใส่เฉพาะตอนสร้างไฟล์ฐานข้อมูลใหม่เท่านั้น
    # ถ้ามีไฟล์อยู่แล้ว ข้อมูลที่แก้/ลบไว้จะอยู่ตามเดิมทุกครั้งที่รันใหม่
    is_new_db = not os.path.exists(DB_PATH)
    db.create_all()
    migrate_term_start_date()
    migrate_subject_elective()
    migrate_teacher_student_fields()
    ensure_default_holidays()

    # Ensure a predictable administrator account exists even when the database
    # already contains legacy teacher records.
    admin_user = Teacher.query.filter_by(username="admin").first()
    if admin_user:
        admin_user.is_admin = True
    else:
        # รหัสผ่านแอดมินตั้งผ่านตัวแปร ADMIN_PASSWORD ถ้าไม่ตั้งจะสุ่มให้และพิมพ์ที่หน้าจอ (ครั้งเดียว)
        admin_password = os.environ.get("ADMIN_PASSWORD") or generate_password(12)
        admin_user = Teacher(name="ผู้ดูแลระบบ", username="admin", is_admin=True)
        admin_user.set_password(admin_password)
        db.session.add(admin_user)
        if not os.environ.get("ADMIN_PASSWORD"):
            print(f"[ตั้งค่าครั้งแรก] สร้างบัญชีแอดมิน username=admin password={admin_password}  (จดไว้ แล้วเปลี่ยนรหัสผ่านหลังเข้าสู่ระบบ)")
    db.session.commit()

    if not is_new_db:
        return

    # บัญชีตัวอย่างสำหรับทดลองเท่านั้น (รหัสผ่านตามที่ระบุใน README) — ใช้งานจริงให้ลบบัญชีเหล่านี้
    t1 = Teacher(name="ครูสมชาย", username="somchaib", is_admin=True)
    t2 = Teacher(name="ครูวิภา", username="wiphas")
    t1.set_password(DEMO_PASSWORD)
    t2.set_password(DEMO_PASSWORD)
    db.session.add_all([t1, t2])
    db.session.commit()

    math = Subject(name="คณิตศาสตร์", code="MA101", room="101", teacher_id=t1.id)
    sci = Subject(name="วิทยาศาสตร์", code="SC101", room="Lab1", teacher_id=t1.id)
    eng = Subject(name="ภาษาอังกฤษ", code="EN101", room="205", teacher_id=t2.id)
    club = Subject(name="ชุมนุมดนตรี", code="CLB01", room="ห้องดนตรี", teacher_id=t1.id,
                   is_elective=True)
    db.session.add_all([math, sci, eng, club])
    db.session.flush()
    math.teachers = [t1]
    sci.teachers = [t1]
    eng.teachers = [t2]
    club.teachers = [t1, t2]
    db.session.commit()

    s1 = Student(student_id="12345", name="ด.ช. สมศักดิ์ ใจดี", grade_level="ม.2", class_room="1")
    s2 = Student(student_id="12346", name="ด.ญ. สายฝน แจ่มใส", grade_level="ม.2", class_room="1")
    s3 = Student(student_id="12347", name="ด.ช. วิชัย ตั้งใจ", grade_level="ม.2", class_room="1")
    s4 = Student(student_id="12348", name="ด.ญ. อารีย์ ขยัน", grade_level="ม.2", class_room="2")
    db.session.add_all([s1, s2, s3, s4])
    db.session.commit()

    # วิชาปกติผูกกับชั้น/ห้อง — นักเรียนในห้องได้รับงานอัตโนมัติ
    for subj in (math, sci, eng):
        subj.classes = [SubjectClass(grade_level="ม.2", section=1),
                        SubjectClass(grade_level="ม.2", section=2)]
    db.session.commit()
    for subj in (math, sci, eng):
        sync_subject_enrollments(subj)
    # วิชาเสริม: ครูเพิ่มนักเรียนเอง
    for st in (s1, s2, s3, s4):
        db.session.add(Enrollment(student_id=st.id, subject_id=club.id))
    db.session.commit()

    now = thai_now()
    a1 = Assignment(
        subject_id=math.id, title="แบบฝึกหัดบทที่ 1",
        description="ทำโจทย์หน้า 10-15 ในหนังสือเรียน เขียนแสดงวิธีทำทุกข้อลงในสมุด\nส่งที่โต๊ะครูหน้าห้องพักครู",
        example_text="ตัวอย่างการเขียนวิธีทำ: ข้อ 1) โจทย์ถามหา x จากสมการ 2x+3=11 → 2x=8 → x=4",
        due_date=now - timedelta(days=5), assignment_type="individual",
        link_url="https://www.mathisfun.com/algebra/index.html",
        link_label="ทบทวนเนื้อหาพีชคณิตเพิ่มเติม",
        accept_late=True,
    )
    a2 = Assignment(
        subject_id=math.id, title="รายงานเรขาคณิต",
        description="จัดทำรายงานเรื่องรูปทรงเรขาคณิตในชีวิตประจำวัน ความยาว 3-5 หน้า ส่งเป็นไฟล์ PDF",
        example_text="โครงร่างรายงาน: ปก / คำนำ / เนื้อหา / สรุป / อ้างอิง",
        due_date=now + timedelta(days=1), assignment_type="individual",
        link_url="https://docs.google.com/forms",
        link_label="ฟอร์มส่งงาน (อัปโหลดไฟล์ PDF ที่นี่)",
        accept_late=True,
    )
    a3 = Assignment(
        subject_id=math.id, title="ใบงานเรื่องเศษส่วน",
        description="ทำใบงานที่ครูแจกในคาบ ส่งภายในกำหนด ไม่รับงานสายเด็ดขาด",
        due_date=now - timedelta(days=2), assignment_type="individual",
        accept_late=False,
    )
    a4 = Assignment(
        subject_id=sci.id, title="ใบงานทดลองวิทยาศาสตร์",
        description="สรุปผลการทดลองเรื่องการเปลี่ยนสถานะของสสาร พร้อมวาดแผนภาพประกอบ",
        due_date=now + timedelta(days=10), assignment_type="individual",
        link_url="https://phet.colorado.edu/",
        link_label="ห้องทดลองเสมือนจริง PhET",
        accept_late=True,
    )
    a5 = Assignment(
        subject_id=math.id, title="โครงงานกลุ่ม: สำรวจสถิติในห้องเรียน",
        description="แบ่งกลุ่มละ 2 คน เลือกหัวข้อสำรวจ เก็บข้อมูลจากเพื่อนในห้อง แล้วนำเสนอเป็นแผนภูมิ",
        example_text="ตัวอย่างหัวข้อ: กีฬาที่ชอบ, วิธีเดินทางมาโรงเรียน, เวลานอนเฉลี่ย",
        due_date=now + timedelta(days=14), assignment_type="group", group_size=2,
        accept_late=True,
    )
    db.session.add_all([a1, a2, a3, a4, a5])
    db.session.commit()

    # ตัวอย่างสถานะ: บางคนส่งแล้ว บางคนยังไม่ส่ง
    db.session.add(Submission(assignment_id=a1.id, student_id=s1.id, submitted=True,
                               submitted_at=now - timedelta(days=6)))
    db.session.add(Submission(assignment_id=a1.id, student_id=s2.id, submitted=True,
                               submitted_at=now - timedelta(days=6)))
    db.session.add(Submission(assignment_id=a4.id, student_id=s1.id, submitted=True,
                               submitted_at=now))
    db.session.add(Submission(assignment_id=a3.id, student_id=s2.id, submitted=True,
                               submitted_at=now - timedelta(days=3)))
    db.session.commit()

    slots = [
        ScheduleSlot(teacher_id=t1.id, subject_id=math.id, day_of_week=0, period=1,
                      duration=1, grade_level="ม.2", section=1),
        ScheduleSlot(teacher_id=t1.id, subject_id=math.id, day_of_week=2, period=1,
                      duration=1, grade_level="ม.2", section=1),
        ScheduleSlot(teacher_id=t1.id, subject_id=sci.id, day_of_week=1, period=3,
                      duration=2, grade_level="ม.2", section=2),
        ScheduleSlot(teacher_id=t2.id, subject_id=eng.id, day_of_week=0, period=2,
                      duration=1, grade_level="ม.3", section=1),
    ]
    db.session.add_all(slots)
    db.session.commit()


    db.session.add(TermSetting(term_start_date=thai_now().date(),
                               term_end_date=thai_now().date() + timedelta(days=90)))
    db.session.commit()


with app.app_context():
    init_db_with_demo_data()


if __name__ == "__main__":
    # เปิดโหมด debug เฉพาะเมื่อตั้ง FLASK_DEBUG=1 และรับเฉพาะเครื่องตัวเองเป็นค่าเริ่มต้น (HOST=0.0.0.0 เพื่อเปิดให้เครื่องอื่นเข้า)
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1",
            host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5000)))
