---
paths:
  - "apps/importer/**"
---

# Importer invariants

- `MARKET_DATA_API_KEY` is environment-only. Never add a CLI secret or persist,
  log, describe, or forward the key outside explicit request-level Bearer
  headers.
- HTTP requests isolate request-level headers and auth from injected session
  headers, `session.auth`, netrc, and redirects. `/ticker_data` and `/klines`
  share the same authenticated session, timeout, retry, and redaction path.
- Treat pagination cursors as opaque. Decode and validate a complete page,
  reject cursor repetition/cycles, commit its usable batch transactionally, and
  only then adopt its `next_cursor`.
- Keep one transaction per imported batch so crash-resume loses at most the
  uncommitted batch.
- HTTP output paths are scoped by full digests of the canonical API base, exact
  symbol, and optional tag. Exact HTTP symbol spelling is preserved; DB-source
  symbols retain legacy uppercasing and legacy output filenames.
