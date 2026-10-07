---
name: Tests before done (older copy)
description: Older duplicate of the run-tests rule
type: feedback
created: 2026-01-15
owner_agent: builder
---

Run the full test suite and paste the pass and fail counts before reporting that a
change is finished. A report that skipped the tests once hid a broken import for a
whole day, and the fix took five minutes once someone actually ran the suite.
