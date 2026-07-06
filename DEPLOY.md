# Deploying to GCP (trial-credit test environment)

This deploys the whole stack to a **single Compute Engine VM** using Docker
Compose:

- `api` — FastAPI/uvicorn (the HTTP backend)
- `worker` — the Pub/Sub ingestion worker (`python -m rag_system.worker`)
- `web` — nginx serving the built frontend and reverse-proxying `/api/*` to `api`

Because nginx serves the frontend and the API from the **same origin**, there's
no CORS to configure and no second domain to manage.

> Scope: this targets the **$300 / 90-day free trial credit** for testing. It runs
> over plain HTTP on the VM's public IP. Before a real production launch, add a
> domain + HTTPS (see the last section) and move secrets out of `.env` into
> Secret Manager.

---

## 0. Prerequisites

- A GCP account with the trial credit activated and **billing enabled** on a project
  (Vertex AI calls require billing on; the credit covers it).
- The `gcloud` CLI installed locally, or use the Cloud Shell in the browser.
- Your `.env` file with all the Pinecone / LlamaParse / GCP / Vertex AI / DB values
  filled in. **Do not commit it.**

Set a few shell variables (adjust as you like):

```bash
export PROJECT_ID="your-project-id"
export ZONE="us-central1-a"
export VM_NAME="rag-console"
gcloud config set project "$PROJECT_ID"
```

---

## 1. Create a firewall rule for HTTP

```bash
gcloud compute firewall-rules create allow-http \
  --allow=tcp:80 \
  --target-tags=rag-console \
  --description="Allow HTTP to the RAG console VM"
```

(SSH on tcp:22 is allowed by GCP's `default-allow-ssh` rule on the default network.)

---

## 2. Create the VM

`e2-small` (2 GB RAM) is the practical minimum since two Python processes load
`llama-index` + `pinecone`. If ingestion or queries feel memory-starved, resize
to `e2-medium` (4 GB) — the trial credit easily covers it.

```bash
gcloud compute instances create "$VM_NAME" \
  --zone="$ZONE" \
  --machine-type=e2-small \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --boot-disk-size=30GB \
  --tags=rag-console
```

Grab the external IP:

```bash
gcloud compute instances describe "$VM_NAME" --zone="$ZONE" \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
```

---

## 3. Install Docker on the VM

SSH in:

```bash
gcloud compute ssh "$VM_NAME" --zone="$ZONE"
```

Then, on the VM:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Run docker without sudo (log out / back in for it to take effect).
sudo usermod -aG docker "$USER"
```

(Optional but recommended on `e2-small` — add a 2 GB swap file so a memory spike
during ingestion doesn't OOM-kill a container.)

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

## 4. Get the code and config onto the VM

Clone the repo (or `scp` it up):

```bash
git clone <your-repo-url> rag-console
cd rag-console
```

Create the `.env` file on the VM. **Do not commit it** — paste/scp it directly:

```bash
nano .env   # paste your real values, save
```

If your code isn't in a remote repo yet, copy it from your machine instead:

```bash
# from your local machine
gcloud compute scp --recurse --zone="$ZONE" \
  ./ "$VM_NAME":~/rag-console \
  --compress
```

---

## 5. Build and start

On the VM, in the project directory:

```bash
docker compose up -d --build
```

Check status and logs:

```bash
docker compose ps
docker compose logs -f api      # watch the API boot (auth + observability schema)
docker compose logs -f worker   # confirm "Ingestion worker started"
```

Open `http://EXTERNAL_IP` in your browser and log in.

---

## 6. Day-to-day

```bash
docker compose logs -f            # all services
docker compose restart api        # restart one service
docker compose down               # stop everything
git pull && docker compose up -d --build   # deploy an update

# Stop billing for compute while not testing (keeps the disk):
gcloud compute instances stop "$VM_NAME" --zone="$ZONE"
gcloud compute instances start "$VM_NAME" --zone="$ZONE"   # note: external IP may change
```

---

## 7. Notes & before real production

- **HTTP only:** JWTs travel in plaintext over the public IP here. Fine for a
  short test, not for production. To add HTTPS, point a domain at the VM's IP
  (reserve a static IP first) and put **Caddy** in front (automatic Let's Encrypt)
  or run certbot with nginx.
- **Secrets:** `.env` lives on the VM in plaintext. For production, move values to
  **GCP Secret Manager** and inject them, and rotate any credentials that have
  been sitting in the repo workspace.
- **External SaaS:** Pinecone, LlamaParse, and the Neon Postgres DB are external
  services, so those calls leave GCP. Works fine; just expect some extra latency
  and egress.
- **Static IP:** a stopped/started VM gets a new ephemeral IP. Reserve a static
  external IP if you want a stable address:
  `gcloud compute addresses create rag-ip --region=<region>`.
