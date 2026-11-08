# AGENTS.md — Governança de IA para o Lean Monorepo

## 1. Escopo e fontes de verdade

Este arquivo se aplica à raiz do repositório e a todos os diretórios abaixo dela.

- `README.md`: setup, comandos e visão geral (índice dos guias).
- `docs/`: guias de fundo — `deploy.md` (Coolify/produção/borda), `backup.md`, `postgres.md`, `auth.md`, `operacoes.md`, `tuning.md`.
- `docs/architecture.md`: decisões arquiteturais vigentes (ADRs).
- Manifests e configurações: versões e comandos efetivamente instalados.
- Em caso de conflito, a ADR vigente e o código executável prevalecem. Atualize a documentação correspondente junto com a mudança; não contorne a arquitetura existente.

## 2. Arquitetura

- **Backend (`/apps/api`)**: Python **3.13** gerenciado por `uv`, FastAPI, Pydantic v2, SQLAlchemy 2.0 assíncrono/Core, Alembic, Procrastinate, pgbouncer (pooler opcional da API) e PostgreSQL.
- **Frontend (`/apps/web`)**: TypeScript strict, React 19, Vite 8 (Rolldown/Oxc), Tailwind CSS 4, Biome e tipos gerados do OpenAPI. Node 24 é o piso do front (o Node 20 está EOL e o Vite 8 exige `^20.19 || >=22.12`); a versão mora em `.node-version`.
- **Princípio arquitetural**: PostgreSQL é a única fonte de estado do backend. Cache, filas, sessões e rate limiting pertencem ao PostgreSQL; não introduza Redis, Celery ou outro serviço sem uma nova ADR.
- **Princípio de interface**: toda rota nova possui contrato Pydantic de entrada e saída; o frontend consome os tipos gerados pelo OpenAPI.

## 3. Regras estritas de geração de código

### Dependências

- Antes de adicionar um pacote npm ou PyPI, confirme no registro oficial a existência, a versão, a procedência, a licença e a manutenção do projeto.
- Prefira a biblioteca padrão, uma dependência já instalada ou um helper local. Não crie abstração ou dependência especulativa.
- O gerenciador de pacotes Python é o `uv`: dependências ficam em `apps/api/pyproject.toml` (pins exatos) e as versões resolvidas em `apps/api/uv.lock`. Não reintroduza `pip install`, `requirements.txt` nem venv manual.
- Use `uv sync` para preparar `apps/api/.venv` e `uv run <comando>` para executar ferramentas e scripts. Em CI use `uv sync --locked`; rode `uv lock` e versione o `uv.lock` junto de qualquer mudança no `pyproject.toml`.
- As versões das ferramentas de dev estão em `mise.toml` (just, uv): `mise install` prepara o ambiente. A versão do Node tem fonte única em `.node-version` — o `mise.toml` não a duplica (só habilita a leitura do arquivo), o CI usa `node-version-file` e o Docker passa `NODE_VERSION` como build-arg. O Python é do `uv` — não declare `python` no mise, e as tasks continuam no `justfile`.
- Mantenha o lockfile `apps/web/package-lock.json` atualizado ao mexer em dependências npm.
- Rode a auditoria após mudar dependências: `just api-audit` (pip-audit via `uvx`, dependências de produção do backend) e `just web-audit` (npm audit de produção).

### Tipagem e contratos

- O backend roda Pyright em `strict` (`apps/api/pyrightconfig.json`, sem lista de `ignore`). Todo código novo passa em `uv run pyright app tests` com 0 erros; não reintroduza exclusões de arquivo nem `# type: ignore` sem motivo explícito.
- `AsyncSession.execute` é corrotina: isole o `await` (`(await session.execute(...)).mappings().first()`) antes de encadear `.mappings()`/`.first()`/`.scalar*()`. Encadear no `await` quebra em runtime.
- DML precisa de `cast(CursorResult[Any], await session.execute(...))` para ler `.rowcount`; libs sem stubs (instrumentação OTel) se normalizam na fronteira com `cast`, sem espalhar `Unknown` pelo app. A lib de passkey é `webauthn==3.0.1` (tipada, `py.typed`): importe os tipos direto, sem `cast`.
- TypeScript deve permanecer em modo strict e não usar `any`; prefira `unknown` com narrowing. O aviso do Biome sobre `any` não substitui essa regra.
- Toda rota nova deve declarar modelos Pydantic de entrada, `response_model` e tipos explícitos para respostas.
- O `response_model` é um **modelo Pydantic explícito**: `dict[str, Any]` vira `Record<string, never>` no TypeScript e apaga o contrato do front. Na fronteira use `model_validate` (entrada) e `model_dump(exclude_none=True)` (saída).
- Quando o shape da resposta não pode ganhar campos nulos ou listas vazias, use `response_model_exclude_unset=True`.
- `apps/web/src/api/schema.d.ts` é gerado. Nunca o edite manualmente; após mudar schemas ou rotas, rode `just web-types` e versione o resultado.
- Não duplique manualmente contratos que já existem em `src/api/types.ts` ou `src/api/endpoints.ts`. Use Zod para validação de formulários no frontend, não como substituto dos tipos da API.
- No front, os tipos vêm de `components["schemas"][...]` (reexportados em `src/api/types.ts`). Não redeclare shapes de resposta à mão.
- `scripts/check-schema-sync.sh` é o gate de contrato: compara o `schema.d.ts` versionado com o OpenAPI atual e falha se alguma **resposta de rota** virar `Record<string, never>`/`unknown`. Rode `just web-types && just check-schema` depois de mexer em rotas ou schemas.
- WebAuthn: as options chegam em base64url; converta para `BufferSource` com `src/lib/webauthn.ts` (`toRegistrationOptions`/`toAuthenticationOptions`) antes de `navigator.credentials`.

