# Troubleshooting

Common issues and fixes.

## Browser cache (missing Company mode / stale JS)
If the Company mode dropdown or free-text question doesn't appear after updating:
1. Hard refresh: **Ctrl+Shift+R** (Chrome/Edge/Firefox on Windows)
2. Or open DevTools (F12) → Network → "Disable cache" and reload
3. Or test with curl: `curl http://127.0.0.1:8765/ | grep 'id="question"'` should return the input when in company mode context? Or just check HTML has `id="mode"` and `id="question"`.

Server already sends `Cache-Control: no-cache, no-store, must-revalidate`, `Pragma: no-cache`, `Expires: 0` for HTML/CSS/JS.

## Web console is reachable from other machines
- It is not supposed to be: `serve` binds `127.0.0.1` and has **no authentication** —
  any caller can read and write any tenant graph (the tenant comes from the request).
- If you saw the "NO authentication" warning, you (or a `Dockerfile`/compose override)
  bound a routable interface. Go back to loopback, or front it with a proxy that
  authenticates. `docker compose --profile app up -d` publishes the port on
  `127.0.0.1` only.

## Windows Unicode errors
If CLI crashes on LLM output with `UnicodeEncodeError`:
- GraphSleuth configures UTF-8 on stdout/stderr at startup (`cli.py`). Also prefer using PowerShell/Windows Terminal.
- Set `PYTHONIOENCODING=utf-8` if running from cmd.exe in rare shells.

## FalkorDB connection
- Check: `python -m graphsleuth status`
- Start: `docker compose up -d` (FalkorDB on localhost:6379)
- If port in use: change `FALKORDB_PORT` or stop other Redis/FalkorDB.
- Every command except `status` checks the connection first and exits with this hint
  instead of a traceback — so an unreachable database never looks like a bug in the agent.

## "Graph ... is empty"
- `demo` and `pipeline` refuse to run on an unseeded graph (otherwise the agent
  investigates nothing and still reports a verdict). Seed it:
  `python -m graphsleuth seed` — or `--tenant <name>` for a non-default tenant.
- `chat` still starts (it has no graph to check up front), but the offline brain
  now reports "**Verdict** — none reached" with what each tool returned, and it
  writes no case, flag or playbook: an empty graph is not a clean account.

## LLM quota/fallback
- With `GROQ_API_KEY` set, it tries `openai/gpt-oss-120b` → `openai/gpt-oss-20b` → `qwen/qwen3.8-27b` automatically.
- Daily cap on 120b? Fallback kicks in (seen in logs).
- Force offline: `--offline` flag on `demo`, `pipeline`, `company-chat`, `chat`.

## MCP stdio
- Run: `python -m graphsleuth.mcp_server` (reads from stdin/stdout)
- 30 tools total (24 fraud + 6 company). Verify with any MCP client.
- Tenant via `GRAPHSLEUTH_TENANT` env var.

## Tests
- Offline: `python tests/test_offline.py` → 33/33 PASS (no DB needed)
- Eval: `python -m graphsleuth eval` → 11/11 (needs FalkorDB + seeded graph)
- Seed: `python -m graphsleuth seed`

## Multitenancy
Isolation proof:
```powershell
python -c "
from graphsleuth import db, memory as m
db.set_tenant('acme'); m.remember('episodic','canary',['ACC-9003']); print('acme:', len(m.recall('ACC-9003')))
db.set_tenant('default'); print('def:', len(m.recall('ACC-9003')))
print('drop:', db.drop_tenant('acme'))
"
```
Should show acme:1, def:0, drop True.

## Docker
- Build: `docker build -t graphsleuth .`
- App only: `docker compose --profile app up -d` (plain `up -d` starts only FalkorDB)
- Set `FALKORDB_HOST=falkordb` inside compose.

## Concurrency
- `python -m graphsleuth bench --writers 4 --readers 16 --seconds 4` — expect 0 errors.
