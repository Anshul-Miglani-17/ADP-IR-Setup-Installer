#!/usr/bin/env python3
"""
=============================================================
  ZADP & Incident Receiver (IR) - One-Click Setup Script
  -------------------------------------------------------
  A single script to install and configure ZADP or IR
  on a remote server. Works for local and production setups.

  Just run:
      python3 setup.py

  It will install any missing dependencies automatically.

  For any queries, contact: amiglani@zscaler.com
=============================================================
"""

# ====================================================================== #
#  Step 0: Install missing dependencies automatically
# ====================================================================== #

import subprocess
import sys

def install_dependencies():
    """Install paramiko if it's not already installed."""
    try:
        import paramiko  # noqa: F401
    except ImportError:
        print("\n  'paramiko' is not installed. Installing it now...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "paramiko"])
        print("  Done!\n")

install_dependencies()


# ====================================================================== #
#  Now import everything we need
# ====================================================================== #

import getpass
import re
import time
import paramiko


# ====================================================================== #
#                                                                        #
#  PART 1: SSH Helper                                                    #
#  ----------------                                                      #
#  Handles all the SSH work: connecting, running commands,               #
#  waiting for reboots, reading/writing files on the server.             #
#                                                                        #
# ====================================================================== #

class SSHHelper:
    """Manages an SSH connection to a remote server."""

    def __init__(self, host, username, password, port=22):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.client = None

    # -- Connect / Disconnect ------------------------------------------ #

    def connect(self):
        """Open an SSH connection to the server."""
        print(f"\n  Connecting to {self.username}@{self.host}:{self.port} ...")
        self.client = paramiko.SSHClient()
        self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.client.connect(
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            timeout=30,
        )
        print(f"  Connected successfully.")

    def disconnect(self):
        """Close the SSH connection."""
        if self.client:
            self.client.close()
            self.client = None
            print(f"  Disconnected from {self.host}.")

    # -- Run a simple command (no sudo) -------------------------------- #

    def run(self, command, timeout=120):
        """
        Run a non-sudo command on the remote server.
        For sudo commands, use run_sudo() instead.
        Returns: (output_text, error_text, exit_code)
        """
        if not self.client:
            self.connect()

        print(f"\n  Running: {command}")
        stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)

        output = stdout.read().decode("utf-8", errors="replace")
        error = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()

        if output.strip():
            print(indent(output))
        if error.strip():
            print(indent(error))

        return output, error, exit_code

    # -- Wait for server to come back after reboot --------------------- #

    def wait_for_reboot(self, max_wait=120, retry_interval=5):
        """
        After a reboot, keep trying to SSH in every few seconds
        until the server is back.
        """
        print(f"\n  Waiting for {self.host} to come back after reboot...")
        print(f"  (checking every {retry_interval}s, giving up after {max_wait}s)")

        self.disconnect()
        time.sleep(15)  # give the server a moment to go down

        start_time = time.time()
        while True:
            elapsed = time.time() - start_time
            if elapsed > max_wait:
                raise TimeoutError(
                    f"Server {self.host} didn't come back after {max_wait}s. "
                    f"Please check it manually."
                )
            try:
                self.connect()
                print(f"  Server is back! (took {int(elapsed)}s)")
                return
            except Exception:
                remaining = int(max_wait - elapsed)
                print(f"  Not ready yet... retrying in {retry_interval}s ({remaining}s left)")
                time.sleep(retry_interval)

    # -- Check if a file exists ---------------------------------------- #

    def file_exists(self, filepath):
        """Returns True if the file exists on the remote server."""
        output, _, _ = self.run(f"test -f '{filepath}' && echo EXISTS || echo MISSING")
        return "EXISTS" in output

    # -- Check if a command exists on the server ----------------------- #

    def command_exists(self, cmd):
        """
        Returns True if the command is available on the server.
        Checks /sc/update/<cmd> directly because paramiko SSH sessions
        don't load the full login shell PATH (so 'which' may miss it).
        """
        output, _, _ = self.run(
            f"test -f /sc/update/{cmd} && echo FOUND || echo NOTFOUND"
        )
        return "FOUND" in output

    # -- Read a file --------------------------------------------------- #

    def read_file(self, filepath):
        """Read the contents of a file on the remote server."""
        output, _, _ = self.run(f"cat '{filepath}'")
        return output

    # -- Insert a line into a file after a given line number ----------- #

    def insert_line_after(self, filepath, line_number, new_line):
        """Insert a new line after the given line number in a file (as root)."""
        safe_line = new_line.replace("'", "'\\''")
        next_line = line_number + 1
        # Portable: head/echo/tail works on both GNU and BSD (unlike sed -i)
        self.run_as_root(
            f"{{ head -n {line_number} '{filepath}'; echo '{safe_line}'; tail -n +{next_line} '{filepath}'; }} > /tmp/_edit.tmp && mv /tmp/_edit.tmp '{filepath}'"
        )
    
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
        """
        Become root via 'sudo su'. Opens a persistent shell channel.
        After this, use run_as_root() to run commands without sudo.
        Call this again after every reboot (old shell dies on reboot).
        """
        if not self.client:
            self.connect()

        print("\n  Becoming root (sudo su)...")

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
            print("  Now running as root.")
        else:
            print(f"  WARNING: may not be root. Got: {check.strip()}")

    def _marker_on_own_line(self, output, marker):
        """
        Check if the marker appears on its OWN line in the output.
        This avoids false matches from the shell echoing the command.

        Shell echo looks like:  /sc/update/zirsvr cleanup; echo __DONE_123__
        Actual marker output:   __DONE_123__
        """
        # Strip ANSI escape codes (e.g. \x1b[?2004h from bracketed paste mode)
        clean = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', output)
        for line in clean.split("\n"):
            stripped = line.strip()
            if stripped == marker:
                return True
            # Shell prompt may follow marker on same line without newline
            if stripped.startswith(marker):
                return True
        return False

    def run_as_root(self, command, timeout=120):
        """
        Run a command in the persistent root shell (after run_sudo_su).
        Returns the output text.

        Example:
            ssh.run_sudo_su()                          # become root once
            ssh.run_as_root("zirsvr status")           # no sudo needed
            ssh.run_as_root("cat /sc/conf/sc.conf")    # no sudo needed
        """
        if not hasattr(self, "shell") or self.shell is None:
            raise RuntimeError("Call run_sudo_su() first!")

        # Unique marker so we know where the command output ends
        marker = f"__DONE_{int(time.time())}__"

        print(f"\n  Running (as root): {command}")

        # Send the command, then echo the marker on a SEPARATE send
        self.shell.send(f"{command}; echo {marker}\n")

        output = ""
        start_time = time.time()

        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                print(f"  Timed out after {timeout}s")
                break

            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                output += chunk

                # Only match marker on its OWN line (not in command echo)
                if self._marker_on_own_line(output, marker):
                    break

            time.sleep(0.05)

        # Clean up: remove the command echo line and marker line
        lines = output.strip().split("\n")
        clean_lines = [
            line for line in lines
            if marker not in line and command not in line
        ]
        clean_output = "\n".join(clean_lines).strip()

        if clean_output:
            print(indent(clean_output))

        return clean_output

    def run_interactive_as_root(self, command, responses, timeout=300):
        """
        Run an interactive command in the persistent root shell.
        Like run_as_root but also handles prompt-response pairs.
        No sudo password needed (already root).
        Returns: (output_text, exit_code)  — exit_code is 0 on normal finish.

        Example:
            output, _ = ssh.run_interactive_as_root(
                "/sc/update/zirsvr cleanup",
                responses=[
                    ("cannot be reversed", "y"),
                    ("reboot?", "y"),
                ],
            )
        """
        if not hasattr(self, "shell") or self.shell is None:
            raise RuntimeError("Call run_sudo_su() first!")

        marker = f"__DONE_{int(time.time())}__"

        print(f"\n  Running (as root, interactive): {command}")

        # Chain the marker echo on the SAME command line so the shell
        # runs it after the command finishes. Sending it as a separate
        # line would let the interactive command consume it as stdin input.
        self.shell.send(f"{command}; echo {marker}\n")

        output = ""
        last_check_pos = 0
        start_time = time.time()

        while True:
            elapsed = time.time() - start_time
            if elapsed > timeout:
                print(f"\n  Timed out after {timeout}s")
                break

            if self.shell.recv_ready():
                chunk = self.shell.recv(65536).decode("utf-8", errors="replace")
                output += chunk
                print(chunk, end="", flush=True)

                # Check for end marker ON ITS OWN LINE (not in command echo)
                if self._marker_on_own_line(output, marker):
                    break

                # Check for prompts in new output only
                new_text = output[last_check_pos:].lower()
                for i, (prompt_text, answer) in enumerate(responses):
                    if prompt_text.lower() in new_text:
                        time.sleep(0.1)  # tiny pause before sending answer
                        self.shell.send(answer + "\n")
                        display = "****" if "password" in prompt_text.lower() else answer
                        print(f"  >>> Sent: {display}")
                        last_check_pos = len(output)
                        responses = responses[:i] + responses[i+1:]
                        break

            time.sleep(0.05)

        # Clean output
        lines = output.strip().split("\n")
        clean_lines = [
            line for line in lines
            if marker not in line
        ]
        clean_output = "\n".join(clean_lines).strip()
        print()
        return clean_output, 0

