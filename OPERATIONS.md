# Hermes Local Operations

This file documents the local Docker setup used on this machine.

## Runtime

- Container: `hermes`
- Image: `nousresearch/hermes-agent:latest`
- Compose file: `docker-compose.local.yml`
- Persistent data: `~/.hermes` mounted as `/opt/data`
- Dashboard: `http://127.0.0.1:9119`
- Gateway command: `hermes gateway run`

## Current Channels

- Telegram is enabled and paired.
- Email/Gmail is disabled in `~/.hermes/config.yaml` via:

```yaml
platforms:
  email:
    enabled: false
```

Do not re-enable Email until a valid app password and allow-list are configured.

## Start, Stop, Restart

```bash
docker compose -f docker-compose.local.yml up -d
docker compose -f docker-compose.local.yml restart gateway
docker compose -f docker-compose.local.yml logs -f gateway
docker compose -f docker-compose.local.yml down
```

Health checks:

```bash
docker ps --filter name=hermes
docker exec hermes hermes gateway status
curl -s http://127.0.0.1:9119/api/status | python3 -m json.tool
```

## Update

```bash
docker compose -f docker-compose.local.yml pull
docker compose -f docker-compose.local.yml up -d
```

After updating, verify the dashboard and channels page. The local dashboard hotfix below may need to be revalidated against upstream.

## Local Dashboard Hotfix

The compose file bind-mounts:

```text
./hermes_cli/web_server.py:/opt/hermes/hermes_cli/web_server.py:ro
```

Reason: Docker Desktop bind-mounted runtime files made the dashboard's gateway PID/file-lock detection unreliable. The local patch lets the dashboard trust `GATEWAY_HEALTH_URL` when the API server health endpoint is reachable.

Do not remove this mount unless upstream dashboard gateway detection works correctly in Docker.

## Backup

Run:

```bash
./scripts/backup-hermes-home.sh
```

Default output:

```text
./backups/hermes-home/hermes-home-YYYYmmdd-HHMMSS.tar.gz
```

The script pauses the container briefly by default before archiving SQLite DB files. Use `HERMES_BACKUP_STOP_CONTAINER=0` for a best-effort live backup.

## State DB

Canonical session data lives in:

```text
~/.hermes/state.db
```

Useful inspection queries:

```bash
sqlite3 ~/.hermes/state.db '.tables'
sqlite3 ~/.hermes/state.db "select source, user_id, count(*) sessions, sum(message_count) messages from sessions group by source, user_id;"
sqlite3 ~/.hermes/state.db "select s.id, s.source, s.user_id, s.model, s.message_count, datetime(s.started_at,'unixepoch') started_at from sessions s order by s.started_at desc limit 20;"
sqlite3 ~/.hermes/state.db "select m.role, datetime(m.timestamp,'unixepoch') ts, substr(m.content,1,160) content from messages m where m.session_id='SESSION_ID' order by m.timestamp;"
```

For self-evolution and analytics, prefer `state.db` over `~/.hermes/sessions/*.json`.

## Local LLM Sidecar

Ollama runs on the macOS host, outside the Hermes Docker container. Docker reaches
the host service through:

```text
http://host.docker.internal:11434/v1
```

Configured local alias in `~/.hermes/config.yaml`:

```yaml
model_aliases:
  gemma-4-12b-local:
    model: gemma4:12b
    provider: ollama-local
    base_url: http://host.docker.internal:11434/v1
  qwen3-4b-local:
    model: qwen3:4b
    provider: ollama-local
    base_url: http://host.docker.internal:11434/v1

providers:
  ollama-local:
    name: Ollama Local
    base_url: http://host.docker.internal:11434/v1
    default_model: gemma4:12b
    api_key: no-key-required
    api_mode: chat_completions
    extra_body:
      think: false
```

Primary inference still uses OpenRouter. The local alias is intended as a
sidecar for cheap/private/offline tasks and smoke tests, not as the default
agent brain.

Do not enable this provider in `fallback_providers` yet. The transport works,
but full Hermes agent turns currently do not produce a final assistant message
with these local Ollama models.

Current verification status:

- Docker can reach Ollama at `http://host.docker.internal:11434/v1`.
- Direct OpenAI-compatible `/v1/chat/completions` works for `gemma4:12b`.
- Full Hermes agent turns with `gemma-4-12b-local` and `qwen3-4b-local` currently
  reach the local endpoint but do not produce a non-empty final assistant
  message. Treat this as a transport-ready but agent-protocol-incomplete
  integration until Hermes/Ollama response adaptation is added.

Verify from the host:

```bash
brew services list | grep ollama
curl -s http://127.0.0.1:11434/v1/models
ollama list
```

Verify from Docker:

```bash
docker exec hermes curl -s http://host.docker.internal:11434/v1/models
docker exec hermes hermes -m gemma-4-12b-local -z "Answer in one short sentence: local model is online."
```

## MCP

Configured MCP servers:

```yaml
mcp_servers:
  filesystem:
    command: npx
    args:
      - -y
      - "@modelcontextprotocol/server-filesystem"
      - /opt/data/workspace
    enabled: true
```

The filesystem MCP server is intentionally restricted to:

```text
/opt/data/workspace
```

This maps to the Hermes persistent home volume and avoids granting broad host filesystem access.

Verify:

```bash
docker exec hermes hermes mcp list
docker exec hermes hermes mcp test filesystem
```

GitHub MCP is not configured yet. Add it only after deciding token scope and storage policy.
