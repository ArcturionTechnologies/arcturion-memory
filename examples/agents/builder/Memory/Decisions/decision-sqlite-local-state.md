---
name: SQLite for local state
description: Use SQLite for small local tool state; a server database only for multi-writer data
type: adr
created: 2026-02-10
owner_agent: builder
---

Use SQLite for local tool state under 1 GB. Move to a server database only when two
machines must write the same data.
