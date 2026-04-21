"""
SSH Helper and Setup Runner
----------------------------
Contact amiglani@zscaler.com to get the script which will run directly and asks input from user. 
All print() calls replaced with self.log() / log() callback
so output streams to the browser instead of stdout.
"""

import re
import threading
import time
import paramiko


class AbortedError(Exception):
    """Raised when a setup job is aborted by the user."""
    pass


def indent(text, spaces=4):
    prefix = " " * spaces
    return "\n".join(prefix + line for line in text.rstrip().split("\n"))


# ====================================================================== #
#  SSH Helper
# ====================================================================== #

class SSHHelper:
    """Manages an SSH connection to a remote server."""

    def __init__(self, host, username, password, port=22, log_fn=None,
                 abort_event=None):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.client = None
        self.shell = None
        self.log = log_fn or print
        self.abort_event = abort_event or threading.Event()

    def _check_abort(self):
        """Raise AbortedError if the job was aborted by the user."""
        if self.abort_event.is_set():
            raise AbortedError("Setup aborted by user.")

    # -- Connect / Disconnect ------------------------------------------ #

    def connect(self):
        self.log(f"Connecting to {self.username}@{self.host}:{self.port} ...")
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            timeout=30,
        )
        self.log("Connected successfully.")

    def disconnect(self):
        if self.client:
            self.client.close()
            self.client = None
            self.log(f"Disconnected from {self.host}.")

    # -- Run a simple command ------------------------------------------ #

    def run(self, command, timeout=120):
        if not self.client:
            self.connect()
        self.log(f"$ {command}")
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        if output.strip():
            self.log(indent(output))
        if error.strip():
            self.log(indent(error))
        return output, error, exit_code

    # -- Wait for server to come back after reboot --------------------- #

    def wait_for_reboot(self, max_wait=120, retry_interval=5):
        self.log(f"Waiting for {self.host} to come back after reboot...")
        self.log(f"(checking every {retry_interval}s, giving up after {max_wait}s)")
        self.disconnect()
        time.sleep(15)
        start_time = time.time()
        while True:
            self._check_abort()
            elapsed = time.time() - start_time
            if elapsed > max_wait:
                raise TimeoutError(
                    f"Server {self.host} didn't come back after {max_wait}s. "
                    f"Please check it manually."
                )
            try:
                self.connect()
                self.log(f"Server is back! (took {int(elapsed)}s)")
                return
            except Exception:
                remaining = int(max_wait - elapsed)
                self.log(f"Not ready yet... retrying in {retry_interval}s ({remaining}s left)")
                time.sleep(retry_interval)

    # -- File / command helpers ---------------------------------------- #

    def file_exists(self, filepath):
        output, _, _ = self.run(f"test -f '{filepath}' && echo EXISTS || echo MISSING")
        return "EXISTS" in output

    def command_exists(self, cmd):
        output, _, _ = self.run(
            f"test -f /sc/update/{cmd} && echo FOUND || echo NOTFOUND"
        )
        return "FOUND" in output

    def read_file(self, filepath):
        output, _, _ = self.run(f"cat '{filepath}'")
        return output

    def insert_line_after(self, filepath, line_number, new_line):
        safe_line = new_line.replace("'", "'\\''")
        next_line = line_number + 1
        # Portable: head/echo/tail works on both GNU and BSD (unlike sed -i)
        self.run_as_root(
            f"{{ head -n {line_number} '{filepath}'; echo '{safe_line}'; tail -n +{next_line} '{filepath}'; }} > /tmp/_edit.tmp && mv /tmp/_edit.tmp '{filepath}'"
        )

    # -- Become root --------------------------------------------------- #

    def _recv_until_quiet(self, timeout=2):
        """Read all available data from shell until no more arrives."""
        data = ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.shell.recv_ready():
                data += self.shell.recv(65536).decode("utf-8", errors="replace")
                deadline = time.time() + 0.3  # reset timer on new data
            else:
                time.sleep(0.05)
        return data

    def run_sudo_su(self):
        if not self.client:
            self.connect()
        self.log("Becoming root (sudo su)...")
        self.shell = self.client.invoke_shell()
        self._recv_until_quiet(timeout=1)
        self.shell.send("sudo su\n")
        output = self._recv_until_quiet(timeout=2)
        if "password" in output.lower():
            self.shell.send(self.password + "\n")
            self._recv_until_quiet(timeout=2)
        self.shell.send("whoami\n")
        check = self._recv_until_quiet(timeout=1)
        if "root" in check:
            self.log("Now running as root.")
        else:
            self.log(f"WARNING: may not be root. Got: {check.strip()}")

    # -- Marker helper ------------------------------------------------- #

    def _marker_on_own_line(self, output, marker):
        # Strip ANSI escape codes (e.g. \x1b[?2004h from bracketed paste mode)
        clean = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]*', '', output)
        for line in clean.split("\n"):
            stripped = line.strip()
            if stripped == marker:
                return True
            # Shell prompt may follow marker on same line without newline
            if stripped.startswith(marker):
                return True
        return False

    # -- Run as root (non-interactive) --------------------------------- #

    def run_as_root(self, command, timeout=120, show_logs=True):
        if not hasattr(self, "shell") or self.shell is None:
            raise RuntimeError("Call run_sudo_su() first!")
        marker = f"__DONE_{int(time.time())}__"
        self.log(f"$ {command}")
        self.shell.send(f"{command}; echo {marker}\n")
        output = ""
        start_time = time.time()
        i=1
        while True:
            self._check_abort()
            elapsed = time.time() - start_time
            if elapsed > timeout:
                self.log(f"Timed out after {timeout}s — reconnecting...")
                break
            time.sleep(0.5)
            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                output += chunk
                # Print live output to the UI stream
                if show_logs == True:
                    self.log(chunk)
            if output.count(marker) >= 2:
                break
            time.sleep(0.05)
            i += 1
        lines = output.strip().split("\n")
        clean_lines = [
            line for line in lines
            if marker not in line and command not in line
        ]
        clean_output = "\n".join(clean_lines).strip()
        return clean_output

    # -- Run as root (interactive with prompts) ------------------------ #

    def run_interactive_as_root(self, command, responses, timeout=300):
        if not hasattr(self, "shell") or self.shell is None:
            raise RuntimeError("Call run_sudo_su() first!")
        marker = f"__DONE_{int(time.time())}__"
        self.log(f"$ {command}")
        # Chain marker on SAME command line so interactive command
        # doesn't consume it as stdin input
        self.shell.send(f"{command}; echo {marker}\n")
        output = ""
        last_check_pos = 0
        start_time = time.time()
        while True:
            self._check_abort()
            elapsed = time.time() - start_time
            if elapsed > timeout:
                self.log(f"Timed out after {timeout}s — reconnecting...")
                break
            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                output += chunk
                self.log(chunk)
                new_text = output[last_check_pos:].lower()
                for i, (prompt_text, answer) in enumerate(responses):
                    if prompt_text.lower() in new_text:
                        time.sleep(0.1)  # tiny pause before sending answer
                        self.shell.send(answer + "\n")
                        display = "****" if "password" in prompt_text.lower() else answer
                        self.log(f">>> Sent: {display}")
                        last_check_pos = len(output)
                        responses = responses[:i] + responses[i + 1:]
                        break
            if output.count(marker) >= 2:
                break
            if "Error: unable to connect to the Update Download server" in output and not check_if_build_installed(output):
                break
            time.sleep(0.05)
        lines = output.strip().split("\n")
        clean_lines = [line for line in lines if marker not in line]
        clean_output = "\n".join(clean_lines).strip()
        return clean_output, 0


