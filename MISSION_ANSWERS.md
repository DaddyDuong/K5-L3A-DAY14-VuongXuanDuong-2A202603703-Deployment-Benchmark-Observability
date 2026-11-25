# Day 14 Lab — Mission Answers

> **Repository:** `K5-L3A-DAY14-VuongXuanDuong-2A202603703-Deployment-Benchmark-Observability`  
> **Note on local ports:** In this test environment, pre-existing local Docker services (`dynamodb-local` on `:8000`, `dynamodb-admin` on `:8001`) were kept running untouched. Standalone local server experiments in Parts 1, 2, 4, and 5 develop were executed on free local ports (`8005`–`8008`), while Docker Compose stacks used their exact configured ports (`80` for Part 2 and `8080` for Part 5). All outputs and measurements below are real, locally verified executions.

---

## Part 1: Localhost vs Production (CP1)

### Exercise 1.1: Anti-patterns Found in `01-localhost-vs-production/develop/app.py`

Comparing `01-localhost-vs-production/develop/app.py` against `01-localhost-vs-production/production/app.py` and `01-localhost-vs-production/production/config.py`, there are **7 concrete anti-patterns**:

1. **Hardcoded secrets and credentials in source code (`develop/app.py:17-18`)**
   - `OPENAI_API_KEY = "sk-hardcoded-fake-key-..."` and `DATABASE_URL = "postgresql://admin:password123@localhost:5432/mydb"` are embedded directly in Python code. Pushing to Git immediately exposes credentials permanently in commit history.
   - **Production fix:** `production/config.py:15-35` loads all secrets and settings from environment variables via `os.getenv(...)` inside a `Settings` dataclass, and `validate()` (`config.py:37-46`) fails fast if `AGENT_API_KEY` is missing in production.

2. **Secret leakage in logs & unstructured `print()` statements (`develop/app.py:33-38, 48`)**
   - Uses plain `print(f"[DEBUG] ...")` and explicitly prints the API key on line 34 (`print(f"[DEBUG] Using key: {OPENAI_API_KEY}")`). Plain text `print()` cannot be cleanly parsed, filtered, or alerted on by log aggregators (Loki, Datadog, CloudWatch), and logging secrets exposes them to anyone with log access.
   - **Production fix:** `production/app.py:28-32, 110-122` configures Python's `logging` module with a structured JSON format (`{"time":"...","level":"...","msg":"..."}`) and logs only safe metadata (`question_length`, `client_ip`, `response_length`), never secrets.

3. **No centralized configuration management (`develop/app.py:21-22`)**
   - `DEBUG = True` and `MAX_TOKENS = 500` are hardcoded module constants, requiring code edits and redeploys to change behavior between dev, staging, and production.
   - **Production fix:** `production/config.py:14-50` centralizes `HOST`, `PORT`, `DEBUG`, `APP_NAME`, `APP_VERSION`, `ENVIRONMENT`, `LLM_MODEL`, `MAX_TOKENS`, and `ALLOWED_ORIGINS` in a single validated `settings` singleton (12-Factor Principle III: Config in the Environment).

4. **Missing health check and readiness endpoints (`develop/app.py:42-43`)**
   - There is no `/health` or `/ready` endpoint. Orchestrators (Docker, Kubernetes, Railway, Render) cannot detect if the process is hung or if startup initialization is complete before routing user traffic.
   - **Production fix:** `production/app.py:135-173` exposes `GET /health` (liveness probe returning status, uptime, version, environment, UTC timestamp), `GET /ready` (readiness probe returning `503` when `is_ready=False`, `200` once ready), and `GET /metrics`.

5. **Hardcoded `host="localhost"`, fixed `port=8000`, and `reload=True` (`develop/app.py:49-54`)**
   - `host="localhost"` binds only to loopback `127.0.0.1`, making the app unreachable from outside a container or cloud proxy. Hardcoding `port=8000` breaks cloud platforms (Railway/Render/Cloud Run inject dynamic `$PORT`). Enabling `reload=True` in production wastes CPU/file descriptors and causes unstable process restarts.
   - **Production fix:** `production/config.py:17-19` & `production/app.py:192-199` bind to `settings.host` (default `0.0.0.0`), read `settings.port` from `$PORT`, and only enable `reload=settings.debug` (default `false`).

6. **No lifecycle management or graceful shutdown (`develop/app.py`)**
   - On `SIGTERM` (e.g., rolling deploy or container scale-down), the process terminates abruptly, dropping in-flight HTTP requests and leaking open DB/HTTP connections.
   - **Production fix:** `production/app.py:39-67` uses FastAPI's `@asynccontextmanager` `lifespan` hook (flips `is_ready = False` and allows in-flight requests to drain on shutdown) together with `signal.signal(signal.SIGTERM, handle_sigterm)` (`lines 180-189`).

7. **No CORS policy and query-parameter-only `/ask` input without validation (`develop/app.py:30-31`)**
   - `ask_agent(question: str)` accepts `question` only as a URL query parameter (`POST /ask?question=...`, which leaks user prompts into proxy access logs and URL history) and configures no CORS restrictions.
   - **Production fix:** `production/app.py:77-82, 102-108` adds `CORSMiddleware` controlled by `ALLOWED_ORIGINS` and parses a JSON request body with explicit `422` validation when `question` is empty.

---

### Exercise 1.2: Running `develop/app.py` (Real Verified Output)

Per `README.md` Note #3, `develop/app.py` accepts `question` as a query parameter (`POST /ask?question=Hello`). Running `01-localhost-vs-production/develop/app.py` on port `8006`:

```bash
cd 01-localhost-vs-production/develop
uvicorn app:app --host 127.0.0.1 --port 8006
```

**Real `curl -i` output:**

```http
$ curl -i -s http://127.0.0.1:8006/
HTTP/1.1 200 OK
date: Wed, 25 Nov 2026 08:06:05 GMT
server: uvicorn
content-length: 54
content-type: application/json

{"message":"Hello! Agent is running on my machine :)"}

$ curl -i -s -X POST "http://127.0.0.1:8006/ask?question=Hello"
HTTP/1.1 200 OK
date: Wed, 25 Nov 2026 08:06:05 GMT
server: uvicorn
content-length: 126
content-type: application/json

{"answer":"Đây là câu trả lời từ AI agent (mock). Trong production, đây sẽ là response từ OpenAI/Anthropic."}
```