# ====================================================================== #
#                                                                        #
#  PART 2: ZADP Installer                                                #
#  ----------------------                                                #
#  Steps:                                                                #
#    1. Check zip file exists                                            #
#    2. Cleanup old install (reboot)                                     #
#    3. Update /etc/hosts (local only)                                   #
#    4. Run zadp configure                                               #
#    5. Install build manually if needed                                 #
#    6. Add QA params to sc.conf (local only)                            #
#    7. Show final status                                                #
#                                                                        #
# ====================================================================== #

def run_zadp_setup(ssh, config):
    """Run all ZADP setup steps."""

    is_local = config["setup_type"] == "local"
    zip_file = config["zip_file"]
    cloud = config.get("cloud_name", "")

    print("\n" + "=" * 60)
    print("  ZADP SETUP - Starting")
    print("  Mode:", "LOCAL" if is_local else "PRODUCTION")
    print("=" * 60)

    # -- Step 0: Become root & set ZSINSTANCE --------------------------- #

    ssh.run_sudo_su()
    print(f"  Setting ZSINSTANCE to /sc")
    ssh.run("export ZSINSTANCE=/sc")

    # -- Step 1: Check zip file ---------------------------------------- #

    print("\n--- Step 1: Checking certificate zip file ---")

    zip_path = f"/home/zsroot/{zip_file}"
    if not ssh.file_exists(zip_path):
        print(f"\n  ERROR: File '{zip_path}' not found on the server!")
        print(f"  Please copy it first:")
        print(f"    scp {zip_file} {ssh.username}@{ssh.host}:/home/zsroot/")
        print(f"\n  Then re-run this script.")
        return False

    print(f"  Found: {zip_path}")

    # -- Step 2: Cleanup old installation ------------------------------ #

    print("\n--- Step 2: Cleaning up old ZADP installation ---")
    print("  This will remove the old install and reboot the server.")

    # Check if zadp command exists (might be a fresh server)
    if ssh.command_exists("zadp"):
        output, _ = ssh.run_interactive_as_root(
            "/sc/update/zadp cleanup",
            responses=[
                ("cannot be reversed", "y"),     # confirm deletion
                ("reboot?", "y"),                # confirm reboot
            ],
            timeout=20,
        )
        ssh.wait_for_reboot()
        print("  Cleanup done. Server is back.")
        ssh.run_sudo_su()
    else:
        print("  'zadp' command not found — skipping cleanup (fresh server).")

    # -- Step 3: Update /etc/hosts (local only) ------------------------ #

    if is_local:
        print("\n--- Step 3: Updating /etc/hosts (local only) ---")

        cdss_ip = config.get("cdss_ip", "")
        smui_ip = config["smui_ip"]
        ca_ip = config["ca_ip"]

        # Build new host entries
        new_entries = []
        if cdss_ip:
            new_entries.append(f"{cdss_ip}  zdistribute.{cloud}.net")
        new_entries.append(f"{smui_ip}  zsapi.{cloud}.net")
        new_entries.append(f"{ca_ip}  smcacluster.{cloud}.net")

        # Read current /etc/hosts and remove old cloud entries
        current_hosts = ssh.read_file("/etc/hosts")
        cleaned = [
            line for line in current_hosts.split("\n")
            if not any(h in line for h in ["zdistribute.", "zsapi.", "admin.", "smcacluster."])
        ]

        # Write back with our new entries
        new_hosts_content = "\n".join(cleaned).rstrip() + "\n\n" + "\n".join(new_entries) + "\n"
        # Write via a temp file to avoid quoting issues with echo
        ssh.run(f"cat << 'HOSTS_EOF' > /tmp/hosts_new\n{new_hosts_content}\nHOSTS_EOF")
        ssh.run_as_root("cp /tmp/hosts_new /etc/hosts")
        ssh.run("rm -f /tmp/hosts_new")

        print("  Updated /etc/hosts with:")
        for entry in new_entries:
            print(f"    {entry}")
    else:
        print("\n--- Step 3: Skipped (not needed for production) ---")

    # -- Step 4: Run zadp configure ------------------------------------ #

    print("\n--- Step 4: Running 'zadp configure' ---")

    server_ip = config["server_ip"]

    # If zadp command doesn't exist after cleanup, install build first
    if not ssh.command_exists("zadp"):
        print("  ERROR -'zadp' command not found. Reinstall VM...")
        return False

        


    output, _ = ssh.run_interactive_as_root(
        f"/sc/update/zadp configure '{zip_path}'",
        responses=[
            ("domain name", server_ip),      # self-signed cert domain
            ("pass phrase", ""),              # no passphrase
            ("continue?", ""),               # accept no passphrase
        ],
        timeout=300,
    )

    build_ok = check_if_build_installed(output)
    if build_ok:
        print("\n  Build installed automatically. Great!")
    else:
        print("\n  Auto build install did not work.")

    # -- Step 5: Manual build install (if needed) ---------------------- #

    if not build_ok:
        print("\n--- Step 5: Manual build installation ---")
        if not install_build_manually(ssh, config, service="zadp"):
            return False
    else:
        print("\n--- Step 5: Skipped (build already installed) ---")

    # -- Step 6: Add QA params to sc.conf (local only) ----------------- #

    if is_local:
        print("\n--- Step 6: Adding QA params to /sc/conf/sc.conf ---")

        sc_conf_output = ssh.run_as_root("cat /sc/conf/sc.conf")

        if "zadp_qa_ca_cert=1" not in sc_conf_output:
            ssh.insert_line_after("/sc/conf/sc.conf", 1, "zadp_qa_ca_cert=1")
            print("  Added: zadp_qa_ca_cert=1")

        if "zsapi_insecure=1" not in sc_conf_output:
            ssh.insert_line_after("/sc/conf/sc.conf", 1, "zsapi_insecure=1")
            print("  Added: zsapi_insecure=1")
    else:
        print("\n--- Step 6: Skipped (not needed for production) ---")

    # -- Step 7: Show final status ------------------------------------- #

    print("\n--- Step 7: Final status ---")
    ssh.run("export ZSINSTANCE=/sc")
    ssh.run_as_root("/sc/update/zadp restart")
    ssh.run_as_root("/sc/update/zadp status")

    print("\n" + "=" * 60)
    print("  ZADP SETUP - Finished!")
    print("=" * 60)
    print("\n  For any queries, contact: amiglani@zscaler.com")
    return True


