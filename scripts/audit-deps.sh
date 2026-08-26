#!/usr/bin/env bash
# audit-deps.sh — check every installed dependency against OSV.dev.
#
# pip-audit is deliberately NOT installed in the runtime image: adding a tool to
# a production container to answer a question is a worse trade than asking the
# same database over HTTP. OSV.dev needs no key and no account.
#
# This checks the WHOLE dependency tree, not the three packages someone
# remembered to write a CVE comment next to in pyproject.toml. Those comments
# asserted remediation; nothing had ever verified it.
set -uo pipefail

CONTAINER="${HERMES_CONTAINER:-hermes}"

# -i is required: without it `docker exec` does not forward stdin, so the
# heredoc below reaches python as an empty program — which exits 0 and
# reports success while having checked nothing.
docker exec -i "$CONTAINER" /opt/hermes/.venv/bin/python - <<'PY'
import json, urllib.request
from importlib.metadata import distributions

pkgs = sorted({(d.metadata["Name"], d.version)
               for d in distributions() if d.metadata["Name"] and d.version})
queries = [{"package": {"name": n, "ecosystem": "PyPI"}, "version": v} for n, v in pkgs]

req = urllib.request.Request(
    "https://api.osv.dev/v1/querybatch",
    data=json.dumps({"queries": queries}).encode(),
    headers={"Content-Type": "application/json"},
)
results = json.load(urllib.request.urlopen(req, timeout=90))["results"]

hits = []
for (name, ver), r in zip(pkgs, results):
    for v in (r.get("vulns") or []):
        hits.append((name, ver, v["id"]))

print(f"checked {len(pkgs)} packages against OSV")
if not hits:
    print("no known vulnerabilities")
    raise SystemExit(0)
for name, ver, vid in sorted(hits):
    print(f"  VULNERABLE  {name}=={ver}  {vid}  https://osv.dev/vulnerability/{vid}")

# Deliberately NOT reporting a "minimum safe version" derived from the fixed
# events. An advisory with no fix contributes no version, so taking the maximum
# of the ones that do have a fix silently ignores exactly the advisories that
# cannot be fixed by upgrading — and reports a version that is still vulnerable
# as if it were clean. That happened: hermes-agent 0.18.0 was computed as the
# safe floor while two advisories still applied to it.
#
# The only trustworthy way to name a safe version is to query candidate versions
# until one comes back with zero advisories, which needs the version list the
# caller is actually willing to move to.
print()
print("To find a safe version, query candidates directly rather than reading")
print("'fixed' fields — advisories with no fix are invisible to that arithmetic:")
print("  curl -s -XPOST https://api.osv.dev/v1/query -d "
      "'{\"package\":{\"name\":\"NAME\",\"ecosystem\":\"PyPI\"},\"version\":\"X.Y.Z\"}'")
raise SystemExit(1)
PY
