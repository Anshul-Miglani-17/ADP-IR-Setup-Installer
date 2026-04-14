# ZADP & IR Setup - Web UI

A web interface for installing and configuring ZADP or Incident Receiver (IR) on remote servers.

## Quick Start

```bash
# Install dependencies
pip3 install -r requirements.txt

# Run the app
python3 app.py
```

Open **http://localhost:5000** in your browser.

## Hosting on Apache

### Option 1: Reverse Proxy (Recommended)

1. Run the Flask app with gunicorn:

```bash
gunicorn --workers 1 --threads 200 --bind 127.0.0.1:5000 app:app
```

> **Why 1 worker?** Job state (SSH connections, logs) is stored in-memory per process.
> A single worker with 200 threads handles 100+ concurrent users easily since SSH
> connections are I/O-bound (threads spend most time waiting on network).

2. Add Apache reverse proxy config (hosting at `/setup-adp-ir/`):

```bash
# RHEL/CentOS
sudo vi /etc/httpd/conf.d/setup-ui.conf
# Debian/Ubuntu
sudo vi /etc/apache2/sites-available/setup-ui.conf
```

```apache
<VirtualHost *:80>
    ServerName 10.66.45.93

    ProxyPreserveHost On
    ProxyTimeout 600
    SetEnv proxy-sendchunked 1

    ProxyPass /setup-adp-ir/ http://127.0.0.1:5000/
    ProxyPassReverse /setup-adp-ir/ http://127.0.0.1:5000/
</VirtualHost>
```

> Apache strips `/setup-adp-ir/` before forwarding to Flask. The `BASE_PATH`
> env var (see systemd below) tells the frontend to prefix API calls correctly.

3. Enable required Apache modules:

```bash
# RHEL/CentOS
sudo yum install mod_proxy mod_proxy_http
# or on Debian/Ubuntu
sudo a2enmod proxy proxy_http
```

4. Restart Apache:

```bash
sudo apachectl configtest        # verify config is valid first
sudo systemctl restart httpd     # RHEL/CentOS
sudo systemctl restart apache2   # Debian/Ubuntu
```

### Option 2: Run with systemd (auto-start on boot)

Create `/etc/systemd/system/setup-ui.service`:

```ini
[Unit]
Description=ZADP & IR Setup Web UI
After=network.target

[Service]
User=yourusername
WorkingDirectory=/path/to/setup-ui
Environment="BASE_PATH=/setup-adp-ir"
ExecStart=/usr/bin/gunicorn --workers 1 --threads 200 --bind 127.0.0.1:5000 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable setup-ui
sudo systemctl start setup-ui
```

## Concurrency (100+ users)

- Uses **1 gunicorn worker + 200 threads** — each user gets their own thread.
- SSH connections are I/O-bound so threads handle this perfectly.
- Each user's setup runs independently with its own SSH session.
- If you need even more users, switch to gevent: `gunicorn --worker-class gevent --workers 4 --bind 0.0.0.0:5000 app:app`

## Features

- **Real-time log streaming** via SSE (Server-Sent Events).
- **Refresh-safe**: If you refresh the browser, it automatically reconnects to the running job and replays all past output.
- **Abort button**: Stop a running setup at any time — closes the SSH connection immediately.
- **Step progress tracker**: Visual indicators show which setup step is currently running.
- Passwords are kept in memory only during the setup job and never written to disk.

## Contact

For any queries: amiglani@zscaler.com
