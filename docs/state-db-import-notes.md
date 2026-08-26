# Hermes State DB Import Notes

Hermes stores current session history in SQLite:

```text
~/.hermes/state.db
```

The legacy JSON path `~/.hermes/sessions/*.json` is not enough for Docker gateway sessions. Use `state.db` as the source for analytics and self-evolution importers.

## Tables

- `sessions`: one row per Hermes session.
- `messages`: ordered conversation turns and tool records.
- `messages_fts`, `messages_fts_trigram`: full-text search indexes.
- `compression_locks`: runtime lock state.
- `state_meta`, `schema_version`: metadata.

Important `sessions` columns:

- `id`
- `source`
- `user_id`
- `model`
- `model_config`
- `system_prompt`
- `started_at`
- `ended_at`
- `message_count`
- `tool_call_count`
- `input_tokens`
- `output_tokens`
- `estimated_cost_usd`
- `title`

Important `messages` columns:

- `session_id`
- `role`
- `content`
- `tool_calls`
- `tool_name`
- `timestamp`
- `token_count`
- `platform_message_id`

## Export Queries

Session inventory:

```sql
select
  source,
  user_id,
  count(*) as sessions,
  sum(message_count) as messages,
  datetime(min(started_at),'unixepoch') as first_seen,
  datetime(max(started_at),'unixepoch') as last_seen
from sessions
group by source, user_id
order by last_seen desc;
```

Session transcript:

```sql
select
  m.role,
  datetime(m.timestamp,'unixepoch') as ts,
  m.content,
  m.tool_name,
  m.tool_calls
from messages m
where m.session_id = :session_id
order by m.timestamp, m.id;
```

Candidate importer contract for self-evolution:

```json
{
  "session_id": "20260604_072810_b69f8898",
  "source": "telegram",
  "user_id": "164473883",
  "turns": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "metadata": {
    "model": "...",
    "started_at": "...",
    "message_count": 3
  }
}
```

