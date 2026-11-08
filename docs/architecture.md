# Architecture decisions (ADR)

> Records of decisions that shaped this template.
> Each section follows: **Status** (current / superseded), **Context**, **Consequences**.
> Edit in place rather than appending new files unless the decision is final.

---

## AD-001 · Postgres-only (no Redis)

**Status**: current.

**Context**: The template needs cache, queues, sessions, full-text and hierarchies. Each could
be a separate service (Redis, Elasticsearch, Neo4j…). Geo and embeddings are possible but are
not required by the template. A monolith template shouldn't ship five different pieces of
infra to be "complete".

**Decision**: Use PostgreSQL 18 (official `postgres:18-alpine` image in the compose) as the
single source of truth for everything except the front. The schema requires only **`citext`**
(created by the migration). The official image already ships the contrib extensions
(`pg_trgm`, `unaccent`, `ltree`, `btree_gist`, `pgcrypto`), created by
`scripts/postgres-init`; **PostGIS, pgvector and pg_cron do not come in it** and are opt-in per
project (managed provider or a custom image — see `docs/postgres.md`, "Extensões opt-in"). UUIDs usam
`gen_random_uuid()` (built-in desde o PG13), então `uuid-ossp` não entra na baseline (AD-033).

**Consequences**:
- One connection pool (opcionalmente atrás de um pgbouncer, AD-037), one backup, one set of credentials.
- Fila (Procrastinate) no próprio Postgres: jobs são reivindicados com
  `SELECT … FOR UPDATE SKIP LOCKED`, ACID com o resto do app.
- Sessions are server-side rows (`sessions.token_hash` + `sha256(token)` in cookie) — instant
  revocation, no JWT blocklist dance.
- `kv_cache` is `UNLOGGED` + TTL for ephemeral cache.
- When something genuinely needs sub-ms latency at massive scale (pub/sub, geofencing with
  millions of writes/s) we add the right tool then, not preemptively.

---

## AD-002 · Procrastinate over Celery/RQ/custom SKIP LOCKED

**Status**: current.

**Context**: We need a job queue. Options: Celery (Redis/RabbitMQ broker), RQ, Dramatiq,
arq, a hand-rolled `SKIP LOCKED` runner, or **Procrastinate** (Postgres-only,
async-native, lib maintained by PeopleDoc/UKG).

The template previously shipped that hand-rolled runner plus its table, visibility-timeout
recovery and exponential backoff. It worked, but reinventing the wheel came with ongoing
maintenance (heartbeat, stall detection, periodic tasks, DLQ, retry strategies — each
implemented and tested by hand). The scaffolding was removed from the baseline (AD-018).

**Decision**: Use **Procrastinate 3.10.0** as the queue. Schema is applied verbatim from the
lib via `SchemaManager.apply_schema()` in the migration — no duplication.

**Consequences**:
- Postgres-only: same transactional guarantees as the rest of the app.
- Async-native (psycopg3), so the API event loop is never blocked by worker I/O.
- Heartbeat + stall recovery are handled by the lib, not by us.
- Periodic tasks built-in (`@app.periodic(cron="...")`).
- Trade-off: a few advanced Celery features (chains, chords, fanout canvas) aren't available.
  For our use cases, the `@app.task` decorator + `defer_async` covers everything.
- See `apps/api/app/jobs/app.py` for `register_task`/`defer_job` and
  `apps/api/app/jobs/tasks/email.py` for an example task.

---

## AD-003 · bcrypt directly (no passlib)

**Status**: current.

**Context**: passlib 1.7.4 (latest) is in maintenance and is incompatible with bcrypt ≥ 4.1
(`AttributeError: module 'bcrypt' has no '__about__'`, plus the silent 72-byte truncation).

**Decision**: Use `bcrypt` library directly. Reject passwords above 72 **UTF-8 bytes**
at the schema (`Password` type) and at the security boundary (`_to_bytes` raises), so a
multibyte password can never be silently truncated into a different secret.

**Consequences**:
- One library instead of two.
- Tests caught the passlib issue at template time; users don't rediscover it.

---

## AD-004 · OpenAPI → TypeScript via openapi-typescript

**Status**: current.

**Context**: Frontend needs typed `fetch` calls. Options: hand-roll types, codegen from
OpenAPI, or use a runtime client like `openapi-fetch`.

**Decision**: 
- Backend exposes OpenAPI at `/api/openapi.json`.
- `python -m app.cli dump-openapi` exports it to `apps/api/openapi.json` (gitignored).
- `npm run gen:api` (alias for `openapi-typescript`) produces `apps/web/src/api/schema.d.ts`
  (committed).
- `src/api/types.ts` re-exports what the app uses via `components["schemas"]`.
- `src/api/endpoints.ts` declares the helpers (`AuthApi.login({…}) → Promise<UserOut>`).

**Consequences**:
- Renaming a field on `UserOut` → typecheck fails on the front → fix or accept.
- Adding a path? TS picks up via `paths["/api/v1/…"]` in the generated schema.
- No runtime cost (types-only).
- Em prod os docs/OpenAPI são desligados (AD-029): a geração de tipos usa o schema do
  ambiente de dev/CI, não o de produção.

---

## AD-005 · SPA served by FastAPI (same host)

**Status**: current.

**Context**: Dev needs proxy /api → :8000 from Vite (:5173). Prod has CORS gotchas and
two deployable artifacts.

**Decision**: Build the front once into `apps/web/dist`. FastAPI mounts `/assets/*` and
serves `/favicon.ico` directly; catch-all GET serves `index.html` for client routing.
CORS only enabled in non-prod (same-origin in prod).

**Consequences**:
- One host, one origin, no CORS issues.
- `Cookie: HttpOnly; SameSite=Lax` works without flags.
- Dev still uses Vite + proxy for HMR.

---

## AD-006 · FastAPI/Starlette default catches unhandled exceptions as JSON

**Status**: current.

**Context**: Default FastAPI behavior on uncaught exception is `500 Internal Server Error`
with empty body — bad UX for clients, bad ops (no log correlation).

**Decision**: Custom `@app.exception_handler(Exception)` that:
1. Logs the traceback with `path` as context.
2. Returns `{"detail": "internal error"}` with status 500.

**Consequences**:
- Stack trace goes to the logger (structlog JSON in prod).
- Client gets a parseable shape.
- Pydantic validation errors keep their native 422 handler (FastAPI default).

---

## AD-007 · CLI is a real CLI, not just `dump-openapi`

**Status**: current.

**Context**: A template needs dev tooling. `dump-openapi` alone isn't enough.

**Decision**: `python -m app.cli <subcommand>`:
- `dump-openapi` — exports OpenAPI to stdout (used by `just web-types`)
- `create-user <email> <password> [name]` — admin tool
- `enqueue-job <kind> [payload-json]` — manual job injection
- `db-status` — quick health snapshot

**Consequences**:
- Subcommands live in `app.cli` next to the app, easy to extend.
- All async via `asyncio.run`.
- Documented via `--help` and the `usage()` function.

---

## AD-008 · Tests live next to code, not in CI

**Status**: current.

**Context**: A "lean" template shouldn't gate first commit behind a CI pipeline.

**Decision**: 
- `apps/api/tests/` with unit tests for pure functions (no DB).
- `just test` runs them locally.
- `.github/workflows/ci.yml` runs the same `just test` + `just lint` + `just typecheck`.

**Consequences**:
- No Postgres needed for `pytest`.
- Pure functions only — runner SQL is tested as text, not executed.
- CI catches regressions; users don't need to run it locally.

---

## AD-009 · Rate limit por IP+endpoint (Postgres-only)

**Status**: current.

**Context**: `/auth/login` e `/auth/register` precisam de rate limit. Sem isso,
força bruta e account-enumeration são triviais. Opções incluem Redis (mais uma peça
de infra) ou tabela `rate_limits` em Postgres.

**Decision**: Tabela `rate_limits (key, count, window_start)` + `INSERT … ON CONFLICT
DO UPDATE` que reseta a janela se expirada, incrementa caso contrário. Chave =
`ip:endpoint`. Algoritmo de janela fixa (não sliding). Limites atuais: login 5/min,
register 10/min. Além do limite por IP, login e register têm limite **por conta**
(`enforce_account_limit`, chave `acct:<scope>:<email>`), porque um IP pode ser compartilhado
(NAT) e a conta continua sendo o alvo do brute force (AD-031).

**Consequences**:
- 1 round-trip por request (`hit()`); aceitável para auth.
- Janela fixa tem o problema clássico de bursts no boundary (5 em :59 + 5 em :00),
  mas é o trade-off certo pra proteger contra força bruta.
- A limpeza de linhas expiradas roda nos jobs de `pg_cron` da baseline quando a
  extensão está instalada (AD-027/AD-033); sem ela, fica com o operador.
- Se virar problema de latência sob carga, migrar para `pg_try_advisory_lock` mantém
  Postgres-only sem mudar a interface.

---

## AD-010 · Multi-region seams (documented, not implemented)

**Status**: documented seams; no implementation.

**Context**: o template não é multi-region, mas precisa apontar **onde** a costura
estaria quando virar. Sem isso, qualquer decisão de infra precisa reabrir tudo.

**Decision**: **só ADR por enquanto.** Multi-region para um app lean é over-engineering.

**Onde mexer:**

| Componente | Estado hoje | Quando virar multi-region |
|---|---|---|
| `sessions.token_hash` | único Postgres | precisa replicação síncrona (ou shared store) |
| Procrastinate (`procrastinate_jobs`) | uma fila global | múltiplos `locked_by` por região precisam de quorum / sharding |
| `rate_limits` | local ao cluster | pode ser local + sync entre regiões |
| Cookie `__Host-` | não usa | importante para multi-domain |
| `app.cli dump-openapi` | uma URL | por região |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | uma | múltiplos collectors |
| Procrastinate heartbeat/recovery | lib trata | por região, com `region` na chave de claim |

**Critério para ativar:**
- Latência p99 de geo > 200ms em algum mercado alvo.
- Ratio read:Write > 75:25 (read replicas compensam).
- Compliance exige dados em jurisdição específica.

Quando algum desses virar realidade, é hora de:
1. Adicionar `region` em `settings`.
2. Multi-cluster Postgres (RDS Multi-AZ ou Citus).
3. Router edge com sticky session.
4. Worker pool por região com separação clara.

---

## AD-011 · Observability: vendor-agnostic via OTLP + Sentry opcional

**Status**: current.

**Context**: o app precisa de tracing/metrics e error tracking sem ficar preso a um
vendor. Grafana stack é a stack open-source/self-hosted que o usuário pediu.

**Decision**:
- `app.core.observability` ativa OTel SDK (FastAPI, SQLAlchemy, logging) quando
  `OTEL_EXPORTER_OTLP_ENDPOINT` está setado.
- `app.core.observability` ativa Sentry SDK quando `SENTRY_DSN` está setado.
- O compose tem um perfil `observability` com OTel Collector + Tempo + Prometheus +
  Grafana, todos Grafana Labs (free, open source).
- App nunca importa o vendor diretamente — sempre OTLP/Sentry SDK.

**Consequences**:
- Quem prefere Grafana Cloud (SaaS) só aponta `OTEL_EXPORTER_OTLP_ENDPOINT` pra URL deles.
- Quem prefere Honeycomb/Dynatrace/New Relic: mesmo mecanismo.
- Sentry self-hosted é **pesado** (Postgres + ClickHouse + Kafka + Snuba) — não cabe
  no `observability` profile. Se quiser, opera separado.
- `traces_sample_rate=1.0` por padrão; ajustar em prod com base em volume.

---

## AD-012 · SQLAlchemy Core (`text()`) para queries, ORM só para schema

**Status**: current.

**Context**: SQLAlchemy oferece dois caminhos: ORM com `select(User).where(...)`
(retornando objetos, com identity map, lazy loading) e Core com `text()`/`select()`
(retornando `Row`/`dict`). Os modelos do projeto (`apps/api/app/db/models/`) usam
`Mapped[]` + `DeclarativeBase` (ORM) — isso alimenta Alembic autogen, metadata
introspection (FKs, indexes, tipos custom como `CITEXT`, `INET`, `JSONB`) e dá tipos
Python para os serviços. **As queries em runtime, porém, são todas Core/raw.**