# ====================================================================== #
#                                                                        #
#  PART 3: IR (Incident Receiver) Installer                              #
#  ----------------------------------------                              #
#  Steps:                                                                #
#    1. Check zip file exists                                            #
#    2. Cleanup old install (reboot)                                     #
#    3. Update /etc/hosts (local only)                                   #
#    4. Create /home/zsroot/IR directory                                 #
#    5. Run zirsvr configure (with SFTP answers)                         #
#    6. Install build manually if needed                                 #
#    7. Show final status                                                #
#                                                                        #
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

    print("\n" + "=" * 60)
    print("  IR (INCIDENT RECEIVER) SETUP - Starting")
    print("  Mode:", "LOCAL" if is_local else "PRODUCTION")
    print(f"  IR Directory: {ir_dir}")
    print("=" * 60)

    # -- Step 0: Become root & set ZSINSTANCE --------------------------- #

    ssh.run_sudo_su()
    print(f"  Setting ZSINSTANCE to /sc")
    ssh.run("export ZSINSTANCE=/sc")

    # -- Step 1: Check zip file ---------------------------------------- #

    print("\n--- Step 1: Checking certificate zip file ---")

    zip_path = f"/home/zsroot/{zip_file}"
    if not ssh.file_exists(zip_path):
        print(f"\n  ERROR: File '{zip_path}' not found on the server!")
        print(f"  Please copy it first:")
        print(f"    scp {zip_file} {ssh.username}@{ssh.host}:/home/zsroot/")
        print(f"\n  Then re-run this script.")
        return False

    print(f"  Found: {zip_path}")

    # -- Step 2: Cleanup old installation ------------------------------ #

    print("\n--- Step 2: Cleaning up old ZIRSVR installation ---")
    print("  This will remove the old install and reboot the server.")

    # Check if zirsvr command exists (might be a fresh server)
    if ssh.command_exists("zirsvr"):
        output, _ = ssh.run_interactive_as_root(
            "/sc/update/zirsvr cleanup",
            responses=[
                ("cannot be reversed", "y"),     # confirm deletion
                ("reboot?", "y"),                # confirm reboot
            ],
            timeout=20,
        )
        ssh.wait_for_reboot()
        print("  Cleanup done. Server is back.")
        ssh.run_sudo_su()
    else:
        print("  'zirsvr' command not found — skipping cleanup (fresh server).")

    # -- Step 3: Update /etc/hosts (local only) ------------------------ #

    if is_local:
        print("\n--- Step 3: Updating /etc/hosts (local only) ---")

        cdss_ip = config.get("cdss_ip", "")
        if cdss_ip:
            hosts_line = f"{cdss_ip}  zdistribute.{cloud}.net"

            current_hosts = ssh.read_file("/etc/hosts")
            cleaned = [
                line for line in current_hosts.split("\n")
                if not any(h in line for h in ["zdistribute.", "zsapi.", "admin.", "smcacluster."])
            ]
            new_hosts_content = "\n".join(cleaned).rstrip() + "\n" + hosts_line + "\n"
            # Write via a temp file to avoid quoting issues
            ssh.run(f"cat << 'HOSTS_EOF' > /tmp/hosts_new\n{new_hosts_content}\nHOSTS_EOF")
            ssh.run_as_root("cp /tmp/hosts_new /etc/hosts")
            ssh.run("rm -f /tmp/hosts_new")
            print(f"  Updated /etc/hosts with:")
            print(f"    {hosts_line}")
        else:
            print("  No CDSS IP provided. Skipping /etc/hosts update.")
    else:
        print("\n--- Step 3: Skipped (not needed for production) ---")

    # -- Step 4: Create IR directory ----------------------------------- #

    print(f"\n--- Step 4: Creating {ir_dir} directory ---")
    ssh.run(f"mkdir -p {ir_dir}")
    print("  Directory ready.")

    # -- Step 5: Run zirsvr configure ---------------------------------- #

    print("\n--- Step 5: Running 'zirsvr configure' ---")
    print("  This sets up the certificate and SFTP configuration.")

    # If zirsvr command doesn't exist after cleanup, install build first
    if not ssh.command_exists("zirsvr"):
        print("  'zirsvr' command not found. Reinstall VM...")
        return False

    output, _ = ssh.run_interactive_as_root(
        f"/sc/update/zirsvr configure '{zip_path}'",
        responses=[
            # Port for Incident Receiver (default 1344)
            ("icaps_port", "1344"),
            # SFTP or S3?
            ("sftp or s3", "sftp"),
            # SFTP server address
            ("storage_sftp_fqdn", server_ip),
            # SFTP port (default 22)
            ("storage_sftp_port", "22"),
            # Upload directory
            ("storage_dir", ir_dir),
            # SFTP username
            ("storage_sftp_username", ssh_username),
            # Redo SSH key setup?
            ("redo the setup", "y"),
            # Regenerate keys?
            ("re-generate", "y"),
            # SSH password for key setup (appears second time)
            ("password", ssh_password),
        ],
        timeout=300,
    )

    build_ok = check_if_build_installed(output)
    if build_ok:
        print("\n  Build installed automatically. Great!")
    else:
        print("\n  Auto build install did not work.")

    # -- Step 6: Manual build install (if needed) ---------------------- #

    if not build_ok:
        print("\n--- Step 6: Manual build installation ---")
        if not install_build_manually(ssh, config, service="zirsvr"):
            return False
    else:
        print("\n--- Step 6: Skipped (build already installed) ---")

    # -- Step 7: Show final status ------------------------------------- #

    print("\n--- Step 7: Final status ---")
    ssh.run("export ZSINSTANCE=/sc")
    ssh.run_as_root("/sc/update/zirsvr restart")
    ssh.run_as_root("/sc/update/zirsvr status")

    print("\n" + "=" * 60)
    print("  IR (INCIDENT RECEIVER) SETUP - Finished!")
    print("=" * 60)
    print("\n  For any queries, contact: amiglani@zscaler.com")
    return True