# ====================================================================== #
#  Shared Helpers
# ====================================================================== #

def check_if_build_installed(configure_output):
    text = configure_output.lower()
    for sign in ["finished installation!", "service is running with pid"]:
        if sign in text:
            return True
    return False


def _check_build_installed(ssh, minutes=2):
    """Check /sc/log/autoupgrade.log for successful install within last N minutes."""
    # Get current VM time
    vm_time_out = ssh.run_as_root("date '+%Y-%m-%d %H:%M'", timeout=10)
    ssh.log(f"VM time: {vm_time_out.strip()}")

    log_output = ssh.run_as_root(
        f"tail -50 /sc/log/autoupgrade.log",
        timeout=10,
    )
    if "Completed installation successfully!" in log_output:
        return True
    return False


def install_build_manually(ssh, config, service):
    build_path = config.get("build_path", "")
    if not build_path:
        ssh.log("No build URL was provided.")
        ssh.log("Please install the build manually:")
        ssh.log(f"  scp <build_file> {ssh.username}@{ssh.host}:/home/zsroot/")
        ssh.log(f"  ssh {ssh.username}@{ssh.host}")
        ssh.log(f"  sudo sh <build_file> -i /sc --fast")
        ssh.log(f"  sudo {service} restart")
        return False

    ssh.log("Reconnecting to server...")
    try:
        ssh.disconnect()
    except Exception:
        pass
    time.sleep(5)
    ssh.connect()
    ssh.run_sudo_su()

    filename = build_path.rstrip("/").split("/")[-1]
    remote_build = f"/home/zsroot/{filename}"
    fetch_cmd = f"fetch -v -o {remote_build} '{build_path}'"
    
    # -- Create the install script on the remote server --
    script_path = "/home/zsroot/run_build_install.sh"

    script_lines = [
        'echo "=== Build Install Started: $(date) ==="',
        'echo "Fetching build..."',
        fetch_cmd,
        'if [ $? -ne 0 ]; then',
        '  echo "ERROR: fetch failed"',
        '  exit 1',
        'fi',
        'echo "Fetch complete."',
        f'echo "Installing build: {remote_build}"',
        f"sh {remote_build} -i /sc --fast",
        'echo "Install exit code: $?"',
        'echo "=== Build Install Finished: $(date) ==="',
        '/sc/bin/smctl.sh start'
    ]
    script_content = "\n".join([l for l in script_lines if l]) + "\n"

    cmd = f"""cat > {script_path} <<'EOF'
    {script_content}EOF
    chmod +x {script_path}
    """
    ssh.run_as_root(cmd, timeout=2, show_logs = False)

    # -- Run the script detached (nohup) so sshd restart won't kill it --
    ssh.log(f"Running build install script: {script_path}")
    ssh.log(f"  Build should install in ~80 seconds (20s wait + install time).")
    ssh.run_as_root(
        f"sh {script_path}",
        timeout=90,
    )

    # -- Wait some 30 for first attempt --
    ssh.log("Waiting some 30 for build to complete...")
    time.sleep(30)

    # -- Reconnect (sshd may have restarted during install) --
    ssh.log("Reconnecting to server...")
    try:
        ssh.disconnect()
    except Exception:
        pass
    time.sleep(5)
    ssh.connect()
    ssh.run_sudo_su()

    # -- Check if build installed successfully --
    ssh.log("Checking build installation status...")
    if _check_build_installed(ssh):
        ssh.log("Build installed successfully!")
        return True

    # -- Still failed --
    ssh.log("ERROR: Build installation failed.")
    ssh.log(f"Check the log file on the server for details:")
    ssh.log(f"  ssh {ssh.username}@{ssh.host}")
    ssh.log(f"  cat {log_file}")
    # Show last few lines of the log for quick debugging
    ssh.log("--- Last 20 lines of build log ---")
    ssh.run_as_root(f"tail -20 {log_file}", timeout=10)
    return False


