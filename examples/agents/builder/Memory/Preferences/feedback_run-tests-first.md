---
name: Run tests before saying done
description: Always paste test counts before reporting a change as finished
type: feedback
created: 2026-02-01
owner_agent: builder
criticality: load-bearing
pinned: true
---

Run the full test suite and paste the pass and fail counts before reporting that a
change is finished. A report that skipped the tests once hid a broken import for a
whole day, and the fix took five minutes once someone actually ran the suite. The
deploy pipeline should never be the first place a failure shows up.
