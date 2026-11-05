"""
models.py
โครงสร้างฐานข้อมูลของ "ระบบเช็คงานค้าง"

หมายเหตุ: ระบบนี้ไม่มีการให้คะแนนหรือคำนวณเกรด
ใช้ติดตาม "สถานะการส่งงาน" ของนักเรียนแต่ละคนเท่านั้น
"""
from datetime import datetime
from werkzeug.security import check_password_hash, generate_password_hash
from zoneinfo import ZoneInfo
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
THAI_TIMEZONE = ZoneInfo("Asia/Bangkok")


def thai_now():
    """เวลาปัจจุบันของไทยในรูปแบบเดียวกับ due_date ที่เก็บในฐานข้อมูล"""
    return datetime.now(THAI_TIMEZONE).replace(tzinfo=None)

DAYS_TH = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]

ASSIGNMENT_TYPES = [
    ("individual", "งานเดี่ยว"),
    ("group", "งานกลุ่ม"),
    ("exam", "สอบ/ชิ้นงานประเมิน"),
]

GRADE_LEVELS = ["ม.1", "ม.2", "ม.3", "ม.4", "ม.5", "ม.6"]
SECTIONS = [1, 2]  # แต่ละระดับชั้นมี 2 ห้อง

# จำนวนวันก่อนถึงกำหนดส่งที่ถือว่า "ใกล้กำหนดส่ง"
SOON_DAYS = 3

# --------------------------------------------------------------------------
# สถานะการส่งงาน (หัวใจของระบบนี้)
# --------------------------------------------------------------------------
# submitted        = ส่งแล้ว
# not_submitted    = ยังไม่ส่ง (ยังมีเวลาเหลือ)
# due_soon         = ใกล้กำหนดส่ง
# late_accepting   = เลยกำหนดส่งแล้วแต่ยังรับอยู่
# late_closed      = เลยกำหนดส่งแล้วแต่ไม่รับแล้ว

STATUS_META = {
    "submitted":      {"label": "ส่งแล้ว",                          "color": "green",  "icon": "✅"},
    "not_submitted":  {"label": "ยังไม่ส่ง",                        "color": "yellow", "icon": "🟡"},
    "due_soon":       {"label": "ใกล้กำหนดส่ง",                     "color": "yellow", "icon": "⏳"},
    "late_accepting": {"label": "เลยกำหนดส่งแล้ว",      "color": "red", "icon": "⛔"},
    "late_closed":    {"label": "ปิดรับแล้ว",      "color": "black",    "icon": "❌"},
}

# งานที่ยังส่งได้และต้องเตือนนักเรียน (ปิดรับแล้วไม่นับเป็นงานค้าง)
PENDING_STATUSES = ("not_submitted", "due_soon", "late_accepting")


def status_label(status):
    return STATUS_META.get(status, {}).get("label", status)


def status_color(status):
    return STATUS_META.get(status, {}).get("color", "gray")


def status_icon(status):
    return STATUS_META.get(status, {}).get("icon", "")


def format_due_countdown(due_date, now=None):
    """เวลาที่เหลือเป็นวันเต็ม ชั่วโมง และนาที"""
    total_minutes = max(0, int((due_date - (now or thai_now())).total_seconds() // 60))
    days, remainder = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remainder, 60)
    return f"{days} วัน {hours:02d} ชม. {minutes:02d} นาที"


# --------------------------------------------------------------------------
# ผู้ใช้งาน
# --------------------------------------------------------------------------

class Teacher(db.Model):
    """
    คุณครู/อาจารย์ — ล็อกอินด้วยชื่อ + รหัสผ่าน
    ครูที่มี is_admin = True จะมีสิทธิ์เข้าแผงควบคุมผู้ดูแลระบบเพิ่มเติมได้
    """
    __tablename__ = "teachers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    username = db.Column(db.String(120), unique=True, nullable=False, default="")
    # ชื่อ-นามสกุลภาษาอังกฤษ (ใช้สร้างชื่อผู้ใช้: ชื่อจริง + อักษรแรกของนามสกุล เช่น SomchaiB)
    name_en = db.Column(db.String(120), nullable=True)
    # เก็บเป็นค่าแฮช (werkzeug) เท่านั้น ไม่เก็บรหัสผ่านจริง
    password = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)

    schedule_slots = db.relationship("ScheduleSlot", backref="teacher", lazy=True,
                                      cascade="all, delete-orphan")

    HASH_PREFIXES = ("pbkdf2:", "scrypt:")

    def set_password(self, raw):
        self.password = generate_password_hash(raw, method="pbkdf2:sha256")

    def check_password(self, raw):
        return check_password_hash(self.password, raw)

    @property
    def password_is_hashed(self):
        return (self.password or "").startswith(self.HASH_PREFIXES)


