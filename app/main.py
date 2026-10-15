import calendar
from contextlib import asynccontextmanager
from datetime import date as date_type, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
import os
from typing import Any, Dict, Literal, Optional

from bson import Decimal128, ObjectId, Timestamp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")

_indexes_created = False


def ensure_indexes() -> None:
    """Create all required indexes idempotently at startup."""
    global _indexes_created
    try:
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING), ("emp_code", ASCENDING)])
        db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING), ("emp_code", ASCENDING)])

        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING), ("status", ASCENDING)])
        db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)])
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])

        _indexes_created = True
    except PyMongoError:
        pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.middleware("http")
async def ensure_indexes_middleware(request, call_next):
    if not _indexes_created and request.url.path != "/health":
        ensure_indexes()
    return await call_next(request)


# Helpers & Business Rules (R1 - R10)



# Round a number using half-up rounding
def round_half_up(value: float | Decimal, decimals: int = 2) -> float:
    if isinstance(value, Decimal):
        d = value  
    else:
        d = Decimal(str(value))

    if decimals == 0:
        quant = Decimal("1")  
    else:
        quant = Decimal("1." + ("0" * decimals))

    return float(d.quantize(quant, rounding=ROUND_HALF_UP))


# Convert milliseconds to UTC datetime 
def ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


# current UTC datetime to seconds
def current_utc_to_sec() -> datetime:

    return datetime.now(timezone.utc).replace(microsecond=0)

# Convert a BSON/Python datetime to integer milliseconds.
def datetime_to_millisecond(dt: Optional[datetime]) -> Optional[int]:
    if dt:
        return int(dt.timestamp() * 1000)
    else:
        return None
    


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


# Compute the IST attendance date for a punch_in
def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    ist_dt = punch_in.astimezone(IST)

    if is_overnight_shift(shift_start, shift_end):
        end_h, end_m = map(int, shift_end.split(":"))

        if (ist_dt.hour, ist_dt.minute, ist_dt.second) < (end_h, end_m, 0):
            return (ist_dt.date() - timedelta(days=1)).isoformat()
        
    return ist_dt.date().isoformat()

# Late only if punch_in is strictly more than 10:00 after shift_start.
# Grace period is 10 minutes
def late_minutes(punch_in: datetime, shift_start: str, date_str: Optional[str] = None, shift_end: Optional[str] = None) -> int:
    pi_ist = punch_in.astimezone(IST)

    if date_str is None:
        if shift_end is not None:
            date_str = attendance_date(pi_ist, shift_start, shift_end)
        else:
            date_str = pi_ist.date().isoformat()

    d = date_type.fromisoformat(date_str)

    sh, sm = map(int, shift_start.split(":"))

    start_dt = datetime(d.year, d.month, d.day, sh, sm, 0, tzinfo=IST)

    elapsed_seconds = int((pi_ist - start_dt).total_seconds())
    
    if elapsed_seconds > 600:
        return elapsed_seconds // 60
    return 0


# Work hours: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
def working_hours(punch_in: datetime, punch_out: datetime) -> float:
    elapsed_seconds = int((punch_out - punch_in).total_seconds())

    return round_half_up(Decimal(elapsed_seconds) / Decimal(3600), 2)


# Overtime: minutes between shift_end and punch_out, 
#     counted only when >= 30; otherwise 0
def total_overtime(punch_out: datetime, shift_end: str, date_str: str, shift_start: Optional[str] = None) -> int:
    po_ist = punch_out.astimezone(IST)

    d = date_type.fromisoformat(date_str)

    eh, em = map(int, shift_end.split(":"))

    if shift_start is not None and is_overnight_shift(shift_start, shift_end):
        d = d + timedelta(days=1)

    end_dt = datetime(d.year, d.month, d.day, eh, em, 0, tzinfo=IST)

    elapsed_seconds = int((po_ist - end_dt).total_seconds())

    minutes = elapsed_seconds // 60

    if minutes >= 30:
        return minutes
    return 0

# employee document for the HTTP API response
def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_millisecond(doc["created_at"]),
    }


