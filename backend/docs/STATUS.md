Done: Task 1 scaffold · Task 2 datetime parser (122 tests passing) · Task 3 parser.py (pending test run)
Tests: smoke 9 + datetime 113 passing; parser: <paste result>
Decisions: parser is state-agnostic except DISAMBIGUATING; expected_field drives bare answers;
           medical/unsupported checked before flow intents; confirm needs pure yes-words
NEXT TASK: Task 4 — engine (brain/engine.py, memory_repo, appointments service) for the booking flow