### Validação de entrada (schemas)

- Todo schema de entrada declara suas constraints no Pydantic v2 (`Field(min_length=..., max_length=..., pattern=...)`); paginação usa `Query(ge=..., le=...)`. A fronteira é o backend — o Zod do front é conveniência, não garantia.
- `RegisterIn.password` e `LoginIn.password` usam o tipo `Password` de `endpoints/auth.py`: `min_length` vem de `settings.password_min_length`, `max_length=72` e um `AfterValidator` rejeita senhas com mais de 72 **bytes UTF-8**. Não há exceção para login legado: o bcrypt 5.x rejeita >72 bytes, então aceitar mais só adiaria o erro para o hash.
- `TwoFactorVerifyIn.code` aceita TOTP (6 dígitos) **ou** recovery code (10 chars): não aplique pattern de 6 dígitos ali. O `^\d{6}$` é só do `TotpConfirmIn` (confirmação de setup).
- Entrada inválida responde **422**, não 401. Constraint não aparece no `schema.d.ts` (o openapi-typescript descarta `minLength`/`maxLength`/`pattern` de campo escalar): ela vale no servidor, e o `check-schema` continua sendo o gate de contrato.

### Lint do frontend (Biome)

- O lint do front é o Biome (`apps/web/biome.json`) e roda como `npm run lint` = `biome check --error-on-warnings`: warning bloqueia igual a erro.
- As regras que importam estão em `error`: `noExplicitAny`, `noConsoleLog`, `useImportType`, `useExhaustiveDependencies`, `useAwait`, `noUnusedImports`, `noUnusedVariables`, `noUnusedFunctionParameters`, `noUndeclaredVariables`, `noNonNullAssertion`, `noParameterAssign`, `noUselessElse`.
- `useNamingConvention` fica `off`: o snake_case vem do contrato da API, não do estilo do código.
- O Biome está na **2.x** (`@biomejs/biome@^2.6.3`): chaves `files.includes` (negação `!`), `linter.rules.preset: "recommended"` (a `recommended: true` é deprecated e, na 2.x, erro de config **aborta** o run) e `assist.actions.source.organizeImports: "on"`.
- `noConsoleLog` **não existe** na 2.x: a regra é `noConsole` e ela bloqueia **todo** `console.*` por padrão. O config lista em `options.allow` todos os métodos **exceto** `log` — o mapeamento fiel do `noConsoleLog`. Se alguém inverter essa lista, o gate de `console.log` some em silêncio.
- `vcs.root: "../.."` é obrigatório: a 2.x não sobe a árvore atrás do `.gitignore` e aborta com "couldn't find an ignore file" sem isso.
- `files.includes` = `["**", "!**/src/api/schema.d.ts", "!**/public"]`: o schema gerado fica fora e o `public/` também (a 2.x passou a parsear SVG e `a11y/noSvgWithoutTitle` disparava no `favicon.svg`).
- Tailwind 4 exige `css.parser.tailwindDirectives: true` (para `@theme`/`@utility`/`@apply`) e `noUnknownAtRules: "off"`.
- `noFloatingPromises`/`noMisusedPromises` são **nursery** na 2.6.3: não vêm com `recommended` e não estão ligadas.
- `src/api/schema.d.ts` é gerado e fica fora do lint.
- Rode `just web-lint` antes de submeter; `just web-lint-fix` aplica as correções automáticas (as *unsafe* de `noUnusedVariables`/`noUnusedImports` precisam de `--unsafe` e não são aplicadas).

### Toolchain do front

