# GCP Setup — RAG Console Deployment (Step by Step)

This document records **exactly** how the RAG Console (FastAPI backend + ingestion
worker + React frontend) was deployed to Google Cloud, with every command, value,
and decision. It is both a record of what was done and a runbook to reproduce it.

> Environment at time of writing: a Windows machine with the Google Cloud CLI
> (`gcloud`) installed and already authenticated. Docker is **not** installed
> locally and does not need to be — all image building happens on the VM.

---

## 0. Final result (quick reference)

| Item | Value |
|------|-------|
| Live URL | https://34.139.146.167.nip.io (HTTP auto-redirects to HTTPS) |
| Health check | http://34.139.146.167/api/health |
| GCP project | `project-619b14fd-4c6b-4f0a-b60` |
| GCP account | `swaraj.bagal22@gmail.com` |
| VM name | `rag-console` |
| Zone | `us-east1-b` |
| Machine type | `e2-medium` (2 vCPU, ~4 GB RAM) |
| OS / disk | Debian 12 (bookworm), 30 GB |
| Firewall rule | `allow-http` (TCP 80, target tag `rag-console`) |
| Docker / Compose | v29.6.1 / v5.2.0 |
| Containers | `rag-console-api-1`, `rag-console-worker-1`, `rag-console-web-1` |

The whole stack runs on **one VM** via Docker Compose. Nginx serves the frontend
and reverse-proxies `/api/*` to the backend, so everything is same-origin (no CORS).

---

## 1. Architecture overview

```
                Internet
                   │  http://34.139.146.167  (port 80)
                   ▼
        ┌─────────────────────────┐   one GCP VM (rag-console, us-east1-b)
        │  web  (nginx)            │
        │   • serves the SPA       │
        │   • /api/* ─► api:8000   │
        └───────────┬─────────────┘
                    │ (internal Docker network)
        ┌───────────▼─────────────┐     ┌──────────────────────────┐
        │  api  (FastAPI/uvicorn)  │     │  worker (SQS ingestion)  │
        │   • auth, query, copilot │     │   • polls SQS forever    │
        └───────────┬─────────────┘     └────────────┬─────────────┘
                    │                                 │
                    └──────────────┬──────────────────┘
                                   ▼
   External services (unchanged, NOT hosted on GCP):
   AWS S3 · AWS SQS · AWS Bedrock · AWS RDS PostgreSQL · Pinecone · LlamaParse · Vertex AI (Gemini)
```

- **api** and **worker** run from the **same Docker image** (built from the root
  `Dockerfile`); only the start command differs.
- **web** is a separate image (built from `frontendkimchi/Dockerfile`) that builds
  the Vite bundle and serves it with nginx.

---

## 2. Files created for the deployment

These were added to the repository to make the deployment reproducible:

| File | Purpose |
|------|---------|
| `Dockerfile` | Backend image (shared by `api` and `worker`). Python 3.12-slim, installs `requirements.txt`, copies `src/` + `main.py`. |
| `.dockerignore` | Keeps the backend build context small and excludes `.env`, `.git`, `frontendkimchi`, caches, logs. |
| `frontendkimchi/Dockerfile` | Two-stage: build the SPA with Node 20, then serve via nginx. |
| `frontendkimchi/nginx.conf` | Serves the SPA, proxies `/api/` → `api:8000`, SSE-friendly, 25 MB upload cap. |
| `frontendkimchi/.dockerignore` | Excludes `node_modules`, `dist`, etc. |
| `docker-compose.yml` | Defines the three services (`api`, `worker`, `web`), reads `.env`, health-checks the API. |
| `vm_setup.sh` | One-shot VM bootstrap: swap + Docker install + unpack the app. |
| `DEPLOY.md` | Generic deployment guide. |
| `gcp_setup.md` | This file — the as-built record. |

### 2.1 How the frontend reaches the backend

