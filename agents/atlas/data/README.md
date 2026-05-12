# atlas's data outputs

Saved snapshots and computed signal exports — CSVs, JSON dumps, anything
the agent wants to read across sessions without going back through
Massive. Use `write_my_note(filename="data/<name>.<ext>", ...)` (mode
"write") or have your quant model write directly via the standard
filesystem.

Convention: prefix the filename with the date the data was captured for
audit (e.g. `2026-05-04_<source>.csv`).