# Convert attendance document for HTTP API response
def serialize_attendance(doc: Dict[str, Any]) -> Dict[str, Any]:
    history = []

    for entry in doc.get("history") or []:
        changes = {}

        for field, diff in (entry.get("changes") or {}).items():
            old_value = diff.get("from")
            new_value = diff.get("to")

            if field in ("punch_in", "punch_out"):
                if isinstance(old_value, datetime):
                    old_value = datetime_to_millisecond(old_value)

                if isinstance(new_value, datetime):
                    new_value = datetime_to_millisecond(new_value)

            changes[field] = {
                "from": old_value,
                "to": new_value
            }

        at = entry.get("at")
        if isinstance(at, datetime):
            at = datetime_to_millisecond(at)

        history.append({
            "at": at,
            "by": entry.get("by", ""),
            "reason": entry.get("reason", ""),
            "changes": changes
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_millisecond(doc.get("punch_in")),
        "punch_out": datetime_to_millisecond(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0) or 0,
        "overtime_minutes": doc.get("overtime_minutes", 0) or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history
    }


def sanitize_bson(obj: Any) -> Any:
    # Check if the object is a dictionary
    if isinstance(obj, dict):
        result = {}
        for key, value in obj.items():
            result[key] = sanitize_bson(value)
        return result

    # Check if the object is a list
    if isinstance(obj, list):
        result = []
        for item in obj:
            result.append(sanitize_bson(item))
        return result

    if isinstance(obj, ObjectId):
        return str(obj)

    if isinstance(obj, datetime):
        return obj.isoformat()

    if isinstance(obj, Timestamp):
        return {"t": obj.time, "i": obj.inc}

    if isinstance(obj, Decimal128):
        return float(obj.to_decimal())

    if isinstance(obj, bytes):
        return obj.hex()

    return obj


def month_range(month: str) -> tuple[str, str, int, int, int]:
    try:
        year, mon = map(int, month.split("-"))
        _, num_days = calendar.monthrange(year, mon)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid month format; expected YYYY-MM")
    
    month_start = f"{month}-01"
    month_end = f"{month}-{num_days:02d}"
    
    return month_start, month_end, year, mon, num_days


def working_days_in_month(year: int, mon: int, num_days: int, joined_on: str) -> int:
    working_days = 0
    for day in range(1, num_days + 1):
        d = date_type(year, mon, day)

        if d.weekday() < 5 and d.isoformat() >= joined_on:
            working_days += 1
            
    return working_days


# Enums & Pydantic Models


class AttendanceStatus(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[int] = Field(None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(..., min_length=1)
    punched_at: Optional[int] = Field(None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[AttendanceStatus] = None
    punch_in: Optional[int] = Field(None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    punch_out: Optional[int] = Field(None, strict=True, ge=100_000_000_000, le=4_102_444_800_000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# Endpoints


@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(emp: EmployeeIn):

    if emp.shift_start == emp.shift_end:
        raise HTTPException(status_code=422, detail="shift_start must differ from shift_end")
    try:
        date_type.fromisoformat(emp.joined_on)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="joined_on must be a valid YYYY-MM-DD date")

    doc = emp.model_dump()
    doc["created_at"] = current_utc_to_sec()

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q: Dict[str, Any] = {}

    if department is not None:
        q["department"] = department

    skip = (page - 1) * page_size

    total = db.employees.count_documents(q)

    cursor = db.employees.find(q).sort("emp_code", ASCENDING)
    cursor = cursor.skip(skip)
    cursor = cursor.limit(page_size)

    items = []
    for d in cursor:
        items.append(serialize_employee(d))

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts = ms_to_utc(body.punched_at)
    else:
        ts = current_utc_to_sec()

    att_date = attendance_date(
        ts, emp["shift_start"], emp["shift_end"]
    )

    late_mins = late_minutes(
        ts, emp["shift_start"], att_date, emp["shift_end"]
    )

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc

    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        ts = ms_to_utc(body.punched_at)
    else:
        ts = current_utc_to_sec()


    rec = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts}
        },
        sort=[("punch_in", DESCENDING)]
    )

    latest_any = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None}
        },
        sort=[("punch_in", DESCENDING)]
    )

    if rec is None:
        if latest_any is not None:
            raise HTTPException(status_code=422, detail="punched_at must be after punch_in")

        raise HTTPException(status_code=404, detail="No punch-in record found")

    if (
        rec.get("punch_out") is not None
        and latest_any is not None
        and latest_any["_id"] != rec["_id"]
        and latest_any.get("punch_out") is None
        and latest_any["punch_in"] > ts
    ):
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")

    pi_dt = rec["punch_in"]

    if ts <= pi_dt:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")

    if (ts - pi_dt).total_seconds() > 86400:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    work_hours = working_hours(pi_dt, ts)

    overtime_mins = total_overtime(ts, emp["shift_end"], rec["date"], emp["shift_start"])

    half_day = bool(work_hours < 4.50)

    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated:
        raise HTTPException(
            status_code=409,
            detail="Record is already punched out"
        )

    return serialize_attendance(updated)