The frontend reads its API base URL from `VITE_API_BASE_URL` **at build time**
(`frontendkimchi/src/api/client.ts`). The compose file builds the `web` image with
`VITE_API_BASE_URL=/api`, so the SPA calls `/api/...` on its own origin. Nginx then
strips the `/api` prefix and forwards to the backend:

```
Browser → http://34.139.146.167/api/auth/login
Nginx   → http://api:8000/auth/login   (trailing slash on proxy_pass strips /api)
```

Because the page and the API share one origin, the backend's CORS rules never
trigger — no `RAG_CORS_ALLOW_ORIGINS` change was needed.

---

## 3. Prerequisites that were verified first

Before creating anything, the following were confirmed (all already true):

```powershell
gcloud --version          # Google Cloud SDK 567.0.0  (installed)
gcloud auth list          # active account: swaraj.bagal22@gmail.com
gcloud config get-value project   # project-619b14fd-4c6b-4f0a-b60

# Billing must be enabled for the trial credit + Compute Engine + Vertex AI:
gcloud billing projects describe project-619b14fd-4c6b-4f0a-b60
#   billingEnabled: true   ✓

# Compute Engine API must be enabled:
gcloud services list --enabled --filter="config.name:compute.googleapis.com"
#   compute.googleapis.com  ✓
```

> The two normally-interactive steps — browser login (`gcloud auth login`) and
> activating the $300 trial credit — were already done, so deployment could run
> entirely from the terminal.

---

## 4. Step-by-step deployment commands

All commands were run from the local Windows terminal. Replace values if you
reproduce this in a different project/region.

### Step 1 — Firewall rule (allow HTTP)

```powershell
gcloud compute firewall-rules create allow-http `
  --allow=tcp:80 `
  --target-tags=rag-console `
  --description="Allow HTTP to the RAG console VM" `
  --project=project-619b14fd-4c6b-4f0a-b60
```

The rule applies only to VMs tagged `rag-console` (set in the next step). SSH
(port 22) is already permitted by GCP's default network rule.

### Step 2 — Create the VM

First attempt used `us-central1-a`, which returned
`ZONE_RESOURCE_POOL_EXHAUSTED` (Google temporarily had no `e2-medium` capacity
there). Several zones were tried; **`us-east1-b` succeeded**:

```powershell
gcloud compute instances create rag-console `
  --zone=us-east1-b `
  --machine-type=e2-medium `
  --image-family=debian-12 `
  --image-project=debian-cloud `
  --boot-disk-size=30GB `
  --tags=rag-console `
  --project=project-619b14fd-4c6b-4f0a-b60
```

The output reported the external IP **34.139.146.167** (ephemeral — see §7).

> If you hit `ZONE_RESOURCE_POOL_EXHAUSTED`, just try another zone:
> `us-east1-b`, `us-east1-c`, `us-west1-b`, etc.

### Step 3 — Package the app locally

A small tarball of the working tree was built on the Windows machine, excluding
heavy/irrelevant folders. This included the latest local code (even uncommitted
changes) and avoided needing GitHub credentials on the VM:

```powershell
cd c:\aaaa
tar -czf deploy.tar.gz `
  --exclude="frontendkimchi/node_modules" `
  --exclude="frontendkimchi/dist" `
  --exclude="frontendkimchi/.vite" `
  src main.py requirements.txt Dockerfile docker-compose.yml `
  .dockerignore DEPLOY.md .env frontendkimchi
# Result: deploy.tar.gz (~478 KB)
```

> The `.env` (with all secrets) is included in this tarball and copied to the VM.
> It is transferred over gcloud's encrypted SCP and lands in plaintext on the VM
> disk — acceptable for a private test box; see §6 for production hardening.

### Step 4 — Copy files to the VM

The first SCP established the SSH key (gcloud auto-generates one and pushes it to
the project metadata). Note: use a **relative** remote path (no `~/`) because the
Windows `pscp` backend does not expand `~`.

```powershell
# App bundle
gcloud compute scp c:\aaaa\deploy.tar.gz rag-console:deploy.tar.gz `
  --zone=us-east1-b --project=project-619b14fd-4c6b-4f0a-b60 --quiet