- A versão do Node tem **fonte única** em `.node-version` (24). O `mise.toml` só habilita a leitura do arquivo (`[settings] idiomatic_version_file_enable_tools = ["node"]` — sem isso o mise ignora o `.node-version`); o CI usa `node-version-file` e o Docker passa `NODE_VERSION` como build-arg. Um guard no CI falha se o default do `ARG` divergir do arquivo. Não reescreva a versão em outro lugar.
- `apps/web/package.json` declara `engines.node: ">=24"` e `apps/web/.npmrc` liga `engine-strict=true`: dev com Node antigo recebe `EBADENGINE` na install, não stack trace no dev server.
- **Tailwind 4 é CSS-first**: não existe `tailwind.config.js` nem `postcss.config.js` (o plugin é `@tailwindcss/vite` no `vite.config.ts`). O tema vive em `@theme` no `src/index.css` (`--color-brand-*`, `--font-sans`) e as classes do design system em `@utility` — no v4, `@apply` de classe customizada é erro fatal.
- No v4, `shadow-sm` é o antigo `shadow` (use `shadow-xs` para o antigo `shadow-sm`) e `outline-none` remove o outline de verdade (use `outline-hidden`).
- O `overrides` de `@hookform/resolvers` (`effect: 4.0.0`) existe porque o npm 11 auto-resolve a cadeia opcional `@typeschema/*`/`@effect/schema` (que exige Effect 3.x) e quebra a install limpa com `ERESOLVE`. Não remova sem testar `npm ci` do zero.
- **TypeScript fica no 5.9.3**: o 7.0.2 (port nativo em Go) não expõe a compiler API, e o `openapi-typescript` — que gera o `schema.d.ts` (gate de contrato) — usa essa API e declara `peer typescript: ^5.x`; com o 7 na raiz o `npm install` quebra com `ERESOLVE`. Só suba quando o `openapi-typescript` acompanhar.

### Banco de dados

- Em runtime, use SQLAlchemy Core com `text()` e bind parameters, conforme a AD-012. Não use `session.query(Model)` nem queries ORM nas rotas.
- Nunca concatene entrada do usuário em SQL. Isso evita SQL Injection mesmo nos pontos de acesso SQL explícito.
- Modelos ORM (`Mapped[]`) servem ao schema e ao Alembic; queries de runtime servem ao Core.
- Gere migrations com `just api-revision`, revise o resultado e não edite uma migration já aplicada.
- **Protocolo de Migrations 80/20 (Retrocompatibilidade N e N+1)**:
  - **Sempre aditiva**: Novas colunas devem ter `nullable=True` ou `server_default` (no PG11+ isso é O(1) nos metadados, sem lock de tabela).
  - **Proibido rename direto**: Nunca renomeie colunas existentes em um único passo — a versão antiga da API que ainda está em execução quebrará com 500.
  - **Rastreabilidade de migrações em 2 passos (Expand & Contract)**:
    - *Passo 1 (Expand)*: Ao criar a nova coluna/tabela sem remover a antiga, marque a migration e os modelos com a tag/comentário padronizado `# EXPAND: [nome_da_mudanca]` e abra/registre a issue ou tarefa de cleanup com o marcador `# CONTRACT: [nome_da_mudanca]`. O código passa a ler/escrever no campo novo com fallback.
    - *Passo 2 (Contract / Cleanup)*: Somente após a versão com o Passo 1 estar 100% implantada e estável em produção, suba a migration com `# CONTRACT: [nome_da_mudanca]` dropando a coluna antiga e removendo os fallbacks do código.
  - **DDL e PgBouncer**: Após rodar DDL que altere tabelas existentes em produção, emita `RECONNECT` no console do pooler para invalidar planos em cache e evitar `cached plan must not change result type`.
- O compose usa a imagem oficial **`postgres:18-alpine`** (arm64 nativo): no PG18 o `PGDATA` é `/var/lib/postgresql/18/docker` e o volume monta `/var/lib/postgresql` (não `/var/lib/postgresql/data`). As contrib (`citext`, `pg_trgm`, `ltree`, `pgcrypto`...) vêm na imagem; `postgis`/`vector`/`pg_cron` **não** vêm e são opt-in por projeto (imagem própria — ex.: `FROM postgres:18` + `postgresql-18-postgis-3` do PGDG — ou provider gerenciado). O bloco `postgis` do init continua lá porque é tolerante: só cria a extensão quando a imagem a tiver. Volume de PG16/17 não sobe no PG18 (dump/restore + volume novo): o sintoma é o container em loop de restart com `mkdir: can't create directory '/var/lib/postgresql/18/': Permission denied` (uid 999 do volume antigo × uid 70 do Alpine do PG18) → dump com o client do major antigo, restore com o client do novo (client ≥ servidor) e volume novo.
- A baseline **não exige `uuid-ossp`**: UUIDs usam `gen_random_uuid()` (built-in desde o PG13). `pg_cron` é **opcional**: só é criada se `pg_available_extensions` a oferecer e os jobs de cleanup só são agendados se ela estiver instalada — em RDS/Aurora/Cloud SQL sem pg_cron o schema sobe e a limpeza periódica fica com o operador. `citext` **continua obrigatória** (coluna `users.email`).
- O template **não** adiciona `created_by`/`updated_by`: nas tabelas atuais o ator é o próprio usuário (tautologia), o delete de usuário é hard delete com cascade e as tabelas de sistema não têm ator humano. Auditoria real é **opt-in por projeto**: tabela append-only por tabela auditada + trigger + `set_config('app.actor_id', …)` na mesma transação (receita em `docs/auth.md`, ver AD-038). Não mexa no schema por causa disso.
- O ator de uma trilha de auditoria vem da **sessão validada no servidor**, nunca do body da request (aceitar `updated_by` do cliente é forjar trilha). O audit de auth (`auth.login`/`register`/`logout`) segue como log estruturado (`outcome` + fingerprint sha256 + `trace_id`), não como trilha consultável.

