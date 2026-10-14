"""Run batches on an HPC cluster (Slurm) from this computer, over SSH.

Each user sets up their own cluster: the code installed there (a copy of this repository with
its .venv), and here a profile (data/hpc_settings.json, never committed) plus, if they log in
with a password, that password in the operating system's credential store (Windows Credential
Manager, via `keyring`), never in a file. With an SSH key nothing is stored.

What a batch does on the cluster is pipeline/batch.py: inputs on the login node (where
compute nodes have no internet, as on Lemhi), the model as a Slurm array, then the USGS
comparison and the summary. From here: upload the fire list, start that in the background
on the login node, read its status files, and copy each finished fire's map-ready files back
so the map can show it. Slurm only for now; the scheduler calls are in pipeline/batch.py.
"""

import json
import posixpath
import re
import shlex
import stat
import sys
from pathlib import Path

import keyring
import paramiko

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from fires import PROJECT_SUFFIXES  # noqa: E402
SETTINGS = ROOT / "data" / "hpc_settings.json"
KNOWN_HOSTS = ROOT / "data" / "hpc_known_hosts"      # hosts first seen from this app
SERVICE = "debris-flow-hunters"
DEFAULTS = {
    "name": "", "host": "", "port": 22, "user": "",
    "jump_host": "", "jump_port": 22, "jump_user": "",
    "auth": "key",                 # key | password
    "key_path": "",                # blank: the usual keys in ~/.ssh and any SSH agent
    "remote_dir": "",              # the repository on the cluster, e.g. ~/scratch/Debris-Flow-Hunters
    "setup": "source .venv/bin/activate",
    "partition": "tiny", "max_running": 20,
    "staged": True,                # compute nodes have no internet: inputs on the login node
    "poll_s": 30,
}
HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")
USER_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


# ---------------------------------------------------------------- settings and secrets

def load():
    s = dict(DEFAULTS)
    if SETTINGS.exists():
        s.update({k: v for k, v in json.loads(SETTINGS.read_text()).items() if k in DEFAULTS})
    return s


def configured(s=None):
    s = s or load()
    return bool(s["host"] and s["user"] and s["remote_dir"])


def validate(s):
    errors = []
    for k in ("host", "jump_host"):
        if s[k] and not HOST_RE.match(s[k]):
            errors.append(f"{k}: a host name like lemhi.c3plus3.org")
    for k in ("user", "jump_user"):
        if s[k] and not USER_RE.match(s[k]):
            errors.append(f"{k}: letters, digits, . _ - only")
    if s["auth"] not in ("key", "password"):
        errors.append("auth: key or password")
    if s["partition"] and not re.fullmatch(r"[\w\-]{1,32}", s["partition"]):
        errors.append("partition: a Slurm partition name")
    if any(c in s["remote_dir"] for c in "\n\r;&|`$<>\"'"):
        errors.append("remote_dir: a folder path, e.g. ~/scratch/Debris-Flow-Hunters")
    for k in ("port", "jump_port", "max_running", "poll_s"):
        try:
            s[k] = int(s[k])
        except (TypeError, ValueError):
            errors.append(f"{k}: a number")
    return errors


def save(fields):
    s = load()
    s.update({k: (v.strip() if isinstance(v, str) else v) for k, v in fields.items() if k in DEFAULTS})
    s["staged"] = bool(s["staged"])
    errors = validate(s)
    if errors:
        raise ValueError("; ".join(errors))
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(s, indent=1))
    return s


def _secret_name(s, which):
    return (f"hpc:{s['user']}@{s['host']}" if which == "main"
            else f"hpc-jump:{s['jump_user'] or s['user']}@{s['jump_host']}")


# The operating system's password store, through keyring: Windows Credential Manager, the
# macOS Keychain, or the Secret Service (GNOME Keyring, KWallet) on a Linux desktop. A Linux
# machine without one (no desktop) has no store: passwords cannot be saved there, SSH keys work.
STORE = {"win32": "Windows Credential Manager", "darwin": "macOS Keychain"}.get(sys.platform,
                                                                                "the system keyring")