# ====================================================================== #
#                                                                        #
#  PART 4: Shared Helpers                                                #
#  ----------------------                                                #
#  Small functions used by both ZADP and IR installers.                  #
#                                                                        #
# ====================================================================== #

def check_if_build_installed(configure_output):
    """
    Look at the output of the configure command to figure out
    if the build was downloaded and installed automatically.
    """
    text = configure_output.lower()
    # Good signs = build installed
    for sign in ["Finished installation!","finished installation!", "ZIRSVR service is running", "service is running with pid"]:
        if sign in text:
            return True

    # Can't tell? Assume it failed (safer to install manually)
    return False


def install_build_manually(ssh, config, service):
    """
    Try to install a build manually from a file on the server.
    'service' is either "zadp" or "zirsvr".
    """
    build_path = config.get("build_path", "")

    if not build_path:
        print("\n  No build URL was provided.")
        print("  Please install the build manually with these commands:")
        print(f"")
        print(f"    # Copy build to the server first:")
        print(f"    scp <build_file> {ssh.username}@{ssh.host}:/home/zsroot/")
        print(f"")
        print(f"    # Then SSH in and install:")
        print(f"    ssh {ssh.username}@{ssh.host}")
        print(f"    cd /home/zsroot")
        print(f"    sudo sh <build_file> -i /sc --fast")
        print(f"    sudo {service} restart")
        print(f"")
        print(f"  After installing, you can re-run this script if needed.")
        return False

    # If it's a URL, download it to /home/zsroot/ on the server
    if build_path.startswith("http://") or build_path.startswith("https://"):
        filename = build_path.rstrip("/").split("/")[-1]
        remote_path = f"/home/zsroot/{filename}"
        print(f"  Downloading build from URL...")
        print(f"    {build_path}")
        ssh.run_as_root(
            f"fetch -o {remote_path} '{build_path}'",
            timeout=600,
        )
        if not ssh.file_exists(remote_path):
            print(f"  ERROR: Download failed — file not found at {remote_path}")
            print("  Try downloading manually and re-run.")
            return False
        print(f"  Downloaded: {remote_path}")
        build_path = remote_path
    else:
        # Legacy: treat as a local path on the server
        if not ssh.file_exists(build_path):
            full_path = f"/home/zsroot/{build_path}"
            if not ssh.file_exists(full_path):
                print(f"\n  ERROR: Build file not found at '{build_path}' or '{full_path}'")
                return False
            build_path = full_path

    print(f"  Found build: {build_path}")
    print(f"  Installing... (this may take a few minutes)")

    ssh.run_as_root(
        f"sh {build_path} -i /sc --fast",
        timeout=600,
    )

    print(f"  Restarting {service}...")
    ssh.run_as_root(f"/sc/update/{service} restart")
    return True