### Filas e jobs

- Tasks são declaradas com `register_task(queue, action)` (`app/jobs/app.py`), que valida queue/action minúsculos e sem ponto/espaço e registra `name=f"{queue}.{action}"` na queue correspondente. A `email.send` é o exemplo (`send_email`).
- `register_task(queue, action, *, max_retries=3)`: `max_retries` é quantas vezes o Procrastinate tenta de novo (0 = uma execução só). Retry infinito não existe — sem teto, a falha nunca vira dead letter.
- Enfileiramento passa por `defer_job(task, ...)`: quando `rate_limit_kind`/`rate_limit_limit` são passados, ele consulta `job_rate_limits` (janela fixa alinhada ao epoch) antes de `defer_async`.
- `register_task` liga a DLQ: a falha só vira `dead_letters` (`kind`, `key`, `payload` com `kwargs`/`attempts`, `error`, `original_job_id` do `ctx`) na **última** tentativa — a mesma decisão de retry do Procrastinate —, então há uma linha por job que desistiu, não uma por tentativa. `JobAborted` não é falha e não entra na DLQ.
- `job_rate_limits` e `dead_letters` são criadas na baseline; o cleanup roda via pg_cron quando a extensão está disponível (rate limits a cada 15min; dead letters não-pending com >90 dias diariamente).
- A DLQ é operável por CLI: `python -m app.cli list-dead-letters [limit]`, `retry-dead-letter <id>` e `discard-dead-letter <id>`.
- `JobRateLimitExceeded` vira **429** (não 500) via handler em `app/main.py`.
- `job_id` é o id do Procrastinate (inteiro), não um UUID.

### Rate limiting e 2FA

- O rate limit de auth é **duplo**: por IP+endpoint (`rate_limit(...)`, tabela `rate_limits`) e por conta (`enforce_account_limit(...)`, chave `acct:<scope>:<email_normalizado>`). Login: IP 5/60s + conta 10/300s; register: IP 10/60s + conta 3/h.
- Os endpoints de 2FA têm limites próprios: `2fa.verify` 10/60s; `2fa.passkey.login.options` e `2fa.passkey.login.verify` 20/60s.
- O `pending_token` do 2FA é **de uso único**: `store_pending` grava no cache com TTL de 300s e `consume_pending` apaga no primeiro uso. No login por passkey, um pending que já carrega `challenge` não pode gerar outro (senão quem tem a senha renova o pending indefinidamente).
- Audit logging em auth: `auth.login`/`auth.register`/`auth.logout` registram outcome e um fingerprint sha256 truncado do e-mail (nunca o endereço cru). E-mail inexistente verifica contra `DUMMY_PASSWORD_HASH` para não vazar existência de conta pelo tempo de resposta.

### Testes

- Testes unitários não tocam Postgres (AD-008): o runner SQL é testado como texto, não executado.
- Validadores, criptografia e rate limiter têm testes de propriedades com Hypothesis (`apps/api/tests/test_properties_*.py`); são testes puramente em memória e não tocam Postgres (AD-008).
- bcrypt custa ~100 ms por hash: use `@settings(deadline=None, max_examples=...)` ou o Hypothesis vira fábrica de flakes.
- Não duplique em Python a semântica que mora no SQL; extraia só a parte pura (chave, decisão) e teste isso.

### E2E (Playwright)

- Specs vivem em `apps/web/e2e/` e rodam com `just e2e` (o `playwright.config.ts` sobe o Vite sozinho quando `E2E_BASE_URL` é a porta do Vite).
- Cenários de falha de autenticação e fluxos assíncronos usam `page.route` (mock): rodam sem API/DB e é isso que o CI executa (`just e2e-mock`).
- Spec que exige a API real deve **pular** quando ela não estiver disponível (`backendAvailable` em `e2e/helpers.ts`) — falhar por falta de stack esconde regressão real.
- `just e2e-full` sobe o compose e roda contra a SPA servida pela API em `:8000` (`E2E_BASE_URL=http://localhost:8000`). O recipe semeia um superuser determinístico (`app.cli create-user ... --superuser`; `E2E_ADMIN_EMAIL`/`E2E_ADMIN_PASSWORD` sobrescrevem o default) porque `POST /users/enqueue-email` é superuser-only, e o Dashboard só mostra a seção de job para superuser.
- Use seletores acessíveis (`getByRole`/`getByLabel`); não crie `data-testid` novo.

