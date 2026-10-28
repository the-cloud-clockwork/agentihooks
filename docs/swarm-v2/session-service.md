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
token is answered 401 before any catalog read.

## HTTP ingestion

`POST /v2/sessions/ingest` accepts the existing version two ingestion envelope:
`schema_version`, `batch_id`, `access_domain`, `session_uid`, `source_generation` and `events`.
Each event carries `sequence`, `byte_start`, `byte_end`, optional `project_id` and a normalized `record`.
The ingestion API owns record validation, quarantine, contiguous watermarks and batch identity.
The server uses the same bearer token grants and one MiB JSON object limit as search.
Missing or unknown tokens receive 401 before body parsing or catalog access; malformed JSON receives 400.
A valid envelope returns 200 with the ingestion result, including `state` and any structured `error.class`.
A refused corpus or project writes nothing. Repeating an identical batch returns `state: replayed`;
reusing its identity for different content returns a revision conflict. Catalog outages return a
retryable dependency refusal. Successful persistence reports `indexed: false`: it does not acknowledge embeddings.

## Embedding process

Run `python -m hooks.memory.embedding_process` as a separate supervised process in the same image.
Run `python -m hooks.memory.embedding_process --once` for one bounded pass; it exits zero on a
completed pass, including a deferred provider outage. Each pass prints one JSON report with the
worker state, embedded and failed chunk identities, pending count, backlog age, skipped sessions and scan cursor.
A supervisor should restart a failed process; configuration errors, model conflicts, invalid provider responses
and queue entries outside its grant exit nonzero. Reports and errors omit the provider credential.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `AGENTIHOOKS_SESSION_CATALOG` | yes | | Same durable catalog as ingestion and retrieval |
| `AGENTIHOOKS_SESSION_EMBED_CORPUS` | yes | | Only corpus this process may index |
| `AGENTIHOOKS_SESSION_EMBED_PROJECTS` | yes | | Nonempty JSON array of allowed project names |
| `AGENTIHOOKS_SESSION_EMBED_GENERATION` | yes | | Durable generation bound to one model and dimension |
| `AGENTIHOOKS_SESSION_EMBED_MODEL` | yes | | Provider model identity |
| `AGENTIHOOKS_SESSION_EMBED_DIMENSION` | yes | | Positive vector dimension |
| `AGENTIHOOKS_SESSION_EMBED_URL` | yes | | Full provider endpoint; HTTPS, or HTTP on loopback for development |
| `AGENTIHOOKS_SESSION_EMBED_TOKEN` | yes | | Provider bearer credential injected through the environment |
| `AGENTIHOOKS_SESSION_EMBED_INTERVAL` | no | `5` | Positive finite seconds between passes |
| `AGENTIHOOKS_SESSION_EMBED_SESSION_LIMIT` | no | `100` | Active sessions scanned per pass |
| `AGENTIHOOKS_SESSION_EMBED_BATCH_SIZE` | no | `32` | Maximum chunks per provider request |
| `AGENTIHOOKS_SESSION_EMBED_BATCH_CHARS` | no | `64000` | Worker batch character budget |
| `AGENTIHOOKS_SESSION_EMBED_MAX_BATCHES` | no | `8` | Maximum batches drained per pass |

The provider accepts JSON `{"model": "configured identity", "input": ["chunk text"]}` with bearer
authentication and returns `{"model": "configured identity", "data": [{"index": 0, "embedding": [0.1]}]}`.
Every input needs exactly one indexed vector of the configured dimension; response order may vary.
Requests time out after thirty seconds and responses are capped at two MiB. Provider URLs cannot carry
credentials, queries or fragments. Provider 429, server errors, connection failures and timeouts defer
pending work until the next pass. Other HTTP errors and malformed responses fail the process without
acknowledging the work. The persistent queue and content hashes make restart and replay idempotent.

The producer must register retained sessions, preserve their archive evidence, build and validate chunks,
and activate their chunk generations through the existing catalog and chunk APIs. Ingestion alone does
not perform those operations. This process discovers retained sessions with active chunk generations,
enqueues changed chunks under its grant and drains the configured generation. Sessions outside its
project grant are skipped; each selected provider batch is checked against the grant and retained
session state before provider access.
Give a generation one owner and a stable grant; changing the model or dimension requires a new generation.

Continuous mode advances its session cursor and wraps at the end so later sessions are visited.
A one shot invocation visits only the first bounded page; use continuous mode for larger corpora.
Index generation activation remains an explicit `IndexWorker.activate` operation after complete coverage;
the process never switches retrieval to an incomplete generation. SIGTERM and SIGINT request shutdown
between passes, interrupt the idle interval and close the catalog after any current bounded pass finishes.