Onde aparece:
- `apps/api/app/core/cache.py` — `GET_SQL`, `UPSERT_SQL`, `DELETE_SQL` como
  constantes módulo-level (`text()` na execução).
- `apps/api/app/core/ratelimit.py` — `INSERT INTO rate_limits ... ON CONFLICT DO UPDATE`
  para janela fixa de rate limit.
- `apps/api/app/api/v1/endpoints/auth.py`, `users.py`, `twofa.py`, `deps.py`,
  `health.py` — `text("SELECT/UPDATE/INSERT ...")` com `:bind` parameters.
- `apps/api/migrations/versions/0001_initial_schema.py` — DDL puro (Alembic op)
  + `SELECT cron.schedule(...)` para `pg_cron`.

**Decision**:
- **Modelos em ORM** (`Mapped[]`) — schema vive em Python, Alembic gera/revisão migrations,
  tipos Postgres exóticos declarados uma vez.
- **Queries em Core** (`text()`) — nenhuma rota passa por `session.query(Model)`;
  o que vai pro banco é SQL explícito com bind params (`session.execute(text(SQL), params)`).
- SQL dos hot paths de cache/ratelimit é exposto como constante módulo-level para
  que testes afirmem na *string* (não no efeito colateral).

**Consequences**:
- `ON CONFLICT DO UPDATE`, `FOR UPDATE SKIP LOCKED`, `RETURNING`, `cron.schedule`,
  CTE recursiva, operadores `JSONB`/`INET` — todos triviais em `text()`, todos
  exigiriam workaround em ORM (e alguns ficariam ilegíveis via `insert(...).on_conflict_do_update(...)`).
- Sem lazy loading → sem N+1 por acidente. Em hot path o custo é só do driver
  (`psycopg`/`asyncpg`), sem hydration de objeto nem identity map.
- Constantes SQL viram contrato — refactor de schema quebra `test_cache.py` /
  `test_ratelimit.py` (`assert "INSERT INTO kv_cache" in UPSERT_SQL`), exatamente
  onde deveria quebrar.
- Resultados das queries são consumidos como `Row`/`dict` e convertidos para Pydantic
  schemas (`UserOut(**dict(r))` em `users.py:list_users`). Sem hydration preguiçosa,
  sem identity map, sem N+1.
- Custo: SQL duplica entre modelo (`Mapped[...]`) e string SQL — schema drift é
  pego por `just check-schema` (gera migration e diffa contra a última commitada).
- Migração para outro ORM/SQL builder é custosa (muita string). Migração para
  dialect diferente de Postgres é igualmente custosa — assumido pela decisão
  Postgres-only em AD-001.

---

## AD-013 · `uv` como gerenciador único de Python (pyproject + uv.lock)

**Status**: current.

**Context**: o backend era instalado com `pip install -r apps/api/requirements.txt`
dentro de um venv criado à mão (`python -m venv .venv`). Isso dava pins diretos, mas
não lockfile: as versões transitivas eram re-resolvidas a cada instalação, o build do
Docker não tinha cache por dependência, o CI só cacheava o `requirements.txt` e não
existia auditoria de dependências Python (o `npm audit` cobria só o frontend).
`pip-audit` em modo `-r requirements.txt` ainda instala as dependências num venv
temporário, o que exige `ensurepip` — inviável com o Python gerenciado pelo `uv`.

**Decision**:
- `uv` é o único gerenciador de Python do projeto.
- Dependências de runtime ficam em `apps/api/pyproject.toml` (`[project]`, pins
  exatos) e as de desenvolvimento em `[dependency-groups] dev` (pytest, pytest-asyncio,
  ruff, pyright, bandit).
- `apps/api/uv.lock` é versionado e é a fonte de verdade das versões resolvidas
  (com hashes). `requirements.txt` foi removido.
- Local: `uv sync` cria/atualiza `apps/api/.venv`; `uv run <cmd>` executa ferramentas.
  CI: `uv sync --locked` (falha se o lock divergir do `pyproject.toml`).
  Docker: binário do `uv` copiado da imagem `ghcr.io/astral-sh/uv` pinada; o venv
  da imagem fica em `/opt/venv` (`UV_PROJECT_ENVIRONMENT`), fora da árvore do projeto.
- Auditoria de dependências de produção: `just api-audit` roda
  `uvx pip-audit@2.10.1 --path` contra um env prod-only (`.venv-audit`), sem
  resolver/instalar nada — logo sem depender de `ensurepip`.

**Consequences**:
- Instalações reprodutíveis (lock com hashes) e cache de camadas por dependência
  no Docker/CI.
- O venv criado pelo `uv` não traz `pip`; ferramentas que dependem de pip rodam
  via `uvx` (semgrep, pip-audit) ou `uv tool install`.
- Atualização de segurança é bump em `pyproject.toml` + `uv lock` (ex.: aiosmtplib
  3.0.1 → 5.1.3, python-multipart 0.0.30 → 0.0.31, pytest 8.3.4 → 9.0.3).
- `uv` exige Python >= 3.10 no host; o projeto fixa 3.13 (`.python-version`).

---

## AD-014 · Tipagem estrita no backend (Pyright strict, sem lista de exclusões)

**Status**: current.

**Context**: `apps/api/pyrightconfig.json` rodava Pyright em `typeCheckingMode: "basic"`
com uma lista `ignore` que cobria endpoints, deps, cli, `core/*` e `tests/**` — a maior
parte do app não era checada. No modo `basic`, o padrão do app
`await session.execute(text(SQL), params).mappings().first()` passava sem aviso, mas
`AsyncSession.execute` é função corrotina: o `await` se aplica ao resultado inteiro e
`.mappings()` é chamado sobre o corrotina → `AttributeError: 'coroutine' object has no
attribute 'mappings'` em runtime. O padrão estava em 14 sites (cache, ratelimit, deps,
auth, users, twofa, cli) — ou seja, **toda query do app quebrava em produção**.

**Decision**:
- `typeCheckingMode: "strict"` com `include: ["app", "tests"]` e **sem** lista de
  exclusões. Única relaxação: `reportMissingTypeStubs: "warning"` (libs de terceiro
  sem stubs, ex.: `py-webauthn`).
- Invariante de runtime: isolar o `await` antes de encadear — `(await session.execute(...)).mappings().first()`.
- Fronteiras sem stub (`py-webauthn`, `pyotp`, `bcrypt`, instrumentação OTel) são
  normalizadas com `cast`/wrapper local, não com exclusão de arquivo nem `# type: ignore`
  em massa.
- `just typecheck` e o CI rodam `uv run pyright app tests` com 0 erros.

**Consequences**:
- O bug de `await` foi corrigido nos 14 sites e agora é erro de tipo, não de produção.
- `reportPrivateUsage` deixa de ser ignorada: código que usa API privada de lib precisa
  de wrapper público ou `getattr` explícito.
- Códigos novos em `app/` e `tests/` precisam de anotações completas; dívida de tipos
  vira falha de CI em vez de descoberta tardia.

---

## AD-015 · Testes de propriedades (Hypothesis) para validadores, cripto e rate limiter

**Status**: current.

**Context**: a suíte tinha 37 testes de caso fixo (`assert` direto), sem Postgres
(AD-008). Validadores Pydantic, criptografia (bcrypt/Fernet/TOTP) e rate limiter tinham
invariantes — round-trip, monotonicidade, limites de threshold — que nenhum teste
expressava.

**Decision**:
- `hypothesis` (dev group) cobre as propriedades em `tests/test_properties_*.py`:
  - **cripto**: round-trip bcrypt, hash ≠ senha, prefixo `$2b$12$`, senha errada → False,
    truncamento em 72 bytes, `ValueError` se e somente se `len < password_min_length`,
    sha256 do token de sessão (64 hex, determinístico), token URL-safe e único,
    TTL exato, round-trip Fernet, recovery codes.
  - **validadores**: `RegisterIn`, `LoginIn`, `EmailStr`, `UserOut`, `TotpConfirmIn`,
    `EnqueueEmailIn` — documentando a dívida atual sem alterar os schemas.
  - **rate limiter**: helpers puros `build_key(ip, endpoint)` e `is_blocked(count, limit)`
    extraídos de `app/core/ratelimit.py`, mais as invariantes do texto de `HIT_SQL`.
- `@settings(max_examples=..., deadline=None)` em tudo que usa bcrypt: um hash custa
  ~100 ms e o deadline padrão (200 ms) vira flake.
- A semântica de contagem/janela **continua no SQL**: extrai-se só a parte pura
  (chave e decisão), sem duplicar o algoritmo em Python.

**Consequences**:
- Invariantes ficam declaradas e verificadas por fuzzing, inclusive nas bordas
  (limiar `limit`/`limit+1`, senhas multibyte, tokens).
- Schemas com regras ausentes (`LoginIn` sem `min_length`, `TotpConfirmIn` sem padrão
  de 6 dígitos, `EnqueueEmailIn` sem `max_length`) ficam registrados como dívida
  nos TODOs dos testes — a correção muda contrato e merece decisão própria.
- AD-008 preservado: nenhum teste toca Postgres.

---

## AD-016 · Tracing distribuído consolidado (config, resource, sampler, log↔span, filas)

**Status**: current.

**Context**: AD-011 ativava o OTel, mas o app lia `os.environ` **no import** do módulo
de observabilidade (configuração não testável), o resource tinha só `service.name`, não
havia sampler explícito, os logs não carregavam `trace_id`, o job de email enfileirado
perdia o trace (a fila não carregava contexto), o provider nunca era finalizado (spans
morriam no container) e a resposta não trazia identificador de trace.

**Decision**:
- Configuração vem de `Settings` (`otel_exporter_otlp_endpoint`, `otel_traces_sampler`,
  `otel_sample_ratio` — aceita `OTEL_TRACES_SAMPLER_ARG` —, `sentry_dsn`,
  `sentry_traces_sample_rate`), **todos com default**: sem endpoint/DSN o tracing é
  no-op.
- `Resource` com `service.name`, `service.version`, `deployment.environment` e
  `service.namespace`; sampler explícito (`always_on`, `always_off`, `traceidratio`,
  `parentbased_traceidratio` — default parentbased com ratio).
- Propagador W3C composto (`tracecontext` + `baggage`) instalado sempre, mesmo sem
  exporter, para que `traceparent` de entrada alimente logs e spans manuais.
- Correlação log↔span: processor structlog `add_trace_context` anexa `trace_id`/`span_id`
  (hex) do span corrente — `None` quando não há span válido, nunca a string de zeros.
- Propagação para a fila: `inject_trace_context` grava o contexto W3C no payload do
  job; `extract_trace_context` o recupera no worker (job antigo sem `trace_context`
  continua funcionando, virando raiz). Spans manuais existem nas fronteiras
  (`email.enqueue` no endpoint, `email.send` no worker).
- `shutdown_tracing()` no `finally` do lifespan da API e no fim do runner do worker
  (flush + shutdown do provider).
- `X-Trace-Id` na resposta via `TraceIdMiddleware`.

**Consequences**:
- Um request e o job de email correspondente ficam no mesmo trace; os logs do
  caminho carregam `trace_id`/`span_id`.
- Sampling e roteamento de vendor continuam sendo decisão do operador (env vars);
  o app permanece vendor-agnostic (AD-011).
- Sem `OTEL_EXPORTER_OTLP_ENDPOINT`, o custo é praticamente zero.
- O endpoint e o worker usam a mesma fila (Procrastinate): o `trace_context` do payload
  liga o span `email.enqueue` ao span `email.send`.

---

## AD-017 · Passkeys na lib `webauthn` (duo-labs) com credential JSON padrão

**Status**: current.

**Context**: o backend pinava `py_webauthn==0.0.6` — o fork `as207960/python-webauthn`
(MIT, últimos releases 0.0.1–0.0.6, projeto abandonado). Mas o código foi escrito
contra a API moderna: usava `result.credential_id` e `result.new_sign_count`, que
**não existem** na 0.0.6 (lá são `attestation...credential_id` e `sign_count`) →
`AttributeError` em runtime nos dois fluxos de passkey. Pior: em 0.0.6
`result.public_key` é um objeto `cryptography`, não bytes → o insert em
`user_passkeys.public_key` (BYTEA) e o `bytes(pk["public_key"])` no login também
estavam errados. A linha legada ainda exigia `certvalidator`→`oscrypto`, e por causa
de um regex de versão do oscrypto 1.3.0 o repo fixava um tarball do GitHub
(hack que quebrava no Linux).