**Observed server logs (demonstrating anti-pattern #2 — secret printed directly to stdout, masked below):**

```text
INFO:     Uvicorn running on http://127.0.0.1:8006 (Press CTRL+C to quit)
[DEBUG] Got question: Hello
[DEBUG] Using key: sk-***MASKED***
[DEBUG] Response: Đây là câu trả lời từ AI agent (mock). Trong production, đây sẽ là response từ OpenAI/Anthropic.
INFO:     127.0.0.1:42484 - "POST /ask?question=Hello HTTP/1.1" 200 OK
```

**Real verification of `01-localhost-vs-production/production/app.py` (port `8006`):**

```http
$ curl -i -s http://127.0.0.1:8006/health
HTTP/1.1 200 OK
content-type: application/json

{"status":"ok","uptime_seconds":0.4,"version":"1.0.0","environment":"development","timestamp":"2026-11-25T08:06:05.960630+00:00"}

$ curl -i -s http://127.0.0.1:8006/ready
HTTP/1.1 200 OK
content-type: application/json

{"ready":true}

$ curl -i -s -X POST http://127.0.0.1:8006/ask -H "Content-Type: application/json" -d '{"question": "Hello"}'
HTTP/1.1 200 OK
content-type: application/json

{"question":"Hello","answer":"Đây là câu trả lời từ AI agent (mock). Trong production, đây sẽ là response từ OpenAI/Anthropic.","model":"gpt-4o-mini"}
```

---

### Exercise 1.3: Develop vs Production Comparison Table

| Feature | Develop (`develop/app.py`) | Production (`production/app.py` + `config.py`) | Why Important? |
| :--- | :--- | :--- | :--- |
| **Config & Secrets** | Hardcoded API key, DB password, `DEBUG`, `MAX_TOKENS` in source (`lines 17-22`) | Read from environment variables via `Settings` dataclass + `validate()` fail-fast (`config.py:14-50`) | Prevents credential leaks in Git history, allows seamless promotion across dev/staging/prod without changing code, and catches missing config at startup rather than mid-request. |
| **Health & Readiness Checks** | None (`lines 42-43`) | `GET /health` (liveness), `GET /ready` (readiness returning 503 if not ready), `GET /metrics` (`lines 135-173`) | Enables platforms/load balancers to restart crashed containers automatically and avoid routing traffic before initialization finishes or during shutdown. |
| **Logging** | Unstructured `print("[DEBUG] ...")`, prints secret API key (`lines 33-38`) | Structured JSON `logging` (`lines 28-32`), logs metadata only, never secrets (`lines 110-122`) | Structured JSON logs can be indexed, queried, and alerted on in centralized logging systems (Loki/Datadog/ELK), while excluding secrets prevents security breaches via log leaks. |
| **Shutdown / Lifecycle** | Abrupt exit, no cleanup or draining | FastAPI `lifespan` hook (`lines 39-67`) + `SIGTERM` handler (`lines 180-189`) | Enables zero-downtime deployments: flips readiness off, finishes in-flight user requests, and cleanly releases network/DB connections before exit. |
| **Network Binding & Port** | `host="localhost"`, hardcoded `port=8000` (`lines 51-52`) | `host="0.0.0.0"`, `port=int(os.getenv("PORT", "8000"))` (`config.py:17-18`, `app.py:196-197`) | Containers and PaaS routers cannot reach `127.0.0.1` inside a container and dynamically assign `$PORT`; binding `0.0.0.0:$PORT` is mandatory for cloud deployment. |
| **Debug / Reload Mode** | `DEBUG = True`, `reload=True` hardcoded (`lines 21, 53`) | `DEBUG` defaults to `false`; `reload=settings.debug` (`config.py:19`, `app.py:198`) | Auto-reloader adds file-watcher overhead, spawns extra processes, and causes unpredictable restarts in production. |
| **CORS & Input Handling** | No CORS middleware; `question` passed in URL query string | `CORSMiddleware` with configurable `ALLOWED_ORIGINS` (`lines 77-82`); JSON POST body with 422 validation (`lines 102-108`) | Protects browser clients against unauthorized cross-origin calls and prevents sensitive prompts from being recorded in proxy/URL logs. |

---

## Part 2: Docker Containerization (CP2)

### Exercise 2.1: Dockerfile Questions (`02-docker/develop/Dockerfile`)

1. **Base image:** `python:3.11` (`FROM python:3.11`, line 8). This is the full Debian-based Python 3.11 image (~1 GB+ uncompressed), which includes the OS, Python interpreter, `pip`, and many general-purpose Linux packages/compilers not needed at runtime.
2. **Working directory:** `/app` (`WORKDIR /app`, line 11). Docker creates `/app` if it does not exist and runs all subsequent `COPY`, `RUN`, and `CMD` instructions relative to this directory.
3. **Why `COPY requirements.txt` before copying source code (`app.py`)?** Docker builds images layer-by-layer and caches each layer. If a layer's inputs change, Docker invalidates cache for that layer and **every layer after it**. Because `requirements.txt` changes much less frequently than `app.py`, copying `requirements.txt` and running `pip install` first lets Docker reuse the cached dependency layer whenever only application code changes—turning minute-long rebuilds into sub-second builds.
4. **Difference between `CMD` and `ENTRYPOINT`:**
   - `ENTRYPOINT` defines the fixed primary executable that always runs when the container starts (it is only replaced if the user explicitly passes `--entrypoint`).
   - `CMD` specifies either the default command (when no `ENTRYPOINT` is set) or the default arguments passed to `ENTRYPOINT`, and is **easily overridden** by appending arguments to `docker run <image> <args>`.
   - Best practice: use `ENTRYPOINT` for the fixed runtime binary and `CMD` for overridable default flags/subcommands.

---

### Exercise 2.2: Build and Test `my-agent:develop` Container

Built from repository root per `README.md` Note #4:

```bash
docker build -f 02-docker/develop/Dockerfile -t my-agent:develop .
docker run -d --rm -p 8005:8000 my-agent:develop
```

**Real `curl -i` test output (query param per `README.md` Note #3):**

```http
$ curl -i -s http://127.0.0.1:8005/health
HTTP/1.1 200 OK
date: Wed, 25 Nov 2026 08:05:53 GMT
server: uvicorn
content-length: 53
content-type: application/json

{"status":"ok","uptime_seconds":0.1,"container":true}

$ curl -i -s -X POST "http://127.0.0.1:8005/ask?question=What%20is%20Docker%3F"
HTTP/1.1 200 OK
date: Wed, 25 Nov 2026 08:05:53 GMT
server: uvicorn
content-length: 103
content-type: application/json

{"answer":"Container là cách đóng gói app để chạy ở mọi nơi. Build once, run anywhere!"}
```

---

### Exercise 2.3: Multi-stage Build Analysis & Measured Image Sizes

**How `02-docker/production/Dockerfile` works:**
- **Stage 1 (`builder`, `FROM python:3.11-slim AS builder`):** Installs build-time system packages (`gcc`, `libpq-dev`), then runs `pip install --no-cache-dir --user -r requirements.txt` into `/root/.local`. This stage is discarded after build.
- **Stage 2 (`runtime`, `FROM python:3.11-slim AS runtime`):** Creates a non-root user (`appuser`), copies **only** the compiled site-packages from `builder` (`/root/.local` → `/home/appuser/.local`) plus `main.py` and `utils/mock_llm.py`, configures `HEALTHCHECK`, switches to `USER appuser`, and runs `uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2`.
- **Why the production image is dramatically smaller:** (1) It replaces the full `python:3.11` base image with `python:3.11-slim`, and (2) multi-stage copying leaves behind `gcc`, `libpq-dev`, apt metadata, and build artifacts in the builder stage—also shrinking the attack surface.

**Real build & measurement (`docker images my-agent` and `docker image inspect`):**

```bash
docker build -f 02-docker/develop/Dockerfile -t my-agent:develop .
docker build -f 02-docker/production/Dockerfile -t my-agent:advanced .
docker images my-agent
```

```text
IMAGE               ID             DISK USAGE   CONTENT SIZE
my-agent:advanced   1faea9bae0a7        238MB         57.7MB
my-agent:develop    92a2e363587b       1.66GB          427MB
```

| Image | Image ID | Exact Size (Bytes) | Size (MB, decimal / Docker CLI) | Size (MiB, binary) |
| :--- | :--- | ---: | ---: | ---: |
| `my-agent:develop` (single-stage, `python:3.11`) | `92a2e363587b` | `1,663,389,410` | **1,663.39 MB** (`1.66 GB`) | 1,586.33 MiB |
| `my-agent:advanced` (multi-stage, `python:3.11-slim`) | `1faea9bae0a7` | `237,718,699` | **237.72 MB** (`238 MB`) | 226.71 MiB |
| **Size Reduction** | — | **`-1,425,670,711`** | **`-1,425.67 MB` (`-85.71%`)** | `-1,359.63 MiB` |

- **Develop:** `1,663.39 MB` (`1.66 GB`)
- **Production:** `237.72 MB` (`238 MB`, well under the `< 500 MB` production target)
- **Exact Reduction:** **`85.71%`**

---

### Exercise 2.4: Docker Compose Stack (`02-docker/production/`)

**Architecture Diagram:**

```text
                       Host Machine (External Traffic)
                                     │
                             HTTP :80 / HTTPS :443
                                     ▼
                    ┌────────────────────────────────┐
                    │   nginx (nginx:alpine)         │
                    │   Reverse Proxy, Rate Limit    │
                    │   (10 req/s/IP), Sec Headers   │
                    └────────────────┬───────────────┘
                                     │  http://agent:8000
    ═══════════════ Docker Bridge Network: "internal" ═══════════════
                                     │
                                     ▼
                    ┌────────────────────────────────┐
                    │   agent (k5-day14-part2-agent) │
                    │   FastAPI + Uvicorn (2 workers)│
                    │   Port 8000 (internal only)    │
                    └───────┬────────────────┬───────┘
                            │                │
         redis://redis:6379/0                http://qdrant:6333
                            ▼                ▼
             ┌────────────────────┐   ┌────────────────────┐
             │ redis (7-alpine)   │   │ qdrant (v1.9.0)    │
             │ Cache & Rate Limit │   │ Vector DB (RAG)    │
             │ Port 6379          │   │ Port 6333 / 6334   │
             └─────────┬──────────┘   └─────────┬──────────┘
                       ▼                        ▼
                 [redis_data]             [qdrant_data]
                (Named Volume)           (Named Volume)
```

**Services started & how they communicate:**
- **4 services:** `nginx` (reverse proxy/load balancer exposing host `:80` and `:443`), `agent` (FastAPI app running internally on `:8000`, not directly exposed to host), `redis` (`redis:7-alpine`, cache/session/rate-limit store on `:6379`), and `qdrant` (`qdrant/qdrant:v1.9.0`, vector database on `:6333`).
- **Communication:** All 4 containers join the isolated bridge network `internal` and resolve each other via Docker's embedded DNS by service name (`agent:8000`, `redis:6379`, `qdrant:6333`). `agent` waits for `redis` and `qdrant` health checks (`depends_on: condition: service_healthy`) before starting.

**Real execution & test output via Nginx (`docker compose -p k5-day14-part2 up -d --build`):**

```text
NAME                      IMAGE                  COMMAND                  SERVICE   STATUS                           PORTS
k5-day14-part2-agent-1    k5-day14-part2-agent   "uvicorn main:app --…"   agent     Up 1 second (health: starting)   8000/tcp
k5-day14-part2-nginx-1    nginx:alpine           "/docker-entrypoint.…"   nginx     Up 1 second                      0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp
k5-day14-part2-qdrant-1   qdrant/qdrant:v1.9.0   "./entrypoint.sh"        qdrant    Up 12 seconds (healthy)          6333-6334/tcp
k5-day14-part2-redis-1    redis:7-alpine         "docker-entrypoint.s…"   redis     Up 11 seconds (healthy)          6379/tcp
```

```http
$ curl -i -s http://localhost/health
HTTP/1.1 200 OK
Server: nginx
Date: Wed, 25 Nov 2026 08:06:06 GMT
Content-Type: application/json
Content-Length: 95
Connection: keep-alive
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
X-XSS-Protection: 1; mode=block

{"status":"ok","uptime_seconds":0.4,"version":"2.0.0","timestamp":"2026-11-25T08:06:06.954016"}

$ curl -i -s http://localhost/ready
HTTP/1.1 200 OK
Server: nginx
Content-Type: application/json
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
X-XSS-Protection: 1; mode=block

{"ready":true}

$ curl -i -s -X POST http://localhost/ask -H "Content-Type: application/json" -d '{"question": "Explain microservices"}'
HTTP/1.1 200 OK
Server: nginx
Date: Wed, 25 Nov 2026 08:06:07 GMT
Content-Type: application/json
Content-Length: 103
Connection: keep-alive
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
X-XSS-Protection: 1; mode=block

{"answer":"Tôi là AI agent được deploy lên cloud. Câu hỏi của bạn đã được nhận."}
```

*(Stack was torn down cleanly via `docker compose -p k5-day14-part2 down -v` and temporary `.env.local` was deleted.)*

---

## Part 3: Cloud Deployment (CP6)

### Exercise 3.1 / 3.2: Live Cloud Deployment (Part 6 Agent on Render)

- **Platform:** Render (Free Tier — Docker Web Service `production-ai-agent` + Valkey 8 Key Value `agent-redis`, Region: Singapore, configured via `06-lab-complete/render.yaml`)
- **Public URL:** `https://production-ai-agent-yvnx.onrender.com`
- **Deployment Details:** See [`DEPLOYMENT.md`](DEPLOYMENT.md)
- **Live CP6 Self-Test Results (`BASE=https://production-ai-agent-yvnx.onrender.com`):**
  - `GET $BASE/health` → **`200`** (`{"status":"healthy","environment":"production"}`)
  - `GET $BASE/ready` → **`200`** (`{"status":"ready","redis":"connected"}`)
  - `POST $BASE/ask` (without `X-API-Key`) → **`401`** (`{"error":"http_error","detail":"Invalid or missing API key"}`)
  - `POST $BASE/ask` (with `-H "X-API-Key: $AGENT_API_KEY"`) → **`200`** *(verified with Render-managed `AGENT_API_KEY` in `screenshots/test.png`; key kept secret on Render Dashboard)*
- **Screenshots:**
  - [Deployment Dashboard](screenshots/dashboard.png)
  - [Service Running / Logs](screenshots/running.png)
  - [Public URL Test Results](screenshots/test.png)

---

### Exercise 3.2: Comparison of `railway.toml` vs `render.yaml`

Comparing `03-cloud-deployment/railway/railway.toml` vs `03-cloud-deployment/render/render.yaml` (and how both are adapted in `06-lab-complete/`):

| Aspect | `railway.toml` (Railway) | `render.yaml` (Render Blueprint) | Why They Differ |
| :--- | :--- | :--- | :--- |
| **File Format & Scope** | **TOML**, per-service build/deploy config (`[build]`, `[deploy]`). Does not declare companion services like Redis. | **YAML**, full **Infrastructure-as-Code (Blueprint)** declaring a list of `services:` (e.g., `type: web` AND `type: redis` in one file). | Railway favors an interactive project canvas/CLI where add-ons (Redis, Postgres) are added via UI/CLI; Render Blueprints model the entire multi-service topology declaratively from Git. |
| **Build Strategy** | `builder = "NIXPACKS"` (auto-detects Python) or `builder = "DOCKERFILE"` + `dockerfilePath = "Dockerfile"` (`06-lab-complete/railway.toml`). | Explicitly specifies `runtime: python` + `buildCommand: pip install -r requirements.txt`, OR `runtime: docker` + `dockerfilePath: ./Dockerfile` (`06-lab-complete/render.yaml`). | Railway uses Nixpacks zero-config detection by default; Render requires explicit runtime and build command declarations for reproducibility. |
| **Start Command & `$PORT`** | `startCommand = "uvicorn app:app --host 0.0.0.0 --port $PORT"` under `[deploy]` (when using Dockerfile, defaults to `CMD`). | `startCommand: uvicorn app:app --host 0.0.0.0 --port $PORT` (when using `runtime: docker`, uses Dockerfile `CMD`). | Both platforms inject `$PORT` dynamically, so apps must bind `0.0.0.0:$PORT`. |
| **Health Check & Restart Policy** | `healthcheckPath = "/health"`, `healthcheckTimeout = 30`, plus explicit `restartPolicyType = "ON_FAILURE"` and `restartPolicyMaxRetries = 3`. | `healthCheckPath: /health`; restart behavior is platform-managed automatically without separate TOML retry keys. | Railway exposes granular restart retry controls per service; Render manages zero-downtime health-gated rollouts and automatic restarts implicitly. |
| **Environment Variables & Secrets** | Not declared in `railway.toml`—set out-of-band via `railway variables set` CLI or Railway Dashboard. | Declared inline under `envVars:` with `value:` for non-secrets, `sync: false` for dashboard-only secrets (`OPENAI_API_KEY`, `REDIS_URL`), and `generateValue: true` to auto-generate random secrets (`AGENT_API_KEY`). | Render embeds config schema and secret placeholders directly in IaC so onboarding a new environment prompts for every required variable; Railway decouples variable management from the config file to minimize config boilerplate. |
| **Region, Plan & Auto-Deploy** | Configured in Railway Dashboard/project settings rather than `railway.toml`. | Explicitly specified in YAML: `region: singapore`, `plan: free`, `autoDeploy: true`, optional `disk:` mounts, and `maxmemoryPolicy: allkeys-lru` on Redis. | Render Blueprints allow one-click, region-pinned, reproducible deployments straight from a forked GitHub repository. |

---

### Exercise 3.3: GCP Cloud Run CI/CD Pipeline (`03-cloud-deployment/production-cloud-run/`)

Based on `cloudbuild.yaml` and `service.yaml`, the automated CI/CD pipeline runs on push to `main` through **4 sequential stages**:
1. **Test (`test`):** Runs in `python:3.11-slim`, installs dependencies + `pytest`, and executes `pytest tests/ -v --tb=short`—aborting the pipeline immediately if tests fail.
2. **Build (`build`, `waitFor: [test]`):** Builds the Docker image using `--cache-from=gcr.io/$PROJECT_ID/ai-agent:latest` and tags it with both immutable `:$COMMIT_SHA` and `:latest`.
3. **Push (`push`, `waitFor: [build]`):** Pushes all image tags (`--all-tags`) to Google Container Registry (`gcr.io/$PROJECT_ID/ai-agent`).
4. **Deploy (`deploy`, `waitFor: [push]`):** Runs `gcloud run deploy ai-agent` in `asia-southeast1` with the immutable `$COMMIT_SHA` image, configuring `min-instances=1` (avoiding cold starts), `max-instances=10`, `512Mi` RAM / `1` CPU, `60s` timeout, and injecting `OPENAI_API_KEY` from GCP Secret Manager (`--set-secrets`).
5. **Declarative Service Spec (`service.yaml`):** Defines the Knative `Service` manifest with `containerConcurrency: 80`, resource requests/limits (`0.5–1 CPU`, `256–512Mi`), Secret Manager `secretKeyRef` bindings for `OPENAI_API_KEY` & `AGENT_API_KEY`, plus separate `livenessProbe` (`GET /health`) and `startupProbe` (`GET /ready`).

---

## Part 4: API Security (CP3)

### Exercise 4.1: API Key Authentication (`04-api-gateway/develop/app.py`)

**Code analysis answers:**
- **Where is the API key checked?** In `verify_api_key()` (`develop/app.py:39-54`), which reads the `X-API-Key` header via `APIKeyHeader(name="X-API-Key", auto_error=False)` and is injected into `POST /ask` using FastAPI's `Depends(verify_api_key)` (`line 70`). Public routes `/` and `/health` remain unprotected.
- **What happens if the key is missing or wrong?** Missing header returns **`401 Unauthorized`** (`"Missing API key. Include header: X-API-Key: <your-key>"`); wrong key returns **`403 Forbidden`** (`"Invalid API key."`).
- **How to rotate API keys safely (zero downtime)?** Store a comma-separated list of valid keys in env/Secret Manager (e.g., `AGENT_API_KEYS=old_key,new_key`), deploy so both keys are accepted during the migration window, roll out `new_key` to all clients, verify zero traffic on `old_key`, then remove `old_key` and redeploy/reload.

**Real `curl -i` test outputs (`AGENT_API_KEY=secret-key-123` on port `8007`):**

```http
# 1. Missing API key -> 401 Unauthorized
$ curl -i -s -X POST http://127.0.0.1:8007/ask -H "Content-Type: application/json" -d '{"question": "Hello"}'
HTTP/1.1 401 Unauthorized
date: Wed, 25 Nov 2026 08:06:08 GMT
server: uvicorn
content-length: 67
content-type: application/json

{"detail":"Missing API key. Include header: X-API-Key: <your-key>"}

# 2. Invalid API key -> 403 Forbidden
$ curl -i -s -X POST http://127.0.0.1:8007/ask -H "X-API-Key: wrong-key" -H "Content-Type: application/json" -d '{"question": "Hello"}'
HTTP/1.1 403 Forbidden
date: Wed, 25 Nov 2026 08:06:08 GMT
server: uvicorn
content-length: 29
content-type: application/json

{"detail":"Invalid API key."}

# 3. Valid API key -> 200 OK
$ curl -i -s -X POST http://127.0.0.1:8007/ask -H "X-API-Key: secret-key-123" -H "Content-Type: application/json" -d '{"question": "Hello"}'
HTTP/1.1 200 OK
date: Wed, 25 Nov 2026 08:06:08 GMT
server: uvicorn
content-length: 114
content-type: application/json

{"question":"Hello","answer":"Agent đang hoạt động tốt! (mock response) Hỏi thêm câu hỏi đi nhé."}
```

---

### Exercise 4.2 & 4.3: JWT Authentication & Rate Limiting (`04-api-gateway/production/`)

**Code analysis answers (`auth.py`, `rate_limiter.py`, `app.py`):**
- **JWT Flow (`auth.py`):** Client authenticates once via `POST /auth/token` with credentials (`student` / `demo123`) → server signs a stateless HS256 JWT containing `sub` (username), `role`, `iat`, and `exp` (60 min) → client sends `Authorization: Bearer <token>` on subsequent requests → `verify_token()` verifies signature and expiry without a DB lookup.
- **Rate Limiting Algorithm (`rate_limiter.py:17-71`):** **Sliding Window** using a per-user `collections.deque` of request timestamps (`self._windows[user_id]`). On each request, timestamps older than `now - window_seconds` (60s) are evicted from the left (`popleft()`). If `len(window) >= max_requests`, it raises **HTTP 429** with `X-RateLimit-Limit`, `X-RateLimit-Remaining: 0`, `X-RateLimit-Reset`, and `Retry-After` headers.
- **Limits:** `student` (`role="user"`) = **10 requests / 60 seconds** (`rate_limiter_user`); `teacher` (`role="admin"`) = **100 requests / 60 seconds** (`rate_limiter_admin`).
- **How to bypass or raise limit for admin:** `production/app.py:141` branches on the JWT `role` claim (`limiter = rate_limiter_admin if role == "admin" else rate_limiter_user`). To completely bypass limits for admins, simply guard the call with `if role != "admin": rate_info = rate_limiter_user.check(username)`.

**Real `curl -i` test outputs (JWT token masked per `README.md` Note #6 & `RULES.md`):**

```http
# 1. No JWT token -> 401 Unauthorized
$ curl -i -s -X POST http://127.0.0.1:8007/ask -H "Content-Type: application/json" -d '{"question": "Explain JWT"}'
HTTP/1.1 401 Unauthorized
date: Wed, 25 Nov 2026 08:06:08 GMT
server: uvicorn
www-authenticate: Bearer
content-length: 76
content-type: application/json
x-content-type-options: nosniff
x-frame-options: DENY
x-xss-protection: 1; mode=block
referrer-policy: strict-origin-when-cross-origin

{"detail":"Authentication required. Include: Authorization: Bearer <token>"}
```

```json
# 2. Obtain JWT token via POST /auth/token (student / demo123 — token masked)
{
  "access_token": "eyJhbGciOiJIUzI...<MASKED>...MnTnRM",
  "token_type": "bearer",
  "expires_in_minutes": 60,
  "hint": "Include in header: Authorization: Bearer <MASKED_TOKEN>"
}
```

```http
# 3. Request #1 with valid Bearer JWT -> 200 OK
$ curl -i -s -X POST http://127.0.0.1:8007/ask \
    -H "Authorization: Bearer <MASKED_TOKEN>" \
    -H "Content-Type: application/json" \
    -d '{"question": "Test 1"}'
HTTP/1.1 200 OK
date: Wed, 25 Nov 2026 08:06:08 GMT
server: uvicorn
content-length: 179
content-type: application/json
x-content-type-options: nosniff
x-frame-options: DENY
x-xss-protection: 1; mode=block
referrer-policy: strict-origin-when-cross-origin

{"question":"Test 1","answer":"Agent đang hoạt động tốt! (mock response) Hỏi thêm câu hỏi đi nhé.","usage":{"requests_remaining":9,"budget_remaining_usd":1.6e-05}}
```

```text
# 4. Loop Requests #2..#15 (limit = 10 req/min for student)
Request 2  -> HTTP 200
Request 3  -> HTTP 200
Request 4  -> HTTP 200
Request 5  -> HTTP 200
Request 6  -> HTTP 200
Request 7  -> HTTP 200
Request 8  -> HTTP 200
Request 9  -> HTTP 200
Request 10 -> HTTP 200
```

```http
# Request #11 hits sliding window limit -> 429 Too Many Requests
HTTP/1.1 429 Too Many Requests
date: Wed, 25 Nov 2026 08:06:10 GMT
server: uvicorn
x-ratelimit-limit: 10
x-ratelimit-remaining: 0
x-ratelimit-reset: 1795594030
retry-after: 59
content-length: 98
content-type: application/json
x-content-type-options: nosniff
x-frame-options: DENY
x-xss-protection: 1; mode=block
referrer-policy: strict-origin-when-cross-origin

{"detail":{"error":"Rate limit exceeded","limit":10,"window_seconds":60,"retry_after_seconds":59}}
```

```text
Request 12 -> HTTP 429
Request 13 -> HTTP 429
Request 14 -> HTTP 429
Request 15 -> HTTP 429
```

```http
# 5. Cost Guard usage inspection via GET /me/usage -> 200 OK
$ curl -i -s -H "Authorization: Bearer <MASKED_TOKEN>" http://127.0.0.1:8007/me/usage
HTTP/1.1 200 OK
content-type: application/json

{"user_id":"student","date":"2026-11-25","requests":10,"input_tokens":40,"output_tokens":296,"cost_usd":0.000184,"budget_usd":1.0,"budget_remaining_usd":0.999816,"budget_used_pct":0.0}
```

---

### Exercise 4.4: Cost Guard — Design, Rate Limit vs Cost Guard, and Why Both Are Needed

**How Cost Guard works (`04-api-gateway/production/cost_guard.py` & Redis-backed production design):**
1. **Pre-call budget check (`check_budget`):** Before invoking the LLM, Cost Guard reads the user's (and global) accumulated spend for the current period (`day` or `month`, e.g., Redis key `budget:{user_id}:YYYY-MM` or `cost:daily:YYYY-MM-DD`).
   - If global spend `>= global_daily_budget_usd` → blocks with **HTTP 503** (circuit breaker protecting total organization spend).
   - If user spend `>= budget_usd` → blocks with **HTTP 402 Payment Required**.
   - If user spend `>= 80%` of budget → logs a warning alert before hard cutoff.
2. **Post-call usage recording (`record_usage`):** After the LLM returns, calculates exact cost from token usage:
   $$\text{cost} = \frac{\text{input\_tokens}}{1000} \times P_{\text{in}} + \frac{\text{output\_tokens}}{1000} \times P_{\text{out}}$$
   In multi-instance production (such as Part 6), spend is updated atomically in Redis via `INCRBYFLOAT` with a TTL (`EXPIRE`, e.g., 48h for daily keys / 32 days for monthly keys) so the budget resets automatically and stays consistent across all replicas and restarts.

**Rate Limiting vs Cost Guard — Why Both Are Needed:**

| Dimension | Rate Limiting (HTTP 429) | Cost Guard (HTTP 402 / 503) |
| :--- | :--- | :--- |
| **What it protects** | **Request velocity & compute capacity** (requests per second/minute) | **Financial spend & token budget** (USD per day/month) |
| **Attack / failure it stops** | Traffic spikes, brute-force scraping, infinite client retry loops, DoS | Expensive long-context prompts, slow-drip abuse, runaway multi-step agent loops |
| **Blind spot if used alone** | 10 requests/min with 100k-token prompts stays *under* the rate limit while draining hundreds of dollars from your LLM bill | Thousands of tiny 5-token requests/min cost almost $0 yet exhaust CPU/threads and crash the API server |

Therefore, production AI agents must enforce **all three layers in sequence**: **Authentication (401/403)** → **Rate Limiting (429)** → **Cost Guard (402)** before calling the LLM.

---

## Part 5: Scaling & Reliability (CP4)

### Exercise 5.1: Liveness (`/health`) vs Readiness (`/ready`)

- **Liveness probe (`GET /health`) — *"Is the process alive?"***
  - Cloud platforms / Kubernetes call `/health` periodically. If it fails or times out, the orchestrator **restarts the container**.
  - **Crucial design rule:** `/health` must return `200 OK` as long as the Python process is responsive—**not** fail just because an external dependency (like Redis) is temporarily unreachable. Otherwise, a brief Redis blip would trigger a cascading restart storm across all agent containers.
- **Readiness probe (`GET /ready`) — *"Can this instance serve traffic right now?"***
  - Load balancers call `/ready` to decide whether to route requests to this container.
  - Returns **`503 Service Unavailable`** during startup (before models/connections load), during graceful shutdown (`_is_ready = False`), or when Redis/DB cannot be pinged. The orchestrator temporarily removes the instance from the load-balancer pool without killing it, and adds it back once `/ready` returns `200`.

**Real test output from `05-scaling-reliability/develop/app.py` (port `8008`):**

```http
$ curl -i -s http://127.0.0.1:8008/health
HTTP/1.1 200 OK
content-type: application/json

{"status":"ok","uptime_seconds":0.3,"version":"1.0.0","environment":"development","timestamp":"2026-11-25T08:06:13.138418+00:00","checks":{"memory":{"status":"ok","note":"psutil not installed"}}}

$ curl -i -s http://127.0.0.1:8008/ready
HTTP/1.1 200 OK
content-type: application/json

{"ready":true,"in_flight_requests":1}
```

---

### Exercise 5.2: Graceful Shutdown (`SIGTERM`)

When Docker or Kubernetes stops/replaces a container, it sends **`SIGTERM`** first (giving a grace window such as 30s) before escalating to uncatchable **`SIGKILL`**. In `05-scaling-reliability/develop/app.py`:
- Middleware tracks `_in_flight_requests` (`lines 72-81`).
- On `SIGTERM`, `uvicorn` (`timeout_graceful_shutdown=30`) stops accepting new connections, finishes active requests, and runs FastAPI's `lifespan` shutdown block (`lines 54-66`), which sets `_is_ready = False`, waits for `_in_flight_requests` to reach `0`, and exits cleanly with code `0`.

**Real verification (sending `POST /ask?question=hello` concurrently with `kill -TERM $PID`):**

```text
Request output during SIGTERM:
{"answer":"Tôi là AI agent được deploy lên cloud. Câu hỏi của bạn đã được nhận."}
HTTP_STATUS:200

Server logs:
INFO:     Uvicorn running on http://0.0.0.0:8008 (Press CTRL+C to quit)
INFO:     Shutting down
INFO:     127.0.0.1:50624 - "POST /ask?question=hello HTTP/1.1" 200 OK
INFO:     Waiting for application shutdown.
2026-11-25 15:06:16,273 INFO 🔄 Graceful shutdown initiated...
2026-11-25 15:06:16,273 INFO ✅ Shutdown complete
INFO:     Application shutdown complete.
INFO:     Finished server process [182933]
2026-11-25 15:06:16,273 INFO Received signal 15 — uvicorn will handle graceful shutdown
```

The in-flight request completed with **HTTP 200** instead of dropping the client connection.

---

### Exercise 5.3: Why In-Memory State Breaks Scaling (Stateless Design with Redis)

If conversation history is stored in a local Python `dict` in RAM:
1. User sends Turn 1 → Nginx routes to **Instance 1** → saved only in Instance 1's memory.
2. User sends Turn 2 → Nginx round-robins to **Instance 2** → Instance 2's memory has **no record** of Turn 1 → broken context.
3. Restarting or scaling down an instance permanently wipes every user session held in that process's RAM.

In `05-scaling-reliability/production/app.py`, all agent instances are **stateless** and externalize session state into shared **Redis** (`_redis.setex(f"session:{session_id}", 3600, json.dumps(data))` and `_redis.get(f"session:{session_id}")`), so any instance can serve any turn of any session identically.

---

### Exercise 5.4 & 5.5: Load Balancing (3 Instances + Nginx + Redis) & `test_stateless.py`

**Stack Architecture (`05-scaling-reliability/production/`):**

```text
Client (:8080)
      │
      ▼
┌───────────────────────────┐
│ Nginx Load Balancer (:80) │  (Docker DNS round-robin: agent:8000,
│ header: X-Served-By       │   proxy_next_upstream error timeout http_503)
└─────────────┬─────────────┘
      ┌───────┼───────┐
      ▼       ▼       ▼
  [agent-1] [agent-2] [agent-3]   (3 stateless replicas, port 8000)
      └───────┼───────┘
              ▼
      ┌───────────────┐
      │ Redis (:6379) │           (Shared session store: session:<uuid>, TTL 3600s)
      └───────────────┘
```

**Real commands run (`README.md` Note #7):**

```bash
cd 05-scaling-reliability/production
touch .env.local
docker compose -p k5-day14-scaling up -d --build --scale agent=3
python3 test_stateless.py
docker compose -p k5-day14-scaling down -v
rm -f .env.local
```

**Real `docker compose -p k5-day14-scaling ps` & Nginx routing verification:**

```text
NAME                       IMAGE                    SERVICE   STATUS                    PORTS
k5-day14-scaling-agent-1   k5-day14-scaling-agent   agent     Up 8 seconds (healthy)    8000/tcp
k5-day14-scaling-agent-2   k5-day14-scaling-agent   agent     Up 8 seconds (healthy)    8000/tcp
k5-day14-scaling-agent-3   k5-day14-scaling-agent   agent     Up 8 seconds (healthy)    8000/tcp
k5-day14-scaling-nginx-1   nginx:alpine             nginx     Up 8 seconds              0.0.0.0:8080->80/tcp
k5-day14-scaling-redis-1   redis:7-alpine           redis     Up 19 seconds (healthy)   6379/tcp
```

```http
$ curl -sS -i http://localhost:8080/health
HTTP/1.1 200 OK
Server: nginx/1.31.10
X-Served-By: 172.27.0.5:8000
Content-Type: application/json

{"status":"ok","instance_id":"instance-4196c3","uptime_seconds":7.6,"storage":"redis","redis_connected":true}

$ curl -sS -i http://localhost:8080/ready
HTTP/1.1 200 OK
Server: nginx/1.31.10
X-Served-By: 172.27.0.4:8000
Content-Type: application/json

{"ready":true,"instance":"instance-946d59"}
```

**Real `python3 test_stateless.py` output:**

```text
============================================================
Stateless Scaling Demo
============================================================

Session ID: f44b7abd-ff14-4579-9f11-c732371e3993

Request 1: [instance-925f6f]
  Q: What is Docker?
  A: Container là cách đóng gói app để chạy ở mọi nơi. Build once, run anywhere!...

Request 2: [instance-4196c3]
  Q: Why do we need containers?
  A: Đây là câu trả lời từ AI agent (mock). Trong production, đây sẽ là response từ O...

Request 3: [instance-946d59]
  Q: What is Kubernetes?
  A: Tôi là AI agent được deploy lên cloud. Câu hỏi của bạn đã được nhận....

Request 4: [instance-925f6f]
  Q: How does load balancing work?
  A: Đây là câu trả lời từ AI agent (mock). Trong production, đây sẽ là response từ O...

Request 5: [instance-4196c3]
  Q: What is Redis used for?
  A: Tôi là AI agent được deploy lên cloud. Câu hỏi của bạn đã được nhận....

------------------------------------------------------------
Total requests: 5
Instances used: {'instance-946d59', 'instance-925f6f', 'instance-4196c3'}
✅ All requests served despite different instances!

--- Conversation History ---
Total messages: 10
  [user]: What is Docker?...
  [assistant]: Container là cách đóng gói app để chạy ở mọi nơi. Build once...
  [user]: Why do we need containers?...
  [assistant]: Đây là câu trả lời từ AI agent (mock). Trong production, đây...
  [user]: What is Kubernetes?...
  [assistant]: Tôi là AI agent được deploy lên cloud. Câu hỏi của bạn đã đư...
  [user]: How does load balancing work?...
  [assistant]: Đây là câu trả lời từ AI agent (mock). Trong production, đây...
  [user]: What is Redis used for?...
  [assistant]: Tôi là AI agent được deploy lên cloud. Câu hỏi của bạn đã đư...

✅ Session history preserved across all instances via Redis!
```

**Explanation of why session state persisted across all 3 instances:**
Nginx distributed the 5 consecutive `POST /chat` requests across all 3 distinct containers (`instance-925f6f`, `instance-4196c3`, `instance-946d59`) using Docker's internal DNS round-robin for `agent:8000`. On every request, whichever instance received the call loaded the existing conversation from the single shared Redis container using key `session:f44b7abd-ff14-4579-9f11-c732371e3993`, appended the user question and assistant reply, and saved it back to Redis with a 3600s TTL. Consequently, when `GET /chat/{session_id}/history` ran afterward, all **10 messages (5 turns)** were intact regardless of which replica served each request.

---

## Part 6: Final Project — Production Readiness Checklist (CP5)

Executed `python check_production_ready.py` (full mode including Docker build, container run, Redis persistence across restarts, and SIGTERM graceful shutdown) inside `06-lab-complete/`:

```text
================================================================
  📋 Checklist Deliverable
     ✅ Dockerfile (multi-stage, < 500 MB)
     ✅ docker-compose.yml (agent + redis)
     ✅ .dockerignore
     ✅ Health check endpoint (GET /health)
     ✅ Readiness endpoint (GET /ready)
     ✅ API Key authentication
     ✅ Rate limiting
     ✅ Cost guard
     ✅ Config từ environment variables
     ✅ Structured logging
     ✅ Graceful shutdown
     ✅ Public URL ready (Railway / Render config)
----------------------------------------------------------------
  Result: 12/12 tiêu chí đạt · 43/43 checks passed
  🎉 PASS 12/12 — PRODUCTION READY! Deploy nào!
================================================================
```

**Measured Part 6 verification summary:**
- Multi-stage `python:3.11-slim` image with non-root user (`appuser`) and `HEALTHCHECK`: **235 MB** (`< 500 MB`), build time 33s.
- Graceful shutdown: in-flight request completed with HTTP `200` on `SIGTERM` (exit code `0`); `docker stop` exited with code `0` in **0.7s** (`< 10s`).
- Security & persistence: `401` on missing/wrong `X-API-Key`; `429` with `Retry-After` via Redis atomic Lua sliding-window surviving container restart; `402` when daily/monthly budget exceeded; 17 valid JSON log lines emitted (0 invalid).
- `06-lab-complete/check_production_ready.py` was left completely unmodified (`git diff` empty).

---

## AI Log (per `RULES.md` §1)

| Task / Section | What Was Asked / Assisted by AI | How Outputs & Code Were Verified Locally |
| :--- | :--- | :--- |
| **Parts 1–5 Code Analysis & Write-up** | Assisted in reviewing source files across `01-localhost-vs-production/` through `05-scaling-reliability/`, organizing comparison tables (`develop` vs `production`, `railway.toml` vs `render.yaml`), summarizing GCP Cloud Run `cloudbuild.yaml`/`service.yaml`, and formatting `MISSION_ANSWERS.md`. | Every line number cited was verified directly against the repo files. Every server test, `curl -i` output (401/403/200/429), Docker image build (`my-agent:develop` = 1,663.39 MB vs `my-agent:advanced` = 237.72 MB, -85.71%), Nginx Compose stack (`k5-day14-part2`), SIGTERM shutdown test, and 3-replica stateless test (`k5-day14-scaling` + `test_stateless.py`) was executed live on the local machine; zero numbers were estimated or fabricated. |
| **Part 6 (`06-lab-complete/`) Implementation** | Assisted in implementing the TODO modules (`config.py`, `redis_client.py`, `auth.py`, `rate_limiter.py`, `cost_guard.py`, `main.py`), multi-stage `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`, `railway.toml`, and `render.yaml` following the contract in `06-lab-complete/README.md`. | Verified with both `python check_production_ready.py --static-only` and full `python check_production_ready.py` (with Docker), achieving **`PASS 12/12` (43/43 checks passed)** with `check_production_ready.py` untouched and no `.env` files or secrets committed. |
| **Hygiene & Privacy Review** | Assisted in verifying secret masking, cleanup of temporary `.env.local` files and Docker stacks, and compliance with `RULES.md` (no email, phone number, or unmasked secrets/JWT tokens). | Verified via `find` (no `.env`/`.env.local` left behind), `docker ps` (no leftover lab containers), and inspection of `MISSION_ANSWERS.md` to ensure JWT tokens and demo keys are masked. |