def indent(text, spaces=4):
    """Add indentation to text for cleaner printing."""
    prefix = " " * spaces
    return "\n".join(prefix + line for line in text.rstrip().split("\n"))


# ====================================================================== #
#                                                                        #
#  PART 5: User Input Collection                                         #
#  ----------------------------                                          #
#  Asks the user for all the info we need, step by step.                 #
#                                                                        #
# ====================================================================== #

def ask(prompt, default="", required=True):
    """Ask the user for a value. Shows default in brackets."""
    display = f"{prompt}[{default}]: " if default else prompt
    while True:
        value = input(display).strip()
        if not value and default:
            return default
        if not value and required:
            print("    This field is required.")
            continue
        return value


def ask_password(prompt):
    """Ask for a password (input is hidden)."""
    while True:
        pw = getpass.getpass(prompt)
        if pw:
            return pw
        print("    Password cannot be empty.")


def ask_choice(prompt, choices):
    """Ask the user to pick one of the given choices."""
    while True:
        value = input(prompt).strip().lower()
        if value in choices:
            return value
        print(f"    Please enter one of: {', '.join(choices)}")


def collect_zadp_inputs():
    """Ask the user for all ZADP-related inputs."""

    print("\n" + "-" * 60)
    print("  ZADP - Tell me about your setup")
    print("-" * 60)

    config = {}

    # SSH
    print("\n  -- Server Access --")
    config["server_ip"] = ask("  Server IP (the ZADP VM): ")
    config["ssh_username"] = ask("  SSH Username: ", default="zsroot")
    config["ssh_password"] = ask_password("  SSH Password: ")
    config["ssh_port"] = int(ask("  SSH Port: ", default="22"))

    # Certificate
    print("\n  -- Certificate --")
    config["zip_file"] = ask("  Certificate zip filename (e.g. AdpClientCertificate_xyz.zip): ")
    print(f"\n  Make sure the file is at /home/zsroot/{config['zip_file']} on the server.")
    print(f"  If not, copy it now:")
    print(f"    scp {config['zip_file']} {config['ssh_username']}@{config['server_ip']}:/home/zsroot/")

    # Environment
    print("\n  -- Environment --")
    config["setup_type"] = ask_choice("  Local or Production? (local/prod): ", ["local", "prod"])

    if config["setup_type"] == "prod":
        print("\n  Production Mode")
        print("  If you are using a Lab VM, get it whitelisted by the LAB team.")
        print("  Or use an Azure / AWS hosted VM.")
        config["cloud_name"] = ""
        config["ca_ip"] = ""
        config["smui_ip"] = ""
        config["smui_port"] = "443"
        config["cdss_ip"] = ""
        config["build_path"] = ""
    else:
        config["cloud_name"] = ask("  Cloud name (e.g. zscalerbeta): ")

        # Network (ZADP needs CA + SMUI)
        print("\n  -- Network --")
        config["ca_ip"] = ask("  CA server IP: ")
        config["smui_ip"] = ask("  SMUI server IP: ")
        config["smui_port"] = ask("  SMUI port: ", default="443")

        # CDSS
        print("\n  -- CDSS (for automatic build download) --")
        config["cdss_ip"] = ask("  CDSS IP (leave empty to skip): ", required=False)

        # Build
        print("\n  -- Build --")
        config["build_path"] = ask(
            "  Build URL (e.g. https://build24.eng.zscaler.com/...sh, leave empty for manual): ",
            required=False,
        )

    return config