### Observabilidade e tracing

- Tracing é OpenTelemetry com propagação W3C (`traceparent`); a configuração vem de `Settings` (`otel_*`, `sentry_*`), nunca de `os.environ` lido no import.
- Span manual existe nas fronteiras: enqueue de job e worker. O contexto de trace viaja no payload do job; jobs antigos sem `trace_context` continuam funcionando.
- Logs carregam `trace_id`/`span_id` do span corrente. Span inválido vira `None`, não string de zeros.
- `provider.shutdown()` no shutdown do app: sem flush, os spans morrem no container.

### Segurança

- Preserve a autenticação vigente: sessão opaca revogável em cookie `HttpOnly`, hash de senha com `bcrypt` e 2FA opcional por TOTP e WebAuthn. JWT e Argon2 não fazem parte da arquitetura atual; não os introduza sem ADR.
- Valide entrada na fronteira, use bind parameters no SQL e preserve as proteções contra XSS existentes no React e na API.
- Nunca versione `.env`, credenciais, tokens ou chaves. Use `.env.example` apenas para nomes de variáveis sem segredos.
- Configuração impossível é erro de config, e erro de config falha **alto e no boot**: o `Settings` valida o que a aplicação não consegue cumprir (ex.: `password_min_length` tem que estar em 1..72, senão o schema de senha é impossível). Nunca capar o valor em silêncio.
- **Fail-fast de produção**: `Settings._validate_prod` (`model_validator(mode="after")`) recusa o boot com `APP_ENV=prod` se `SECRET_KEY` for o default ou tiver <32 chars, se `COOKIE_SECURE=false` ou se `WEB_ORIGIN` não começar com `https://`. Não relaxe esse validador para "fazer subir".
- **TLS é requisito de deployment; a app NUNCA termina TLS**: o repo serve HTTP puro e não embute proxy (nem Caddy nem `cloudflared`). O proxy é externo — Traefik/Caddy do Coolify ou Cloudflare Tunnel. Sem TLS o cookie `Secure` não volta ao navegador e HSTS não protege nada. O caminho de deploy no Coolify está em `docs/deploy.md` ("Implantar no Coolify") e o contrato de borda é a AD-036.
- `FORWARDED_ALLOW_IPS` (consumido pelo `uvicorn --proxy-headers`) deve listar **só** os IPs/redes do proxy real. **Nunca `*`**: qualquer cliente forjaria `X-Forwarded-For` e ganharia um bucket novo de rate limit e poluiria os logs. Os três valores válidos: proxy em loopback `127.0.0.1`; Coolify `172.16.0.0/12` (o Traefik tem IP dinâmico na rede do recurso — nunca o IP exato, e não existe env var oficial do Coolify com esse IP); Cloudflare Tunnel `127.0.0.1`. Mecânica do uvicorn: peer TCP fora da lista → `X-Forwarded-For`/`X-Forwarded-Proto` são **ignorados**; peer confiável → caminha o XFF **da direita para a esquerda** e devolve o primeiro host **não** confiável; com `*` devolve o item **mais à esquerda** (forjável pelo cliente). Por isso o proxy sempre **ANEXA** o IP que observou e nunca repassa o XFF cru do cliente. Ver AD-032/AD-036.
- `DB_SSLMODE` é anexado/sobrescrito na DSN direta (`app/db/session.py::apply_sslmode`) para Postgres gerenciado (ex.: `require`); não hardcode SSL no código. **Nunca** aplique `DB_SSLMODE` ao `DATABASE_URL_POOLER`: o pgbouncer local não tem certificado e o `asyncpg` faria TLS contra ele (falha no boot).
- **Pooler (pgbouncer)**: só a API usa `DATABASE_URL_POOLER` (vazio = DSN direto, comportamento padrão). `worker` e `migrate` continuam diretos: o Procrastinate usa `LISTEN/NOTIFY` e conexão persistente, que em `pool_mode=transaction` falham em silêncio, e o Alembic faz DDL. O serviço `pgbouncer` sobe sem `profiles:` (sempre no ar) mas **não tem efeito** enquanto a env não for setada. Ver AD-037.
- **Passkey é vinculada ao usuário do pending**: lookup e update usam `credential_id AND user_id` (`PASSKEY_LOOKUP_SQL`/`PASSKEY_UPDATE_SQL`). Sem o predicado de `user_id`, a passkey do atacante verifica contra o challenge da vítima e a sessão sai para a vítima — account takeover. Não simplifique para lookup só por `credential_id`.
- `GET /users` e `POST /users/enqueue-email` são **superuser-only** (`Depends(require_superuser)` → 403). `DELETE /users/{id}` permite o próprio usuário ou superuser.
- Em prod, `SecurityHeadersMiddleware` adiciona HSTS (`max-age=31536000; includeSubDomains`) e a CSP estática (`CSP_POLICY`); em local/dev não entram (quebrariam o HMR do Vite). `docs_url`/`redoc_url`/`openapi_url` são `None` em prod — `/api/docs`, `/api/redoc` e `/api/openapi.json` não existem.
- `/favicon.ico` e `/favicon.svg` são servidos do `dist/` (`FileResponse`, com 404 se nenhum existir).

