"""
ZADP & IR Setup - Web UI
--------------------------
Flask app that serves the setup UI and streams
real-time output via Server-Sent Events (SSE).

Concurrency: Use gunicorn with 1 worker + many threads.
SSH connections are I/O-bound so threads handle 100+ users fine.
  gunicorn --workers 1 --threads 200 --bind 0.0.0.0:5000 app:app
"""

import json
import os
import threading
import time
import uuid

from flask import (Flask, render_template, request, jsonify, Response,
                   send_from_directory)

from runner import SSHHelper, AbortedError, run_zadp_setup, run_ir_setup

app = Flask(__name__)

# Subpath prefix when hosted behind a reverse proxy (e.g. /setup-adp-ir)
# Set via environment variable: export BASE_PATH=/setup-adp-ir
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")

# Temp directory for uploaded files
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# In-memory job store: job_id -> Job
jobs = {}


class Job:
    """Represents a running setup job with log history and abort support.

    Uses an index-based pub/sub model so multiple SSE connections
    (tabs, refreshes) can independently read from the same history.
    """

    def __init__(self, job_id, service, config):
        self.id = job_id
        self.service = service
        self.config = config
        self.history = []           # full log history (append-only)
        self._cond = threading.Condition()  # notifies all waiting SSE readers
        self.status = "running"     # running | success | failed | error | aborted
        self.finished = False       # True once the job thread exits
        self.abort_event = threading.Event()
        self.thread = None
        self.created_at = time.time()
        self.ssh = None

    def log(self, message):
        """Append a log message and wake all SSE readers."""
        with self._cond:
            self.history.append(message)
            self._cond.notify_all()

    def _mark_finished(self):
        """Signal all SSE readers that no more messages are coming."""
        with self._cond:
            self.finished = True
            self._cond.notify_all()

    def wait_for_new(self, position, timeout=30):
        """Block until history has entries beyond *position*, or timeout.
        Returns the new slice of messages and the updated position.
        Each SSE connection calls this independently with its own position."""
        with self._cond:
            if len(self.history) > position:
                msgs = self.history[position:]
                return msgs, position + len(msgs)
            # Wait for new data or finish signal
            self._cond.wait(timeout=timeout)
            if len(self.history) > position:
                msgs = self.history[position:]
                return msgs, position + len(msgs)
            return [], position

    def abort(self):
        """Signal the job to stop as soon as possible."""
        self.abort_event.set()
        self.status = "aborted"
        # Force-close SSH to unblock any blocking reads
        if self.ssh:
            try:
                self.ssh.disconnect()
            except Exception:
                pass

    def run(self):
        """Execute the setup in a background thread."""
        try:
            self.ssh = SSHHelper(
                host=self.config["server_ip"],
                username=self.config["ssh_username"],
                password=self.config["ssh_password"],
                port=int(self.config.get("ssh_port", 22)),
                log_fn=self.log,
                abort_event=self.abort_event,
            )
            self.ssh.connect()
            try:
                if self.service == "zadp":
                    success = run_zadp_setup(self.ssh, self.config)
                else:
                    success = run_ir_setup(self.ssh, self.config)

                if success:
                    self.log("\n--- All done! Setup completed successfully. ---")
                    self.status = "success"
                else:
                    self.log("\n--- Setup did not complete. Check the log above. ---")
                    self.status = "failed"
            finally:
                self.ssh.disconnect()
        except AbortedError:
            self.log("\n--- Setup aborted by user. ---")
            self.status = "aborted"
        except Exception as e:
            if self.abort_event.is_set():
                self.log("\n--- Setup aborted by user. ---")
                self.status = "aborted"
            else:
                self.log(f"\nERROR: {e}")
                self.status = "error"
        finally:
            self.ssh = None
            self._mark_finished()


# ------------------------------------------------------------------ #
#  Routes
# ------------------------------------------------------------------ #

@app.route("/")
def index():
    return render_template("index.html", base_path=BASE_PATH)


@app.route("/download/setup-script")
def download_script():
    """Download the runner script."""
    return send_from_directory(
        app.root_path,
        "runner.py",
        as_attachment=True,
        download_name="runner.py",
    )


@app.route("/api/upload", methods=["POST"])
def upload_file():
    """Upload a zip/build file and SCP it to the target server."""
    import paramiko

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "No file provided"}), 400

    server_ip = request.form.get("server_ip")
    ssh_username = request.form.get("ssh_username", "zsroot")
    ssh_password = request.form.get("ssh_password")
    ssh_port = int(request.form.get("ssh_port", 22))

    # Save locally first
    local_path = os.path.join(UPLOAD_DIR, f.filename)
    f.save(local_path)

    try:
        # SCP to server
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(server_ip, port=ssh_port, username=ssh_username,
                       password=ssh_password, timeout=30)
        sftp = client.open_sftp()
        remote_path = f"/home/zsroot/{f.filename}"
        sftp.put(local_path, remote_path)
        sftp.close()
        client.close()
        return jsonify({"ok": True, "filename": f.filename,
                        "remote_path": remote_path})
    except Exception as e:
        return jsonify({"error": f"SCP failed: {e}"}), 500
    finally:
        # Cleanup local temp file
        try:
            os.remove(local_path)
        except OSError:
            pass


@app.route("/api/start", methods=["POST"])
def start_job():
    """Start a new setup job. Returns a job_id for SSE streaming."""
    data = request.json
    service = data.get("service")
    config = data.get("config", {})

    if service not in ("zadp", "ir"):
        return jsonify({"error": "Invalid service. Must be 'zadp' or 'ir'."}), 400

    # Basic validation
    required = ["server_ip", "ssh_username", "ssh_password", "zip_file",
                "setup_type"]
    if config.get("setup_type") == "local":
        required.append("cloud_name")
    for field in required:
        if not config.get(field):
            return jsonify({"error": f"Missing required field: {field}"}), 400

    if service == "zadp" and config.get("setup_type") == "local":
        for field in ["ca_ip", "smui_ip"]:
            if not config.get(field):
                return jsonify({"error": f"Missing required field: {field}"}), 400

    job_id = str(uuid.uuid4())[:8]
    job = Job(job_id, service, config)
    jobs[job_id] = job

    thread = threading.Thread(target=job.run, daemon=True)
    job.thread = thread
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/job/<job_id>")
def get_job(job_id):
    """Get job status + config (used to reconnect after page refresh)."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "job_id": job.id,
        "service": job.service,
        "status": job.status,
        "created_at": job.created_at,
    })


@app.route("/api/stream/<job_id>")
def stream(job_id):
    """SSE endpoint — each connection independently reads from history.

    Multiple tabs / refreshes each get their own read cursor so no
    messages are ever lost.  History is replayed first, then the
    connection tails live output until the job finishes.
    """
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    def generate():
        pos = 0  # this connection's independent read cursor

        while True:
            msgs, pos = job.wait_for_new(pos, timeout=30)

            if msgs:
                for msg in msgs:
                    data = json.dumps({"type": "log", "message": msg})
                    yield f"data: {data}\n\n"

            # Check if job is done and we've drained all messages
            if job.finished and pos >= len(job.history):
                data = json.dumps({"type": "done", "status": job.status})
                yield f"data: {data}\n\n"
                return

            if not msgs:
                # Heartbeat keep-alive (no new data, job still running)
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/abort/<job_id>", methods=["POST"])
def abort_job(job_id):
    """Abort a running setup job."""
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job.status != "running":
        return jsonify({"error": "Job is not running", "status": job.status}), 400
    job.abort()
    return jsonify({"ok": True, "message": "Abort signal sent"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)