**Decision**:
- Trocar para `webauthn==3.0.1` (`duo-labs/py_webauthn`, BSD-3, Python >=3.10,
  `py.typed`). Mesmo import name (`webauthn`), mesmas deps sem
  `certvalidator`/`oscrypto` → o tarball do `oscrypto` sai do `pyproject.toml`.
- Contrato: o browser envia o **credential JSON padrão** (`id`, `rawId`, `type`,
  `response.clientDataJSON` e `response.attestationObject`/`authenticatorData`/`signature`,
  tudo em base64url **sem padding** — `btoa` puro gera `+/=` e a lib rejeita) em vez
  de 3-4 campos base64 separados. As options voltam como dict camelCase
  (`options_to_json_dict`).
- Challenge: gerado pela lib, **guardado no pending** (TTL 300s, consumo único) e
  comparado com o enviado no verify — o cliente não é fonte de verdade do challenge.
- Lookup: a passkey é buscada e atualizada **por `credential_id`** (não mais
  `SELECT ... WHERE user_id LIMIT 1` com `UPDATE` por user_id).
- `public_key` passa a ser o COSE cru de `credential_public_key` → continua BYTEA,
  **sem migration**.

**Consequences**:
- A lib mantida tem `py.typed`: os casts/`import_module` do adapter saem e os
  warnings de stub desaparecem.
- Os 4 endpoints de passkey mudam de contrato (schemas Pydantic + payload do
  frontend) → `apps/web/src/api/schema.d.ts` regenerado.
- `expected_origin` passa a ser `settings.web_origin` (igualdade exata) e `rp_id` o
  hostname sem porta — checks independentes, como manda a spec.
- Limite conhecido: attestation/assertion reais não são exercitados em teste
  automatizado (não há authenticator no CI) — o round-trip precisa de validação
  manual no browser.

---

## AD-018 · Contratos tipados ponta a ponta (OpenAPI → TypeScript) e enfileiramento na fila

**Status**: current.

**Context**: a geração de tipos já existia (`just web-types` → `src/api/schema.d.ts`)
e o CI já comparava bytes, mas o **conteúdo** do schema não era verificado: um
`response_model` ausente ou um `dict[str, Any]` produz `Record<string, never>` no
TypeScript e o front seguia consumindo um contrato inexistente. Em quatro rotas isso
acontecia de fato (`POST /auth/login`, `/2fa/verify`, `/passkey/login/verify` e os
options/credentials de passkey). O front ainda redeclarava esses shapes à mão e
castava a resposta (`response as UserOut`), o que zerava `user.email` em runtime.
Em paralelo, `POST /users/enqueue-email` gravava numa tabela de fila legada que
ninguém consumia — o worker Procrastinate lê a fila própria, então o e-mail nunca
era enviado.

**Decision**:
- Toda resposta de rota declara um **modelo Pydantic explícito**; `dict[str, Any]`
  como `response_model` está proibido (`Record<string, never>`). `LoginOut` e `OkOut`
  entram em uso; `response_model_exclude_unset=True` no login para o shape não
  ganhar `pending_token: null`/`methods: []`.
- `scripts/check-schema-sync.sh` ganha um **gate semântico**: além do diff de bytes,
  falha se alguma resposta de rota virar `Record<string, never>`/`unknown`.
- O front consome `components["schemas"][...]` (reexportado em `src/api/types.ts`);
  tipos manuais de resposta saem.
- `POST /users/enqueue-email` passa a chamar `send_email.defer_async(...)` — a mesma
  task do worker e do CLI — e `job_id` vira o id do Procrastinate (inteiro).
- A tabela legada de fila, as tabelas de DLQ/rate-limit por kind e o modelo
  `BackgroundJob` foram **removidos do template** (baseline migration e código):
  a única fila é a do Procrastinate.

**Consequences**:
- `schema.d.ts` regenerado (`+168/−15`) e o contrato do login/verify passa a ter
  `properties` de verdade; `apps/api/tests/test_contracts.py` fixa esse contrato.
- As options WebAuthn são JSON base64url: o front precisa convertê-las para
  `BufferSource` (`src/lib/webauthn.ts`) — o `navigator.credentials` exige bytes e
  o fluxo de passkey estava quebrado no browser.
- O `job_id` é `number` no TypeScript (id do Procrastinate); o front só exibe o valor.

---

## AD-019 · Rigor de lint estrito (Biome) e E2E com mock de rede

**Status**: current.

**Context**: o lint do front rodava `biome check` com as regras relevantes em
`warn` — ou seja, nada bloqueava. `noExplicitAny` só avisava. O `files.include`
(`["src/**","e2e/**","*.{ts,tsx,js,jsx,json}"]`) **não casava arquivos da raiz** no
Biome 1.9.4: `vite.config.ts`, `playwright.config.ts`, `tailwind.config.js` e
`postcss.config.js` escapavam do lint (medido: "Checked 0 files"). No lado E2E havia
só 2 specs, ambos exigindo API+DB (o CI não rodava E2E) e o `e2e-full` apontava para
`:5173`, enquanto o compose serve a SPA pela API em `:8000`.

**Decision**:
- `npm run lint` passa a ser `biome check --error-on-warnings` e as regras
  relevantes viram `error` (`noExplicitAny`, `noConsoleLog`, `useImportType`,
  `useExhaustiveDependencies`, `useAwait`, `noUnusedImports`, `noUnusedVariables`,
  `noUnusedFunctionParameters`, `noUndeclaredVariables`, `noNonNullAssertion`,
  `noParameterAssign`, `noUselessElse`). `useNamingConvention` fica `off`: o
  snake_case vem do contrato da API.
- `files.include` vira `["**"]` (o glob da raiz não funciona no 1.9.4) e o schema
  gerado segue ignorado.
- Cenários de falha de autenticação e fluxos assíncronos passam a ser specs com
  `page.route` (mock de rede) em `apps/web/e2e/`: rodam sem API/DB e é isso que o CI
  executa (`just e2e-mock`). Spec que exige backend real **pula** quando ele não
  está disponível.
- `playwright.config.ts` passa a ler `E2E_BASE_URL` e a subir o Vite sozinho quando
  o alvo é a porta do Vite; `just e2e-full` aponta para `:8000` e sempre derruba o
  compose (mesmo em falha).

**Consequences**:
- O job `e2e` do CI roda só os specs mockados (sem Postgres, sem Docker).
- O lint do front passou a pegar arquivos que escapavam; corrigir isso exigiu
  `import type { FormEvent }` em `Security.tsx` (o `React.FormEvent` era o namespace
  UMD, sem import) e a reformatação dos 3 configs da raiz.
- `noDefaultExport`, `useBlockStatements` e `useSortedClasses` ficaram de fora:
  são churn de estilo, não rigor.

---

## AD-020 · Validação de entrada na fronteira (constraints nos schemas)

**Status**: current.

**Context**: os schemas de entrada aceitavam quase tudo. `RegisterIn.password`
tinha `min_length=8` hardcoded (duplicando `settings.password_min_length`),
`LoginIn.password` não tinha limite nenhum, `TotpConfirmIn.code` aceitaria "abc",
`TwoFactorVerifyIn.code` não tinha limites (e precisa aceitar TOTP **ou**
recovery code), `pending_token` não tinha limites, `EnqueueEmailIn.subject/body`
não tinham teto e `list_users` aceitava `limit`/`offset` negativos ou absurdos.
O front já validava senha (mín. 8) e código (mín. 6) no Zod — ou seja, o backend
era a fronteira **mais permissiva**, e um cliente que não passasse pelo front
passaria.

**Decision**:
- Todo schema de entrada declara suas constraints em Pydantic v2
  (`Field(min_length=..., max_length=..., pattern=...)`), e paginação usa
  `Query(ge=..., le=...)`.
- `RegisterIn.password` e `LoginIn.password` usam o tipo `Password`:
  `min_length` vem do `Settings` (não de um literal), `max_length=72` e um
  `AfterValidator` rejeita >72 **bytes UTF-8**. O teto em bytes é o que importa:
  72 caracteres multibyte podem passar de 72 bytes e seriam truncados pelo
  bcrypt.
- Login usa a mesma regra do registro. Não há caminho legado de senha >72
  bytes: o bcrypt 5.x também as rejeita, então aceitá-las no schema só
  transferiria o erro para o hash.
- `TwoFactorVerifyIn.code` aceita 6..64 chars (TOTP de 6 dígitos **ou** recovery
  code de 10 chars) e **não** recebe pattern de 6 dígitos. O pattern `^\d{6}$`
  fica só em `TotpConfirmIn.code`, que é a confirmação de setup do TOTP.
- `pending_token` (16..128), `challenge` de passkey (8..1024), `subject` (≤200),
  `body` (≤100 000), `limit` (1..100) e `offset` (≥0).

**Consequences**:
- Entrada inválida passa a responder **422** em vez de 401. O front já validava
  no cliente, então o fluxo normal não muda.
- As constraints **não** aparecem no `schema.d.ts`: o openapi-typescript descarta
  `minLength`/`maxLength`/`pattern` de campos escalares. Elas ficam no
  `apps/api/openapi.json` e são aplicadas no servidor — o gate de contrato
  (`just check-schema`) continua válido.
- `apps/api/tests/test_validation.py` (20 testes) fixa os limites exatos e
  documenta a armadilha do login; `tests/test_properties_validators.py` passou a
  exigir rejeição.
- `password_min_length` acima de 72 é **configuração impossível**, e por isso
  falha no boot: `Settings` valida `1 <= password_min_length <= 72` e levanta
  `ValidationError` citando `PASSWORD_MIN_LENGTH` e o teto de 72 bytes do
  bcrypt. Sem essa validação, `Field(min_length>72, max_length=72)` só
  apareceria como erro de schema na construção do modelo. O valor nunca é
  capado em silêncio: política impossível de cumprir é erro de config, e erro de
  config deve falhar alto.
- O limite é validado em **bytes UTF-8**, não em caracteres: uma senha de 72
  chars multibyte é rejeitada na fronteira e nunca chega truncada ao bcrypt.

---

## AD-021 · Imagem enxuta non-root, saúde em camadas, boot resiliência e backup com prova

**Status**: current.

**Context**: a imagem final carregava o toolchain de compilação (`build-essential`,
`libpq-dev`, `uv`) e rodava como root; `COPY apps/api` levava `tests/` e
`.hypothesis/`; o `curl` existia só para o healthcheck. O healthcheck do
container chamava `/api/v1/healthz`, que checa o banco — uma queda do Postgres
marcava o container unhealthy e reiniciava um processo saudável; o próprio
endpoint prometia 503 no docstring e devolvia 500, com `degraded`/`db=False`
inalcançável. O boot tinha duas falhas: o loop de espera do Postgres terminava
**sem erro** após 60 tentativas (o Alembic mascarava a causa) e `api` e `worker`
rodavam `alembic upgrade head` ao mesmo tempo. O backup existia (`pg_dump` → R2,
retenção de 14 dias), mas a sanidade era só "arquivo não vazio" — um dump
truncado passava — e **não existia** `restore.sh` nem teste de restauração.

**Decision**:
- **Imagem**: 3 stages (`build-web` → `build-api` → `runtime`) com cache mounts de
  npm e uv (BuildKit); toolchain e uv ficam só no stage de build; a imagem final
  copia apenas `/opt/venv` e roda como `app` (uid 10001, sem shell). O `curl`
  sai: o healthcheck usa o `urllib` do próprio venv. A base do runtime é a
  **mesma** do build porque o venv referencia `/usr/local/bin/python3.13` por
  symlink absoluto — trocar a base quebra o venv. O stage `runtime` remove o `pip`
  da base (e o wheel do `ensurepip`): as libs vendorizadas dele (`msgpack`,
  `urllib3`, `pkg_resources`) têm CVE HIGH (4 findings do Trivy no CI) e o venv do
  `uv` em `/opt/venv` não usa system site-packages.
- **Saúde em camadas**: `/api/v1/livez` (liveness, não toca o banco) e
  `/api/v1/healthz` (readiness, timeout de 2s, 503 + `degraded`/`db=False`
  reais). O HEALTHCHECK do container usa **livez**: é o liveness que decide
  restart, readiness é para proxy/LB. `worker` e `migrate` desligam o healthcheck
  da imagem (não servem HTTP).
