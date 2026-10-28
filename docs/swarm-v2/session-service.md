# Session memory service

The hosted session memory service serves the session search call and the session tool calls over authenticated HTTP.
It runs from the swarm image with the command `python -m hooks.memory.service`.

## Configuration

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `AGENTIHOOKS_SESSION_CATALOG` | yes | | SQLite file path, or a `postgresql://` URL for Postgres with pgvector |
| `AGENTIHOOKS_SESSION_OBJECTS` | yes | | Directory of the session archive object store; the deployment owns its volume and backup |
| `AGENTIHOOKS_SESSION_TOKENS` | yes | | JSON token file: SHA-256 hex digest of each bearer token mapped to `principal`, `corpus` and `projects` |
| `AGENTIHOOKS_SESSION_HOST` | no | `0.0.0.0` | Bind address; TLS terminates in front of the service |
| `AGENTIHOOKS_SESSION_PORT` | no | `8790` | Bind port |

The Postgres database needs PostgreSQL 16 or later with the `vector` extension already installed; the service refuses
a database without it and never creates it.

## Routes

| Route | Answer |
|---|---|
| `GET /healthz` | 200 when the catalog answers, 503 otherwise |
| `POST /v2/sessions/search` | session search |
| `POST /v2/sessions/tools/<name>` | the named session tool |

Every `POST` needs `Authorization: Bearer <token>`. The grant comes only from the token file; a missing or unknown
token is answered 401 before any catalog read. Ingest is not served over HTTP.