# ====================================================================== #
#  ZADP Setup
# ====================================================================== #

def run_zadp_setup(ssh, config):
    """Run all ZADP setup steps."""

    is_local = config["setup_type"] == "local"
    zip_file = config["zip_file"]
    cloud = config.get("cloud_name", "")

    ssh.log("=" * 50)
    ssh.log("  ZADP SETUP - Starting")
    ssh.log(f"  Mode: {'LOCAL' if is_local else 'PRODUCTION'}")
    ssh.log("=" * 50)

    # -- Step 0: Become root & set ZSINSTANCE
    ssh.run_sudo_su()
    ssh.log("Setting ZSINSTANCE to /sc")
    ssh.run("export ZSINSTANCE=/sc")

    # -- Step 1: Check zip file
    ssh.log("\n--- Step 1: Checking certificate zip file ---")
    zip_path = f"/home/zsroot/{zip_file}"
    if not ssh.file_exists(zip_path):
        ssh.log(f"ERROR: File '{zip_path}' not found on the server!")
        ssh.log(f"Copy it first: scp {zip_file} {ssh.username}@{ssh.host}:/home/zsroot/")
        return False
    ssh.log(f"Found: {zip_path}")

    # -- Step 2: Cleanup old installation
    ssh.log("\n--- Step 2: Cleaning up old ZADP installation ---")
    ssh.log("This will remove the old install and reboot the server.")
    if ssh.command_exists("zadp"):
        ssh.run_interactive_as_root(
            "/sc/update/zadp cleanup",
            responses=[
                ("cannot be reversed", "y"),
                ("reboot?", "y"),
            ],
            timeout=20,
        )
        ssh.wait_for_reboot()
        ssh.log("Cleanup done. Server is back.")
        ssh.run_sudo_su()
    else:
        ssh.log("'zadp' command not found - skipping cleanup (fresh server).")

    # -- Step 3: Update /etc/hosts (local only)
    if is_local:
        ssh.log("\n--- Step 3: Updating /etc/hosts (local only) ---")
        cdss_ip = config.get("cdss_ip", "")
        smui_ip = config["smui_ip"]
        ca_ip = config["ca_ip"]
        new_entries = []
        if cdss_ip:
            new_entries.append(f"{cdss_ip}  zdistribute.{cloud}.net")
        new_entries.append(f"{smui_ip}  zsapi.{cloud}.net")
        new_entries.append(f"{ca_ip}  smcacluster.{cloud}.net")
        current_hosts = ssh.read_file("/etc/hosts")
        cleaned = [
            line for line in current_hosts.split("\n")
            if not any(h in line for h in ["zdistribute.", "zsapi.", "admin.", "smcacluster."])
        ]
        new_hosts_content = "\n".join(cleaned).rstrip() + "\n\n" + "\n".join(new_entries) + "\n"
        ssh.run(f"cat << 'HOSTS_EOF' > /tmp/hosts_new\n{new_hosts_content}\nHOSTS_EOF")
        ssh.run_as_root("cp /tmp/hosts_new /etc/hosts", timeout=10)
        ssh.run("rm -f /tmp/hosts_new")
        ssh.log("Updated /etc/hosts with:")
        for entry in new_entries:
            ssh.log(f"  {entry}")
    else:
        ssh.log("\n--- Step 3: Skipped (not needed for production) ---")

    # -- Step 4: Run zadp configure
    ssh.log("\n--- Step 4: Running 'zadp configure' ---")
    server_ip = config["server_ip"]
    if not ssh.command_exists("zadp"):
        ssh.log("ERROR: 'zadp' command not found. Reinstall VM...")
        return False
    output, _ = ssh.run_interactive_as_root(
        f"/sc/update/zadp configure '{zip_path}'",
        responses=[
            ("domain name", server_ip),
            ("pass phrase", ""),
            ("continue?", ""),
        ],
        timeout=300,
    )
    build_ok = check_if_build_installed(output)
    if build_ok:
        ssh.log("Build installed automatically!")
    else:
        ssh.log("Auto build install did not work.")

    # -- Step 5: Manual build install (if needed)
    if not build_ok:
        ssh.log("\n--- Step 5: Manual build installation ---")
        if not install_build_manually(ssh, config, service="zadp"):
            return False
    else:
        ssh.log("\n--- Step 5: Skipped (build already installed) ---")

    # -- Step 6: Add QA params to sc.conf (local only)
    if is_local:
        ssh.log("\n--- Step 6: Adding QA params to /sc/conf/sc.conf ---")
        sc_conf_output = ssh.run_as_root("cat /sc/conf/sc.conf",timeout=10)
        if "zadp_qa_ca_cert=1" not in sc_conf_output:
            ssh.insert_line_after("/sc/conf/sc.conf", 1, "zadp_qa_ca_cert=1")
            ssh.log("Added: zadp_qa_ca_cert=1")
        if "zsapi_insecure=1" not in sc_conf_output:
            ssh.insert_line_after("/sc/conf/sc.conf", 1, "zsapi_insecure=1")
            ssh.log("Added: zsapi_insecure=1")
        # Update zsapi_port with user-provided SMUI port
        smui_port = config.get("smui_port", "443")
        if smui_port and smui_port != "443":
            ssh.run_as_root(
                f"if grep -q '^zsapi_port=' /sc/conf/sc.conf; then "
                f"  sed -i 's/^zsapi_port=.*/zsapi_port={smui_port}/' /sc/conf/sc.conf; "
                f"else "
                f"  {{ head -n 1 /sc/conf/sc.conf; echo 'zsapi_port={smui_port}'; tail -n +2 /sc/conf/sc.conf; }} > /tmp/_edit.tmp && mv /tmp/_edit.tmp /sc/conf/sc.conf; "
                f"fi", timeout=10
            )
            ssh.log(f"Set: zsapi_port={smui_port}")
        else:
            ssh.log("zsapi_port: using default (443)")
    else:
        ssh.log("\n--- Step 6: Skipped (not needed for production) ---")

    # -- Step 7: Show final status
    ssh.log("\n--- Step 7: Final status ---")
    ssh.run("export ZSINSTANCE=/sc")
    ssh.run_as_root("/sc/update/zadp restart",timeout=60)
    ssh.run_as_root("/sc/update/zadp status",timeout=60)

    ssh.log("\n" + "=" * 50)
    ssh.log("  ZADP SETUP - Finished!")
    ssh.log("=" * 50)
    return True