def collect_ir_inputs():
    """Ask the user for all IR-related inputs."""

    print("\n" + "-" * 60)
    print("  IR (Incident Receiver) - Tell me about your setup")
    print("-" * 60)

    config = {}

    # SSH
    print("\n  -- Server Access --")
    config["server_ip"] = ask("  Server IP (the IR VM): ")
    config["ssh_username"] = ask("  SSH Username: ", default="zsroot")
    config["ssh_password"] = ask_password("  SSH Password: ")
    config["ssh_port"] = int(ask("  SSH Port: ", default="22"))

    # Certificate
    print("\n  -- Certificate --")
    config["zip_file"] = ask("  Certificate zip filename (e.g. IncidentReceiverCertificate_xyz.zip): ")
    print(f"\n  Make sure the file is at /home/zsroot/{config['zip_file']} on the server.")
    print(f"  If not, copy it now:")
    print(f"    scp {config['zip_file']} {config['ssh_username']}@{config['server_ip']}:/home/zsroot/")

    # IR Directory
    print("\n  -- IR Storage Directory --")
    config["ir_dir"] = ask("  IR directory path on server: ", default="/home/zsroot/IR")

    # Environment
    print("\n  -- Environment --")
    config["setup_type"] = ask_choice("  Local or Production? (local/prod): ", ["local", "prod"])

    if config["setup_type"] == "prod":
        print("\n  Production Mode")
        print("  If you are using a Lab VM, get it whitelisted by the LAB team.")
        print("  Or use an Azure / AWS hosted VM.")
        config["cloud_name"] = ""
        config["cdss_ip"] = ""
        config["build_path"] = ""
    else:
        config["cloud_name"] = ask("  Cloud name (e.g. zscalerbeta): ")

        # CDSS
        print("\n  -- CDSS (for automatic build download) --")
        config["cdss_ip"] = ask("  CDSS IP (leave empty to skip): ", required=False)

        # Build
        print("\n  -- Build --")
        config["build_path"] = ask(
            "  Build URL (e.g. https://build24.eng.zscaler.com/...sh, leave empty for manual): ",
            required=False,
        )

    return config


