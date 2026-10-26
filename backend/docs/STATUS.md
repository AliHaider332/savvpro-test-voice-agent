Done: Task 1 scaffold · Task 2 datetime parser · Task 3 parser.py (all passing)
      Task 4 engine (booking) + memory repo + service  (pending your test run)
Tests: <paste summary>
Decisions: single mutation point Engine._execute behind authorize(); fingerprint = sha256(flow|doctor|slot|name|appt id|version);
           TTL 10 min; own-booking detection via last_completed.booking_reference; NO_ACTION event added
NEXT TASK: Task 5 — reschedule + cancel in the engine (repo: get_appointment_by_reference, reschedule, cancel; verify ref+name; version-checked updates)