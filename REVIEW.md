# REVIEW.md

Defects found in the starter `app/main.py` (helpers and endpoints):

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health()` | Always returned 200 without checking if MongoDB is actually reachable. | Stop MongoDB and hit GET `/health` — it still returns `{"status": "ok"}`. | Added `db.command("ping")` inside a try/except block, raising `HTTPException(503)` if ping fails. |
| 2 | `create_employee()` | Checked if employee exists with `find_one` before inserting. Two rapid requests can both pass the check and insert duplicates. | Send two identical POST `/employees` requests at the same instant — both return 201. | Removed `find_one` and relied on a unique index on `emp_code`, catching `DuplicateKeyError` to return 409. |
| 3 | `create_employee()` | Used `datetime.now()` (naive local time) and returned a string instead of epoch milliseconds. | The response has an ISO date string for `created_at` instead of an integer in milliseconds. | Used `datetime.now(timezone.utc)` and converted it to epoch milliseconds for the response. |
| 4 | `create_employee()` | Missing validation: allowed invalid employee code formats, invalid emails, and didn't check that `shift_start != shift_end`. | POST with `emp_code: "XYZ"` or `shift_start == shift_end` was accepted without error. | Added Pydantic field validators and regex patterns matching the OpenAPI spec. |
| 5 | `list_employees()` | Pagination used `skip = page * page_size`, which skips page 1 completely. | GET `/employees?page=1` returned results from page 2. | Changed to `skip = (page - 1) * page_size`. |
| 6 | `list_employees()` | `total` counted all employees in the collection instead of the filtered count. | Filter by `department=Engineering` and `total` returns the total count for the whole company. | Changed to `db.employees.count_documents(q)`. |
| 7 | `list_employees()` | Results were not sorted by `emp_code` ascending. | Employees came back in arbitrary database insertion order. | Added `.sort("emp_code", ASCENDING)`. |
| 8 | `punch_in()` | Did not return 404 when employee does not exist; crashed with `TypeError` when reading `emp["shift_start"]`. | POST punch-in with unknown `emp_code` crashed with a 500 error. | Added explicit check `if not emp: raise HTTPException(404)`. |
| 9 | `punch_in()` | Used `datetime.fromtimestamp(ms / 1000)` without timezone info, causing wrong attendance dates on non-UTC servers. | Running on a UTC server caused the attendance date to be off because of local time offsets. | Converted timestamp using `tz=timezone.utc`, then converted to IST for date calculations. |
| 10 | `punch_in()` | Did not truncate epoch milliseconds to whole seconds (Rule R1). | Punching in with milliseconds caused small rounding errors in late minute calculations. | Truncated milliseconds using `(ms // 1000) * 1000` before doing calculations. |
| 11 | `punch_in()` | Did not handle overnight shifts (e.g. 22:00 - 06:00). An early morning punch-in got today's date instead of yesterday's shift date. | Night shift worker punches in at 01:00 AM IST and `date` is recorded as today instead of yesterday. | Added `attendance_date()` helper to attribute punches before `shift_end` to the previous day. |
| 12 | `punch_in()` | Checked if record exists before inserting. Two simultaneous punch-ins could create two records for the same day. | Two simultaneous requests both return 201, leaving two records for the same employee and date. | Created a unique compound index on `(emp_code, date)` and caught `DuplicateKeyError` to return 409. |
| 13 | `punch_in()` | No validation on `punched_at`: allowed floats, strings, negative values, and seconds instead of milliseconds. | Sending `punched_at: 1720000000` (in seconds) or a float was accepted. | Added Pydantic validation with `strict=True` and range checks (`ge=100_000_000_000`). |
| 14 | `punch_in()` & `list_attendance()` | Response included internal MongoDB `_id` / `id` field, which is not part of the API schema. | Response JSON contained an unwanted `"id"` field. | Formatted responses using `serialize_attendance()` with only schema-defined fields. |
| 15 | `list_attendance()` | Loaded the entire collection into memory with `list(...)` and sorted in Python. | With 100k records, the endpoint would run out of memory or take several seconds. | Used MongoDB database cursor `.sort().skip().limit()`. |
| 16 | `list_attendance()` | Sorted only by `date` descending, missing the required secondary sort by `emp_code` ascending. | Two records on the same date came back in random order. | Sorted by `[("date", DESCENDING), ("emp_code", ASCENDING)]`. |
| 17 | `list_attendance()` | No check that `date_from <= date_to`, and no bounds check on `page_size`. | Passing `date_from=2026-12-01&date_to=2026-01-01` returned empty results silently instead of 422. | Added validation raising 422 if `date_from > date_to`. |
| 18 | `compute_late_minutes()` | Built shift start time using punch-in date directly, which fails for overnight shifts starting the previous day. | Night shift employee showed huge or negative late minutes. | Rewrote helper to build shift start time on the actual attendance date in IST. |
| 19 | `compute_work_hours()` | Used Python's default `round()` which rounds half-to-even (banker's rounding) instead of half-up. | `round(8.415, 2)` gave `8.41` instead of `8.42`. | Wrote `round_half_up()` using `Decimal` and `ROUND_HALF_UP`. |
| 20 | `compute_overtime()` | Missing the 30-minute threshold check, and did not handle overnight shift end times. | Working 15 minutes past shift end gave 15 minutes overtime instead of 0. | Added the 30-minute threshold and added 1 day to shift end for overnight shifts. |
| 21 | Database setup | No indexes were created at startup. | Queries would perform collection scans and uniqueness was not enforced at the database level. | Added `ensure_indexes()` called during FastAPI lifespan startup. |

### Things looked at and decided were fine:
- Using `load_dotenv(override=False)` is fine because real environment variables still take priority over local `.env` values.
- Leaving `punched_at: Optional[int] = None` in the punch-in model is fine since omitting it defaults to the current server time.


