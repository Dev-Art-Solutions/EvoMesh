# EvoMesh v1.0 readiness audit (PR 00)

Status: **audit only**. This PR changes no runtime code. Every claim below was checked by reading
source at the SHAs listed. The test results are the ones recorded in the baseline section. Nothing
here claims that a feature exists unless a file and function are named for it.

Contract proposal: [../integrations/data-collector-contract.md](../integrations/data-collector-contract.md).

## 1. Baseline (2026-11-24)

| Repo | HEAD audited | Working tree | CI on HEAD | Local checks |
|---|---|---|---|---|
| Dev-Art-Solutions/EvoMesh | `5a3a02f` (merge of #3, M3 Lead Researcher) | clean worktree; the live checkout has 4 untracked mesh-authored `skills/` dirs, not touched | success | `ruff` clean; `pyright` 0 errors; `pytest`: see §1.1 |
| Dev-Art-Solutions/EvoMesh-LeadCollector | `cfc34dd` | clean | success (LeadCollector + Extension) | not run: no `node_modules` installed locally, none installed for this audit |
| iovigi/Data-Collector | `34c5a28` (2.0.0rc1 + Next.js admin) | clean | success (`87d5bf5` before it failed, fixed by `34c5a28`) | not run: the local `venv` has no pytest, and nothing was installed into it |

### 1.1 EvoMesh pytest

Local run on `5a3a02f` (Windows, `--basetemp=.pytest-tmp`): **1483 passed, 10 skipped, 1 failed**
in 288 s.

The failure is `tests/test_capabilities.py::test_console_install_refresh_and_grants_keep_permissions`.
The test passes an unquoted path to `/tool install`. Here the path contains a space
(`D:/Projects/Dev-art solutions/...`), so the console splits it and answers
`'D:/Projects/Dev-art is not a file or directory.'`. CI paths have no spaces, so CI is green.
This is a test bug (it should quote the path). It also exposes a real usability edge: a Windows
user whose install path has a space must quote paths in `/tool install`. Fix it in PR 00b or 12;
it is not hidden here. CI on `5a3a02f` is green.

## 2. What exists: source, not the plan

### EvoMesh

| Area | Where | Fact |
|---|---|---|
| SMTP | `src/evomesh/mailer.py` `Mailer.send`, `_deliver` | Named accounts; `starttls`/`ssl`/`none`; allow-list per account (`recipient_allowed`); `max_recipients`, `max_body_chars`; JSONL audit `data/email-audit.jsonl` |
| SMTP rate limit | `Mailer._check_rate` | **In-memory `deque`.** A restart resets the hourly budget |
| `send_email` tool | `mailer.build_send_email_tool` | **Sends immediately.** There is no draft, no human approval and no idempotency key. The account grant is the only gate |
| Header safety | `parse_recipients`, `Mailer.build` | Rejects CR/LF/`,;<>` in addresses; subject whitespace collapsed (strips CR/LF). Good base |
| Mail grants | `AgentDefinition.email_accounts` (`contracts.py`), `/email grant|revoke` (`ops_commands.py`) | Per agent, persisted with the agent definition |
| IMAP | none | **Confirmed absent.** Nothing in `src/` imports `imaplib` |
| Secrets | `config._load_secrets`, `_resolve_api_key_refs` | `evomesh.secrets.yaml`, plaintext, gitignored. **No ACL or DPAPI** anywhere in `src/`, `scripts/`, `desktop/` |
| MCP | `mcp_client.py` `McpManager`, `McpConnection` | stdio + streamable HTTP, per-agent `AgentDefinition.mcp`, fingerprint-keyed connections. Reuse as is |
| Capabilities view | `capabilities.py` | Read-only inventory, never a registry (root CLAUDE.md) |
| SQLite | `storage.py` `MIGRATIONS` (2 scripts, `schema_version` 1–2), `state` k/v table | Additive `CREATE TABLE IF NOT EXISTS` migrations. **No down-migrations, no pre-migration backup, no version check that refuses a newer DB** |
| Harness queue | `harness_queue.py` | **Not durable by design** (root CLAUDE.md): stopping the mesh cancels queued jobs and reports it |
| Durable state | `save_state` callers: manager trust/repeats/desk, evolution pipeline, blackboard, ideas intake, Telegram offsets | No generic job/checkpoint table. The outbox needs its own table |
| News cache | `tools/news_fetch/scripts/news_fetch.py` `_cache_path()`, `MAX_CACHE_ENTRIES = 5000`, `cache_days` | JSONL inside the tool's `scripts/` directory. The first DC producer reads from here |
| Control port | `control.py` `ControlServer` on `127.0.0.1:8765` | **No authentication.** Any local process can issue any console command. See P0-1 |
| Desktop | `desktop/EvoMesh.Desktop/MainForm.cs` tabs: Console & Chat, Agents, Settings | **No Email or Data Collector UI.** `EvoMeshYamlSettings` round-trips unknown keys (`CollectExtras`/`WriteExtras`), so adding `email:`/`data_collector:` blocks is safe for today's editor |
| Dependencies | `pyproject.toml` | **Six** runtime deps (`mcp[cli]` was added). Root CLAUDE.md rule 16 still says five, so the rule is stale |
| CI | `.github/workflows/ci.yml` | Linux: `uv sync --locked`, ruff, pyright, pytest. Windows: build plus 3 desktop self-tests, packaging. **The package is labelled `v0.1.0-alpha.1` in the workflow regardless of version** |
| LeadCollector link | `agent-templates/lead-researcher/` | MCP + browser template; `mcp: []` by default. Do not rebuild |

### EvoMesh-LeadCollector

- SQLite with its own migrations: `apps/api/src/modules/jobs/migrations.ts` (`jobs`, `leads`, `provider_ids`, `job_leads`, `lead_sources`, `lead_contacts`, `enrichment_runs`, `export_artifacts`).
- MCP tools as documented in `apps/api/MCP.md`; `request_id` makes job start idempotent; exports return path + SHA-256.
- **No outbound sync of any kind** (no outbox, webhook, or Data Collector reference in `apps/api/src`).

### Data-Collector

| Area | Where | Fact |
|---|---|---|
| Records REST | `api/v1/records.py` | create, `batch` (207 on partial), list, search, get, patch (`expected_version`), delete/restore |
| Single-create idempotency | `records.create_record` + `IdempotencyRepository` | `Idempotency-Key` header, **24 h TTL** (`mongo.py` TTL index) |
| **Batch idempotency** | `RecordService.batch_create` | **None.** Every item is a fresh `insert_one`. A retried batch, or a retried 207, duplicates every item that already landed. Only `ValidationError` is caught per item. Any other error aborts mid-batch with partial writes and no per-item report |
| Natural key / upsert | `records` indexes in `infrastructure/mongo.py` | **None.** There is no unique index on a producer key. Two observations of the same article are two records |
| Query DSL | `services/query_dsl.py` | Bounded: `eq ne gt gte lt lte in exists contains`, depth 4, 20 predicates, 16 KB, signed cursor. Reuse for aggregation filters |
| Aggregation | none | **Confirmed absent.** No `$group` or aggregate path in `data_collector/` |
| MCP | `mcp/tools.py` | list/describe/get/search/count/create/update/prepare-delete/delete records, export jobs. No aggregate |
| Scopes | `domain/identities.py` `Scope`; API keys carry `allowed_collection_ids` (`auth_service.py`) | A **collection-scoped write-only ingest key is already expressible**. Per-agent principals need no new auth model |
| Artifacts | `infrastructure/storage.py` `LocalVolumeArtifactStore` | Local volume only |
| Deployment | `docs/operations/deployment.md`, `deploy/caddy`, `compose.yaml` | Docker + Caddy. **No IIS/Cloudflare document.** `PRODUCTION_READINESS_REPORT.md` says `STAGING_GO`; the ChatGPT/Claude remote connector is listed as **not externally validated** |
| Live host | `data.devart.solutions` | **Not probed in this audit.** Its version, auth issuer and reachability are unknown here |

## 3. Where the plan and the code disagree

1. **The plan omits the unauthenticated control port.** Every grant it proposes (DC write scopes, mail read/send, approvals) can be issued through it. See P0-1.
2. **`send_email` already sends without approval.** Plan C5 requires approval of every external send. Making that the default **changes behaviour for agents granted an account today**. That is a decision for the operator (see §6, D-1), not a silent change.
3. **The plan assumes DC can absorb at-least-once delivery.** It cannot today: batch create has no idempotency and there is no natural key. PR 01 must land before PR 02 can be correct, not merely tested.
4. **24 h idempotency TTL is shorter than the plan's offline window.** The outbox backoff caps at 30 min and has 12 attempts, but a laptop offline over a weekend replays keys DC has forgotten. Identity must come from a stored unique key, not the TTL cache.
5. **The plan's §3 B0 asks to "reuse existing aggregation/export jobs".** Export jobs exist; aggregation does not.
6. **Root CLAUDE.md rule 16 (five deps)** is already false (`mcp[cli]`). IMAP via stdlib `imaplib` + `email` keeps the count at six. The DC client fits on `httpx`, which is already a dependency.

## 4. Threat model delta (v1 scope)

| Threat | Today | Needed before the feature it guards |
|---|---|---|
| A web page drives the control port | **Open at 5a3a02f.** `_dispatch` answers and skips non-JSON lines, so a browser `fetch()` to `127.0.0.1:8765` runs its body as a command | **PR 00b:** a non-JSON line closes the connection, and a per-start token is required for every command but `/ping` |
| Agent code escalates its own grants | **Inherent, not a port bug.** On the live config, `harness.enabled` + `allow_write` + `shell_allow: [python]` means arbitrary code as the user. Such a script can read the token, edit `evomesh.yaml`, or write the SQLite grants directly. *Correction to the first draft of this audit, which pinned this on the control port* | Agents that read untrusted content (mail, browser, crawler) get **no `python` in `shell_allow`**. The mail-assistant template (PR 07) has no shell at all. An OS-level sandbox is out of v1 scope; say so in the README |
| Secrets at rest | Plaintext `evomesh.secrets.yaml`, readable by harness `read` if a grant covers the root | Deny-list secrets/`data/` in `permissions.py` regardless of grants; ACL the file to the user; DPAPI optional later |
| Untrusted email/web content steering tools | Browser already has sensitive-action confirmation (`browser.py`) | Same pattern for mail: content never authorises a send, grant or move; approvals bound to a content digest |
| Duplicate external effects after crash | `send_email` has no idempotency; rate counter is lost on restart | Durable mail outbox; ambiguous SMTP outcome is never auto-retried |
| Over-collection / PII to DC | n/a | Default deny; `privacy_class` on every event; mail export off by default |
| Cross-tenant reads in DC aggregation | n/a (no aggregation) | Tenant + collection predicate injected server-side before any stage; adversarial tests |
| Cloudflare → IIS plaintext origin | Not documented | Document it as a known internal hop. Do not call it end-to-end TLS |

## 5. Ranked PR matrix (revised)

New: **PR 00b**. Its numbering is otherwise the plan's own.

| PR | Repo | P | Scope (first file it touches) | Migration | Gate |
|---|---|---|---|---|---|
| 00 | EvoMesh | P0 | this audit + contract | none | review |
| **00b** | EvoMesh | **P0** | control-port token: `control.py` `_dispatch`, `EvoMeshRuntimeProcess.cs`, `scripts/evomesh-service.ps1`; deny secrets/`data/` in `permissions.py` | token file `.runtime/control.token`, regenerated each start | an unauthenticated `/ping` and `/email grant` refused; desktop self-tests green |
| 01 | Data-Collector | P0 | `source_key` unique per (workspace, collection); `batch_upsert` with per-item `created/updated/unchanged/stale/error`; every exception per item; ingest-key provisioning CLI | new partial unique index; existing records untouched (no `source_key`) | replayed batch = 0 new records; stale `observed_at` never overwrites |
| 02 | EvoMesh | P0 | `datacollector.py` client + outbox (migration 3: `dc_outbox`), `data_collector:` settings, `/dc status|sync-now|pause|resume|dead-letter` | additive table; feature off by default | offline → restart → online: 0 lost, 0 duplicated |
| 03 | EvoMesh | P1 | `skills/data-collector/`, `dc_record_event`/`dc_query` tools, NewsWatcher producer reading the news cache | none | replay of the cache twice = same record count |
| 04 | LeadCollector | P1 | own outbox + mapping to `entities`/`contacts`/`source_observations` | LC migration | contract fixtures pass on both sides |
| 05 | EvoMesh | P0 | `imap_client.py`, `mail_store.py` (migration 4), account model gains optional `imap:`; SMTP-only YAML loads unchanged | additive | fixture IMAP server: UIDVALIDITY reset, MIME corpus |
| 06 | EvoMesh | P0 | mail tools, drafts, approval bound to digest, durable send outbox; `send_email` routed through it per D-1 | additive | no send without approval; no duplicate on ambiguous SMTP |
| 07 | EvoMesh | P1 | `agent-templates/mail-assistant`, desktop Email tab | none | two-account E2E (J3) |
| 08 | Data-Collector | P1 | aggregation service on `query_dsl` filters | indexes per collection schema | 50k oracle, tenant isolation |
| 09 | Data-Collector | P1 | MCP aggregate tools, IIS/Cloudflare deployment doc | none | remote MCP from an external host |
| 10 | EvoMesh | P1 | other producers; opt-in mail metadata | none | J4 privacy |
| 11 | EvoMesh | P0 | backup/restore command, refuse newer-schema DB, pre-migration copy, durable rate limits | migration guard | J6 |
| 12 | all | P0 | Windows E2E, version-correct packaging label, RC docs | none | sign-off |

00b goes first because a web page could issue any grant through the port. It does not make
agents with `python` safe; see §4.

## 6. Owner decisions (2026-11-24)

- **D-1 (PR 06): approval by default.** Every `send_email` and every new mail send goes through an approval bound to the content digest. Agents that already have a grant change behaviour; the CHANGELOG must say so.
- **D-2 (PR 02): local, client-editable, never committed.** The DC URL, workspace, project and credential live in the operator's own `evomesh.yaml` (`data_collector:` block) and `evomesh.secrets.yaml`. Both are already gitignored (`.gitignore` lines 15–16). Only `evomesh.yaml.example` gets the block, with fake values. The Control Center must be able to edit it. Real values arrive from the owner later.
- **D-3: Data Collector server work is out of scope here.** Another agent owns the Data-Collector repo. PRs 01, 08 and 09 are not done from this program. The contract document is the handoff: this side builds against the current API, and the outbox treats the receiver's lack of producer-key dedupe as a known gap (see §3, item 3) until the other side ships `upsert-batch`.
- **D-4: Gmail and hMailServer, read and send.** SMTP send already exists. Reading is required on both: IMAP first (both Gmail and hMailServer speak it, and it is the only protocol with folders, flags and UIDs), with **POP3 as well, because the owner asked for it**. POP3 gives inbox-only reads with UIDL; there are no folders, flags or moves, and the tools must say so rather than pretend. Gmail authenticates with an app password (OAuth2 later). hMailServer works with a plain account password over TLS (993/995), or plaintext only by explicit opt-in on a LAN.

The PR matrix in §5 stands, minus 01/08/09 (handed off), with 05 reading IMAP **and** POP3.

## 6a. What shipped in 0.10.0-alpha.1 (2026-11-24)

| PR | Status | Where |
|---|---|---|
| 00b control token | done | `control.py`, all port clients |
| 02 DC client + outbox | done | `datacollector.py`, migration 3, `/dc` |
| 03 skill, tools, news producer | done | `skills/data-collector`, `dc_record_event`/`dc_query`, `producers.news` |
| 04 LeadCollector sync | done, in that repo | `apps/api/src/modules/datacollector`, LC migration 2 |
| 05 IMAP/POP3 + store | done | `mail_client.py`, `mail_service.py`, migration 4 |
| 06 tools, drafts, approval | done | `mail_service.py` (rule 25) |
| 07 template + UI | done | `agent-templates/mail-assistant`, Control Center Email tab + DC settings |
| 10 mail metadata export | done (opt-in per account) | `producers.mail_metadata` / `mail_bodies` |
| 11 backup, schema guard, durable caps, crash report | done | `backup.py`, `storage.initialize`, `MailService.unsettled_report` |
| 01/08/09 receiver, aggregation, MCP | **handed off (D-3)** | the contract doc says what the receiver needs |
| 12 v1.0.0-rc.1 | **not tagged** | the gates below are not met |

**Not established.** These were tested only against fakes, never against the real thing:
- No live Gmail or hMailServer account. IMAP/POP3 ran against in-process fake sessions; SMTP ran a real socket round trip to a local fake server.
- No live Data Collector. The DC tests use an HTTP mock transport.
- No ChatGPT MCP aggregate (J5), because that is the receiver's work.
- No restore onto a clean Windows install (J6). The restore was rehearsed into a fresh directory in tests.

Each of these is a v1.0 RC gate, which is why the release is `0.10.0-alpha.1` and not `v1.0.0-rc.1`. Smaller items still open: `mail_download_attachment` is not built (attachments are listed, never fetched); the CI packaging step still labels its smoke archive `v0.1.0-alpha.1`; the DC receiver dedupes only by `Idempotency-Key` for 24 h until it ships `upsert-batch`.

## 7. Non-goals of v1 (from the plan, confirmed)

HA, compliance certification, automated bulk outreach to collected leads, Marketing Audit rendering (M4 keeps only its schema), merging repositories, enabling trading or self-evolution promotion by default.

## 8. Rollback for this PR

Docs only: revert the commit.