### Imagem, saúde e boot

- O Dockerfile é multi-stage (`build-web` → `build-api` → `runtime`): toolchain e uv ficam só no stage de build; a imagem final copia apenas `/opt/venv` e roda como `app` (uid 10001, sem shell). Não reinstale toolchain na final, não adicione `curl` (o healthcheck usa o `urllib` do venv) e **não troque a base do runtime**: o venv referencia `/usr/local/bin/python3.13` por symlink absoluto.
- O stage `runtime` **remove o `pip` da base** `python:3.13-slim` (e o wheel do `ensurepip`): o pip vendoriza `msgpack`/`urllib3`/`pkg_resources` com CVE HIGH (4 findings do Trivy no CI) e o venv do `uv` em `/opt/venv` não usa system site-packages.
- Liveness ≠ readiness: `/api/v1/livez` não toca o banco (liveness decide restart); `/api/v1/healthz` checa o banco e devolve **503** + `degraded`/`db=false` (readiness é para proxy/LB). O HEALTHCHECK do container é o **livez** — se fosse o readiness, uma queda do Postgres reiniciaria um processo saudável.
- `worker` e `migrate` desligam o healthcheck da imagem (`healthcheck: disable: true`): não servem HTTP.
- Papéis do entrypoint: `migrate` (one-shot, roda `alembic upgrade head`), `api`, `worker`. api/worker **não** rodam migration (evita corrida). A espera do Postgres usa `pg_isready` (fallback TCP) e **termina com `exit 1`** ao estourar.
- `API_INTERNAL_PORT` é a porta interna do container; `API_PORT` é a porta publicada no host. O `docker-compose.yml` (artefato de deploy) **não publica porta nenhuma** em `api`/`postgres` — só `expose:`. As portas de host saem do `docker-compose.override.yml` (`127.0.0.1:${API_PORT:-8000}:8000`, `127.0.0.1:${POSTGRES_PORT:-5432}:5432`), que o `docker compose` **auto-mescla sem `-f`**; o Coolify e o CI usam `-f docker-compose.yml` e ignoram o override. O proxy do Coolify fala com o container pelo IP da rede Docker, nunca por porta do host.
- **Compose para deploy**: `postgres`, `pgbouncer`, `migrate`, `api` e `worker` **não têm `profiles:`**. Não reintroduza — o Coolify roda `docker compose up -d` sem `--profile` (bug #6395) e serviço com profile não sobe. Não reintroduza bind-mount de `./apps/api` ou `./scripts` sobre a imagem: a imagem buildada é a fonte da verdade e o bind-mount mascararia o artefato (o compose só mantém o mount read-only de `./scripts/postgres-init` e dos YAMLs de observability).
- **Coolify trata o compose como 1 resource** e, na v4.5.0, **não tem rolling update nem réplicas** (compose com `scale`/`replicas` > 1 é rejeitado) → downtime curto por deploy. O status agregado pode aparecer como `running:unknown` (a UI não parseia health de compose — é cosmético): verifique por `/api/v1/healthz` + `docker compose ps`. A ordem `migrate → api/worker` vem do `depends_on` do compose (o pre-deployment command da Coolify é pulado na 1ª deploy). Ver AD-036.
- **Deploy Modular no Coolify (Sem Compose em Produção)**: para ecossistemas com múltiplos serviços satélites (GrowthBook, Mautic, Listmonk, OpenObserver), o deploy em produção é feito via recursos independentes (**Database** PostgreSQL nativo + **Application** via Dockerfile para API + **Application** para Worker). As migrations rodam manualmente sob demanda (`alembic upgrade head` via exec ou task one-off). O `docker-compose.yml` da raiz permanece no repo servindo ao desenvolvimento local (`just init`/`just up`) e à validação em CI. Ver `docs/deploy.md` ("Deploy Modular no Coolify").
- `env_file: .env` é **obrigatório** em `migrate`, `api` e `worker`. Em clone fresco, `just validate-compose` (e o job `compose` do CI) materializam um `.env` temporário a partir do `.env.example` e o removem no fim; para uso real, `cp .env.example .env` e preencha os segredos. O `pgbouncer` não monta `.env`: recebe só as credenciais interpoladas do compose (`POSTGRES_*`) e a API recebe `DATABASE_URL_POOLER` (vazio por default = DSN direto).
- O job `docker` do CI **mede o tamanho real da imagem** e falha acima do teto (700 MB): a imagem atual mede **294,7 MB**. A estimativa local (~250–450 MB) era chute; quem manda é o gate.
- O mesmo job **boota o stack** (`docker compose up -d` → readiness `db=true` → `alembic_version` → worker vivo → `select 1` via pgbouncer): build+scan não pega entrypoint quebrado (exit 127), migration que não aplica nem health que não volta. Foi o boot que expôs os dois bugs abaixo — nenhum dos dois era visível em build, lint ou unit test.
- O entrypoint **exporta o PATH do venv** (`/opt/venv/bin`): o `bash -lc` do compose executa `/etc/profile`, que reescreve o PATH e faz `alembic`/`uvicorn`/`python` sumirem (exit 127 em `migrate`, `api` e `worker`).
- `op.execute` do Alembic leva **um statement por chamada**: o Alembic roda com engine async (asyncpg), que usa prepared statement e rejeita múltiplos comandos ("cannot insert multiple commands into a prepared statement").
- App e banco usam o **mesmo** `POSTGRES_PASSWORD`: o compose replica `${POSTGRES_PASSWORD:-app}` para `migrate`/`api`/`worker`. Sem isso, um `.env` com a senha vazia deixa o app com DSN sem senha e o banco exigindo senha → "password authentication failed" no boot (o `pg_hba` exige `scram-sha-256` na rede do Docker).

### Backup e restauração

- **Backup em Produção (Coolify)**: no deploy modular oficial (PostgreSQL como recurso nativo Database da Coolify), o backup é feito diretamente pela interface do Coolify para S3/Cloudflare R2 via `pg_dump` lógico (com agendamento cron nativo, histórico e alertas, sem downtime e sem parar o container).
- **Backup no host / testes**: os scripts `scripts/backup.sh` e `restore.sh` continuam disponíveis para dumps manuais e restaurações. `test-restore.sh` é a prova de integridade (cria bancos temporários, faz backup→restore e confere MD5, count, constraints e sequências), executado localmente via `just test-restore` e no CI pelo job `restore-test`.
- `POSTGRES_CONTAINER` (ex.: `postgres`) coloca `backup.sh`/`restore.sh` no **modo container**: `pg_dump`/`pg_restore` rodam por `docker compose -f "$BACKUP_COMPOSE_FILE" exec -T` (sem porta publicada no host e sem client `pg_*` na máquina); `BACKUP_COMPOSE_FILE` aponta para o compose implantado (default `<repo>/docker-compose.yml`). Sem `POSTGRES_CONTAINER` o modo host exige `POSTGRES_HOST`/`POSTGRES_PORT` alcançáveis. Não use `COMPOSE_FILE` para isso: é variável do próprio Docker Compose.
- O dump é criado com `umask 077`/`chmod 600` (contém hashes de senha, TOTP cifrado e PII). `BACKUP_ENCRYPT_PASSPHRASE` o cifra (`openssl enc -aes-256-cbc -pbkdf2`, extensão `.dump.enc`); sem ela o script avisa que o arquivo sai em claro. `BACKUP_KEEP_COUNT` (default 14) é a retenção local; `BACKUP_KEEP_DAYS` é a remota.
- `scripts/restore.sh` exige alvo explícito (`--db`/`POSTGRES_RESTORE_DB`) e `--yes`: nunca adivinhe o banco de destino.

### CI e supply chain

- As actions do CI/deploy são pinadas por **commit SHA** (com a versão em comentário); não troque por tag flutuante.
- O job `e2e` é endurecido contra o travamento do download do Chromium (1x em 2 runs, CDN/apt do runner): cache dos browsers (`actions/cache`, `~/.cache/ms-playwright`), teto por tentativa (`timeout` + 3 tentativas) e `timeout-minutes` por step. O cache é salvo logo após o install, então o download caro não se repete quando o teste falha.
- Dependabot (`.github/dependabot.yml`) abre PRs semanais para `pip`, `npm`, `github-actions` e `docker`; as instrumentações OTel sobem agrupadas.
- O gate de auditoria do front no CI é `npm audit --omit=dev --audit-level=moderate` (local: `just web-audit`, `--audit-level=high`).
- `just ci` roda `just validate-compose`, que materializa um `.env` a partir do `.env.example` se não houver um. O fallback do Compose no serviço Grafana (`${GRAFANA_ADMIN_PASSWORD:-admin}`) garante que validações e builds passem sem exigir a senha no deploy do app.
- O deploy (`.github/workflows/deploy.yml`) publica no GHCR em push na `main`/tag `v*`, gera SBOM SPDX com syft e assina imagem + SBOM com cosign keyless (OIDC), com permissões mínimas (`packages: write`, `id-token: write`).

### SAST automatizado (Semgrep)

As regras acima que dá para checar mecanicamente estão em `semgrep.yml` (raiz). `just semgrep` roda o gate local; o CI roda o mesmo scan no job `sast`.

| Regra (`semgrep.yml`) | Regra que ela cobre |
|---|---|
| `no-orm-query-in-routes` | AD-012: runtime usa Core, nunca `session.query()`/`select()` ORM |
| `no-dynamic-sql-text` | `text()` recebe literal; entrada do usuário nunca entra no SQL |
| `fastapi-response-model-required` | Toda rota declara `response_model` |
| `no-wildcard-cors` | CORS sem origem curinga |
| `ts-no-explicit-any` | TypeScript strict, sem `any` |

- O scan é local e offline: `semgrep scan --config semgrep.yml --error apps/api/app apps/web/src apps/web/e2e`. Não use `--config auto` — ele envia languages, frameworks e a URL do projeto ao registry e muda o resultado entre execuções.
- Semgrep é ferramenta de análise, não dependência da aplicação: mantenha fora de `apps/api/pyproject.toml` e instale com `pipx install semgrep` (ou rode via `uvx semgrep`).
- Ao criar, alterar ou remover uma regra, atualize `semgrep.yml` e esta tabela na mesma mudança.
- Falso positivo legítimo: suprima com `# nosemgrep: <rule-id>` na linha ou na linha anterior e explique o motivo.

## 4. Fluxo de trabalho

1. **Investigue**: leia o código relevante, o `README.md` (índice), o guia em `docs/` que afeta a mudança e as ADRs afetadas; verifique todos os chamadores de uma função antes de alterá-la.
2. **Planeje**: antes de editar, apresente um plano curto com objetivo, arquivos afetados, decisões e validação. Para mudanças amplas, destrutivas ou que alterem arquitetura, autenticação, contratos ou dependências, aguarde aprovação explícita.
3. **Implemente**: faça a menor mudança completa que atenda ao objetivo, sem refatorações ou formatações não relacionadas.
4. **Valide**: rode os checks correspondentes e registre qualquer comando que não possa ser executado por indisponibilidade do ambiente.
5. **Commit**: quando solicitado, mantenha o commit isolado por assunto e marque-o como `[AI-assisted] feat(api): ...`. Não faça commit ou push sem pedido explícito.

## 5. Validação local

Use a menor matriz que cubra a mudança e rode `just ci` antes de submeter alterações de código:

| Mudança | Checks necessários |
|---|---|
| Backend | `just test && just lint && just typecheck` |
| Auth, segurança ou código sensível |Checks anteriores + `just bandit` |
| Frontend | `just web-typecheck && just web-lint` |
| Build, bundle ou rotas do frontend | Checks do frontend + `just web-build` |
| Fluxo E2E | `just e2e` (pula o que exige backend) · `just e2e-mock` (só mocks; é o que o CI roda) · `just e2e-full` (stack real) |
| Schema Pydantic, rota ou contrato OpenAPI | `just web-types && just check-schema` (o `check-schema` falha se alguma resposta de rota ficar sem contrato tipado) |
| Dependências Python ou npm | `just api-audit` e `just web-audit` |
| Versão do Node ou toolchain | `mise install` + `mise ls` (fonte única em `.node-version`) + `npm ci` limpo (o `overrides` do `@hookform/resolvers` só se prova com install do zero) |
| Qualquer alteração de código coberta pelas regras do `semgrep.yml` | `just semgrep` |
| Dockerfile, compose ou entrypoint | `just validate-compose` (usa um valor de validação para `GRAFANA_ADMIN_PASSWORD` e materializa um `.env` temporário se preciso) + `bash -n scripts/entrypoint.sh` (sem daemon: `docker compose config --quiet`; o build real, o teto de tamanho da imagem e o **boot do stack** são do job `docker` do CI) + `just e2e-full` com daemon (sobe a stack, roda as migrations e a suíte E2E real) |
| Backup ou restauração | `bash -n scripts/backup.sh scripts/restore.sh scripts/test-restore.sh` + `just test-restore` (roda em container; exige Docker, não client no host) |

`just ci` inclui `just semgrep` (SAST), as auditorias de dependências e `just e2e-mock`; não inclui build do frontend, E2E contra a stack real, build Docker, Trivy nem o teste de restauração; rode esses checks quando a mudança os alcançar. O pre-commit não substitui `just ci`.
