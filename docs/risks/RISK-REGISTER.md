# Risk Register

> Managed by great_cto. See `skills/great_cto/references/risk-register.md` for schema.
> Do NOT edit header. Append risk rows below.

## Active risks

| ID | Title | Impact | Prob | Owner | Source | Status |
|----|-------|--------|------|-------|--------|--------|
| R-01 | UnicodeDecodeError bug class recurs at 8+ file-read sites (cron/jobs.py, gateway/run.py, tui_gateway/server.py most critical) | High — repeat of the 2026-08-24 full-scheduler outage | Medium — depends on non-UTF-8 bytes reaching a scanned dir | avelikiy | audit-2026-08-25 | Open — hermes-agent-n8k/cj5/udv/53e/x55/vgu |
| R-02 | 3 CVEs (requests, PyJWT, starlette) remediated only by version-pin comments, never machine-verified | High if a pin regresses silently | Low — pins are exact (`==`) | avelikiy | audit-2026-08-25 | Open — hermes-agent-wp6 |
| R-03 | gateway/run.py is a 19,776-line, 910-commits/6mo god file | Medium — every change to it is high-risk-of-regression | High — file is under constant active development | avelikiy | audit-2026-08-25 | Open — hermes-agent-e1b |
| R-04 | GitHub Actions billing-blocked account-wide; deployed image built via manual-only GCP script | Medium — deployed image can silently drift from HEAD | Medium | avelikiy | audit-2026-08-25 | Open — hermes-agent-hvd |

## Closed risks

See `docs/risks/closed/`.