# ====================================================================== #
#                                                                        #
#  PART 6: Main - Ties everything together                               #
#                                                                        #
# ====================================================================== #

def show_summary(service, config):
    """Print a summary of what we're about to do."""
    name = "ZADP" if service == "zadp" else "Incident Receiver"
    is_local = config["setup_type"] == "local"
    mode = "LOCAL" if is_local else "PRODUCTION"

    print("\n" + "=" * 60)
    print(f"  Review - {name} ({mode})")
    print("=" * 60)
    print(f"  Server       : {config['ssh_username']}@{config['server_ip']}")
    print(f"  Zip file     : {config['zip_file']}")

    if is_local and config.get("cloud_name"):
        print(f"  Cloud        : {config['cloud_name']}")

    if not is_local:
        print(f"\n  Production mode — ensure VM is whitelisted or use Azure/AWS.")

    if service == "zadp" and is_local:
        print(f"  CA IP        : {config.get('ca_ip', '')}")
        print(f"  SMUI IP      : {config.get('smui_ip', '')}:{config.get('smui_port', '443')}")

    if service == "ir":
        print(f"  IR Directory : {config.get('ir_dir', '/home/zsroot/IR')}")

    if is_local:
        if config.get("cdss_ip"):
            print(f"  CDSS IP      : {config['cdss_ip']}")

        if config.get("build_path"):
            print(f"  Build URL    : {config['build_path']}")

        if service == "zadp":
            print(f"\n  Local-only extras:")
            print(f"    - /etc/hosts will be updated")
            print(f"    - zadp_qa_ca_cert=1 added to sc.conf")
            print(f"    - zsapi_insecure=1 added to sc.conf")