def _get_password(name):
    try:
        return keyring.get_password(SERVICE, name)
    except keyring.errors.KeyringError:        # no password store on this computer
        return None


def set_password(s, password, which="main"):
    try:
        keyring.set_password(SERVICE, _secret_name(s, which), password)
    except keyring.errors.KeyringError as e:
        raise RuntimeError(f"this computer has no password store to keep it in ({e}); "
                           "log in with an SSH key instead") from None


def clear_password(s, which="main"):
    try:
        keyring.delete_password(SERVICE, _secret_name(s, which))
    except keyring.errors.KeyringError:        # not saved, or no password store
        pass


def has_password(s, which="main"):
    return _get_password(_secret_name(s, which)) is not None


def public(s=None):
    """The profile for the browser: everything but secrets, plus whether passwords are set."""
    s = s or load()
    return {**s, "configured": configured(s), "password_store": STORE,
            "has_password": has_password(s) if s["host"] else False,
            "has_jump_password": has_password(s, "jump") if s["jump_host"] else False}


# ---------------------------------------------------------------- connection

class Connection:
    """An SSH session to the cluster's login node, through the jump host if one is set."""

    def __init__(self, s=None):
        self.s = s or load()
        if not configured(self.s):
            raise RuntimeError("HPC is not set up: open Settings > HPC")
        self.jump = self.client = self._sftp = None
        self.fingerprints = {}

    def _client(self):
        c = paramiko.SSHClient()
        c.load_system_host_keys()
        if KNOWN_HOSTS.exists():
            c.load_host_keys(str(KNOWN_HOSTS))
        # first connection to a host: accept its key and remember it here (shown in the test)
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        return c

    def _auth(self, which):
        """Each login (jump host, cluster) tries SSH keys and the agent first, then its saved
        password, so a gateway that wants a password and a cluster that takes a key both work.
        A jump host with no password of its own is tried with the cluster's."""
        s = self.s
        pw = _get_password(_secret_name(s, which))
        if which == "jump" and pw is None:
            pw = _get_password(_secret_name(s, "main"))
        if which == "main" and s["auth"] == "password" and pw is None:
            raise RuntimeError("no password saved for the HPC: Settings > HPC > Password")
        key = {"key_filename": str(Path(s["key_path"]).expanduser())} if s["key_path"] else {}
        return {"password": pw, "look_for_keys": True, "allow_agent": True, **key}

    def __enter__(self):
        s = self.s
        sock = None
        hop = s["jump_host"] or s["host"]         # named in the error, so a jump-host failure is clear
        try:
            if s["jump_host"]:
                self.jump = self._client()
                self.jump.connect(s["jump_host"], s["jump_port"], s["jump_user"] or s["user"],
                                  timeout=30, banner_timeout=30, auth_timeout=60, **self._auth("jump"))
                self.fingerprints[s["jump_host"]] = _fingerprint(self.jump)
                sock = self.jump.get_transport().open_channel(
                    "direct-tcpip", (s["host"], s["port"]), ("127.0.0.1", 0))
            hop = s["host"]
            self.client = self._client()
            self.client.connect(s["host"], s["port"], s["user"], sock=sock, timeout=30,
                                banner_timeout=30, auth_timeout=60, **self._auth("main"))
            self.fingerprints[s["host"]] = _fingerprint(self.client)
            KNOWN_HOSTS.parent.mkdir(parents=True, exist_ok=True)
            self.client.save_host_keys(str(KNOWN_HOSTS))
        except paramiko.AuthenticationException:
            self.close()
            raise RuntimeError(f"{hop} refused the login (check the user name, the password "
                               "or key, and that this account can log in without a code prompt)") from None
        except (OSError, paramiko.SSHException) as e:
            self.close()
            hint = " (use the full host name, e.g. lemhi.c3plus3.org)" if "getaddrinfo" in str(e) and "." not in hop else ""
            raise RuntimeError(f"could not connect to {hop}: {e}{hint}") from None
        self.home = self.run("pwd")[1].strip()
        self.root = self.expand(s["remote_dir"])
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        for x in (self._sftp, self.client, self.jump):
            try:
                x and x.close()
            except Exception:
                pass

    def expand(self, path):
        return posixpath.join(self.home, path[2:]) if path.startswith("~/") else \
            (self.home if path == "~" else path)

    def run(self, command, timeout=120):
        """(exit code, stdout, stderr) of a shell command on the login node."""
        _, out, err = self.client.exec_command(command, timeout=timeout)
        code = out.channel.recv_exit_status()
        return code, out.read().decode(errors="replace"), err.read().decode(errors="replace")

    def in_repo(self, command, timeout=120):
        """A command in the repository folder with its environment set up."""
        return self.run(f"cd {shlex.quote(self.root)} && {self.s['setup']} && {command}", timeout)

    @property
    def sftp(self):
        if self._sftp is None:
            self._sftp = self.client.open_sftp()
        return self._sftp

    def read(self, rel):
        with self.sftp.open(posixpath.join(self.root, rel)) as f:
            return f.read().decode("utf-8", errors="replace")

    def exists(self, rel):
        try:
            self.sftp.stat(posixpath.join(self.root, rel))
            return True
        except OSError:
            return False

    def listdir(self, rel):
        try:
            return self.sftp.listdir_attr(posixpath.join(self.root, rel))
        except OSError:
            return []


