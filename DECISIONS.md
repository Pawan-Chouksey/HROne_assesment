# DECISIONS.md

## 1. Indexes

I added a unique index on `employees.emp_code` to make employee lookups faster and prevent duplicate employee codes.

I also added compound indexes on `employees.(department, emp_code)` and `(joined_on, department, emp_code)` to improve department filtering and headcount queries.

For `attendance_logs`, I added these indexes:

- `(emp_code, date)` — prevents multiple attendance records for the same employee on the same day.
- `(date, emp_code)` — helps with sorting attendance records.
- `(date, late_minutes, emp_code)` — helps with the late leaderboard query.
- `(emp_code, punch_in)` — helps find the latest punch-in record during punch-out.

I considered adding a partial index on `{punch_out: null}`, but the existing `(emp_code, punch_in)` index was sufficient for my queries, so I did not add another index.

## 2. Punch-in race

When two punch-in requests arrive at the same time, both try to insert a record directly.

The unique index on `(emp_code, date)` ensures that only one request can insert a record for that employee and date. The successful request returns 201, while the other raises `DuplicateKeyError`, which my code catches and returns as HTTP 409 Conflict.

## 3. Ties

I used MongoDB's `$setWindowFields` with `$rank` to rank employees by `total_late_minutes` in descending order, following the competition ranking requirement.

The query returns all records where `rank <= limit`. If multiple employees have the same rank at the cutoff, all of them are included. Therefore, the response can contain more rows than the specified limit.

## 4. Headcount

The department summary starts with the `employees` collection and matches employees whose `joined_on` date is on or before the end of the selected month.

It then uses `$lookup` to get matching records from `attendance_logs`, with `preserveNullAndEmptyArrays: true`.

This ensures that employees without any attendance records are still included in the department headcount.

## 5. One thing I would change

If the amount of data grew 100 times, running aggregations over all attendance records for every request could become slow.

I would store pre-calculated daily and monthly statistics in a separate collection and update them whenever attendance records are created or modified. This would reduce the amount of data that needs to be processed for summary requests.