# Bootstrap script
gcloud compute scp c:\aaaa\vm_setup.sh rag-console:vm_setup.sh `
  --zone=us-east1-b --project=project-619b14fd-4c6b-4f0a-b60 --quiet
```

### Step 5 — Bootstrap the VM (swap + Docker + unpack)

`vm_setup.sh` does three things: adds a 2 GB swap file, installs Docker + the
Compose plugin from Docker's official apt repo, and unpacks the tarball into
`~/rag-console`. It was run remotely (the `sed` strips Windows CR line endings so
bash doesn't choke):

```powershell
gcloud compute ssh rag-console --zone=us-east1-b `
  --project=project-619b14fd-4c6b-4f0a-b60 `
  --command="sed -i 's/\r$//' vm_setup.sh && bash vm_setup.sh" --quiet
```

Result: `Docker version 29.6.1`, `Docker Compose version v5.2.0`, app unpacked.

What `vm_setup.sh` runs on the VM (summary):

```bash
# 2 GB swap (build headroom on a 4 GB box)
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Docker from the official repo
sudo apt-get update && sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update && sudo apt-get install -y \
  docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"

# Unpack app
mkdir -p ~/rag-console && tar -xzf ~/deploy.tar.gz -C ~/rag-console
```

### Step 6 — Build images and start the stack

```powershell
gcloud compute ssh rag-console --zone=us-east1-b `
  --project=project-619b14fd-4c6b-4f0a-b60 `
  --command="cd ~/rag-console && sudo docker compose up -d --build" --quiet
```

This took a few minutes: it installed all Python dependencies (FastAPI,
llama-index, pinecone, boto3, etc.), built the frontend bundle, and produced three
images — `rag-console-api`, `rag-console-worker`, `rag-console-web` — then started
all three containers.

> `sudo` is used for Docker because the current SSH session predates the
> `usermod -aG docker` group change. After reconnecting (new SSH session) you can
> drop `sudo`.

### Step 7 — Verify

```powershell
gcloud compute ssh rag-console --zone=us-east1-b `
  --project=project-619b14fd-4c6b-4f0a-b60 `
  --command="cd ~/rag-console && sudo docker compose ps && curl -fsS http://localhost/api/health" --quiet
```

Observed:
- `api` → `Up (healthy)`; logs show "Applied auth schema", "Applied observability
  schema", "Application startup complete" (i.e. it reached the RDS database).
- `worker` → `Up`; logs show "SqsIngestionQueue initialised" and "Ingestion worker
  started".
- `web` → `Up`, publishing `0.0.0.0:80->80`.
- `GET /api/health` → `{"status":"ok"}`.

External reachability was confirmed from the local machine:

```powershell
Invoke-WebRequest http://34.139.146.167/api/health   # HTTP 200 {"status":"ok"}
Invoke-WebRequest http://34.139.146.167/             # HTTP 200 (SPA index.html)
```

---

## 5. Day-to-day operations

Connect to the VM:

```powershell
gcloud compute ssh rag-console --zone=us-east1-b --project=project-619b14fd-4c6b-4f0a-b60
```

Inside the VM, from `~/rag-console`:

```bash
sudo docker compose ps              # status of the 3 containers
sudo docker compose logs -f         # live logs (all services)
sudo docker compose logs -f api     # one service
sudo docker compose restart         # restart everything
sudo docker compose down            # stop & remove containers (images kept)
sudo docker compose up -d --build   # rebuild + restart after a code/.env change
```

### Updating configuration (e.g. editing `.env`)

```bash
cd ~/rag-console
nano .env                           # edit values
sudo docker compose up -d --build   # apply (rebuild not strictly needed for .env, but safe)
```

### Deploying new code

Either re-run the tarball steps (§3–§4) from your machine, then
`sudo docker compose up -d --build` on the VM; or set up `git` on the VM and pull
from `github.com/Swaraj9822/ragDMScopilot.git` (requires a credential for a
private repo).

### Saving trial credit when idle

Stopping the VM halts compute billing (disk is still kept):

```powershell
gcloud compute instances stop  rag-console --zone=us-east1-b
gcloud compute instances start rag-console --zone=us-east1-b
```

---

## 6. Security notes (read before real production)

- **HTTP only / no TLS.** Traffic — including JWT tokens — is unencrypted over the
  public IP. Fine for short credit-funded testing; add a domain + HTTPS before real
  users (see §8).
- **Secrets on disk.** `.env` sits in plaintext in `~/rag-console/.env` on the VM.
  For production, use **GCP Secret Manager** and inject at runtime, and rotate any
  credentials that have lived in the repo workspace.
- **Open port.** Only port 80 is exposed publicly; the API (8000) is reachable only
  on the internal Docker network, not from the internet.
- **Cross-cloud.** The data/AI plane (S3, SQS, Bedrock, RDS) is on AWS, so calls
  leave GCP. Expect a little extra latency and AWS egress.

---

## 7. Cost

- `e2-medium` running 24/7 ≈ **$25–35/month-equivalent**, fully covered by the
  **$300 / 90-day trial credit**. Stopping the VM between tests stretches the credit
  further.
- This covers **only** the GCP VM. AWS, Pinecone, LlamaParse, and Vertex AI usage
  bill separately.

---

## 8. Next steps (optional)

1. **Static IP** — the current IP (34.139.146.167) is ephemeral and may change on
   stop/start. Reserve a static one:
   ```powershell
   gcloud compute addresses create rag-ip --region=us-east1
   # then attach it to the instance's access config
   ```
2. **Domain + HTTPS** — point a domain at the (static) IP and front the stack with
   **Caddy** (automatic Let's Encrypt certificates) or add certbot to nginx.
3. **Secret Manager** — move `.env` values out of plaintext.
4. **Backups / monitoring** — enable VM snapshots and Cloud Monitoring alerts once
   this moves beyond testing.

---

## Appendix — Troubleshooting notes from this deployment

- **`ZONE_RESOURCE_POOL_EXHAUSTED`** when creating the VM in `us-central1-*`:
  Google had no `e2-medium` capacity in those zones at that moment. Fixed by
  creating in `us-east1-b`.
- **`pscp: unable to open ~/deploy.tar.gz`**: the Windows `pscp` backend doesn't
  expand `~`. Fixed by using a relative remote path (`rag-console:deploy.tar.gz`).
- **CRLF in shell scripts**: scripts authored on Windows carry `\r`; run
  `sed -i 's/\r$//' script.sh` before `bash script.sh`.
- **`sudo` needed for Docker**: the SSH session that ran the build predated the
  docker-group membership change; reconnecting removes the need for `sudo`.

---

## Error 1 Fix — Vertex AI (Gemini) `403 ACCESS_TOKEN_SCOPE_INSUFFICIENT`

### Symptom

Running a query in the UI failed. The correlated logs showed the request reaching
the backend and starting query classification, then repeatedly failing on the
Gemini (Vertex AI) call before giving up:

```
WARNING rag_system.retry  Retrying ...BedrockQueryClassifier.classify ... raised
  ClientError: 403 PERMISSION_DENIED. {'error': {'code': 403,
  'message': 'Request had insufficient authentication scopes.',
  'status': 'PERMISSION_DENIED',
  'details': [{'reason': 'ACCESS_TOKEN_SCOPE_INSUFFICIENT', ...
  'method': 'google.cloud.aiplatform.v1beta1.PredictionService.GenerateContent',
  'service': 'aiplatform.googleapis.com'}]}}
ERROR   rag_system.router  query classification failed after 3194ms
ERROR   rag_system.api     Streaming query failed
```

### Root cause

The app authenticates to Vertex AI using **Application Default Credentials**, which
on a GCP VM means the **attached service account identity** (the `.env` value
`GOOGLE_APPLICATION_CREDENTIALS` is empty, so no key file is used). The VM had been
created with the **default, restricted access scopes**, which did **not** include
Vertex AI. So tokens minted by the VM metadata server were authenticated but lacked
the scope to call `aiplatform.googleapis.com` → `403 ACCESS_TOKEN_SCOPE_INSUFFICIENT`.

This is unrelated to the user's personal `gcloud` / email login.

The VM's service account and its original (insufficient) scopes were:

```
service account: 744448677871-compute@developer.gserviceaccount.com
scopes:
  devstorage.read_only, logging.write, monitoring.write, pubsub,
  service.management.readonly, servicecontrol, trace.append
  (no cloud-platform / aiplatform)
```

### Fix

Two things were required: the service account needed the **Vertex AI User** IAM
role, and the VM needed the **`cloud-platform`** access scope. Because changing a
VM's scopes requires the instance to be stopped (which would normally change the
ephemeral IP), the current IP was first reserved as static.

```powershell
# 1. Grant the Vertex AI User role to the VM's service account (no downtime).
gcloud projects add-iam-policy-binding project-619b14fd-4c6b-4f0a-b60 `
  --member="serviceAccount:744448677871-compute@developer.gserviceaccount.com" `
  --role="roles/aiplatform.user" `
  --condition=None

# 2. Reserve the current external IP as static so it survives the restart.
gcloud compute addresses create rag-ip `
  --addresses=34.139.146.167 `
  --region=us-east1 `
  --project=project-619b14fd-4c6b-4f0a-b60

# 3. Stop the VM (scope changes require a stopped instance).
gcloud compute instances stop rag-console `
  --zone=us-east1-b --project=project-619b14fd-4c6b-4f0a-b60

# 4. Widen the access scope to cloud-platform.
gcloud compute instances set-service-account rag-console `
  --zone=us-east1-b --project=project-619b14fd-4c6b-4f0a-b60 `
  --service-account=744448677871-compute@developer.gserviceaccount.com `
  --scopes=cloud-platform

# 5. Start the VM again (came back on the same static IP 34.139.146.167).
gcloud compute instances start rag-console `
  --zone=us-east1-b --project=project-619b14fd-4c6b-4f0a-b60
```

After restart, the scope was confirmed:

```powershell
gcloud compute instances describe rag-console --zone=us-east1-b `
  --project=project-619b14fd-4c6b-4f0a-b60 `
  --format="value(serviceAccounts.scopes)"
# → ['https://www.googleapis.com/auth/cloud-platform']
```

The containers restarted automatically (`restart: unless-stopped`).

### Verification

A real Vertex AI call was run from inside the `api` container (rather than assuming
success). A small script was copied in and executed:

```python
# test_vertex.py
from google import genai
c = genai.Client(vertexai=True,
                 project="project-619b14fd-4c6b-4f0a-b60",
                 location="global")
r = c.models.generate_content(model="gemini-3.5-flash",
                              contents="reply with the single word: pong")
print("VERTEX_OK:", (r.text or "").strip()[:80])
```

```powershell
gcloud compute scp c:\aaaa\test_vertex.py rag-console:rag-console/test_vertex.py `
  --zone=us-east1-b --project=project-619b14fd-4c6b-4f0a-b60 --quiet

gcloud compute ssh rag-console --zone=us-east1-b `
  --project=project-619b14fd-4c6b-4f0a-b60 `
  --command="cd ~/rag-console && sudo docker compose cp test_vertex.py api:/tmp/test_vertex.py && sudo docker compose exec -T api python /tmp/test_vertex.py"
# → VERTEX_OK: pong
```

Result: `VERTEX_OK: pong` — Vertex AI permission works and the `gemini-3.5-flash`
model responds. The temporary `test_vertex.py` was then removed from the host,
the container, and the local machine.

### Side effects / notes

- **The external IP is now static** (`34.139.146.167`, reserved as `rag-ip` in
  `us-east1`). A reserved static IP is free while attached to a running VM but
  incurs a small charge if left reserved while the VM is stopped/deleted.
- **No code or `.env` change was needed** — this was purely a GCP IAM + VM scope
  configuration fix.

### Unrelated warning still open

The logs also show `Could not load copilot schema catalog for router ... tables=0`.
This does not affect document/RAG answers but may cause database-backed (SQL
copilot) questions to return little or nothing. Tracked separately from Error 1.

---

## Error 2 Fix — Database copilot `Copilot schema catalog not found`

### Symptom

After Error 1 was fixed, a database-style question ("which product sold the most")
classified correctly to the `database` route, then failed during SQL generation.
The span inspector showed:

```
Root_Span  error
exception.type:    FileNotFoundError
exception.message: Copilot schema catalog not found at
                   /app/config/copilot_schema_catalog.json.
                   Create it from config/copilot_schema_catalog.example.json.
```

Correlated logs also showed the recurring warning:

```
WARNING rag_system.router  Could not load copilot schema catalog for router
INFO    rag_system.router  AgenticRouter initialised (copilot=available, tables=0)
...
ERROR   rag_system.copilot copilot SQL generation failed after 40ms
```

### Root cause

The database copilot loads a schema catalog (table/column descriptions + business
rules) that tells the LLM how to write SQL. The path is resolved in
`src/rag_system/copilot.py::load_schema_catalog` relative to the app root, i.e.
`/app/config/copilot_schema_catalog.json` inside the container.

The real catalog **exists locally** (`config/copilot_schema_catalog.json`, ~61 KB,
10 tables), **but it was never shipped to the VM**:

- The original deployment tarball only packaged `src main.py requirements.txt ...`
  — the `config/` folder was omitted.
- The backend `Dockerfile` only had `COPY src ./src` and `COPY main.py ./` — it
  never copied `config/` into the image.

So the file was absent at `/app/config/`, and `tables=0` meant the copilot had no
schema to generate SQL from.

### Fix

**1. Dockerfile** — added a line so the catalog ships in the backend image
(`Dockerfile`, applies to both `api` and `worker`):

```dockerfile
COPY src ./src
COPY config ./config      # <-- added
COPY main.py ./
```

**2. Redeploy** — repackaged the app *including* `config/`, copied it up, extracted
over the existing project dir, and rebuilt the images:

```powershell
# Local: rebuild the bundle with config/ included.
cd c:\aaaa
tar -czf deploy.tar.gz `
  --exclude="frontendkimchi/node_modules" `
  --exclude="frontendkimchi/dist" `
  --exclude="frontendkimchi/.vite" `
  src config main.py requirements.txt Dockerfile docker-compose.yml `
  .dockerignore DEPLOY.md .env frontendkimchi

gcloud compute scp c:\aaaa\deploy.tar.gz rag-console:deploy.tar.gz `
  --zone=us-east1-b --project=project-619b14fd-4c6b-4f0a-b60 --quiet
```

```powershell
# On the VM: unpack and rebuild.
gcloud compute ssh rag-console --zone=us-east1-b `
  --project=project-619b14fd-4c6b-4f0a-b60 `
  --command="tar -xzf ~/deploy.tar.gz -C ~/rag-console && cd ~/rag-console && sudo docker compose up -d --build"
```

### Verification

Confirmed the file is present in the container and that the app's own loader parses
it and sees the tables (run via a throwaway `load_check.py`, since removed):

```
$ sudo docker compose exec -T api ls -la /app/config
-rw-r--r-- 1 root root  1554 ... copilot_schema_catalog.example.json
-rw-r--r-- 1 root root 61073 ... copilot_schema_catalog.json

$ sudo docker compose exec -T api python /tmp/load_check.py
CATALOG_OK tables= 10
```

`tables= 10` (was `0`) — the copilot now has the full schema catalog and can
generate SQL for database questions.

### Notes

- This is now permanent: because the `Dockerfile` copies `config/`, every future
  rebuild includes the catalog automatically.
- If the database schema changes, update `config/copilot_schema_catalog.json`
  locally, redeploy (tarball → scp → `docker compose up -d --build`), and the new
  catalog ships with the image.
- The catalog path is overridable via the `COPILOT_SCHEMA_CATALOG_PATH` env var
  (defaults to `config/copilot_schema_catalog.json`).

---

## HTTPS Setup (Caddy + free nip.io hostname)

Added trusted HTTPS without buying a domain. Certificate authorities won't issue
certs for a bare IP, so a hostname is required — here we use **nip.io**, a free
wildcard-DNS service where `34.139.146.167.nip.io` automatically resolves to the
VM's static IP. **Caddy** sits in front of nginx, terminates TLS, auto-obtains and
auto-renews a Let's Encrypt certificate, and redirects HTTP → HTTPS.

New public URL: **https://34.139.146.167.nip.io**

### Architecture change

```
Before:  Internet :80 ─► web (nginx) ─► api
After:   Internet :443/:80 ─► caddy (TLS) ─► web (nginx) ─► api
```

The `web` (nginx) container no longer publishes a host port; only Caddy is exposed
to the internet (80 + 443), and it reaches nginx over the internal Docker network.

### Files

**`Caddyfile`** (repo root):

```
34.139.146.167.nip.io {
	reverse_proxy web:80
}
```

**`docker-compose.yml`** changes:
- `web`: removed the `ports: ["80:80"]` mapping (replaced with `expose: ["80"]`).
- Added a `caddy` service (image `caddy:2-alpine`) publishing `80`, `443`, and
  `443/udp` (HTTP/3), mounting the `Caddyfile` and two named volumes:
  - `caddy_data:/data` — **persists issued certificates** across restarts (avoids
    re-issuing and hitting Let's Encrypt rate limits).
  - `caddy_config:/config`.

### Steps performed

```powershell
# 1. Open HTTPS in the firewall (note the quotes so PowerShell keeps the comma).
gcloud compute firewall-rules create allow-https `
  --allow="tcp:443,udp:443" `
  --target-tags=rag-console `
  --description="Allow HTTPS (and HTTP/3) to the RAG console VM" `
  --project=project-619b14fd-4c6b-4f0a-b60

# 2. Copy the new Caddyfile + docker-compose.yml to the VM.
gcloud compute scp c:\aaaa\docker-compose.yml c:\aaaa\Caddyfile `
  rag-console:rag-console/ `
  --zone=us-east1-b --project=project-619b14fd-4c6b-4f0a-b60 --quiet

# 3. Bring the stack up (pulls Caddy, recreates web, starts caddy).
gcloud compute ssh rag-console --zone=us-east1-b `
  --project=project-619b14fd-4c6b-4f0a-b60 `
  --command="cd ~/rag-console && sudo docker compose up -d"
```

Caddy solved the ACME `tls-alpn-01` challenge and logged
`certificate obtained successfully` for `34.139.146.167.nip.io`.

### Verification

```powershell
Invoke-WebRequest https://34.139.146.167.nip.io/api/health   # 200 {"status":"ok"}, trusted cert
Invoke-WebRequest https://34.139.146.167.nip.io/             # 200 (SPA)
Invoke-WebRequest http://34.139.146.167.nip.io/ -MaximumRedirection 0   # 308 -> https
```

No frontend rebuild was needed: the SPA calls the API via the relative `/api`
path, so it inherits whatever scheme the page is served over.

### Switching to a real custom domain later

1. Buy a domain and create a DNS **A record** pointing at `34.139.146.167`.
2. Replace the hostname in `Caddyfile` with the domain.
3. `gcloud compute scp` the Caddyfile up and run
   `sudo docker compose restart caddy` — Caddy issues a fresh cert for the new name.

### Notes

- **Auto-renewal:** Caddy renews certificates automatically well before expiry; no
  cron or manual step. Certs survive restarts via the `caddy_data` volume.
- **Static IP dependency:** the nip.io hostname is derived from the IP. The IP is
  reserved static (`rag-ip`), so it remains valid across VM stop/start.
- Port 80 stays open because Caddy uses it for the HTTP→HTTPS redirect and as a
  fallback ACME challenge path.