@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[AttendanceStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    
    for name, val in (("date_from", date_from), ("date_to", date_to)):

        if val is not None:
            try:
                date_type.fromisoformat(val)
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail=f"{name} must be in YYYY-MM-DD format")
            
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

    q: Dict[str, Any] = {}
    if emp_code is not None:
        q["emp_code"] = emp_code

    if date_from is not None or date_to is not None:
        q["date"] = {}

        if date_from is not None:
            q["date"]["$gte"] = date_from

        if date_to is not None:
            q["date"]["$lte"] = date_to

    if status is not None:
        q["status"] = status.value if isinstance(status, AttendanceStatus) else status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)

    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)])
    cursor = cursor.skip(skip)
    cursor = cursor.limit(page_size)

    items = []
    for d in cursor:
        items.append(serialize_attendance(d))
    
    return {"items": items, "total": total, "page": page, "page_size": page_size}



@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(body: RegularizeIn, emp_code: str = Path(...),date: str = Path(...)):
    try:
        date_type.fromisoformat(date)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="date must be in YYYY-MM-DD format")

    fields_set = body.model_fields_set

    for field in ("status", "punch_in", "punch_out"):
        if field in fields_set and getattr(body, field) is None:
            raise HTTPException(status_code=422, detail=f"{field} cannot be null when provided")

    emp = db.employees.find_one({"emp_code": emp_code})

    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date
    })

    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found for this date")

    old_vals: Dict[str, Any] = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0) or 0,
        "overtime_minutes": rec.get("overtime_minutes", 0) or 0,
        "half_day": bool(rec.get("half_day", False))
    }

    if "status" in fields_set and body.status is not None:
        new_status = body.status.value
    else:
        new_status = old_vals["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in fields_set or "punch_out" in fields_set:
            raise HTTPException(status_code=422, detail="Cannot supply punch_in or punch_out when status is ABSENT or LEAVE")

        new_punch_in: Optional[datetime] = None
        new_punch_out: Optional[datetime] = None
        new_work_hours: Optional[float] = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False

    else:
        if "punch_in" in fields_set and body.punch_in is not None:
            new_punch_in = ms_to_utc(body.punch_in)
        else:
            new_punch_in = old_vals["punch_in"]

        if "punch_out" in fields_set and body.punch_out is not None:
            new_punch_out = ms_to_utc(body.punch_out)
        else:
            new_punch_out = old_vals["punch_out"]

        if new_punch_in is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

        att_date = attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])

        if att_date != date:
            raise HTTPException(status_code=422, detail="punch_in must fall on the record's attendance date")

        new_late_minutes = late_minutes(new_punch_in, emp["shift_start"], date, emp["shift_end"])

        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")

            if (new_punch_out - new_punch_in).total_seconds() > 86400:
                raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

            new_work_hours = working_hours(new_punch_in, new_punch_out)

            new_overtime_minutes = total_overtime(new_punch_out, emp["shift_end"], date, emp["shift_start"])

            new_half_day = bool(new_work_hours < 4.50)

        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    new_vals: Dict[str, Any] = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day
    }

    tracked_fields = (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day"
    )

    changes: Dict[str, Dict[str, Any]] = {}

    for field in tracked_fields:
        if old_vals[field] != new_vals[field]:
            changes[field] = {
                "from": old_vals[field],
                "to": new_vals[field]
            }

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    history_entry = {
        "at": current_utc_to_sec(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    concurrency_filter: Dict[str, Any] = {
        "_id": rec["_id"],
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out")
    }

    if "history" in rec:
        concurrency_filter["history"] = rec["history"]
    else:
        concurrency_filter["history"] = {"$exists": False}

    updated = db.attendance_logs.find_one_and_update(
        concurrency_filter,
        {
            "$set": new_vals,
            "$push": {"history": history_entry}
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent modification detected")

    return serialize_attendance(updated)



@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str = Path(...),month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):

    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    month_start, month_end, year, mon, num_days = month_range(month)
    working_days = working_days_in_month(year, mon, num_days, emp["joined_on"])

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": month_start, "$lte": month_end}}},
        {
            #convert date to numbers
            "$addFields": {
                "iso_dow": {
                    "$isoDayOfWeek": {
                        "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                # weekday & present add  1 - full day or 0.5 - halfday 
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {"$lte": ["$iso_dow", 5]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                # weekday & leave add 1
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                
                # late_minutes > 0 add 1
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },

                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},

                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late_minutes = int(row.get("total_late_minutes", 0))
        total_overtime_minutes = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct: Optional[float] = None
    else:
        attendance_pct = round_half_up(
            (Decimal(str(present_days)) / Decimal(working_days)) * Decimal(100), 2
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str] = Query(None)):

    month_start, month_end, _, _, _ = month_range(month)
    emp_match: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
                    {
                        "$addFields": {
                            "iso_dow": {
                                "$isoDayOfWeek": {
                                    "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                                }
                            },
                            "is_present": {"$in": ["$status", list(PRESENCE_STATUSES)]},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "emp_codes": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$eq": ["$logs.is_present", True]}, {"$lte": ["$logs.iso_dow", 5]}]},
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$logs.is_present", True]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            {"$ifNull": ["$logs.work_hours", 0]},
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$eq": ["$logs.is_present", True]},
                                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        wh_count = r.get("work_hours_count", 0)
        avg_wh = (
            round_half_up(float(r["work_hours_sum"]) / wh_count, 2)
            if wh_count > 0 and r.get("work_hours_sum") is not None
            else None
        )
        items.append({
            "department": r["_id"],
            "headcount": len(r.get("emp_codes", [])),
            "present_days": float(r.get("present_days", 0.0)),
            "avg_work_hours": avg_wh,
            "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "leave_count": int(r.get("leave_count", 0)),
            "on_duty_count": int(r.get("on_duty_count", 0)),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int = Query(10, ge=1, le=50), department: Optional[str] = Query(None)):
    month_start, month_end, _, _, _ = month_range(month)
    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ])

    raw_items = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in raw_items:
        emp = r.get("emp", {})
        items.append({
            "rank": r["rank"],
            "emp_code": r["_id"],
            "name": emp.get("name", ""),
            "department": emp.get("department", ""),
            "total_late_minutes": r["total_late_minutes"],
            "late_count": r["late_count"],
        })
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str = Path(...), from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    try:
        d_from = date_type.fromisoformat(from_date)
        d_to = date_type.fromisoformat(to_date)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="from and to must be in YYYY-MM-DD format")

    if d_to < d_from:
        raise HTTPException(status_code=422, detail="'to' cannot be earlier than 'from'")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    from_dt = datetime(d_from.year, d_from.month, d_from.day, 0, 0, 0, tzinfo=timezone.utc)
    to_dt = datetime(d_to.year, d_to.month, d_to.day, 0, 0, 0, tzinfo=timezone.utc)
    to_plus_1_dt = to_dt + timedelta(days=1)

    pipeline = [
        {"$match": {"department": department}},
        {"$limit": 1},
        {"$project": {"_id": 0, "dt": {"$literal": from_dt}}},
        {
            "$densify": {
                "field": "dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_plus_1_dt],
                },
            }
        },
        {"$match": {"dt": {"$gte": from_dt, "$lte": to_dt}}},
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dt"}},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$dt"}, 5]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {"$match": {"department": department}},
                    {
                        "$group": {
                            "_id": None,
                            "codes": {"$push": "$emp_code"},
                            "headcount": {
                                "$sum": {
                                    "$cond": [
                                        {"$lte": ["$joined_on", "$$d"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "emp_info",
            }
        },
        {
            "$addFields": {
                "headcount": {"$ifNull": [{"$first": "$emp_info.headcount"}, 0]},
                "dept_codes": {"$ifNull": [{"$first": "$emp_info.codes"}, []]},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "date",
                "foreignField": "date",
                "let": {"codes": "$dept_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$in": ["$emp_code", "$$codes"]}
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                        0.0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "day_logs",
            }
        },
        {
            "$addFields": {
                "present_count": {"$ifNull": [{"$first": "$day_logs.present_count"}, 0.0]},
                "late_count": {"$ifNull": [{"$first": "$day_logs.late_count"}, 0]},
            }
        },
        # calculate attendance rate: present_count / headcount (working days only)
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        # 7-day moving average using window function in mongodb
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
    ]

    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        rate = round_half_up(r["attendance_rate"], 4) if r.get("attendance_rate") is not None else None
        avg_7d = round_half_up(r["moving_avg_7d"], 4) if r.get("moving_avg_7d") is not None else None

        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": int(r.get("headcount", 0)),
            "present_count": float(r.get("present_count", 0.0)),
            "late_count": int(r.get("late_count", 0)),
            "attendance_rate": rate,
            "moving_avg_7d": avg_7d,
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"] = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status: Optional[AttendanceStatus] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if not _indexes_created:
        ensure_indexes()

    ep = endpoint

    if ep == "attendance_list":
        if date_from is not None:
            try:
                date_type.fromisoformat(date_from)
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail="date_from must be in YYYY-MM-DD format")

        if date_to is not None:
            try:
                date_type.fromisoformat(date_to)
            except (ValueError, TypeError):
                raise HTTPException(status_code=422, detail="date_to must be in YYYY-MM-DD format")

        if date_from is not None and date_to is not None:
            if date_from > date_to:
                raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

        q: Dict[str, Any] = {}

        if emp_code is not None:
            q["emp_code"] = emp_code

        if date_from is not None or date_to is not None:
            q["date"] = {}

            if date_from is not None:
                q["date"]["$gte"] = date_from

            if date_to is not None:
                q["date"]["$lte"] = date_to

        if status is not None:
            q["status"] = status.value if isinstance(status, AttendanceStatus) else status

        cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size
        }

        raw = db.command("explain", cmd, verbosity="executionStats")

        return {
            "endpoint": ep,
            "collection": "attendance_logs",
            "explain": sanitize_bson(raw)
        }

    if ep == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required for employee_monthly")

        m_start, m_end, _, _, _ = month_range(month)

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": m_start, "$lte": m_end}
                }
            },
            {
                "$group": {
                    "_id": None,
                    "count": {"$sum": 1}
                }
            }
        ]

        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {}
        }

        raw = db.command("explain", cmd, verbosity="executionStats")

        return {
            "endpoint": ep,
            "collection": "attendance_logs",
            "explain": sanitize_bson(raw)
        }

    if ep == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")

        _, m_end, _, _, _ = month_range(month)

        emp_match: Dict[str, Any] = {"joined_on": {"$lte": m_end}}

        if department is not None:
            emp_match["department"] = department

        pipeline = [
            {"$match": emp_match},
            {
                "$group": {
                    "_id": "$department",
                    "count": {"$sum": 1}
                }
            }
        ]

        cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {}
        }

        raw = db.command("explain", cmd, verbosity="executionStats")

        return {
            "endpoint": ep,
            "collection": "employees",
            "explain": sanitize_bson(raw)
        }

    if ep == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")

        m_start, m_end, _, _, _ = month_range(month)

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": m_start, "$lte": m_end},
                    "late_minutes": {"$gt": 0}
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"}
                }
            }
        ]

        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {}
        }

        raw = db.command("explain", cmd, verbosity="executionStats")

        return {
            "endpoint": ep,
            "collection": "attendance_logs",
            "explain": sanitize_bson(raw)
        }

    if ep == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required for department_trend")

        pipeline = [
            {"$match": {"department": department}},
            {"$limit": 1}
        ]

        cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {}
        }

        raw = db.command("explain", cmd, verbosity="executionStats")

        return {
            "endpoint": ep,
            "collection": "employees",
            "explain": sanitize_bson(raw)
        }

    raise HTTPException(status_code=422, detail="Unknown endpoint")