def _fingerprint(client):
    key = client.get_transport().get_remote_server_key()
    return f"{key.get_name()} {key.get_fingerprint().hex(':')}"


# ---------------------------------------------------------------- what the app does there

def test(s=None):
    """Checks, in order, each with ok and a detail: login, the repository, its Python
    environment (wildcat), the batch script, Slurm."""
    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail.strip()[:300]})
        return ok

    try:
        with Connection(s) as c:
            add("login", True, f"{c.s['user']}@{c.s['host']}"
                + (f" via {c.s['jump_host']}" if c.s['jump_host'] else "")
                + "; host keys: " + "; ".join(f"{h} {fp}" for h, fp in c.fingerprints.items()))
            code, out, err = c.run(f"test -d {shlex.quote(c.root)} && echo yes || echo no")
            if not add("repository folder", out.strip() == "yes", c.root):
                return checks
            # neither package has __version__; the installed metadata does
            code, out, err = c.in_repo("python -c \"import wildcat, pfdf; from importlib.metadata import version; "
                                       "print('wildcat', version('wildcat'), 'pfdf', version('pfdf'))\"")
            add("Python environment", code == 0, out or err)
            present = c.exists("pipeline/batch.py") and c.exists("slurm/batch_fires.slurm")
            # an older batch.py ignores --partition (unknown options are skipped), so say so
            current = present and "--partition" in c.read("pipeline/batch.py")
            add("batch script", current,
                "pipeline/batch.py and slurm/batch_fires.slurm" + (
                    "" if current else
                    ": older copy, without --partition; copy this repository's pipeline/batch.py there"
                    if present else ": missing, copy this repository's code there"))
            code, out, err = c.run("command -v sbatch && sinfo -h -o '%P %a %l' | head -5")
            add("Slurm", code == 0, out or err or "sbatch not found")
    except RuntimeError as e:
        add("login", False, str(e))
    return checks