- **Boot**: o entrypoint ganha o papel `migrate` (one-shot); api e worker passam a
  depender de `service_completed_successfully` — sem corrida de migrations. A
  espera do Postgres usa `pg_isready` (fallback TCP) e **termina com exit 1** ao
  estourar. A API também depende do `pgbouncer` saudável (AD-037), mesmo quando
  `DATABASE_URL_POOLER` está vazio. `API_INTERNAL_PORT` é a porta interna do
  container; `API_PORT` é a porta publicada no host. O worker ganhou retry com
  backoff na abertura do app (`tenacity`, que já era dependência e não era usado).
- **Backup com prova**: `pg_restore --list` valida o dump (não só o tamanho),
  sidecar `.sha256`, `flock` contra execuções sobrepostas (com degradação no
  macOS, que não tem `flock`), modo local quando não há bucket (mantém o dump) e
  `scripts/restore.sh` + `scripts/test-restore.sh` — o teste cria bancos de
  descarte, faz backup→restore e confere count, `to_regclass`, md5 do conteúdo,
  índice, PK e sequência. O CI ganhou o job `restore-test` com Postgres como
  service container.

**Consequences**:
- O enunciado original da Fase 4 citava **Redis** e um **"daemon Go"**: não
  existem neste repositório (AD-001 é Postgres-only; o daemon real é `uvicorn` +
  o worker Procrastinate, no mesmo binário). O escopo foi adaptado ao stack real.
- A imagem final passa de ~500–800 MB (estimativa) para ~250–450 MB — o número
  real só sai com `docker build`, que não foi executado (sem daemon).
- `docker build`, o HEALTHCHECK real, o drain de 30s do worker e o job
  `restore-test` **não foram validados localmente**: dependem de daemon. O bind
  mount `:ro` com uid 10001 funciona no Docker Desktop; em Linux puro o host
  precisa permitir leitura pelo uid do container.
- O serviço `migrate` roda `alembic upgrade head` a cada `up` — idempotente, mas
  significa que ele sempre roda antes da API.
- A base da imagem ainda não é pinada por digest (exige consultar o registry).

---

## AD-022 · Biome 2.x e react-router 7 (front)

**Status**: current.