class Student(db.Model):
    """นักเรียน — ล็อกอินด้วยเลขประจำตัวนักเรียน"""
    __tablename__ = "students"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    grade_level = db.Column(db.String(10), nullable=False, default="")
    class_room = db.Column(db.String(20), nullable=False, default="")
    roll_number = db.Column(db.Integer, nullable=True)

    enrollments = db.relationship("Enrollment", backref="student", lazy=True,
                                   cascade="all, delete-orphan")

    def full_class_label(self):
        if self.grade_level and self.class_room:
            return f"{self.grade_level}/{self.class_room}"
        return self.grade_level or self.class_room or "-"


# --------------------------------------------------------------------------
# วิชา / การลงทะเบียนเรียน
# --------------------------------------------------------------------------

subject_teachers = db.Table(
    "subject_teachers",
    db.Column("subject_id", db.Integer, db.ForeignKey("subjects.id"), primary_key=True),
    db.Column("teacher_id", db.Integer, db.ForeignKey("teachers.id"), primary_key=True),
)


class Subject(db.Model):
    __tablename__ = "subjects"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    code = db.Column(db.String(20), nullable=False, default="")
    room = db.Column(db.String(50), nullable=False, default="")
    teacher_id = db.Column(db.Integer, db.ForeignKey("teachers.id"), nullable=True)

    # วิชาเสริม (เช่น ชุมนุม) — แอดมินสร้างและกำหนดครูผู้สอนเท่านั้น
    # นักเรียนจะถูกเพิ่มโดยครูผู้สอนของวิชานั้นเอง
    is_elective = db.Column(db.Boolean, nullable=False, default=False)

    # ครูผู้สอนทั้งหมดของวิชานี้ (รองรับหลายคนต่อวิชา เช่น วิชาชุมนุม)
    teachers = db.relationship("Teacher", secondary=subject_teachers, lazy="subquery",
                                backref=db.backref("taught_subjects", lazy=True))

    assignments = db.relationship("Assignment", backref="subject", lazy=True,
                                   cascade="all, delete-orphan")
    enrollments = db.relationship("Enrollment", backref="subject", lazy=True,
                                   cascade="all, delete-orphan")
    schedule_slots = db.relationship("ScheduleSlot", backref="subject", lazy=True,
                                      cascade="all, delete-orphan")

    # ชั้น/ห้องที่วิชานี้สอน — นักเรียนในห้องเหล่านี้ได้รับงานของวิชานี้ทุกคนโดยอัตโนมัติ
    classes = db.relationship("SubjectClass", backref="subject", lazy=True,
                              cascade="all, delete-orphan",
                              order_by="SubjectClass.grade_level, SubjectClass.section")

    def teacher_names(self):
        return ", ".join(t.name for t in self.teachers) or "-"

    def class_labels(self):
        return ", ".join(f"{c.grade_level}/{c.section}" for c in self.classes)

    def uses_classes(self):
        """True = นักเรียนมาจากชั้น/ห้องโดยอัตโนมัติ, False = ครูเพิ่มนักเรียนเอง (เช่น วิชาเสริม)"""
        return len(self.classes) > 0


class SubjectClass(db.Model):
    """ชั้น/ห้องที่วิชาหนึ่งสอน เช่น ม.2/1"""
    __tablename__ = "subject_classes"

    id = db.Column(db.Integer, primary_key=True)
    subject_id = db.Column(db.Integer, db.ForeignKey("subjects.id"), nullable=False)
    grade_level = db.Column(db.String(10), nullable=False)
    section = db.Column(db.Integer, nullable=False)

    __table_args__ = (db.UniqueConstraint("subject_id", "grade_level", "section"),)


class Enrollment(db.Model):
    __tablename__ = "enrollments"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("students.id"), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("subjects.id"), nullable=False)

    __table_args__ = (db.UniqueConstraint("student_id", "subject_id"),)


# --------------------------------------------------------------------------
# งานที่สั่ง
# --------------------------------------------------------------------------