def start_batch(name, lines, s=None):
    """Upload the list and start the batch on the login node in the background; returns at once."""
    s = s or load()
    lst = "\n".join(lines) + "\n"
    staged = "" if s["staged"] else " --compute-internet"
    command = (f"python pipeline/batch.py all data/batches/{name}.txt --name {name} --runner slurm "
               f"--max-running {int(s['max_running'])} --partition {shlex.quote(s['partition'])}"
               f"{staged} --wait")
    with Connection(s) as c:
        c.run(f"mkdir -p {shlex.quote(posixpath.join(c.root, 'data', 'batches', name))} "
              f"{shlex.quote(posixpath.join(c.root, 'logs'))}")
        with c.sftp.open(posixpath.join(c.root, "data", "batches", f"{name}.txt"), "w") as f:
            f.write(lst)
        log = f"data/batches/{name}/app.log"
        done = f"data/batches/{name}/app.exit"
        # detached from this session; the exit code lands in app.exit when the batch is over.
        # Only the nohup command goes to the background, with every stream redirected: a
        # backgrounded `cd && nohup ...` list is a subshell that keeps the SSH channel open
        # until the batch ends, so reading this command's output would hang.
        inner = f"cd {shlex.quote(c.root)} && {s['setup']} && {command}; echo $? > {done}"
        code, out, err = c.run(f"cd {shlex.quote(c.root)} || exit 1; "
                               f"nohup setsid bash -c {shlex.quote(inner)} > {log} 2>&1 < /dev/null & "
                               f"echo started", timeout=60)
        if code or "started" not in out:
            raise RuntimeError(f"could not start the batch on the cluster: {err or out}")


def remote_state(name, s=None, conn=None):
    """The batch as the cluster sees it: per-fire status (from batch.py's status files), the
    tail of its log, and whether it has finished."""
    def read(c):
        fires, statuses = [], {}
        if c.exists(f"data/batches/{name}/fires.tsv"):
            text = c.read(f"data/batches/{name}/fires.tsv").splitlines()
            head = text[0].split("\t")
            fires = [dict(zip(head, line.split("\t"))) for line in text[1:] if line.strip()]
        for a in c.listdir(f"data/batches/{name}/status"):
            if a.filename.endswith(".json") and stat.S_ISREG(a.st_mode):
                statuses[a.filename[:-5]] = json.loads(c.read(f"data/batches/{name}/status/{a.filename}"))
        log = c.read(f"data/batches/{name}/app.log") if c.exists(f"data/batches/{name}/app.log") else ""
        exit_code = c.read(f"data/batches/{name}/app.exit").strip() if c.exists(f"data/batches/{name}/app.exit") else None
        return {"fires": fires, "status": statuses, "log_tail": "\n".join(log.splitlines()[-12:]),
                "finished": exit_code is not None, "exit": exit_code}
    if conn:
        return read(conn)
    with Connection(s) as c:
        return read(c)


def fetch_fire(c, key, config_rel):
    """Copy one finished fire's map-ready files from the cluster: its config and, from its
    project, the exports, perimeter, preprocessed DEM and severity, and the USGS check.
    Returns the local project folder, or None if the cluster has no finished project."""
    for suffix in PROJECT_SUFFIXES:
        project = f"data/{key}{suffix}"
        if c.exists(f"{project}/exports/segments.shp"):
            break
    else:
        return None
    wanted = [config_rel, f"{project}/usgs_check.json", f"{project}/sources.json",
              f"{project}/configuration.py", f"{project}/preprocessed/dem.tif",
              f"{project}/preprocessed/severity.tif"]
    wanted += [f"{project}/exports/{a.filename}" for a in c.listdir(f"{project}/exports")
               if stat.S_ISREG(a.st_mode)]
    wanted += [f"{project}/inputs/{a.filename}" for a in c.listdir(f"{project}/inputs")
               if a.filename.startswith("perimeter.")]
    for rel in wanted:
        if not c.exists(rel):
            continue
        local = ROOT / Path(*rel.split("/"))
        local.parent.mkdir(parents=True, exist_ok=True)
        part = local.with_name(local.name + ".part")
        c.sftp.get(posixpath.join(c.root, rel), str(part))
        part.replace(local)
    return ROOT / project