**Context**: o front estava no Biome 1.9.4 — a última release 1.x, sem correção
de bug ou segurança desde outubro de 2024 (o `latest` já era o 2.6.3). O
`react-router-dom` 6.30.6 carregava duas CVEs sem patch na linha 6.x
(GHSA-wrjc-x8rr-h8h6, open redirect via `\` em `<Link>`/`useNavigate`,
CVE-2026-53669; e GHSA-337j-9hxr-rhxg, constructor injection no hydration SSR,
CVE-2026-53666): o `npm audit --omit=dev` acusava 2 moderate e o fix exigia v7.

**Decision**:
- **Biome 2.6.3**: `files.include`/`ignore` → `files.includes` (negação `!`),
  `linter.rules.recommended` → `preset: "recommended"`,
  `organizeImports` → `assist.actions.source.organizeImports: "on"`.
  `noConsoleLog` não existe mais: a regra é `noConsole`, que bloqueia todo
  `console.*` por padrão, e o config lista em `options.allow` todos os métodos
  **exceto** `log` — o mapeamento fiel do gate antigo. `vcs.root: "../.."` é
  obrigatório (a 2.x não sobe atrás do `.gitignore` e aborta sem ele) e
  `public/` fica fora do lint (a 2.x parseia SVG e `a11y/noSvgWithoutTitle`
  disparava no favicon). Tailwind 3 exige `css.parser.tailwindDirectives: true` e
  `noUnknownAtRules: "off"`.
- **react-router 7.18.4** no lugar do `react-router-dom` (que sai do
  `package.json`): o pacote `react-router` exige Node ≥20 e React ≥18 — casa com
  o repo. Declarative Mode (`BrowserRouter`/`Routes`/`Route`) continua suportado,
  então **não** precisa de `@react-router/dev` nem de config de Framework Mode; os
  tipos vêm embutidos (sem `@types/react-router`). A única mudança de código são
  5 imports: `react-router-dom` → `react-router`.

**Consequences**:
- O `biome migrate --write` **não** serve sozinho: ele gera
  `noConsole.allow: ["log"]` (inverte a semântica e silencia o gate de
  `console.log`) e não seta `css.parser.tailwindDirectives` (5 parse errors em
  `@apply`). O config foi escrito à mão e validado com
  `biome check --error-on-warnings`.
- O `npm audit --omit=dev --audit-level=high` (gate de produção do CI) passou a dar
  **0 vulnerabilidades**: as 2 moderate do react-router 6 sumiram. As 7 restantes
  do audit completo são todas em devDependencies (vite/esbuild/tailwindcss/
  postcss) e estão fora do gate.
- A 2.x reordena imports (assist) e o `lint:fix` passou a aplicar essa
  reordenação: 4 arquivos de código tiveram diff mecânico, zero mudança de
  comportamento.
- `noFloatingPromises`/`noMisusedPromises` são **nursery** na 2.6.3 (não vêm com
  `recommended`) e `useSortedClasses` foi renomeada (`useTailwindSortedClasses`,
  exige Tailwind 4): nenhuma entra no gate hoje.
- `useNavigate()` passa a retornar uma Promise; no código atual o retorno é
  ignorado, então nada quebra — mas quem chamar `.then()` precisa de `void`/await
  para não gerar promise flutuante.

---

## AD-023 · Vite 8 (Rolldown) e Node 24 no front

**Status**: current.

**Context**: o Vite 5 carregava o advisory GHSA-4w7w-66w2-5vf9 (path traversal em
`.map` dos deps otimizados, dev server) — **todas as 5.x são afetadas e a linha 5
não recebeu patch** (EOL): o fix existe só a partir de 6.4.2 / 7.3.2 / 8.0.5. O
Node usado no build e no CI era o 20, **EOL desde 2026-04-30**. O `npm audit
--omit=dev --audit-level=high` (gate de produção) continuava em 0, então a dívida
era de dev server e de runtime sem patch, não de bundle.

**Decision**:
- **Vite 8.3.5** (Rolldown + Oxc no lugar de Rollup + esbuild; CSS minificado por
  Lightning CSS) e **`@vitejs/plugin-react` 6.1.2** — o 4.x não aceita Vite 8 no
  peer range. O `vite.config.ts` **não mudou**: o repo não usa `define`,
  `esbuild`, `rollupOptions`, `base` nem `assetsDir`, `server.proxy` não mudou
  entre 6/7/8 e `build.target: 'es2022'` está pinado (imune à mudança de default).
  Tailwind 3 continua compatível (é plugin PostCSS, não plugin Vite).
- **Node 24** (Active LTS) no Dockerfile (`node:24-alpine`) e nos 3 jobs do CI que
  rodam Node (`frontend`, `e2e`, `schema-sync`). O Node 26 ainda é "Current" e
  entra em LTS em 2026-10-28 — não foi pinado. O Vite 8 exige
  `^20.19.0 || >=22.12.0`.

**Consequences**:
- O `npm audit --omit=dev --audit-level=high` continua em **0** e o advisory do Vite
  5 saiu do alcance; o audit completo caiu de 5 para 3 moderate, **todas em
  devDependencies** e todas na cadeia do Tailwind 3
  (`postcss-selector-parser` → `postcss-nested` → `tailwindcss`): o fix exige
  Tailwind 4 (breaking), fora do escopo.
- O build ficou mais rápido (220 → 155 módulos, 4,78 s → 916 ms) e o bundle
  encolheu (JS 336,35 → 331,14 kB; CSS 11,83 → 11,67 kB); nenhum chunk novo.
- O risco de interop CJS unificado do Vite 8 (`import QRCode from 'qrcode'` em
  `Security.tsx`) **não se materializou**: build sem warning e o `default` import
  foi validado em Chromium headless. `legacy.inconsistentCjsInterop` não é
  necessário.
- Node <20.19/22.12 deixa de servir: `apps/web/package.json` não declara
  `engines`, então o erro aparece na install, não antes.
- A versão do Node aparece em três lugares (Dockerfile + 3 jobs do CI) — sem
  fonte única dentro do repo (a tag da imagem Docker é por definição externa).

---

## AD-024 · Tailwind 4 (CSS-first) e React 19 no front

**Status**: current.

**Context**: o front estava no Tailwind 3.4.17 e no React 18.3.1. O Tailwind 3
carregava 3 advisories moderate (cadeia `postcss-selector-parser` →
`postcss-nested` → `tailwindcss`) cujo único fix é o Tailwind 4 — breaking, e
portanto nunca ia entrar num bump automático. O React 18 seguia na série
mantida, mas o 19 já era o `latest` e nenhuma dependência do repo o bloqueava.

**Decision**:
- **Tailwind 4.3.3 no modo CSS-first**: o plugin `@tailwindcss/vite` entra no
  `vite.config.ts` e `tailwind.config.js` e `postcss.config.js` são **deletados**
  (o `postcss` e o `autoprefixer` saem do `package.json`: no v4 o import e o
  prefixo de vendor são do próprio Tailwind). O tema sai do JS para o CSS
  (`@theme` com `--color-brand-*` e `--font-sans`) e as 5 classes do design
  system (`.btn`, `.btn-primary`, `.btn-ghost`, `.input`, `.card`) viram
  `@utility` — no v4 `@apply` de classe **customizada** é erro fatal, e
  `@apply` de utility core dentro de `@layer components` continua funcionando.
- **React 19.3.1** (+ `@types/react` 19.3.0, `@types/react-dom` 19.3.0). O repo
  não usa `useRef`, `forwardRef`, `React.FC`, `JSX.Element` global, `useReducer`
  com genérico nem `defaultProps` — que são os breaks reais do 19.
- Bumps menores no mesmo passo: `react-hook-form` 7.91.0, `@hookform/resolvers`
  6.0.0 (o breaking é só no resolver de Effect; usamos `zodResolver`) e
  `@playwright/test` 1.64.1.
- **react-router 8.5.0**: a barreira era o baseline do v8 (React ≥19.2.7,
  Node ≥22.22, Vite 7+, ESM-only), que só o React 19 deste mesmo lote
  destravou. Os componentes usados (`BrowserRouter`, `Routes`, `Route`,
  `Navigate`, `useNavigate`, `Link`) continuam exportados pela **raiz** do
  pacote (`react-router/dom` é outro entry), então **nenhuma linha de código
  mudou**. Os breaking do v8 (flags `future.v8_*` removidas, `meta.data`,
  `react-router-dom` derrubado, contextos `UNSAFE_` reestruturados no 8.4) não
  atingem este app.
- **Fica de fora, de propósito**: `typescript` (5.9.3). O 7.0.2 é o port nativo em
  Go e o typecheck do app passaria nele (o único ajuste seria um
  `src/vite-env.d.ts`, pelo default novo `noUncheckedSideEffectImports`), mas o
  `openapi-typescript@7.13.0` — o gerador do `schema.d.ts`, que é o gate de
  contrato do repo (AD-018) — usa a **compiler API** do TypeScript
  (`ts.factory.createKeywordTypeNode`) e declara `peerDependencies:
  {typescript: "^5.x"}`. Com o TS 7 na raiz o `npm install` morre com
  `ERESOLVE` (peer conflitante) e, com `--legacy-peer-deps`, o gerador quebra
  em runtime porque o 7.0 não expõe a compiler API (ela só chega no 7.1). Não
  existe v8 do `openapi-typescript`, então o TS 7 fica de fora até a tool
  acompanhar.

**Consequences**:
- O `npm audit` completo ficou em **0 vulnerabilidades** (as 3 moderate do
  Tailwind 3 sumiram) e o gate de produção segue em 0.
- O `react-router` foi para o **8.5.0** no mesmo lote: ESM-only, Node ≥22.22 e
  Vite 7+ já eram atendidos, o `react-router-dom` não é usado desde o v7 e
  nenhum outro breaking do v8 toca este app — **zero mudança de código**, só
  `package.json` e lockfile.
- O `typescript` continua no **5.9.3**: o 7.0.2 foi testado (o typecheck do app
  passava, com o único ajuste sendo um `src/vite-env.d.ts` para o default novo
  `noUncheckedSideEffectImports`), mas o `npm install` quebra com `ERESOLVE` por
  causa do peer `typescript: ^5.x` do `openapi-typescript`, e o gerador do
  `schema.d.ts` depende da compiler API que o 7.0 não expõe. A tentativa foi
  revertida.
- Paridade visual exige dois ajustes: no v4 `shadow-sm` é o antigo `shadow` (o
  antigo `shadow-sm` virou `shadow-xs`) e `outline-none` agora remove o outline
  de verdade (o antigo virou `outline-hidden`). Ambos foram aplicados nas
  `@utility`.
- O CSS grew (11,67 → 16,20 kB) porque o v4 emite o tema inteiro como custom
  properties; o JS cresceu com o React 19 (331,14 → 411,92 kB; gzip 106,64 →
  129,94 kB).
- O `package.json` ganhou um `overrides` mínimo
  (`{"@hookform/resolvers": {"effect": "4.0.0"}}`): sem ele o npm 11 auto-resolve
  a cadeia opcional `@typeschema/*`/`@effect/schema`, que exige Effect 3.x, e a
  install limpa quebra com `ERESOLVE`. A alternativa seria `--legacy-peer-deps`
  em toda install.
- `useTailwindSortedClasses` (Biome 2.x, nursery) **não** foi ligada: ela passa a
  entender a semântica do v4, mas gera churn de reordenação de classes nos pages.
- O Biome 2.6.3 parseia `@theme`/`@utility` sem config extra
  (`css.parser.tailwindDirectives: true` continua no `biome.json`).

---

## AD-025 · Versão do Node com fonte única (`.node-version`) e `uv` 0.13

**Status**: current.

**Context**: a versão do Node aparecia em três lugares (a tag `node:24-alpine`
do Dockerfile e o `node-version` de 3 jobs do CI) e nenhuma era a fonte de
verdade — trocar o Node exigia três edições e nenhuma detectava drift. O `mise`
existia com `node = "24"` dentro de `[tools]`, o que criava uma quarta cópia. O
`uv` pinado no Dockerfile estava na 0.10.10 com a 0.13.5 já publicada.

**Decision**:
- **`.node-version` (24) é a fonte única.** O `mise.toml` não duplica a versão:
  ele só habilita a leitura do arquivo idiomático
  (`[settings] idiomatic_version_file_enable_tools = ["node"]`) — sem isso o
  mise 2026.5.9 **ignora** o `.node-version`. O CI usa
  `node-version-file: .node-version` nos 3 jobs e o Dockerfile passa
  `NODE_VERSION` como build-arg (`ARG NODE_VERSION=24` é só o fallback).
- Um **guard de drift** no CI compara o `.node-version` com o default do `ARG`
  no Dockerfile e falha se divergirem.
- `apps/web/package.json` declara `engines.node: ">=24"` e `apps/web/.npmrc`
  liga `engine-strict=true`: um dev com Node antigo recebe `EBADENGINE` na
  install, não um stack trace no dev server.
- `uv` 0.13.5 no Dockerfile (`ghcr.io/astral-sh/uv:0.13.5`) e `uv = "0.13"` no
  `mise.toml`.

**Consequences**:
- Trocar a versão do Node é uma edição em um arquivo; mise, Docker e CI leem
  dele. O `ARG NODE_VERSION` continua no repositório (a tag da imagem Docker é
  externa por definição), mas o guard impede que ele vire uma cópia silenciosa.
- `engine-strict` derruba a install se **qualquer** dependência declarar `engines`
  incompatível — hoje nenhuma declara.
- O mise 2026.5.9 não aceita `node = "node:.node-version"` nem `"file:..."`: o
  caminho do request syntax é o prefixo `path:`, que **não** ativa o binário.
  A solução é o setting de version file idiomático.
- uv 0.13.x muda o default do Python para 3.15 quando nenhum é pedido — não
  afeta este projeto, que fixa 3.13 (`.python-version` + `requires-python`).

---

## AD-026 · Dependências do backend atualizadas (bcrypt 5, FastAPI 0.162, SQLAlchemy 2.0.55)

**Status**: current.

**Context**: o backend estava 2 a 6 versões atrás em 18 pacotes: FastAPI 0.142.2
(0.162.0 no `latest`), Pydantic 2.10.3 (2.14.1), SQLAlchemy 2.0.36 (2.0.55 na
série 2.0; 2.1.6 na série 2.1), Alembic 1.14.0 (1.20.1), bcrypt 4.2.1 (5.0.0),
uvicorn 0.32.1 (0.54.0), structlog 24.4.0 (26.1.0), sentry-sdk 2.19.2 (2.73.0),
ruff 0.8.4 (0.17.2), pytest 9.0.3 (9.1.1), bandit 1.8.6 (1.9.4). Duas
dependências estavam **declaradas e sem uso**: `python-multipart` e
`itsdangerous` (esta última é transitiva do Starlette).

**Decision**:
- Subir as 18 versões no `pyproject.toml` (pins exatos) e regerar o `uv.lock`
  (92 → 90 pacotes: 18 upgrades, 2 remoções).
- **Remover `python-multipart` e `itsdangerous`**: nenhum `Form`/`File`/
  `UploadFile` no app (a sessão é um token opaco, não um cookie assinado) e a
  `itsdangerous` chega pelo Starlette.
- **SQLAlchemy 2.0.55, não 2.1.6**: o `opentelemetry-instrumentation-sqlalchemy`
  declara `sqlalchemy<2.1.0` e os autores do instrumentor não testam a série
  2.1 — e o repo instrumenta o engine (AD-016). A série 2.0.x ainda é mantida.
- **Python continua 3.13** (EOL só em out/2029): trocar de interpreter exige
  reescrever `requires-python`, `.python-version`, `ruff.target-version`,
  `pyrightconfig.json` e **os dois** `FROM` do Dockerfile (o venv referencia o
  interpretador por symlink absoluto) — mudança própria, não um bump.
- Alembic 1.16 trocou `version_path_separator` por `path_separator`: o
  `alembic.ini` foi atualizado para não emitir warning.

**Consequences**:
- **bcrypt 5.0.0 rejeita senhas acima de 72 bytes** (`ValueError`) em vez de
  truncar em silêncio. O app faz a mesma checagem na fronteira (tipo `Password`)
  e em `_to_bytes`: senhas multibyte acima de 72 bytes são rejeitadas, nunca
  truncadas.
- O `OpenAPI` gerado ficou **byte-idêntico** (33.514 bytes antes e depois): o
  Pydantic ≥2.11 passou a emitir `additionalProperties: true` para
  `dict[str, Any]`, mas nenhum `response_model` do repo usa `dict` — todos são
  models explícitos (AD-018). `apps/web/src/api/schema.d.ts` não mudou.
- O ruff 0.17.2 promoveu `PLR0917` (too many positional arguments) de preview
  para estável: `app/jobs/tasks/email.py` (6 posicionais: `ctx` + payload da
  fila) ganhou um `per-file-ignore` explícito.
- O pyotp 2.10 tipa `provisioning_uri(**kwargs)` sem anotação → `Unknown` no
  Pyright strict: o `app/core/totp.py` ganhou um
  `# pyright: ignore[reportUnknownMemberType]` na chamada (o `cast` não resolve,
  vira `reportUnnecessaryCast`).
- `pip-audit` continua em "No known vulnerabilities found" e o `uv lock --check`
  passa. A auditoria de produção do front segue em 0.

---

## AD-027 · Jobs padronizados, rate limit por janela fixa e DLQ no Postgres

**Status**: current.

**Context**: as tasks eram registradas à mão com `@app.task(name=...)`: o nome
podia divergir da queue, não havia proteção contra rajadas de enqueue e uma
falha que esgotava o retry do Procrastinate desaparecia sem rastro. O template
já é Postgres-only (AD-001), então rate limit e DLQ devem viver no mesmo banco,
sem Redis/Celery.

**Decision**:
- `register_task(queue, action, *, max_retries=3, pass_context=True, **kwargs)`
  valida queue/action (minúsculos, sem `.` nem espaço) e registra
  `name=f"{queue}.{action}"`, `queue=queue`. `max_retries` é o número de novas
  tentativas que o Procrastinate faz (0 = uma execução só); não existe retry
  infinito, senão a falha nunca vira dead letter.
- `defer_job(task, *, rate_limit_kind=None, rate_limit_key="",
  rate_limit_limit=None, rate_limit_window_s=60, **kwargs)` chama
  `check_job_rate_limit` antes de `task.defer_async(**kwargs)` quando há
  kind+limit configurados.
- `job_rate_limits (kind, key, window_start, count)` tem PK composta
  `(kind, key, window_start)`. A janela é fixa e alinhada ao epoch:
  `to_timestamp(floor(extract(epoch from now()) / :window) * :window)`; o
  `INSERT ... ON CONFLICT DO UPDATE ... RETURNING count` conta a tentativa e
  levanta `JobRateLimitExceeded` quando `count > limit`. O handler em
  `app/main.py` transforma a exceção em **429**.
- `dead_letters (id SERIAL, kind, key, payload JSONB, error, retries,
  original_job_id, status, created_at, resolved_at)` é preenchida pelo wrapper
  de `register_task`: a falha é registrada e a exceção relançada, então o retry
  do Procrastinate continua valendo. A gravação acontece só na **última**
  tentativa — a decisão de retry é a mesma do Procrastinate
  (`RetryStrategy.get_retry_decision`), então há uma linha por job que
  desistiu, não uma por tentativa. `JobAborted` não é falha e não entra na
  DLQ. `original_job_id` vem de `ctx.job.id` quando `pass_context=True`.
- `apps/api/app/cli.py` expõe `list-dead-letters`, `retry-dead-letter <id>`
  (reenfileira o payload pela task do `kind` e marca `retried`) e
  `discard-dead-letter <id>` (marca `discarded`).
- Os modelos ORM `JobRateLimit`/`DeadLetter` espelham as tabelas para
  schema/Alembic (`app/db/models/jobs.py`).

**Consequences**:
- Nome e queue não podem divergir; o worker encontra a task pelo nome
  `<queue>.{action}`.
- Retry é sempre finito e a DLQ é o destino final do job que falhou.
- Rate limit é transacional e distribuído com o resto do Postgres (janela fixa:
  pode haver rajada na virada da janela, trade-off aceito pela simplicidade).
- Falhas ficam auditáveis e reexecutáveis pela CLI; a DLQ não substitui o retry
  do Procrastinate.
- pg_cron limpa `job_rate_limits` com mais de 1 dia a cada 15min e
  `dead_letters` não-`pending` com mais de 90 dias diariamente (quando a extensão
  está instalada; AD-033).
- Testes (`apps/api/tests/test_jobs.py`) cobrem nomes, SQL da janela, contagem
  do rate limit e o wrapper de DLQ (grava só na última tentativa, ignora
  `JobAborted`) sem tocar Postgres (AD-008).

---

## AD-028 · Configuração de produção fail-fast (SECRET_KEY, COOKIE_SECURE, WEB_ORIGIN, DB_SSLMODE)

**Status**: current.

**Context**: o `APP_ENV=prod` sozinho não garantia nada: o app subia com o `SECRET_KEY`
default (ou curto), com `COOKIE_SECURE=false` e com `WEB_ORIGIN` http. O primeiro caso
compromete a chave da qual o app deriva a cifra do segredo TOTP; o segundo faz o
cookie `Secure` não voltar no HTTPS; o terceiro quebra a validação de origem do WebAuthn.
Todas essas falhas apareceriam em runtime, no pior momento possível. Em paralelo, Postgres
gerenciado exige `sslmode` na DSN, e deixar isso para o operador editar a URL à mão é
pedir para esquecer.

**Decision**:
- `Settings._validate_prod` (`model_validator(mode="after")`) só age quando
  `app_env == "prod"` e acumula **todos** os problemas numa única `ValueError`:
  `SECRET_KEY` igual ao default ou com <32 chars, `COOKIE_SECURE=false`,
  `WEB_ORIGIN` que não começa com `https://`.
- `password_min_length` continua com o `field_validator` próprio (1..72): a política de
  senha também é validada no boot, com mensagem acionável.
- `db_sslmode` entra em `Settings`; `app/db/session.py::apply_sslmode` anexa ou
  sobrescreve `sslmode` na query string da **DSN direta**, preservando os outros
  parâmetros. O DSN do pooler (`DATABASE_URL_POOLER`) não passa por ele (AD-037).
- `.env.example` documenta o contrato de prod, o `DB_SSLMODE` e o
  `DATABASE_URL_POOLER` (vazio = sem pooler).

**Consequences**:
- Config impossível falha **alto e no boot**, com a lista completa de problemas — não é
  preciso descobrir um por vez reiniciando o container.
- `apps/api/tests/test_prod_config.py` fixa o fail-fast e o append/override do sslmode
  sem tocar Postgres (AD-008).
- O validador não substitui TLS: ele só recusa a config insegura. O HTTPS continua sendo
  responsabilidade do proxy/LB (AD-029/AD-032).
- `DB_SSLMODE` fica no código como configuração, não hardcoded — o mesmo artefato serve
  dev, staging e bancos gerenciados.

---

## AD-029 · Edge hardening: HSTS/CSP só em prod, docs desligados e favicon

**Status**: current.

**Context**: os cabeçalhos de segurança eram globais. Em dev isso é ativamente ruim: a CSP
bloqueia o inline/eval e o WebSocket do HMR do Vite, e HSTS em `http://localhost` é inútil
(ou pior, gruda o browser em https para o dev). Ao mesmo tempo, produção expunha `/api/docs`,
`/api/redoc` e `/api/openapi.json`, que entregam o mapa completo de rotas a qualquer anônimo.
O favicon era só `.ico`, mas o build do Vite copia `public/favicon.svg` para `dist/` e o
navegador pedia `/favicon.svg`.

**Decision**:
- `SecurityHeadersMiddleware` aplica sempre `X-Content-Type-Options`, `X-Frame-Options`,
  `Referrer-Policy` e `Permissions-Policy`. HSTS (`max-age=31536000; includeSubDomains`) e
  `CSP_POLICY` (estática, sem nonce) entram **somente** quando `settings.is_prod`.
- A CSP permite `style-src 'self' 'unsafe-inline'` e `connect-src 'self'`; é uma constante
  única para o app sobrescrever sem mexer no middleware.
- Em prod, `docs_url`, `redoc_url` e `openapi_url` são `None` — as três rotas não existem.
- `/favicon.ico` e `/favicon.svg` servem o arquivo que existir em `dist/` (svg primeiro),
  com `404` JSON se nenhum existir.

**Consequences**:
- Dev mantém HMR e o browser não fixa HSTS em localhost; prod ganha HSTS/CSP sem código
  condicional espalhado.
- A CSP é estática: quem precisar de nonce/hash de script troca `CSP_POLICY` e o middleware,
  não adiciona exceção por rota.
- Desligar o OpenAPI em prod não quebra o front: os tipos são gerados em dev/CI (AD-004/AD-018).
- O favicon em SVG e ICO evita 404 no browser e não depende de rota de API.

---

## AD-030 · Autorização dos endpoints de usuário e e-mail (superuser-only)

**Status**: current.

**Context**: `GET /users` listava todos os usuários para qualquer sessão autenticada e
`POST /users/enqueue-email` permitia que qualquer conta usasse o relay SMTP para mandar
e-mail arbitrário — enumeração de PII e abuso de envio. O `DELETE /users/{id}` já tinha a
regra correta (o próprio usuário ou superuser), mas os outros dois não tinham nenhuma.

**Decision**:
- `require_superuser(user: CurrentUser)` levanta `403` quando `user.is_superuser` é falso.
- `GET /users` e `POST /users/enqueue-email` declaram `Depends(require_superuser)`.
- `DELETE /users/{id}` continua permitindo o próprio usuário ou superuser.
- O enqueue mantém o rate limit por IP (`users.enqueue_email`, 5/60s) e o rate limit por
  job (`email.send`, 20/60s por usuário).

**Consequences**:
- Listar usuários e disparar e-mail deixam de ser superfície para conta comum; o `403` é
  o mesmo shape para os dois endpoints e não revela se a conta existe.
- O admin é criado pelo CLI (`python -m app.cli seed` / `create-user`), não por uma rota pública.
- A autorização é dependency declarada na rota, então o Semgrep/contrato continuam vendo
  a rota normalmente.

---

## AD-031 · Hardening de 2FA e throttle de autenticação

**Status**: current.

**Context**: quatro furos independentes no fluxo de 2FA. (1) O login por passkey buscava a
credencial só por `credential_id`: quem soubesse a senha da vítima podia responder o
challenge com a **própria** passkey e a sessão era emitida para a vítima — account takeover.
(2) `/2fa/verify` não tinha rate limit, e um espaço de 10^6 códigos TOTP com 3 válidos por
janela torna brute force viável. (3) O `pending_token` podia ser reutilizado e, no fluxo de
passkey, um pending já trocado por challenge podia gerar outro indefinidamente. (4) Login e
register só tinham limite por IP; um atacante distribuído (ou NAT compartilhado) não era
coberto por conta, e não havia trilha de auditoria nem proteção contra enumeração por tempo.

**Decision**:
- **Passkey vinculada ao usuário do pending**: lookup e update usam
  `credential_id = :cid AND user_id = :uid` (`PASSKEY_LOOKUP_SQL`/`PASSKEY_UPDATE_SQL`).
  As constantes são expostas para o teste verificar o binding sem banco.
- **Limites próprios de 2FA**: `2fa.verify` 10/60s; `2fa.passkey.login.options` e
  `2fa.passkey.login.verify` 20/60s (por IP+endpoint).
- **Pending de uso único**: `store_pending` grava no cache com TTL de 300s e
  `consume_pending` apaga no primeiro uso. O `passkey/login/options` recusa um pending que
  já carrega `challenge` — não dá para renovar o pending sem uma nova senha.
- **Throttle por conta** em auth: `enforce_account_limit` usa a chave
  `acct:<scope>:<email_normalizado>` (lowercase). Login: IP 5/60s + conta 10/300s;
  register: IP 10/60s + conta 3/h.
- **Auditoria e timing**: `auth.login`/`auth.register`/`auth.logout` registram `outcome` e
  um fingerprint sha256 truncado (12 hex) do e-mail normalizado, nunca o endereço cru.
  E-mail inexistente verifica contra `DUMMY_PASSWORD_HASH` (mesmo custo do bcrypt) e devolve
  o mesmo 401.
- `clear_session_cookie` usa `_cookie_delete_attrs()` **sem** `max_age`: o
  `Response.delete_cookie` do Starlette já manda `Max-Age=0` e passar `max_age` levanta
  `TypeError` no logout.

**Consequences**:
- A passkey de um atacante não autentica contra o pending da vítima; o binding é o que
  impede o ATO, e removê-lo é regressão de segurança, não simplificação.
- Brute force de TOTP/recovery code fica limitado por IP, e o pending não sobrevive a uma
  segunda tentativa nem ao reuso.
- Logs de auth permitem investigar tentativas sem armazenar PII; o hash dummy fecha o
  oráculo de tempo para e-mail inexistente.
- O throttle por conta usa o e-mail normalizado — o mesmo e-mail com caixa diferente cai no
  mesmo bucket.

---

## AD-032 · Proxy confiável (`FORWARDED_ALLOW_IPS`)

**Status**: current.

**Context**: o rate limit usa o IP do cliente (`request.client.host`) como parte da chave.
Atrás de um proxy, esse IP é o do proxy; o uvicorn só troca pelo `X-Forwarded-For` quando
`--proxy-headers` está ligado. Se ele confiar em qualquer origem, qualquer cliente forja o
cabeçalho e ganha um bucket novo de rate limit (e polui os logs). O default do uvicorn é
confiar só em loopback, mas isso não estava explícito no entrypoint.

**Decision**:
- O entrypoint exporta `FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-127.0.0.1}"` e sobe o
  uvicorn com `--proxy-headers`.
- O operador define os IPs/redes do proxy real (ex.: CIDR do ALB/ingress). Nunca `*`.
- `.env.example` documenta a regra e o risco.

**Consequences**:
- Com o proxy correto, o rate limit e os logs enxergam o IP real do cliente.
- Sem configurar, todas as requisições atrás do proxy parecem vir de `127.0.0.1` e
  compartilham o mesmo bucket — comportamento seguro por padrão, mas exige ajuste no deploy.
- `*` deixa de ser uma opção válida: seria um bypass de rate limit por header spoofing.

---

## AD-033 · Portabilidade do Postgres: pg_cron opcional, uuid-ossp removido, citext obrigatório

**Status**: current.

**Context**: a baseline criava `uuid-ossp` mesmo com o PG13+ oferecendo `gen_random_uuid()`
built-in, e criava/agendava `pg_cron` incondicionalmente. Em bancos gerenciados
(RDS/Aurora/Cloud SQL) a extensão pode não estar disponível ou não estar em
`shared_preload_libraries`; o `alembic upgrade head` falhava e o schema não subia por causa
de uma limpeza periódica opcional. `citext` é diferente: a coluna `users.email` depende dela,
então o schema é impossível sem a extensão.

**Decision**:
- UUIDs usam `DEFAULT gen_random_uuid()`; `uuid-ossp` sai da baseline.
- `pg_cron` é criada dentro de um bloco `DO` que só executa se `pg_available_extensions` a
  oferecer; falha de `CREATE EXTENSION` vira `NOTICE`, não erro.
- Os 6 jobs de limpeza (`kv_cache`, `rate_limits`, `job_rate_limits`, `dead_letters`,
  recovery codes, sessions) só são agendados se `pg_extension` confirmar a instalação; sem
  ela a migration imprime um aviso e segue.
- `downgrade()` só faz `cron.unschedule` quando a extensão está instalada.
- `citext` continua obrigatória (`CREATE EXTENSION IF NOT EXISTS citext` sem guarda).
- O compose usa a imagem oficial `postgres:18-alpine` (arm64 nativo). `postgis` continua no
  bootstrap apenas porque o bloco é tolerante: só cria a extensão se a imagem a tiver. A
  imagem oficial não a tem, então PostGIS/pgvector/pg_cron ficam opt-in por projeto (provider
  gerenciado ou imagem própria); a antiga `postgis/postgis` era amd64-only (emulada em arm64).
- No PG18 o `PGDATA` vive em `/var/lib/postgresql/18/docker` e o volume é
  `/var/lib/postgresql`; o mount `pgdata:/var/lib/postgresql` reflete isso (majors 16/17
  usavam `.../data`).

**Consequences**:
- O schema sobe em RDS/Aurora/Cloud SQL sem pg_cron; a limpeza periódica passa a ser
  responsabilidade do operador (job externo).
- O baseline não falha mais por uma extensão opcional; o aviso no log deixa o trade-off
  explícito.
- `citext` permanece um requisito duro do schema, documentado no `docs/postgres.md` e no AGENTS.md.
- Bancos existentes não precisam remover `uuid-ossp`; a mudança só vale para instalações novas.
- Volume de PG16/17 **não** é reutilizável no PG18 por causa do novo `PGDATA`: instalações
  existentes precisam de dump/restore (o caminho lógico do repo) e de um volume novo. O sintoma é
  o container do Postgres em loop de restart com `mkdir: can't create directory
  '/var/lib/postgresql/18/': Permission denied` — o volume antigo pertence ao uid 999 (Debian) e o
  Alpine do PG18 roda como uid 70; o cluster precisa ser recriado (dump com o client do major
  antigo, restore com o client do novo, volume novo).

---

## AD-034 · Confidencialidade e retenção de backups

**Status**: current.

**Context**: o dump contém hashes de senha, segredo TOTP cifrado e PII. O modo do arquivo
dependia do umask do host, o dump era enviado ao R2 em claro e, no modo local (sem
`RCLONE_BUCKET`), nada era podado — o diretório crescia para sempre. A retenção remota de
14 dias existia, mas a local não.

**Decision**:
- `umask 077` no início e `chmod 600` no dump, no `.sha256` e no arquivo cifrado.
- `BACKUP_ENCRYPT_PASSPHRASE` opcional cifra com
  `openssl enc -aes-256-cbc -pbkdf2 -salt` (extensão `.dump.enc`); a passphrase vai por
  **stdin** (`-pass stdin`), não em `argv` (visível no `ps`). Sem ela, o script imprime um
  aviso explícito de que hashes/TOTP/PII saem em claro.
- O `trap cleanup` remove o plaintext intermediário e os artefatos parciais em qualquer saída
  que não seja sucesso.
- A sanidade (`pg_restore --list`) roda **antes** da cifra/upload; um dump ilegível é
  removido e nada é enviado.
- `BACKUP_KEEP_COUNT` (default 14) poda os dumps locais; `BACKUP_KEEP_DAYS` (default 14)
  continua sendo a retenção remota.
- `restore.sh` exige alvo explícito e `--yes`; `test-restore.sh` prova o ciclo em bancos
  descartáveis.

**Consequences**:
- A confidencialidade em repouso é uma escolha explícita do operador; sem passphrase o
  arquivo é utilizável, mas inseguro, e o log deixa isso claro.
- Perder a passphrase significa perder o backup — ela vai no secret manager, não no
  repositório.
- O `.sha256` é calculado sobre o arquivo final (cifrado, quando há cifra), então a
  verificação cobre exatamente o que foi armazenado.
- O modo local fica com disco limitado; o remoto continua com a janela de 14 dias.

---

## AD-035 · Supply chain e entrega (actions por SHA, Dependabot, GHCR + SBOM + cosign)

**Status**: current.

**Context**: as actions do CI/deploy eram referenciadas por tag flutuante (`@v4`), que pode
ser removida e reapontada para outro commit; com pins exatos nas dependências, um CVE só
aparecia na auditoria manual; e não havia artefato reproduzível, SBOM nem assinatura da
imagem publicada. O deploy precisa construir com os mesmos build args que o CI valida e
entregar uma imagem com trilha verificável.

**Decision**:
- Todas as actions do CI e do deploy são pinadas por **commit SHA**, com a versão em
  comentário (`uses: owner/action@<sha> # vX.Y.Z`).
- Dependabot (`.github/dependabot.yml`) abre PRs semanais para `pip` (`/apps/api`), `npm`
  (`/apps/web`), `github-actions` e `docker`; as instrumentações OTel sobem num grupo só.
- O gate de produção do front no CI é `npm audit --omit=dev --audit-level=moderate`; local,
  `just web-audit` usa `--audit-level=high`.
- `.github/workflows/deploy.yml` roda em push na `main` e em tags `v*`, publica no GHCR
  (nome normalizado para minúsculo, tags `ref_name` + `sha`, labels OCI), gera SBOM SPDX
  com `syft`, assina a imagem (`cosign sign`) e anexa o SBOM (`cosign attest --type
  spdxjson`), com `persist-credentials: false` no checkout.
- Permissões mínimas: `contents: read` no workflow e, no job de publicação,
  `packages: write` + `id-token: write` (OIDC keyless). O SBOM também sobe como artifact.
- `just ci` valida o compose, que interpola `${GRAFANA_ADMIN_PASSWORD:?...}`. O gate
  materializa um `.env` a partir do `.env.example` se preciso e injeta
  `GRAFANA_ADMIN_PASSWORD=validation-only` quando a variável está ausente/vazia; a senha
  real só é exigida ao subir o stack de observability de verdade.

**Consequences**:
- Um tag retarget não muda silenciosamente o que roda no CI; atualizar action exige trocar
  o SHA (e o Dependabot faz isso em PR revisável).
- A imagem publicada é verificável: digest, SBOM SPDX e assinatura keyless, sem chave
  estática no repositório.
- O piso `moderate` no CI é mais estrito que o `high` local; a divergência é intencional
  (CI é a barreira final).
- O compose (inclusive o stack de observability) é validado no `just ci` com um valor de
  validação para `GRAFANA_ADMIN_PASSWORD`, sem exigir a senha real na invocação.

---

## AD-036 · TLS na borda e contrato de proxy confiável

**Status**: current.

**Context**: a app serve **HTTP puro** (AD-028/AD-029) e TLS é requisito de deployment, não
de código. O alvo real é uma VPS com **Coolify** na frente de Cloudflare (proxy laranja ou
Tunnel). Sem um contrato explícito de borda, cada deploy reinventa onde termina o TLS, quem
manda os headers de segurança e como o IP do cliente chega ao rate limit — e as duas
últimas decisões erram em silêncio (HSTS/CSP duplicados quebram o app; `FORWARDED_ALLOW_IPS`
errado fura o throttle). A AD-005 (a app serve o SPA), a AD-029 (HSTS/CSP em prod) e a
AD-032 (proxy confiável) continuam vigentes; esta ADR as conecta ao caminho de deploy.

**Decision**:
- **O proxy é externo ao repo**: o Coolify entrega Traefik v3.7 por padrão (trocável por
  Caddy ou "none" na UI); Cloudflare Tunnel é a alternativa de borda. O repositório não
  embute Caddy nem `cloudflared` e **a app nunca termina TLS** — nem no uvicorn, nem por
  configuração condicional.
- **O repo entrega o contrato, não o proxy**: compose com os serviços core sem profile,
  **nenhuma porta publicada no host** (o `docker-compose.yml` só tem `expose:`; as portas de
  host vêm do `docker-compose.override.yml`, mesclado automaticamente pelo `docker compose`
  sem `-f`), `FORWARDED_ALLOW_IPS` explícito e a doc operacional no
  `docs/deploy.md` ("Implantar no Coolify"). A imagem buildada é a fonte da verdade: nada de
  bind-mount de `apps/api`/`scripts` mascarando o artefato.
- O Coolify fala com o container pelo **IP na rede Docker**, não pela porta publicada no
  host — por isso o compose de deploy não precisa (e não deve) publicar `ports:`.

**Contrato de cabeçalhos e confiança**:
- O uvicorn (`--proxy-headers`) só olha `X-Forwarded-For`/`X-Forwarded-Proto` quando o peer
  TCP está em `FORWARDED_ALLOW_IPS`; peer fora da lista → ambos são **ignorados**.
- Com peer confiável, o uvicorn caminha o XFF **da direita para a esquerda** e devolve o
  primeiro host **não** confiável. Com `*` ele devolve o item **mais à esquerda** — que o
  cliente controla. Por isso **`*` é proibido** e o proxy deve sempre **ANEXAR** o IP que
  observou, nunca repassar o XFF cru do visitante.
- Os três valores válidos de `FORWARDED_ALLOW_IPS`: `127.0.0.1` (proxy no host ou
  `cloudflared` em loopback), `172.16.0.0/12` (Coolify: o Traefik tem IP **dinâmico** na
  rede do recurso, sem subnet fixa, e não existe env var oficial do Coolify com esse IP) e
  de novo `127.0.0.1` (Cloudflare Tunnel).
- O Traefik default do Coolify **apaga** os `X-Forwarded-*` do peer e **anexa** o próprio
  peer. Para preservar o IP do visitante com Cloudflare na frente, configure
  `--entrypoints.{http,https}.forwardedHeaders.trustedIPs=<ranges IP da Cloudflare>` nos
  comandos custom do proxy (o Coolify os preserva). Sem isso a app enxerga só o IP da borda
  CF ou o do proxy.
- A borda Cloudflare anexa o IP real no fim do XFF; `CF-Connecting-IP` é o header de IP real
  (o `X-Real-IP` é removido).

**Matriz de responsabilidades**:

| Camada | Responsabilidade |
|---|---|
| Proxy (Traefik/Caddy do Coolify, Cloudflare Tunnel) | terminação TLS, redirect 80→443, HTTP/2 e HTTP/3, compressão, access log |
| App (FastAPI) | HSTS, CSP, authn/authz, throttle por IP **e** por conta, validação de entrada, audit log, cookie de sessão |

- **HSTS e CSP ficam NA APP** (AD-029). O proxy **não injeta headers de segurança**: a CSP
  evolui junto com o front (o `dist/` é servido pela própria app, AD-005), e duas CSPs se
  intersectam — a mais restritiva vence e quebra o app em silêncio, sem erro no deploy.
- O throttle de borda do Cloudflare Free é grosseiro demais (1 regra, janela de 10 s, sem
  campo de conta); o **rate limit da aplicação** (AD-009/AD-031) é o principal.

**Consequences**:
- `postgres`, `pgbouncer`, `migrate`, `api` e `worker` **sem `profiles:`**: o Coolify roda
  `docker compose up -d` sem `--profile` (bug #6395) e serviço com profile não sobe.
- `api` e `postgres` **não publicam porta no host**: o `docker-compose.yml` (artefato de deploy)
  traz só `expose:`; as portas de host (`127.0.0.1:${API_PORT:-8000}:8000`,
  `127.0.0.1:${POSTGRES_PORT:-5432}:5432`) vêm do `docker-compose.override.yml`, que o
  `docker compose` auto-mescla **sem `-f`** — o Coolify e o CI passam `-f docker-compose.yml` e o
  ignoram. A internet fala com o proxy, nunca com o container.
- O compose não monta o código-fonte sobre a imagem: a imagem é o artefato que o CI mediu e
  assinou (AD-035), e bind-mount a mascararia.
- Deploy e borda documentados em `docs/deploy.md`, não espalhados em comentários de compose.

**Limites conhecidos**:
- Cloudflare corta corpo em **100 MB** (413) e leitura em **125 s** (524): upload grande vai
  por **URL pré-assinada do R2** e resposta HTTP rápida (job assíncrono).
- O IP do proxy do Coolify é dinâmico → CIDR, nunca IP exato.
- O Traefik default apaga o XFF do peer → sem `trustedIPs` o IP real se perde com Cloudflare.
- ACME HTTP-01 **não completa** atrás de Cloudflare nem de tunnel → DNS challenge ou Origin
  Certificate.
- Rate limit de borda do plano Free não substitui o da app.
- Compose na Coolify v4.5.0 **não tem rolling update nem réplicas** (`scale`/`replicas` > 1 é
  rejeitado) → cada deploy recria os containers, com downtime curto. A Coolify também marca o
  deploy **FINISHED antes do healthcheck** (não espera `--wait`) e renomeia `container_name`,
  projeto e volumes.
- O status agregado do resource compose pode aparecer como `running:unknown`: é **cosmético** —
  a UI da Coolify não parseia o health do compose (`exclude_from_hc` é o padrão do serviço
  one-shot de migration). A saúde real se vê por serviço (Runtime Logs/Terminal), por
  `/api/v1/healthz` (com `db=true`) e por `docker compose ps`.
- Backup do banco: o Postgres do compose **não** é database resource da Coolify; o backup nativo
  é o **backup de volume** do `pgdata` (`tar.gz` do data dir) → S3/R2, com `stop_during_backup`
  (a Coolify para os containers durante a cópia) para consistência. O restore é **file-level**
  (exige o mesmo major do Postgres e o banco parado), então o caminho de restore confiável é o
  dump **lógico** do repo (`scripts/backup.sh`, `just test-restore` e job `restore-test` do CI).
  Sem PITR em nenhum caminho: restaurar devolve a última cópia — PITR exige outro provider, que o
  repo não oferece. Como o compose de deploy não publica a porta 5432, o dump lógico em prod roda
  no **modo container** (`POSTGRES_CONTAINER=postgres`): `pg_dump`/`pg_restore` por
  `docker compose exec -T`, com o dump (e a cifra/rclone) no host.
- `FORWARDED_ALLOW_IPS` errado falha **em silêncio**: o rate limit passa a compartilhar um
  bucket (ou a aceitar IP forjado), sem erro no boot.

**Rejeitados**:
- **Monólito em compose único no Coolify com múltiplos satélites**: acoplar Postgres, aplicação e serviços de terceiros (GrowthBook, Mautic, Listmonk, OpenObserver) num único Compose faz com que qualquer rebuild da app reinicie ou ameace o banco e os satélites. Para ecossistemas com múltiplos serviços, o modelo adotado é o **Deploy Modular** (`docs/deploy.md`): PostgreSQL como resource Database nativo (com backups S3/R2 nativos), API e Worker como Applications via Dockerfile e migrations manuais sob demanda. O `docker-compose.yml` da raiz fica mantido para o desenvolvimento local e validação em CI.
- Caddy ou `cloudflared` **dentro** do compose: o proxy é do Coolify ou da Cloudflare; o repo
  não mantém um segundo caminho de borda.
- TLS no uvicorn: a app não tem nem deve ter material de chave; o certificado é do proxy.
- `FORWARDED_ALLOW_IPS="*"`: bypass de rate limit por header spoofing.
- Servir o `dist/` pelo proxy: a app continua servindo o SPA e a API na mesma origem (AD-005).

**Checklist de validação (quando o Docker puder rodar)**:
1. `docker compose build` e `docker compose up -d` com `GRAFANA_ADMIN_PASSWORD` definida.
2. `docker compose run --rm migrate` (ou o one-shot do compose) contra um Postgres real:
   `alembic upgrade head` aplica sem erro.
3. `just e2e-full` contra a SPA servida pela API em `:8000`.
4. TLS real: `https://` responde com certificado válido e `http://` redireciona para https.
5. IP real refletido: com `FORWARDED_ALLOW_IPS` correto, `request.client.host` (log/rate
   limit) mostra o IP do visitante, não o da borda; com o valor errado, muda para o da borda.
6. Cookie de sessão sai com `Secure` e volta no navegador sob HTTPS.

---

## AD-037 · Pooler de conexão (pgbouncer) só para a API

**Status**: current.

**Context**: cada request da API abre uma session no pool do SQLAlchemy, mas o
Postgres impõe um teto de conexões e cada conexão custa memória. Sob concorrência
HTTP, o pool da aplicação sozinho não evita que picos multipliquem conexões por
processo/container. Um pooler na frente do Postgres multiplexa clientes em poucas
conexões de servidor. A escolha precisa conviver com as restrições do
Procrastinate (conexão persistente + `LISTEN/NOTIFY` + `FOR UPDATE SKIP LOCKED`) e
do Alembic (DDL), que **não** funcionam em `pool_mode=transaction`.

**Decision**:
- O serviço `pgbouncer` entra no compose **sem `profiles:`** (o Coolify roda
  `docker compose up -d` sem `--profile`, bug #6395), depois do `postgres` e antes do
  `migrate`, com `restart: unless-stopped` e `depends_on: postgres` saudável. Fica
  **sempre no ar**, mas **sem efeito** enquanto `DATABASE_URL_POOLER` não for
  definido: sem a env a API fala direto no Postgres (o comportamento anterior).
- **Só a API usa o pooler.** `migrate` (Alembic, DDL) e `worker` (Procrastinate)
  continuam com o DSN direto `POSTGRES_*`/`DATABASE_URL`. A API também abre o
  Procrastinate (`defer_async` em `app/main.py`), e por isso o `POSTGRES_HOST`
  dela continua `postgres`: a `database_url_procrastinate` deriva de `POSTGRES_*`.
- A imagem é **`edoburu/pgbouncer:v1.26.1-p0`** (pinada; nunca `latest`). O
  upstream precisa ser **>= 1.26.1** por causa do **CVE-2026-107350** (bypass de
  autenticação SCRAM em <= 1.25.x). `bitnami/pgbouncer` está descontinuada e não é
  usada. A imagem gera `pgbouncer.ini`/`userlist.txt` no boot a partir das envs
  `DB_*`, `LISTEN_PORT`, `AUTH_TYPE`, `POOL_MODE` etc.
- Porta interna **6432** (`LISTEN_PORT`), não publicada no host; o console admin
  (`ADMIN_USERS`) não é exposto. Credenciais são as mesmas do Postgres (`DB_USER`,
  `DB_PASSWORD`, `DB_HOST=postgres`, `DB_NAME`): o `auth_file` aceita a senha em
  texto e o pgbouncer faz o SCRAM com o servidor. Nenhum segredo novo.
- Knobs para 4 vCPU/8 GB: `pool_mode=transaction`, `max_client_conn=1000`,
  `default_pool_size=25`, `min_pool_size=2`, `reserve_pool_size=5`,
  `reserve_pool_timeout=5`, `max_db_connections=50` (folga para worker/migrate
  diretos), `query_wait_timeout=60`, `ignore_startup_parameters=extra_float_digits`,
  `auth_type=scram-sha-256` e `max_prepared_statements=200` **explícito** — é o que
  faz o asyncpg funcionar em transaction mode; não dependa do default da imagem.
- O DSN separado é `Settings.database_url_pooler` (env `DATABASE_URL_POOLER`) e a
  propriedade `Settings.database_url_api` devolve o pooler quando setado, senão o
  DSN direto. O engine da API usa `database_url_api`; `migrations/env.py` e
  `jobs/app.py` continuam em `database_url_async`/`database_url_procrastinate`.
- **TLS**: `apply_sslmode` só atua na DSN **direta**. Aplicar `DB_SSLMODE=require`
  ao DSN do pooler faria o asyncpg abrir TLS contra um pgbouncer local sem
  certificado e falhar no boot. Se o operador colocar `sslmode` no próprio
  `DATABASE_URL_POOLER`, ele é respeitado (o DSN é dele) — não é injetado.
- O CI prova os dois caminhos: readiness/migration/worker no caminho direto e
  `psql -h pgbouncer -p 6432` (rodando no container `postgres`) no caminho do pooler,
  além de `pgbouncer` em `docker compose ps --services --filter status=running`. A
  imagem do pgbouncer também entra no scan do Trivy.

**Incompatibilidades do `pool_mode=transaction`** (o motivo do bypass):
- `LISTEN`/`NOTIFY` dependem da conexão de sessão: a notificação nunca chega ao
  cliente. É o mecanismo que o worker do Procrastinate usa para acordar — daí o
  worker ficar direto.
- Estado de sessão (`SET`, `search_path`, `application_name`, parâmetros de sessão)
  vaza ou se perde entre transações; advisory locks de escopo de sessão e `WITH
  HOLD` cursors também não sobrevivem. Use só o que for transacional.
- Prepared statements: o suporte a named prepared statements existe desde o
  pgbouncer 1.24 e é habilitado por `max_prepared_statements`; com ele explícito o
  asyncpg funciona. Sem ele (ou com pgbouncer antigo) o asyncpg falha em transaction
  mode.
- DDL do Alembic não deve ir pelo pooler por causa de locks/estado de sessão e do
  contrato de migration aplicada no banco real.

**Consequences**:
- A API ganha um hop a mais; worker e migrate seguem diretos e continuam
  consumindo conexões do Postgres (o `max_db_connections=50` deixa folga).
- Sem `DATABASE_URL_POOLER`, nada muda no runtime, mas a API passa a depender do
  healthcheck do `pgbouncer` para subir. É o custo de ter o serviço sempre no ar
  no Coolify.
- Dois caminhos de conexão para diagnosticar: `just psql` (admin/sessão, direto) e
  `just psql-pooler` (rota da aplicação). `test-restore` e migrations permanecem
  diretos.
- O pgbouncer local não usa TLS upstream (Postgres do compose); num Postgres
  gerenciado o pooler precisa de `SERVER_TLS_SSLMODE`/CA, não de `DB_SSLMODE` no
  DSN do cliente.

---

## AD-038 · Auditoria: o que é default e o que é opt-in

**Status**: current.

**Context**: o template registra `created_at`/`updated_at` (default do banco; o
trigger `set_updated_at()` existe só em `users`) e o audit de auth como log
estruturado. Surge a pergunta se toda tabela deve ganhar `created_by`/`updated_by`.
Os frameworks mainstream não fazem isso: Django usa `auto_now_add`/`auto_now` +
`admin.LogEntry` (só ações do admin, `user` com `CASCADE` apaga a trilha) e o
`django-auditlog` (delta JSON + actor + action); Rails tem PaperTrail (`versions` +
`whodunnit` = `current_user.id`, e `lock_version`); Laravel usa
`spatie/laravel-activitylog` (`activity_log` com `causer`/`subject`/`properties`);
Prisma documenta `@updatedAt` + exemplo de trigger com GUC; Supabase usa RLS com
`auth.uid()` + audit logs da plataforma. O padrão é: timestamps automáticos + **uma**
tabela de log opt-in, com o ator vindo do contexto do request.

**Decision**:
- O default **não muda**: `created_at`/`updated_at` do banco e o audit de auth como
  log estruturado (`outcome`, fingerprint sha256 do e-mail, `trace_id`). Esse é o
  tier que OWASP/ASVS pedem, mas é observabilidade, não trilha consultável: não tem
  IP/UA, não é transacional e não sobrevive ao delete do usuário.
- O template **não** adiciona `created_by`/`updated_by`. Nas tabelas atuais o ator é
  o próprio usuário (register, 2FA, passkey) → coluna tautológica; `DELETE
  /users/{id}` é hard delete com `CASCADE` → a autoria morre junto (e `RESTRICT`
  quebraria o delete); nas tabelas de sistema (`kv_cache`, `rate_limits`,
  `job_rate_limits`, `dead_letters`) não há ator humano. A coluna só se justifica em
  tabelas de negócio futuras (multi-tenant, admin editando recurso de outro).
- Auditoria real é **opt-in por projeto e por tabela auditada**: tabela append-only
  (`<tabela>_audit`), trigger `AFTER INSERT OR UPDATE OR DELETE` gravando o delta em
  `JSONB`, e o ator via `set_config('app.actor_id', …, true)` na **mesma transação**
  do DML. A transação explícita é obrigatória porque o `SET LOCAL` morre no fim dela;
  com pgbouncer em `pool_mode=transaction` isso é seguro por construção (AD-037). A
  receita completa (DDL, trigger, privilégios, LGPD/retenção) está em `docs/auth.md`,
  "Auditoria (receita opt-in)".
- O ator vem da **sessão validada no servidor** (cookie opaco), nunca do body da
  request — aceitar `updated_by` do cliente é forjar a trilha.
- A role da aplicação recebe `INSERT`/`SELECT` na tabela de auditoria, mas
  **nenhum `UPDATE`/`DELETE`** (`REVOKE`): append-only para a app.
- LGPD: gravar o **delta**, não before/after completo; omitir colunas sensíveis;
  identificar o titular por hash/fingerprint; retenção menor que a dos dados de
  negócio e documentada.
- O trigger não audita `SELECT` nem DDL — só as três operações de linha.

**Consequences**:
- Nenhuma migration, campo ou mudança de contrato OpenAPI (`UserOut` fica como
  está). O default continua barato.
- Um projeto com requisito de compliance adiciona a receita só nas tabelas que
  importam: cada tabela auditada ganha um trigger, uma tabela de log e o custo de
  escrita correspondente.
- O audit de auth continua sendo log de observabilidade; não o trate como prova de
  conformidade. A trilha consultável é a tabela opt-in.

**Limites**:
- Append-only por privilégio **não é WORM**: owner/superuser do banco ainda pode
  reescrever a tabela. Armazenamento imutável externo não faz parte do template.
- O trigger roda como invoker; se a role da app não tiver `INSERT` na tabela de
  auditoria, a DML na tabela auditada falha.
- Cobertura é DML de linha; `SELECT` e DDL não entram na trilha.
- A receita é por tabela: não existe auditoria automática de tabelas futuras.

**Rejeitados**:
- **CDC (`pgoutput` + Debezium) e `pgAudit`**: um segundo sistema para operar, PII no
  log de sessão e, no CDC, `wal_level=logical`. Custo desproporcional ao template.
- **Soft delete global**: não é auditoria; exige `WHERE deleted_at IS NULL` em toda
  query, índice parcial e `UNIQUE NULLS NOT DISTINCT` para não quebrar constraints.
- **Outbox/event log próprio**: o Procrastinate já é a fila; outbox resolve dual
  write, não requisito de compliance.
- **`updated_by` universal**: mesma tautologia do `created_by` nas tabelas atuais.
- **System versioning / temporal tables do PG18**: **não existem**. O PG18 trouxe
  apenas *temporal constraints* (`WITHOUT OVERLAPS`/`PERIOD`); o patch do Commitfest
  2316 foi devolvido em 2021 e as extensões third-party estão paradas.
- **Expor `xmin`**: é token interno do PG, sofre freeze/wraparound; lock otimista em
  Core usa uma coluna `version` explícita.

---

## Future ADs (open questions)

- _(none open — all shipped infra is documented above; product-level decisions like
  2FA enforcement policy and OAuth/SSO live in the project README roadmap)_
## AD-031 · Autorização ABAC centralizada (`app/core/authz.py`)

**Contexto.** As regras estavam espalhadas (`require_superuser` em `users.py`, `if` inline no delete).

**Decisão.** Modelo ABAC com uma única fonte de verdade: `Action` (enum), `POLICIES` (funções puras sobre atributos do usuário e do recurso), `can/authorize/require` e `scope_for`, que traduz a política em filtro SQL parametrizado para leituras em lote. Permissões ficam no código (estáticas, revisáveis em PR); deny-by-default; usuário inativo nega tudo.

**Fora de escopo (por ora).** ReBAC (hierarquias) e permissões editáveis em banco/UI.
