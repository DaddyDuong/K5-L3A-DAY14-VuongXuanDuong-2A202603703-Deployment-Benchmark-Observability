# Deployment Information

## Public URL

`https://production-ai-agent-yvnx.onrender.com`

## Platform

**Render** (Free tier — Region: **Singapore**, Docker Web Service + Free Render Key Value / Valkey 8 `agent-redis`)

- Blueprint config: [06-lab-complete/render.yaml](06-lab-complete/render.yaml)
- Dockerfile: [06-lab-complete/Dockerfile](06-lab-complete/Dockerfile) (multi-stage `python:3.11-slim`, non-root `appuser`, `235 MB`)

## How to Deploy on Render (Free Tier)

### Option A — Render Blueprint (recommended)
1. Push this repository to GitHub (Public).
2. In Render Dashboard, click **New → Blueprint** and connect this GitHub repo.
3. Point Render to `06-lab-complete/render.yaml` (or create the two services manually via Option B if Render only auto-detects root `render.yaml`).
4. Review the two services:
   - `production-ai-agent` (`type: web`, `runtime: docker`, `rootDir: 06-lab-complete`, `plan: free`, `healthCheckPath: /health`)
   - `agent-redis` (`type: keyvalue`, `plan: free`, `ipAllowList: []`)
5. Click **Apply / Deploy**. Render auto-generates `AGENT_API_KEY` (`generateValue: true`) and wires `REDIS_URL` automatically from `agent-redis`.
6. Open `production-ai-agent` → **Environment** to copy the generated `AGENT_API_KEY` for local verification (never commit this key to git).

### Option B — Manual Render Dashboard Setup
1. **New → Key Value** → Name: `agent-redis`, Plan: **Free** → copy the **Internal Key Value URL** (`redis://red-...:6379`).
2. **New → Web Service** → connect this repo:
   - **Language / Runtime:** Docker
   - **Root Directory:** `06-lab-complete`
   - **Dockerfile Path:** `./Dockerfile`
   - **Instance Type:** Free
   - **Health Check Path:** `/health`
3. Add Environment Variables under **Environment** (see table below) and click **Create Web Service**.

---

## Test Commands (CP6 Verification)

Set your deployed URL and secret API key in your terminal (do **not** commit the key):

```bash
export BASE="https://production-ai-agent-yvnx.onrender.com"
export AGENT_API_KEY="<YOUR-AGENT-API-KEY-FROM-RENDER-DASHBOARD>"
```

### 1. CP6 4-Command Verification (verified live: `200 / 200 / 401 / 200`)

| Check | Endpoint | Verified HTTP Status | Result |
|---|---|---|---|
| Liveness | `GET /health` | `200` | `{"status":"healthy","environment":"production"}` |
| Readiness | `GET /ready` | `200` | `{"status":"ready","redis":"connected"}` |
| Auth (no key) | `POST /ask` (missing `X-API-Key`) | `401` | `{"error":"http_error","detail":"Invalid or missing API key"}` |
| Auth (valid key) | `POST /ask` (with `X-API-Key`) | `200` | Authenticated response confirmed (see `screenshots/test.png`) |

```bash
curl -sS -o /dev/null -w '%{http_code}\n' "$BASE/health"
# Verified: 200

curl -sS -o /dev/null -w '%{http_code}\n' "$BASE/ready"
# Verified: 200 (confirms Valkey 8 / Redis connection is healthy)

curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE/ask" \
  -H 'Content-Type: application/json' \
  -d '{"question":"Hello"}'
# Verified: 401 (missing X-API-Key)

curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE/ask" \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $AGENT_API_KEY" \
  -d '{"question":"Hello"}'
# Verified: 200
```

### 2. Full JSON Response Examples

```bash
# Health check
curl -s "$BASE/health"
# Expected: {"status":"healthy","environment":"production"}

# Readiness check
curl -s "$BASE/ready"
# Expected: {"status":"ready","redis":"connected"}

# Authenticated ask
curl -s -X POST "$BASE/ask" \
  -H "X-API-Key: $AGENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "test", "question": "What is deployment?"}'
# Expected: 200 JSON with question, answer, user_id, cost_usd
```

### 3. Production Readiness Checker against Public URL

```bash
cd 06-lab-complete
python check_production_ready.py --public-url "$BASE"
```

---

## Environment Variables Set

All secrets are configured exclusively in the Render Dashboard / Blueprint (`generateValue: true` or `fromService`) — **zero secrets are committed to this repository**.

| Variable | Source / Value on Render | Purpose |
|----------|--------------------------|---------|
| `PORT` | Managed automatically by Render | Port bound by `uvicorn --port ${PORT:-8000}` |
| `ENVIRONMENT` | `production` | Runtime environment tag in `/health` and JSON logs |
| `LOG_LEVEL` | `INFO` | Structured JSON logging level |
| `AGENT_API_KEY` | Auto-generated secret (`generateValue: true`) | Verified against `X-API-Key` header (`401` on mismatch) |
| `REDIS_URL` | Wired via `fromService` (`agent-redis` `connectionString`) | Shared Redis for `/ready`, rate limiting, and cost guard |
| `RATE_LIMIT_PER_MINUTE` | `60` | Sliding-window request limit per API key (`429` + `Retry-After`) |
| `DAILY_BUDGET_USD` | `5.0` | Daily token-cost cap (`402` when exceeded) |
| `MONTHLY_BUDGET_USD` | `50.0` | Monthly token-cost cap (`402` when exceeded) |

---

## Screenshots

Verified Render deployment screenshots in `screenshots/`:

- [Deployment dashboard](screenshots/dashboard.png) — Render dashboard showing `production-ai-agent` (Live) and `agent-redis` (Available)
- [Service running](screenshots/running.png) — Render service logs / live URL health view
- [Test results](screenshots/test.png) — Terminal output of the 4 CP6 `curl` commands (`200`, `200`, `401`, `200`, zero key exposed)
