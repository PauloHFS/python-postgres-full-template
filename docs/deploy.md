# Deploy: Coolify, produção e borda

Guia de deploy do template no Coolify:
- **Modelo de Produção Recomendado**: [Deploy Modular via Dockerfile](#deploy-modular-no-coolify-dockerfile--docker-image-sem-docker-compose) (PostgreSQL nativo isolado + API e Worker via Dockerfile + Migrations sob demanda + Satélites).
- **Modelo de Monólito / Dev**: [Deploy com Docker Compose Completo](#deploy-legadocompose-na-vps-stack-inteira-em-1-resource) (apenas se desejar rodar banco e app juntos no mesmo Compose).
- Borda com Cloudflare, DNS-01, QUIC/BBR e backups.

---

## Checklist de produção

O repositório serve **HTTP puro**. **TLS é requisito de deployment**: coloque um reverse proxy/balanceador terminando HTTPS na frente da API. Sem TLS o cookie `Secure` não volta ao navegador e o HSTS não protege nada — a app não embute TLS e o repo não embute proxy (o caminho Coolify está logo abaixo; contrato em [architecture.md](architecture.md) (AD-036)).

- **Fail-fast de configuração**: com `APP_ENV=prod` o boot aborta se `SECRET_KEY` ainda for o default (ou tiver <32 chars), se `COOKIE_SECURE=false` ou se `WEB_ORIGIN` não começar com `https://`. Config impossível não sobe.
- **Proxy confiável**: `FORWARDED_ALLOW_IPS` deve listar **só** os IPs/redes do seu proxy (default `127.0.0.1`). **Nunca use `*`**: qualquer cliente forjaria `X-Forwarded-For` e ganharia um bucket novo de rate limit. No Coolify o Traefik tem IP dinâmico na rede do recurso: use `172.16.0.0/12`, nunca o IP exato.
- **Postgres isolado ou gerenciado**: com o Postgres nativo do Coolify ou provider gerenciado (Neon/RDS), defina `DB_SSLMODE` se exigido. A baseline não exige `uuid-ossp` (usa `gen_random_uuid()`) e trata `pg_cron` como opcional (RDS/Aurora/Cloud SQL sobem sem ela; a limpeza periódica fica com o operador). `citext` continua obrigatória.
- **Observability / Métricas**: A aplicação exporta OTLP padrão (`OTEL_EXPORTER_OTLP_ENDPOINT`). Em produção, a stack de monitoramento (Grafana ou OpenObserver) roda como um serviço satélite compartilhado, e sua app apenas envia os dados para ela.
- **Mailpit**: fica restrito ao dev local (`just up` ou `--profile dev`). Em produção, use qualquer SMTP relay (Resend, SES, Postmark, Brevo, etc.).
- **Docs desligados**: em prod `/api/docs`, `/api/redoc` e `/api/openapi.json` não existem. HSTS e CSP só são aplicados em prod.
- **Backup**: em produção modular, o backup é configurado diretamente no recurso do Banco de Dados no Coolify (S3/Cloudflare R2).
- **Entrega**: `.github/workflows/deploy.yml` publica no GHCR em tags `v*` (ou sob demanda via `workflow_dispatch`) e anexa SBOM SPDX (syft) + assinatura keyless (cosign/OIDC); no dia a dia o Coolify faz o deploy direto do Dockerfile ou do compose após o `ci.yml` validar a `main`.

---

## Deploy Modular no Coolify: Dockerfile / Docker Image (Modelo Recomendado em Produção)

Para produção, especialmente com múltiplos serviços satélites (**GrowthBook**, **Mautic**, **Listmonk**, **OpenObserver**), o modelo oficial e desacoplado é:

- **PostgreSQL**: Recurso de Banco nativo no Coolify (com backup agendado para S3/R2 e zero risco de restart durante deploys do app).
- **API (Web SPA + FastAPI)**: Recurso do tipo **Application** usando o `Dockerfile` do repositório (ou puxando a imagem pronta do GHCR).
- **Worker (Procrastinate)**: Segundo recurso do tipo **Application** apontando para o mesmo repo/imagem, apenas mudando o comando de inicialização.
- **Migrations manuais**: Controle total de quando rodar DDL/schema upgrades, sem surpresas e sem lock automático no boot.
- **Satélites**: GrowthBook, Mautic, Listmonk, OpenObserver / OTel rodando cada um no seu próprio recurso.

### 1. Provisionar o Banco de Dados (PostgreSQL)

1. No Coolify, vá em **+ New Resource** → **Databases** → **PostgreSQL**.
2. Nome: `app-postgres`.
3. Anote as credenciais:
   - Host interno na rede Docker: `app-postgres`
   - Porta interna: `5432`
   - `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`
4. Vá na aba **Backups** e configure rotina diária para Cloudflare R2 ou AWS S3 (ver [backup.md](backup.md)).
5. *(Opcional)* Se for compartilhar este mesmo PostgreSQL com Listmonk ou GrowthBook, acesse o terminal do banco e crie bases separadas:
   ```sql
   CREATE DATABASE listmonk;
   CREATE DATABASE growthbook;
   ```

### 2. Subir a Aplicação Web + API (FastAPI + Front)

1. No Coolify, clique em **+ New Resource** → **Application** → **Public/Private Repository**.
2. **Build Pack**: Selecione **Dockerfile**.
3. **Dockerfile Location**: `/Dockerfile`.
4. Em **General / Configuration**:
   - **Domains**: `https://app.seu-dominio.com:8000` (porta `8000`).
   - **Exposed Port**: `8000`.
   - **Health Check Path**: `/api/v1/livez` (liveness que não depende do banco).
5. Em **Environment Variables**:
   ```dotenv
   APP_ENV=prod
   SECRET_KEY=<gere-uma-string-segura-com-mais-de-32-chars>
   COOKIE_SECURE=true
   WEB_ORIGIN=https://app.seu-dominio.com
   FORWARDED_ALLOW_IPS=172.16.0.0/12

   # Conexão com o banco isolado (criado no Passo 1):
   POSTGRES_HOST=app-postgres
   POSTGRES_PORT=5432
   POSTGRES_DB=app
   POSTGRES_USER=app
   POSTGRES_PASSWORD=<senha-do-postgres>

   # SPA estática servida em /:
   SPA_DIST_PATH=/app/apps/web/dist
   ```
6. O `CMD` default do Dockerfile já sobe a API automaticamente:
   ```bash
   bash -lc "/app/scripts/entrypoint.sh api"
   ```

### 3. Subir o Worker (Procrastinate)

O worker processa filas de tarefas (e-mails, webhooks, rotinas em segundo plano) assincronamente.

1. No Coolify, crie outra **Application** apontando para o mesmo repositório Git (ou use **Clone** da aplicação criada no passo 2).
2. Nome: `app-worker`.
3. **Domains**: Deixe **vazio** (o worker não expõe porta nem recebe tráfego HTTP).
4. Em **General**:
   - **Custom Docker Command** (ou Start Command):
     ```bash
     bash -lc "/app/scripts/entrypoint.sh worker"
     ```
   - **Health Check**: Desabilite (worker não tem endpoint HTTP).
5. Em **Environment Variables**: Use as mesmas variáveis de banco e ambiente configuradas na API.

### 4. Como Rodar as Migrations Manualmente

Com o banco de dados e a aplicação isolados, você não quer que cada push na `main` rode DDL no banco automaticamente. Você tem 3 formas simples e seguras de rodar migrations sob demanda:

#### Opção A: Pelo Terminal do Container no Coolify (Recomendado)
1. No Coolify, abra o container da aplicação `app`.
2. Vá na aba **Terminal / Exec**.
3. Execute o comando de migration:
   ```bash
   alembic upgrade head
   ```
   *(O ambiente do container já possui o venv do uv no `PATH` e as variáveis de conexão com o banco carregadas).*

#### Opção B: Via "One-off Task / Command" no Coolify
1. Nas configurações da sua aplicação no Coolify, você pode acionar a aba **Commands / Tasks**.
2. Rode:
   ```bash
   /app/scripts/entrypoint.sh migrate
   ```
   Ele roda `alembic upgrade head` uma única vez e sai com código 0.

#### Opção C: Da sua máquina local conectada via Tunnel/Wireguard/Tailscale
1. Com acesso de rede ao Postgres da VPS:
   ```bash
   DATABASE_URL="postgresql+asyncpg://app:senha@ip-ou-tailscale:5432/app" just api-migrate
   ```

### 5. Integrando com Satélites (GrowthBook, Mautic, Listmonk, OpenObserver) e Rede

No Coolify, você **não precisa (e nem deve) declarar rede em nenhum arquivo compose**. O Coolify gerencia isso nativamente na infraestrutura:

- **Como funciona a rede no Coolify**: Ao criar recursos (Applications, Databases, Services) dentro do mesmo **Server / Environment / Project**, o Coolify conecta todos automaticamente na mesma rede Docker bridge daquele servidor (geralmente chamada `coolify`).
- **Resolução de nomes**: Os containers se enxergam pelo **nome do recurso / container_name** (ou UUID interno gerado pelo Coolify). Por exemplo, se seu banco se chama `app-postgres`, a API o acessa diretamente via `app-postgres:5432`.
- **Configuração na UI**: Em qualquer recurso, na aba **Settings / Advanced**, você pode ver ou trocar a rede Docker vinculada (Network), sem encostar em nenhum arquivo `.yml`.

#### OpenTelemetry + OpenObserver
1. Crie um Resource no Coolify para o **OpenObserver** (usando imagem oficial `public.ecr.aws/zinclabs/openobserver:latest`).
2. Defina porta web (ex: `5080`) e configure o domínio (ex: `https://logs.seu-dominio.com`).
3. Na aplicação principal (`app`), adicione nas variáveis de ambiente:
   ```dotenv
   OTEL_EXPORTER_OTLP_ENDPOINT=http://openobserver:5080/api/default
   OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <token-base64>
   ```

#### Listmonk (E-mail Marketing / Newsletters)
1. Suba o template do Listmonk no Coolify (ou Application com a imagem `listmonk/listmonk:latest`).
2. Aponte a conexão do banco para o `app-postgres` na base `listmonk`:
   ```text
   postgres://listmonk:senha@app-postgres:5432/listmonk?sslmode=disable
   ```

#### GrowthBook (Feature Flags / Testes A/B)
1. Suba o GrowthBook via Resource Docker Image (`growthbook/growthbook:latest`) ou compose oficial.
2. Defina o endpoint da API do GrowthBook e consuma no front do `apps/web` sem qualquer atrito.

---

## Deploy com Docker Compose (Alternativa: Stack Inteira em 1 Resource)

Se por algum motivo de teste você quiser subir a aplicação e o banco em um único recurso Compose no Coolify (comportamento de dev/monólito):

1. **Recurso**: build pack **Docker Compose**, Base Directory = raiz do repo, Docker Compose Location = `docker-compose.yml`.
2. **Env vars**: cole na UI do Coolify as variáveis do `.env.example`.
3. O Compose sobe `postgres`, `migrate`, `api` e `worker` juntos. Lembre-se que neste modo cada deploy recria os containers da stack e o backup de produção fica atrelado ao volume persistente.

---

## Cloudflare + Traefik (DNS-01, IPs reais, QUIC/BBR e Zero Trust)

Quando a VPS fica longe dos usuários (ex.: VPS na Europa atendendo usuários no Brasil), o proxy da Cloudflare (**nuvem laranja**) é essencial para terminar a conexão QUIC/TLS na borda local (~10–35 ms), entregar os assets estáticos cacheados no país do usuário e manter conexões aquecidas até a VPS sobre TCP **BBR**.

### 1. Configurações no painel da Cloudflare

1. **DNS (`DNS` → `Records`)**:
   - Crie registros **`A`** (IPv4 da VPS) e **`AAAA`** (IPv6 da VPS) com **Proxy status = Proxied (nuvem laranja)** para o domínio da aplicação (ex.: `app.example.com`) e para o subdomínio do próprio Coolify (ex.: `coolify.example.com`).
   - *Atenção com wildcards*: se a zona tiver um `CNAME *` apontando para um Cloudflare Tunnel local, qualquer subdomínio explícito (`A`/`AAAA`) tem precedência no DNS sobre o wildcard `*`.
2. **SSL/TLS (`SSL/TLS` → `Overview`)**:
   - Modo de criptografia: **`Full (Strict)`** (nunca use *Flexible*; e prefira *Full (Strict)* em vez de *Automatic/Full* para validar o certificado Let's Encrypt do Traefik).
   - **Origin connection & post-quantum encryption**: mantenha **`Automatic key exchange`** ligado (e as caixas de *Compliance requirements* desmarcadas). O Traefik v3.7 negocia troca de chaves pós-quântica híbrida (`X25519MLKEM768`) automaticamente e economiza 1 round-trip no handshake TLS entre Cloudflare e a origem.
3. **Speed (`Speed` → `Optimization` → `Protocol Optimization`)**:
   - **HTTP/2**: ON
   - **HTTP/3 (with QUIC)**: ON
   - **0-RTT Connection Resumption**: ON

### 2. Token da Cloudflare e `coolify-proxy` (DNS-01 + Trusted IPs)

O ACME **HTTP-01** padrão exige rota pública direta e falha em renovações atrás do proxy laranja ou proteção anti-bot da Cloudflare. Além disso, o **Integration Token** cadastrado na UI do Coolify (*Security → Integration Tokens*) sincroniza zonas e cria registros DNS automaticamente, mas **não é injetado sozinho** no container `coolify-proxy` (Traefik).

Para configurar o Traefik do Coolify (**Servers → localhost → Proxy → Configuration**) de forma persistente (ou via `SaveProxyConfiguration` no banco do Coolify):

1. Adicione a variável de ambiente `CF_DNS_API_TOKEN` no serviço `traefik` (pode ser o mesmo token com permissão `Zone → DNS → Edit` usado no Integration Token do Coolify):
   ```yaml
   services:
     traefik:
       environment:
         - CF_DNS_API_TOKEN=<token_cloudflare_edit_zone_dns>
   ```
2. Na lista `command:` do `traefik`, troque o `httpchallenge` por `dnschallenge` e declare os ranges IPv4 e IPv6 da Cloudflare em `forwardedHeaders.trustedIPs` nos entrypoints `http` e `https` (para que a API receba o IP real do cliente em `X-Forwarded-For` e o rate limit não agrupe todos os usuários no IP da borda):
   ```yaml
   command:
     # ... manter flags existentes (--entrypoints.https.http3 já vem ativo no Coolify)
     - '--entrypoints.http.forwardedHeaders.trustedIPs=173.245.48.0/20,103.21.244.0/22,103.22.200.0/22,103.31.4.0/22,141.101.64.0/18,108.162.192.0/18,190.93.240.0/20,188.114.96.0/20,197.234.240.0/22,198.41.128.0/17,162.158.0.0/15,104.16.0.0/13,104.24.0.0/14,172.64.0.0/13,131.0.72.0/22,2400:cb00::/32,2606:4700::/32,2803:f800::/32,2405:b500::/32,2405:8100::/32,2a06:98c0::/29,2c0f:f248::/32'
     - '--entrypoints.https.forwardedHeaders.trustedIPs=173.245.48.0/20,103.21.244.0/22,103.22.200.0/22,103.31.4.0/22,141.101.64.0/18,108.162.192.0/18,190.93.240.0/20,188.114.96.0/20,197.234.240.0/22,198.41.128.0/17,162.158.0.0/15,104.16.0.0/13,104.24.0.0/14,172.64.0.0/13,131.0.72.0/22,2400:cb00::/32,2606:4700::/32,2803:f800::/32,2405:b500::/32,2405:8100::/32,2a06:98c0::/29,2c0f:f248::/32'
     - '--certificatesresolvers.letsencrypt.acme.dnschallenge=true'
     - '--certificatesresolvers.letsencrypt.acme.dnschallenge.provider=cloudflare'
     - '--certificatesresolvers.letsencrypt.acme.storage=/traefik/acme.json'
   ```
3. Salve e recrie o proxy (`docker compose up -d --force-recreate` em `/data/coolify/proxy` ou botão **Restart Proxy**).

### 3. BBR + QUIC no Kernel da VPS

Certifique-se de que a VPS está com controle de congestionamento **BBR** + fila **`fq`** e buffers UDP de 16 MB (necessários para QUIC/HTTP3 e alto throughput em links transatlânticos):

```bash
sysctl net.ipv4.tcp_congestion_control net.core.default_qdisc net.core.rmem_max net.core.wmem_max
# Esperado:
# net.ipv4.tcp_congestion_control = bbr
# net.core.default_qdisc = fq
# net.core.rmem_max = 16777216
# net.core.wmem_max = 16777216
```

### 4. Acelerar e proteger o Dashboard do Coolify (Cloudflare + Zero Trust)

Acessar o painel do Coolify direto pela porta `:8000` (mesmo via Tailscale) trafega quase **1 MB** de CSS/JS sem compressão Brotli/Zstd em HTTP/1.1 puro, levando >6s em links intercontinentais e desabilitando login por Passkey (que exige HTTPS).

1. **Domínio da instância**: em **Settings → General → Instance's Domain**, defina `https://coolify.example.com` (com **Force HTTPS** habilitado). O Coolify gera `/data/coolify/proxy/dynamic/coolify.yaml` automaticamente; com a Cloudflare na frente, os assets (`/build/assets/*`) são comprimidos (~80% menores) e cacheados na borda local (~3x mais rápido no carregamento total), mantendo `http://<tailscale-ip>:8000` como acesso de contingência.
2. **Proteção com Cloudflare Zero Trust (Access)**:
   - Em [one.dash.cloudflare.com](https://one.dash.cloudflare.com/) → **Access → Applications → Add an application → Self-hosted (Public DNS)**:
     - **Application name**: `Coolify` · **Session Duration**: `1 month` (`720 hours`)
     - **Public hostname**: `coolify.example.com`
     - **Access policy**: `Action = Allow`, regra `Include → Emails` com os e-mails dos administradores.
     - **Authentication**: ative **Apply instant authentication** para pular a tela de seleção de provedor.
     - **Additional settings → Cookie settings**: mantenha **HTTP Only** ligado e **Enable Binding Cookie** **desligado** (o Binding Cookie é incompatível com os WebSockets `wss://` de logs/status em tempo real do Coolify).
   - **Bypass para Webhooks do GitHub** (se usar deploy automático por push): crie uma segunda aplicação/política para `coolify.example.com` com **Path = `webhooks/*`**, **Action = `Bypass`** e **Include = `Everyone`** (as rotas `/webhooks/*` já validam a assinatura HMAC do GitHub).

Guia extraído do README. Índice e atalhos: [README](../README.md).