def main():
    # -- Welcome banner --
    print()
    print("=" * 60)
    print("  ZADP & Incident Receiver - Setup Installer")
    print("=" * 60)
    print()
    print("  This script will help you install ZADP or IR")
    print("  on a remote server (local or production).")
    print()
    print("  Prerequisites:")
    print("    ZADP : Download certificate from SMUI >")
    print("           Administration > Index Tools > Add Index Tool")
    print("    IR   : Download certificate from SMUI >")
    print("           Administration > DLP Incident Receiver > Add IR")
    print()

    # -- What to install? --
    print("  What do you want to install?")
    print("    1) ZADP")
    print("    2) IR (Incident Receiver - SFTP)")
    print()

    while True:
        choice = input("  Enter 1 or 2: ").strip()
        if choice in ("1", "2"):
            break
        print("  Please enter 1 or 2.")

    service = "zadp" if choice == "1" else "ir"

    # -- Collect inputs --
    if service == "zadp":
        config = collect_zadp_inputs()
    else:
        config = collect_ir_inputs()

    # -- Show summary --
    show_summary(service, config)

    # -- Confirm --
    print()
    confirm = input("  Ready to start? (yes/no) [yes]: ").strip().lower()
    if confirm and confirm not in ("yes", "y", ""):
        print("\n  Cancelled. No changes made.")
        return

    # -- Connect and run --
    ssh = SSHHelper(
        host=config["server_ip"],
        username=config["ssh_username"],
        password=config["ssh_password"],
        port=config.get("ssh_port", 22),
    )

    try:
        ssh.connect()

        if service == "zadp":
            success = run_zadp_setup(ssh, config)
        else:
            success = run_ir_setup(ssh, config)

        if success:
            print("\n  All done!")
        else:
            print("\n  Setup did not complete. Check the messages above.")

    except KeyboardInterrupt:
        print("\n\n  Interrupted. Exiting.")
    except Exception as e:
        print(f"\n  ERROR: {e}")
        print(f"  For help, contact: amiglani@zscaler.com")
    finally:
        ssh.disconnect()


if __name__ == "__main__":
    main()