# ====================================================================== #
#  IR (Incident Receiver) Setup
# ====================================================================== #

def run_ir_setup(ssh, config):
    """Run all IR setup steps."""

    is_local = config["setup_type"] == "local"
    zip_file = config["zip_file"]
    cloud = config.get("cloud_name", "")
    server_ip = config["server_ip"]
    ssh_username = config["ssh_username"]
    ssh_password = config["ssh_password"]
    ir_dir = config.get("ir_dir", "/home/zsroot/IR")

    ssh.log("=" * 50)
    ssh.log("  IR (INCIDENT RECEIVER) SETUP - Starting")
    ssh.log(f"  Mode: {'LOCAL' if is_local else 'PRODUCTION'}")
    ssh.log(f"  IR Directory: {ir_dir}")
    ssh.log("=" * 50)

    # -- Step 0: Become root & set ZSINSTANCE
    ssh.run_sudo_su()
    ssh.log("Setting ZSINSTANCE to /sc")
    ssh.run("export ZSINSTANCE=/sc")

    # -- Step 1: Check zip file
    ssh.log("\n--- Step 1: Checking certificate zip file ---")
    zip_path = f"/home/zsroot/{zip_file}"
    if not ssh.file_exists(zip_path):
        ssh.log(f"ERROR: File '{zip_path}' not found on the server!")
        ssh.log(f"Copy it first: scp {zip_file} {ssh.username}@{ssh.host}:/home/zsroot/")
        return False
    ssh.log(f"Found: {zip_path}")

    # -- Step 2: Cleanup old installation
    ssh.log("\n--- Step 2: Cleaning up old ZIRSVR installation ---")
    ssh.log("This will remove the old install and reboot the server.")
    if ssh.command_exists("zirsvr"):
        ssh.run_interactive_as_root(
            "/sc/update/zirsvr cleanup",
            responses=[
                ("cannot be reversed", "y"),
                ("reboot?", "y"),
            ],
            timeout=20,
        )
        ssh.wait_for_reboot()
        ssh.log("Cleanup done. Server is back.")
        ssh.run_sudo_su()
    else:
        ssh.log("'zirsvr' command not found - skipping cleanup (fresh server).")

    # -- Step 3: Update /etc/hosts (local only)
    if is_local:
        ssh.log("\n--- Step 3: Updating /etc/hosts (local only) ---")
        cdss_ip = config.get("cdss_ip", "")
        if cdss_ip:
            hosts_line = f"{cdss_ip}  zdistribute.{cloud}.net"
            current_hosts = ssh.read_file("/etc/hosts")
            cleaned = [
                line for line in current_hosts.split("\n")
                if not any(h in line for h in ["zdistribute.", "zsapi.", "admin.", "smcacluster."])
            ]
            new_hosts_content = "\n".join(cleaned).rstrip() + "\n" + hosts_line + "\n"
            ssh.run(f"cat << 'HOSTS_EOF' > /tmp/hosts_new\n{new_hosts_content}\nHOSTS_EOF")
            ssh.run_as_root("cp /tmp/hosts_new /etc/hosts", timeout=10)
            ssh.run("rm -f /tmp/hosts_new")
            ssh.log(f"Updated /etc/hosts: {hosts_line}")
        else:
            ssh.log("No CDSS IP provided. Skipping /etc/hosts update.")
    else:
        ssh.log("\n--- Step 3: Skipped (not needed for production) ---")

    # -- Step 4: Create IR directory
    ssh.log(f"\n--- Step 4: Creating {ir_dir} directory ---")
    ssh.run(f"mkdir -p {ir_dir}")
    ssh.log("Directory ready.")

    # -- Step 5: Run zirsvr configure
    ssh.log("\n--- Step 5: Running 'zirsvr configure' ---")
    ssh.log("This sets up the certificate and SFTP configuration.")
    if not ssh.command_exists("zirsvr"):
        ssh.log("'zirsvr' command not found. Reinstall VM...")
        return False
    output, _ = ssh.run_interactive_as_root(
        f"/sc/update/zirsvr configure '{zip_path}'",
        responses=[
            ("icaps_port", "1344"),
            ("sftp or s3", "sftp"),
            ("storage_sftp_fqdn", server_ip),
            ("storage_sftp_port", "22"),
            ("storage_dir", ir_dir),
            ("storage_sftp_username", ssh_username),
            ("redo the setup", "y"),
            ("re-generate", "y"),
            ("password", ssh_password),
        ],
        timeout=300,
    )
    build_ok = check_if_build_installed(output)
    if build_ok:
        ssh.log("Build installed automatically!")
    else:
        ssh.log("Auto build install did not work.")

    # -- Step 6: Manual build install (if needed)
    if not build_ok:
        ssh.log("\n--- Step 6: Manual build installation ---")
        if not install_build_manually(ssh, config, service="zirsvr"):
            return False
    else:
        ssh.log("\n--- Step 6: Skipped (build already installed) ---")

    # -- Step 7: Show final status
    ssh.log("\n--- Step 7: Final status ---")
    ssh.run("export ZSINSTANCE=/sc")
    ssh.run_as_root("/sc/update/zirsvr start",timeout=60)
    ssh.run_as_root("/sc/update/zirsvr status",timeout=60)

    ssh.log("\n" + "=" * 50)
    ssh.log("  IR (INCIDENT RECEIVER) SETUP - Finished!")
    ssh.log("=" * 50)
    return True
