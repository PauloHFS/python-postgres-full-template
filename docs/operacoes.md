# Operações: observability, email e CI

Traces e métricas (OTLP/Grafana), e-mail transacional por SMTP e o que o CI e o
deploy entregam.

## Observability (opcional, vendor-agnostic)

App emite **OTLP** quando `OTEL_EXPORTER_OTLP_ENDPOINT` está setado. Sem env, é no-op.

```bash
# 1) Sobe a stack + Grafana/Tempo/Prometheus/OTel Collector
just up-obs

# 2) Configure .env:
#    OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
#    OTEL_TRACES_SAMPLER=parentbased_traceidratio
#    OTEL_SAMPLE_RATIO=1.0
#    SENTRY_DSN=https://...@sentry.io/...

# 3) Restart api/worker
just down && just up
```

Amostragem: `OTEL_TRACES_SAMPLER` aceita `always_on`, `always_off`, `traceidratio`
ou `parentbased_traceidratio` (default); o ratio vem de `OTEL_SAMPLE_RATIO`
(alias `OTEL_TRACES_SAMPLER_ARG`), default `1.0`.

Cada resposta traz `X-Trace-Id` quando há span; logs incluem `trace_id`/`span_id`
e o worker continua o trace do job automaticamente via `defer_job` e `register_task`
(ex.: request → `email.enqueue` → `email.send`), além de instrumentar cada query SQL.

Métricas OTLP emitidas pela API e pelo worker (expostas no Prometheus via Collector):
- **Rotas HTTP** (`FastAPIInstrumentor`): latência (`http.server.duration` para p95/p99 por rota) e taxa de erro por status HTTP.
- **Execução de jobs** (`lean.jobs`): `jobs.execution.duration` (histograma em segundos) e `jobs.outcomes.total` (`status=success|retry|dead_letter|aborted`).
- **Filas e DLQ no Postgres** (`lean.jobs.queue`, coletadas pelo worker a cada 15s): `jobs.queue.size` (`todo`/`doing` por fila), `jobs.queue.oldest_age` (idade em segundos do job `todo` mais antigo) e `jobs.dead_letters.size` (`pending`/`retried`/`discarded`).

URLs:
- Grafana: <http://localhost:3000> (user `admin`; senha = `GRAFANA_ADMIN_PASSWORD`)
- Tempo API: <http://localhost:3200>
- Prometheus: <http://localhost:9090>

> 💡 **Em Produção com OpenObserver**: Se você optar por rodar o OpenObserver no Coolify como serviço satélite (all-in-one para logs, traces e métricas), não precisa subir Tempo/Prometheus/Grafana. Apenas configure `OTEL_EXPORTER_OTLP_ENDPOINT=http://openobserver:5080/api/default` na aplicação e acesse a UI do OpenObserver diretamente.

As portas do stack de observability (Grafana 3000, Tempo 3200, Prometheus 9090,
OTel Collector 4317/4318) e do Mailpit (1025/8025) publicam **só em loopback**
(`127.0.0.1`) — igual no dev local, e na VPS nada fica exposto na internet.

Credenciais default do compose (Postgres `app`/`app`) são só para dev local.
`GRAFANA_ADMIN_PASSWORD` precisa de um valor sempre que o Compose for interpolado: o compose usa `${GRAFANA_ADMIN_PASSWORD:?...}` e a interpolação é global — `docker compose up` falha sem ela mesmo sem o profile `observability` (o Grafana 11.3.1 cairia em `admin`/`admin`). Qualquer valor satisfaz o `config`; o valor real só importa quando o stack de observability sobe. `just validate-compose` (e o job do CI) injeta um valor de validação quando a variável está ausente, então não é preciso passá-la na invocação.

Sem Sentry self-hosted (pesado demais). Se precisar de error tracking, use Sentry SaaS via `SENTRY_DSN`.

## Email transacional

**SMTP-only.** Um caminho de código, qualquer provedor.

### Dev local (Mailpit)

```bash
docker compose --profile dev up -d   # Postgres + migrate + api + worker + mailpit
just mail                                          # abre http://localhost:8025
```

Quando o worker processar `email.send`, o email aparece na UI do Mailpit. Zero credenciais, zero custo.

### Prod — qualquer provedor SMTP

| Provider | Host | Porta | TLS |
|---|---|---|---|
| Resend | `smtp.resend.com` | 465 | `SMTP_SSL=true` |
| AWS SES | `email-smtp.<region>.amazonaws.com` | 587 | `SMTP_STARTTLS=true` |
| Postmark | `smtp.postmarkapp.com` | 587 | `SMTP_STARTTLS=true` |
| Mailgun | `smtp.mailgun.org` | 587 | `SMTP_STARTTLS=true` |
| Brevo | `smtp-relay.brevo.com` | 587 | `SMTP_STARTTLS=true` |

Exemplo (Resend):
```bash
SMTP_HOST=smtp.resend.com
SMTP_PORT=465
SMTP_SSL=true
SMTP_USERNAME=resend
SMTP_PASSWORD=${RESEND_API_KEY}
EMAIL_FROM="App <noreply@example.com>"
```

Exemplo (SES):
```bash
SMTP_HOST=email-smtp.us-east-1.amazonaws.com
SMTP_PORT=587
SMTP_STARTTLS=true
SMTP_USERNAME=AKIAxxxxxx
SMTP_PASSWORD=<smtp-password>
EMAIL_FROM="App <noreply@example.com>"
```

### Staging/CI sem envio real

```bash
EMAIL_LOG_ONLY=true   # só loga, não envia
```

## CI e entrega

`../.github/workflows/ci.yml`:
- **backend**: ruff + pyright + bandit + pytest
- **frontend**: tsc + lint (Biome) + build + `npm audit --omit=dev --audit-level=moderate`
- **sast**: semgrep com as regras locais de `../semgrep.yml`
- **dependency-audit**: pip-audit (via `uvx`) nas deps de produção do backend
- **e2e**: Playwright nos cenários mockados (`page.route`), sem API/DB
- **schema-sync**: regenera `schema.d.ts` e falha se estiver desatualizado
- **compose**: valida que `../docker-compose.yml` continua válido
- **docker**: build da imagem + medição do tamanho real (teto de 700 MB) + **boot do stack** (readiness `db=true`, `alembic_version`, worker vivo) + Trivy (CRITICAL/HIGH bloqueita)

`../.github/workflows/restore-test.yml` roda backup → restore → verificação num Postgres efêmero.

`../.github/workflows/deploy.yml` publica a imagem no GHCR em tags `v*` (ou sob demanda via `workflow_dispatch`), gera SBOM SPDX com syft e assina imagem + SBOM com cosign keyless (OIDC). As actions do CI e do deploy são pinadas por **commit SHA**; o Dependabot abre PRs semanais para pip, npm, GitHub Actions e Docker.

Local: `just ci` roda os mesmos checks (backend, frontend, semgrep, auditorias, E2E mock, schema e compose). O `validate-compose` materializa um `.env` a partir do `../.env.example` se não houver um e usa `GRAFANA_ADMIN_PASSWORD=validation-only` quando a variável está ausente/vazia — não é preciso passá-la na invocação. O build da imagem, a medição do tamanho, o Trivy e a prova de restauração só rodam no CI (exigem daemon).

Guia extraído do README. Índice e atalhos: [README](../README.md).