class Assignment(db.Model):
    __tablename__ = "assignments"

    id = db.Column(db.Integer, primary_key=True)
    subject_id = db.Column(db.Integer, db.ForeignKey("subjects.id"), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    example_text = db.Column(db.Text, nullable=False, default="")
    due_date = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    assignment_type = db.Column(db.String(20), nullable=False, default="individual")
    group_size = db.Column(db.Integer, nullable=True)

    # ลิงก์แนบ (ใบงาน, ตัวอย่างงาน, ฟอร์มส่งงาน ฯลฯ)
    link_url = db.Column(db.String(500), nullable=False, default="")
    link_label = db.Column(db.String(200), nullable=False, default="")

    # เลยกำหนดส่งแล้วยังรับงานอยู่หรือไม่
    accept_late = db.Column(db.Boolean, nullable=False, default=True)

    submissions = db.relationship("Submission", backref="assignment", lazy=True,
                                   cascade="all, delete-orphan")
    groups = db.relationship("Group", backref="assignment", lazy=True,
                              cascade="all, delete-orphan")

    def type_label(self):
        return dict(ASSIGNMENT_TYPES).get(self.assignment_type, self.assignment_type)

    def is_overdue(self):
        return thai_now() > self.due_date

    def days_until_due(self):
        delta = self.due_date - thai_now()
        return delta.total_seconds() / 86400.0

    def unsubmitted_status(self):
        """
        สถานะของงานชิ้นนี้สำหรับคนที่ "ยังไม่ส่ง"
        ขึ้นกับกำหนดส่งและนโยบายรับงานสาย
        """
        if self.is_overdue():
            return "late_accepting" if self.accept_late else "late_closed"
        if self.days_until_due() <= SOON_DAYS:
            return "due_soon"
        return "not_submitted"


class Group(db.Model):
    """กลุ่มของนักเรียนสำหรับงานกลุ่มชิ้นหนึ่ง ๆ"""
    __tablename__ = "groups"

    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignments.id"), nullable=False)
    label = db.Column(db.String(50), nullable=False)

    members = db.relationship("Submission", backref="group", lazy=True)


class Submission(db.Model):
    """สถานะการส่งงานของนักเรียนแต่ละคนต่องานแต่ละชิ้น (ไม่มีคะแนน)"""
    __tablename__ = "submissions"

    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey("assignments.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("students.id"), nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), nullable=True)

    submitted = db.Column(db.Boolean, nullable=False, default=False)
    submitted_at = db.Column(db.DateTime, nullable=True)
    note = db.Column(db.String(300), nullable=False, default="")  # หมายเหตุของครู

    student = db.relationship("Student")

    __table_args__ = (db.UniqueConstraint("assignment_id", "student_id"),)


# --------------------------------------------------------------------------
# ตารางสอน / ตารางเวร / วันหยุด
# --------------------------------------------------------------------------

class ScheduleSlot(db.Model):
    __tablename__ = "schedule_slots"

    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey("teachers.id"), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("subjects.id"), nullable=False)
    day_of_week = db.Column(db.Integer, nullable=False)
    period = db.Column(db.Integer, nullable=False)
    duration = db.Column(db.Integer, nullable=False, default=1)
    grade_level = db.Column(db.String(10), nullable=False, default="")
    section = db.Column(db.Integer, nullable=False, default=1)

    def day_name(self):
        return DAYS_TH[self.day_of_week]

    def class_label(self):
        if self.grade_level:
            return f"{self.grade_level}/{self.section}"
        return "-"

    def period_range(self):
        return list(range(self.period, self.period + self.duration))


class Holiday(db.Model):
    """วันหยุด — วันที่ตรงกับวันหยุดจะไม่ถูกนับรวมเป็นคาบสอนที่เหลือ"""
    __tablename__ = "holidays"

    id = db.Column(db.Integer, primary_key=True)
    holiday_date = db.Column(db.Date, nullable=False, unique=True)
    description = db.Column(db.String(200), nullable=False, default="")


class HolidaySeed(db.Model):
    """จำปีที่เพิ่มวันหยุดเริ่มต้นแล้ว เพื่อไม่เพิ่มกลับหลังผู้ดูแลแก้หรือลบ"""
    __tablename__ = "holiday_seeds"

    year = db.Column(db.Integer, primary_key=True)


class TermSetting(db.Model):
    __tablename__ = "term_settings"

    id = db.Column(db.Integer, primary_key=True)
    term_start_date = db.Column(db.Date, nullable=True)
    term_end_date = db.Column(db.Date, nullable=False)


# --------------------------------------------------------------------------
# ฟังก์ชันช่วยคำนวณสถานะ
# --------------------------------------------------------------------------

def get_status(assignment, submission):
    """สถานะการส่งงานของนักเรียนคนหนึ่งต่องานชิ้นหนึ่ง"""
    if submission and submission.submitted:
        return "submitted"
    return assignment.unsubmitted_status()


def subject_status_summary(student_id, subject_id):
    """
    สรุปสถานะงานทั้งหมดของนักเรียนในวิชาหนึ่ง
    คืนค่า dict: {สถานะ: จำนวนงาน} และรายการงานที่ค้าง
    """
    assignments = Assignment.query.filter_by(subject_id=subject_id).all()
    counts = {key: 0 for key in STATUS_META}
    pending = []
    for a in assignments:
        s = Submission.query.filter_by(assignment_id=a.id, student_id=student_id).first()
        status = get_status(a, s)
        counts[status] += 1
        if status in PENDING_STATUSES:
            pending.append((a, status))
    # เรียงงานที่ยังส่งได้: เลยกำหนด > ใกล้กำหนด > ยังมีเวลา
    order = {"late_accepting": 0, "due_soon": 1, "not_submitted": 2}
    pending.sort(key=lambda x: (order.get(x[1], 9), x[0].due_date))
    return counts, pending


def pending_assignments_for_student(student_id, subject_id):
    """รายการงานที่ค้างของนักเรียนในวิชาหนึ่ง"""
    _, pending = subject_status_summary(student_id, subject_id)
    return pending


# --------------------------------------------------------------------------
# ตารางสอน / ตารางเรียน (ใช้ร่วมกันทั้งฝั่งครูและฝั่งนักเรียน)
# --------------------------------------------------------------------------

# โครงตารางคงที่: คาบ 1-4 / พักเที่ยง / คาบ 5-8  (รวม 9 คอลัมน์ด้านบน)
MORNING_PERIODS = [1, 2, 3, 4]
AFTERNOON_PERIODS = [5, 6, 7, 8]
ALL_PERIODS = MORNING_PERIODS + AFTERNOON_PERIODS
WEEKDAY_COUNT = 5  # จันทร์ - ศุกร์


def _build_half(occupancy, half_periods):
    """
    สร้างรายการเซลล์ของครึ่งวันหนึ่ง (เช้า หรือ บ่าย)
    คาบต่อเนื่องที่อยู่ในครึ่งเดียวกันจะรวมเป็นเซลล์เดียว (colspan = 2)
    คาบที่ไม่มีการสอนจะเป็นเซลล์ว่าง
    """
    cells = []
    i = 0
    while i < len(half_periods):
        p = half_periods[i]
        entry = occupancy.get(p)
        if entry is None:
            cells.append({"empty": True, "colspan": 1})
            i += 1
            continue

        slot = entry["slot"]
        # นับว่าคาบถัดไปในครึ่งวันเดียวกันเป็นคาบต่อเนื่องของ slot เดิมหรือไม่
        span = 1
        while i + span < len(half_periods):
            nxt = occupancy.get(half_periods[i + span])
            if nxt and nxt["slot"].id == slot.id:
                span += 1
            else:
                break
        cells.append({"empty": False, "slot": slot, "colspan": span})
        i += span
    return cells


def build_schedule_grid(slots, weekdays=WEEKDAY_COUNT):
    """
    สร้างตารางสำหรับแสดงผลแบบคงที่ 6 แถว x 9 คอลัมน์
      - แถว: หัวตาราง + วันจันทร์ถึงวันศุกร์
      - คอลัมน์: คาบ 1-4, พักเที่ยง, คาบ 5-8

    คืนค่า (morning, afternoon, rows)
      rows[day] = {"morning": [...], "afternoon": [...]}
      แต่ละเซลล์ = {"empty": True, "colspan": n}
                หรือ {"empty": False, "slot": ScheduleSlot, "colspan": n}
    """
    rows = {}
    for d in range(weekdays):
        occupancy = {}
        for s in slots:
            if s.day_of_week != d:
                continue
            for p in s.period_range():
                if p in ALL_PERIODS:
                    occupancy[p] = {"slot": s}
        rows[d] = {
            "morning": _build_half(occupancy, MORNING_PERIODS),
            "afternoon": _build_half(occupancy, AFTERNOON_PERIODS),
        }
    return MORNING_PERIODS, AFTERNOON_PERIODS, rows


def schedule_slots_for_class(grade_level, section):
    """
    ตารางเรียนของห้องเรียนหนึ่ง ๆ — ดึงจากคาบสอนที่ครูบันทึกไว้โดยตรง
    ทำให้ตารางเรียนของนักเรียนสัมพันธ์กับตารางสอนของครูเสมอ
    """
    if not grade_level:
        return []
    try:
        sec = int(section)
    except (TypeError, ValueError):
        return []
    return ScheduleSlot.query.filter_by(
        grade_level=grade_level, section=sec
    ).order_by(ScheduleSlot.day_of_week, ScheduleSlot.period).all()


# --------------------------------------------------------------------------
# กล่อง "คาบตอนนี้ / คาบต่อไป" (คำนวณฝั่ง Python ทั้งหมด)
# --------------------------------------------------------------------------

# (ชื่อ, หมายเลขคาบ หรือ None ถ้าเป็นพักเที่ยง, เวลาเริ่ม, เวลาจบ)
PERIOD_SCHEDULE = [
    ("คาบ 1", 1, "08:30", "09:20"),
    ("คาบ 2", 2, "09:20", "10:10"),
    ("คาบ 3", 3, "10:10", "11:00"),
    ("คาบ 4", 4, "11:00", "11:50"),
    ("พักเที่ยง", None, "11:50", "12:40"),
    ("คาบ 5", 5, "12:40", "13:30"),
    ("คาบ 6", 6, "13:30", "14:20"),
    ("คาบ 7", 7, "14:20", "15:10"),
    ("คาบ 8", 8, "15:10", "16:00"),
]
PERIOD_TIMES = {p: (start, end) for _, p, start, end in PERIOD_SCHEDULE if p}


def _minutes(hhmm):
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def build_period_lookup(slots, label_fn):
    """{วัน: {คาบ: {"label", "start_period", "end_period"}}} จากรายการคาบสอน"""
    lookup = {}
    for slot in slots:
        day_map = lookup.setdefault(slot.day_of_week, {})
        info = {"label": label_fn(slot), "start_period": slot.period,
                "end_period": slot.period + slot.duration - 1}
        for p in slot.period_range():
            day_map[p] = info
    return lookup


def _describe_period(entry, today_periods, with_end):
    """คืน (ข้อความ, css, นาทีที่บล็อกคาบนี้จบ) ของคาบหนึ่ง ๆ"""
    label, period, start, end = entry
    suffix = (lambda s, e: f" ({s}-{e})") if with_end else (lambda s, e: f" ({s} น.)")
    if period is None:
        return label + suffix(start, end), "lunch", _minutes(end)
    info = today_periods.get(period)
    if not info:
        return f"ไม่มีคาบเรียน ({label}{suffix(start, end)})", "done", _minutes(end)
    first, last = info["start_period"], info["end_period"]
    start = PERIOD_TIMES[first][0]
    end = PERIOD_TIMES[last][1]
    count = last - first + 1
    if count > 1:
        head, count_text = f"คาบ {first}-{last}", f" (เรียนติดกัน {count} คาบ)"
    else:
        head, count_text = f"คาบ {first}", ""
    return f"{head} - {info['label']}{count_text}{suffix(start, end)}", "", _minutes(end)


def period_box_state(period_lookup, holiday_map, term, now=None):
    """
    ข้อความของกล่อง "คาบตอนนี้ / คาบต่อไป"
    คืนค่า {"current": (ข้อความ, css), "next": (ข้อความ, css)}
    holiday_map = {date: คำอธิบาย}
    """
    now = now or thai_now()
    today = now.date()

    def both(text):
        return {"current": (text, "done"), "next": (text, "done")}

    if term and term.term_start_date and today < term.term_start_date:
        return both("ยังไม่เปิดเทอม")
    if term and term.term_end_date and today > term.term_end_date:
        return both("ปิดเทอมแล้ว")
    if now.weekday() >= 5:
        return both("วันหยุด (เสาร์-อาทิตย์)")
    if today in holiday_map:
        description = holiday_map[today]
        return both("วันหยุด" + (f": {description}" if description else ""))

    today_periods = period_lookup.get(now.weekday(), {})
    now_min = now.hour * 60 + now.minute
    reference = now_min
    current = None
    for entry in PERIOD_SCHEDULE:
        if _minutes(entry[2]) <= now_min < _minutes(entry[3]):
            text, css, block_end = _describe_period(entry, today_periods, True)
            current = (text, css)
            reference = max(now_min, block_end - 1)   # คาบต่อเนื่อง: ข้ามทั้งบล็อก
            break
    if current is None:
        current = (("ยังไม่เริ่มเรียน" if now_min < _minutes(PERIOD_SCHEDULE[0][2])
                    else "หมดเวลาเรียนแล้ว"), "done")

    upcoming = next((e for e in PERIOD_SCHEDULE if _minutes(e[2]) > reference), None)
    if upcoming:
        text, css, _ = _describe_period(upcoming, today_periods, False)
        return {"current": current, "next": (text, css)}
    return {"current": current, "next": ("ไม่มีคาบต่อไปวันนี้", "done")}
