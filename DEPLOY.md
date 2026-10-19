# Deployment Guide for Timely CRM Backend

This backend runs on FastAPI and connects directly to **MongoDB Atlas (`crmDB`)** for CRM data and **`attendanceDB`** for federated directory single sign-on (SSO). It has **zero SQLite dependencies**.

---

## Prerequisites

- **Python 3.12+** OR **Docker & Docker Compose**
- Network access to MongoDB Atlas (`cluster0.hrgeeif.mongodb.net`)
- Domain reverse proxy (Nginx, Traefik, Caddy, Cloudflare, etc.) routing to port `8010`

---

## 1. Quick Deploy with Docker (Recommended)

1. Clone or pull the repository on your server:
   ```bash
   git clone https://github.com/Manideep0704/CRMbackend.git
   cd CRMbackend
   ```

2. (Optional) Create or edit `.env`:
   ```bash
   cp .env.example .env
   ```

3. Build and run:
   ```bash
   docker compose up -d --build
   ```

4. Verify health:
   ```bash
   curl http://127.0.0.1:8010/health
   ```
   Expected response:
   ```json
   {"status":"ok","service":"Timely CRM","database":"MongoDB Atlas (crmDB)","attendance_directory":{"configured":true,"connected":true}}
   ```

---

## 2. Deploy with Systemd or PM2 (Bare Metal / VPS)

1. Pull latest code and remove any legacy SQLite files:
   ```bash
   cd /path/to/CRMbackend
   git pull origin main
   rm -f timely.db
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Ensure initial database records:
   ```bash
   python init_db.py
   ```

4. Start service:
   - **With PM2**:
     ```bash
     pm2 start "python -m uvicorn app.main:app --host 0.0.0.0 --port 8010 --workers 2 --proxy-headers --forwarded-allow-ips='*'" --name crmbackend
     pm2 save
     ```
   - **With Systemd**:
     Create `/etc/systemd/system/crmbackend.service`:
     ```ini
     [Unit]
     Description=Timely CRM Backend
     After=network.target

     [Service]
     User=www-data
     WorkingDirectory=/path/to/CRMbackend
     ExecStart=/usr/bin/python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8010 --workers 2 --proxy-headers --forwarded-allow-ips='*'
     Restart=always
     RestartSec=5
     EnvironmentFile=/path/to/CRMbackend/.env

     [Install]
     WantedBy=multi-user.target
     ```
     Enable and restart:
     ```bash
     sudo systemctl daemon-reload
     sudo systemctl enable crmbackend
     sudo systemctl restart crmbackend
     ```

---

## Default Credentials

- **Organization Slug**: `timely-health`
- **Admin Email**: `admin@timelyhealth.in`
- **Admin Password**: `TimelyAdmin@2026`
- **Attendance Directory SSO**: Employees and admins registered in `attendanceDB` can sign in directly using their attendance credentials.
- **Website Key**: `tly_X6vBwOKDgBXupQlwFYnkWcOci5gtIlvQT52w_bboJRc`
- **Public Lead Ingest Endpoint**: `POST https://crmbackend.iryax.com/api/v1/public/leads`
  - Header: `X-API-Key: tly_X6vBwOKDgBXupQlwFYnkWcOci5gtIlvQT52w_bboJRc`
  - Header: `Idempotency-Key: <unique-submission-id>`
