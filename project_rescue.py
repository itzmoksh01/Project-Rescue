#!/usr/bin/env python3
"""
Project Rescue - lose the folder, keep the project.

An always-on safety net for every project you work on with an AI coding agent:
  * exact, incremental local mirror of the project (every run = a restorable version)
  * private GitHub copy (secrets like .env / keys are never sent to GitHub)
  * your project's own .git history is preserved (bundle), so restore is complete
  * one-command install for Claude Code (hooks + standing orders + /rescue command)
  * one-command rescue after data loss

Everyday commands:  install | connect | auto | run | status | rescue | list
Advanced:           init | watch | restore | hook | agent-hook | uninstall
Needs: Python 3.8+ and git. Optional: gh (GitHub CLI) to auto-create private repos.
"""
import argparse
import datetime
import fnmatch
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

VERSION = "1.0.0"
HOME_DIR = os.environ.get("RESCUE_HOME") or os.path.join(os.path.expanduser("~"), ".project-rescue")
CFG_DIR = os.path.join(HOME_DIR, "projects")
BUNDLE = ".rescue-git.bundle"
MANIFEST = ".rescue-manifest.json"
RESERVED = {".git", BUNDLE, MANIFEST}  # live at mirror root, never treated as project files
DEFAULT_EXCLUDES = ["node_modules", "__pycache__", ".venv", "venv", ".pytest_cache",
                    ".mypy_cache", ".DS_Store", "Thumbs.db", "*.pyc"]
SECRET_PATTERNS = [".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks",
                   "id_rsa*", "id_ed25519*", "id_ecdsa*", "id_dsa*", "credentials.json",
                   "client_secret*.json", "service-account*.json", "serviceaccount*.json",
                   "secrets.json", "secrets.yml", "secrets.yaml", "secrets.toml",
                   ".npmrc", ".pypirc", ".netrc", ".htpasswd", "*.tfstate", "*.tfstate.*", ".git-credentials", ".pgpass", "*.kdbx", "terraform.tfvars", "*.auto.tfvars"]
SECRET_SAFE = [".env.example", ".env.sample", ".env.template", ".env.dist", "*.pub"]
GITHUB_FILE_LIMIT = 95 * 1024 * 1024  # GitHub rejects files > 100 MB
MTIME_TOLERANCE_NS = 20_000_000
DROP_ENV = ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_PREFIX", "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
            "GIT_QUARANTINE_PATH", "GIT_INTERNAL_SUPER_PREFIX"]

QUIET = False


# ----------------------------------------------------------------- helpers
def say(msg):
    if not QUIET:
        print(msg, flush=True)


def warn(msg):
    print("project-rescue: warning: " + msg, file=sys.stderr, flush=True)


def die(msg, code=1):
    print("project-rescue: error: " + msg, file=sys.stderr, flush=True)
    sys.exit(code)


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def git(args, cwd=None, git_dir=None, work_tree=None, check=True, timeout=900, text=True):
    cmd = ["git"]
    if git_dir:
        cmd += ["--git-dir", git_dir]
    if work_tree:
        cmd += ["--work-tree", work_tree]
    cmd += args
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        die("git is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        if check:
            raise RuntimeError("git %s timed out" % " ".join(args[:2]))
        return subprocess.CompletedProcess(cmd, 124, b"", b"timeout")
    if text:
        r.stdout = r.stdout.decode("utf-8", "surrogateescape")
        r.stderr = r.stderr.decode("utf-8", "surrogateescape")
    if check and r.returncode != 0:
        err = r.stderr if text else r.stderr.decode("utf-8", "replace")
        raise RuntimeError("git %s failed: %s" % (" ".join(args[:3]), err.strip()))
    return r


def mgit(cfg, args, **kw):
    """git against the mirror repo, explicit git-dir so we can never touch another repo."""
    return git(args, git_dir=os.path.join(cfg["mirror"], ".git"), work_tree=cfg["mirror"], **kw)


def pgit(cfg, args, **kw):
    """git against the project's own repo."""
    return git(["-C", cfg["project"]] + args, **kw)


def slug_for(project):
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(project.rstrip("/\\")) or "project")
    return "%s-%s" % (base, hashlib.sha1(project.encode("utf-8", "surrogateescape")).hexdigest()[:8])


def cfg_path(slug):
    return os.path.join(CFG_DIR, slug + ".cfg.json")


def state_path(slug):
    return os.path.join(CFG_DIR, slug + ".state.json")


def all_cfgs():
    out = []
    for f in sorted(glob.glob(os.path.join(CFG_DIR, "*.cfg.json"))):
        c = load_json(f, None)
        if c and "project" in c:
            out.append(c)
    return out


def find_cfg(project_arg=None):
    start = os.path.realpath(project_arg or os.getcwd())
    best = None
    for c in all_cfgs():
        p = c["project"]
        if start == p or start.startswith(p.rstrip(os.sep) + os.sep):
            if best is None or len(p) > len(best["project"]):
                best = c
    return best


def load_cfg(project_arg=None):
    best = find_cfg(project_arg)
    if not best:
        die("no backup is set up for %s. Run:  python3 %s auto   (or init --project <folder>)" % (
            os.path.realpath(project_arg or os.getcwd()), os.path.abspath(__file__)))
    return best


def is_within(a, b):
    a, b = os.path.realpath(a), os.path.realpath(b)
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


# ------------------------------------------------------------ file scanning
def excluded(rel, name, patterns):
    for p in patterns:
        p = p.strip()
        if not p:
            continue
        if "/" in p:
            if fnmatch.fnmatchcase(rel, p.strip("/")):
                return True
        elif fnmatch.fnmatchcase(name, p):
            return True
    return False


def scan_tree(root, excludes, is_mirror=False, limit=0):
    """Return ({rel: (kind, size, mtime_ns, link_target)}, [errors])."""
    items, errors = {}, []
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        abs_dir = os.path.join(root, rel_dir) if rel_dir else root
        try:
            entries = list(os.scandir(abs_dir))
        except OSError as e:
            errors.append("cannot read folder %s: %s" % (abs_dir, e))
            continue
        for e in entries:
            name = e.name
            rel = rel_dir + "/" + name if rel_dir else name
            if is_mirror:
                if not rel_dir and name in RESERVED:
                    continue
            else:
                if name == ".git" or excluded(rel, name, excludes):
                    continue
            try:
                if e.is_symlink():
                    items[rel] = ("link", 0, 0, os.readlink(e.path))
                elif e.is_dir(follow_symlinks=False):
                    items[rel] = ("dir", 0, 0, None)
                    stack.append(rel)
                elif e.is_file(follow_symlinks=False):
                    st = e.stat(follow_symlinks=False)
                    items[rel] = ("file", st.st_size, st.st_mtime_ns, None)
                # sockets / fifos / devices are skipped on purpose
            except OSError as ex:
                errors.append("cannot read %s: %s" % (rel, ex))
            if limit and len(items) > limit:
                errors.append("LIMIT")
                return items, errors
    return items, errors


def make_plan(src, dst):
    dels = [r for r in dst if r not in src or dst[r][0] != src[r][0]]
    dels.sort(key=lambda r: r.count("/"), reverse=True)
    copies = []
    for r in sorted(src):
        s = src[r]
        d = dst.get(r)
        if d is None or d[0] != s[0]:
            copies.append((r, "add"))
        elif s[0] == "file" and (s[1] != d[1] or abs(s[2] - d[2]) >= MTIME_TOLERANCE_NS):
            copies.append((r, "update"))
        elif s[0] == "link" and s[3] != d[3]:
            copies.append((r, "update"))
    return dels, copies


def _rm_readonly(func, path, _exc):
    try:
        os.chmod(path, 0o700)
        func(path)
    except OSError:
        pass


def apply_plan(project, mirror, src, dst, dels, copies):
    errors = []
    stats = {"added": 0, "updated": 0, "deleted": 0}
    for r in dels:
        p = os.path.join(mirror, r)
        try:
            if dst[r][0] == "dir":
                shutil.rmtree(p, onerror=_rm_readonly)
            else:
                try:
                    os.remove(p)
                except PermissionError:
                    os.chmod(p, 0o600)
                    os.remove(p)
            stats["deleted"] += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            errors.append("cannot delete %s: %s" % (r, e))
    for r, why in copies:
        sp, dp = os.path.join(project, r), os.path.join(mirror, r)
        kind = src[r][0]
        try:
            if kind == "dir":
                os.makedirs(dp, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            if kind == "link":
                if os.path.lexists(dp):
                    os.remove(dp)
                os.symlink(src[r][3], dp)
            else:
                tmp = os.path.join(os.path.dirname(dp), ".pbk-%d.tmp" % os.getpid())
                shutil.copy2(sp, tmp)
                os.replace(tmp, dp)
            stats["added" if why == "add" else "updated"] += 1
        except FileNotFoundError:
            errors.append("vanished while copying (will retry next run): " + r)
        except OSError as e:
            errors.append("cannot copy %s: %s" % (r, e))
    return stats, errors


# --------------------------------------------------------------------- lock
class Lock(object):
    def __init__(self, slug):
        self.path = os.path.join(CFG_DIR, slug + ".lock")
        self.flag = os.path.join(CFG_DIR, slug + ".rerun")

    def _stale(self):
        try:
            age = time.time() - os.path.getmtime(self.path)
            if age > 1800:
                return True
            if os.name != "nt":
                pid = int(open(self.path).read().strip() or 0)
                if pid > 0:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return True
                    except PermissionError:
                        return False
        except (OSError, ValueError):
            return True
        return False

    def acquire(self):
        os.makedirs(CFG_DIR, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except FileExistsError:
                if self._stale():
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass
                    continue
                return False
        return False

    def release(self):
        try:
            os.remove(self.path)
        except OSError:
            pass


# ------------------------------------------------------------------- github
def parse_github(url):
    m = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$", url or "")
    return (m.group(1), m.group(2)) if m else None


def github_private(url):
    """True / False / None (unknown)."""
    gh = parse_github(url)
    if not gh or not shutil.which("gh"):
        return None
    try:
        r = subprocess.run(["gh", "repo", "view", "%s/%s" % gh, "--json", "isPrivate", "-q", ".isPrivate"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.strip().lower()
    return True if out == "true" else False if out == "false" else None


def resolve_remote(value, create):
    """Return the git URL for --github. Optionally creates a PRIVATE repo with gh."""
    looks_url = ("://" in value or value.startswith("git@") or value.endswith(".git")
                 or os.path.isdir(value) or os.path.isabs(value))
    if looks_url:
        if create:
            warn("--create ignored: %s is already a full URL/path" % value)
        return value
    if not re.match(r"^[\w.-]+(/[\w.-]+)?$", value):
        die("--github must be owner/repo, a repo name (with --create), or a full git URL")
    if not create:
        if "/" not in value:
            die("give owner/repo, or add --create to make a new private repo named '%s'" % value)
        return "https://github.com/%s.git" % value
    if not shutil.which("gh"):
        die("--create needs the GitHub CLI (gh). Install it and run `gh auth login`, "
            "or create an EMPTY PRIVATE repo on github.com yourself and pass --github owner/repo")
    if subprocess.run(["gh", "auth", "status"], capture_output=True).returncode != 0:
        die("gh is not logged in. Run `gh auth login` first")
    r = subprocess.run(["gh", "repo", "create", value, "--private"], capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        for line in reversed((r.stdout + r.stderr).splitlines()):
            if line.strip().startswith("http"):
                return line.strip().rstrip("/") + ".git"
    # maybe it already exists: accept only if private
    v = subprocess.run(["gh", "repo", "view", value, "--json", "url,isPrivate"], capture_output=True, text=True)
    if v.returncode == 0:
        j = json.loads(v.stdout)
        if not j.get("isPrivate"):
            die("repo %s already exists and is PUBLIC. Refusing; pick another name or make it private" % value)
        return j["url"].rstrip("/") + ".git"
    die("could not create repo: " + (r.stderr or r.stdout).strip())


def do_push(cfg, state):
    url = cfg.get("remote")
    if not url:
        return True
    ts = state.get("visibility_checked_at", 0)
    if parse_github(url) and not cfg.get("allow_public") and time.time() - ts > 3600:
        priv = github_private(url)
        state["visibility_checked_at"] = time.time()
        if priv is False:
            warn("GitHub repo is PUBLIC - push refused. Make it private (or re-run init with --allow-public).")
            return False
    r = mgit(cfg, ["push", "-q", "origin", "HEAD:refs/heads/" + cfg.get("branch", "main")],
             check=False, timeout=1800)
    if r.returncode != 0:
        msg = (r.stderr or "").strip()
        hint = ""
        if re.search(r"authentication|permission denied|credential|40[13]", msg, re.I):
            hint = " Hint: run `gh auth login` then `gh auth setup-git`, or use an SSH remote."
        elif re.search(r"rejected|non-fast-forward|fetch first", msg, re.I):
            hint = " The remote has commits this backup lacks (another machine or manual edit). Use a dedicated, empty repo."
        warn("GitHub push failed (local backup is fine; will retry next run): %s.%s" % (msg.splitlines()[-1] if msg else "?", hint))
        return False
    return True


# ------------------------------------------------------------ mirror repo
def ensure_repo(cfg):
    mirror = cfg["mirror"]
    if not os.path.isdir(os.path.join(mirror, ".git")):
        os.makedirs(mirror, exist_ok=True)
        git(["init", "-q", mirror])
        mgit(cfg, ["symbolic-ref", "HEAD", "refs/heads/" + cfg.get("branch", "main")])
    for k, v in (("core.autocrlf", "false"), ("core.safecrlf", "false"), ("core.longpaths", "true"),
                 ("core.quotepath", "false"), ("commit.gpgsign", "false"), ("gc.auto", "256")):
        mgit(cfg, ["config", k, v])
    if not mgit(cfg, ["config", "user.name"], check=False).stdout.strip():
        mgit(cfg, ["config", "user.name", "project-rescue"])
    if not mgit(cfg, ["config", "user.email"], check=False).stdout.strip():
        mgit(cfg, ["config", "user.email", "project-rescue@localhost"])
    url = cfg.get("remote")
    have = mgit(cfg, ["remote", "get-url", "origin"], check=False)
    if url:
        if have.returncode != 0:
            mgit(cfg, ["remote", "add", "origin", url])
        elif have.stdout.strip() != url:
            mgit(cfg, ["remote", "set-url", "origin", url])
    elif have.returncode == 0:
        mgit(cfg, ["remote", "remove", "origin"], check=False)


def is_secret(rel):
    base = rel.rsplit("/", 1)[-1].lower()
    if any(fnmatch.fnmatchcase(base, p) for p in SECRET_SAFE):
        return False
    if any(fnmatch.fnmatchcase(base, p) for p in SECRET_PATTERNS):
        return True
    parts = rel.lower().split("/")
    if ".ssh" in parts[:-1] or (".aws" in parts[:-1] and base == "credentials"):
        return True
    return False


def write_manifest_and_bundle(cfg, state):
    project, mirror = cfg["project"], cfg["mirror"]
    has_git = os.path.exists(os.path.join(project, ".git"))
    man = {"tool": "project-rescue", "version": VERSION, "project_name": os.path.basename(project),
           "original_path": project, "excludes": cfg["excludes"], "has_git_history": False}
    warnings = []
    if has_git and pgit(cfg, ["rev-parse", "--verify", "-q", "HEAD"], check=False).returncode == 0:
        refs = pgit(cfg, ["for-each-ref"], check=False).stdout
        head_sym = pgit(cfg, ["symbolic-ref", "-q", "HEAD"], check=False).stdout.strip()
        head_sha = pgit(cfg, ["rev-parse", "HEAD"], check=False).stdout.strip()
        remotes = {}
        for line in pgit(cfg, ["config", "--get-regexp", r"^remote\..*\.url$"], check=False).stdout.splitlines():
            k, _, v = line.partition(" ")
            remotes[k[len("remote."):-len(".url")]] = v
        man.update({"has_git_history": True, "head_ref": head_sym or None,
                    "head_commit": head_sha, "remotes": remotes})
        h = hashlib.sha1((refs + head_sym + head_sha).encode("utf-8", "surrogateescape")).hexdigest()
        bpath = os.path.join(mirror, BUNDLE)
        if h != state.get("refs_hash") or not os.path.exists(bpath):
            tmp = bpath + ".tmp"
            r = pgit(cfg, ["bundle", "create", tmp, "--all"], check=False)
            if r.returncode == 0:
                os.replace(tmp, bpath)
                state["refs_hash"] = h
            else:
                warnings.append("could not bundle git history: " + (r.stderr or "").strip()[:200])
                if os.path.exists(tmp):
                    os.remove(tmp)
    else:
        bpath = os.path.join(mirror, BUNDLE)
        if os.path.exists(bpath):
            os.remove(bpath)
        state.pop("refs_hash", None)
    text = json.dumps(man, indent=2, sort_keys=True)
    mp = os.path.join(mirror, MANIFEST)
    try:
        same = open(mp, "r", encoding="utf-8").read() == text
    except OSError:
        same = False
    if not same:
        with open(mp, "w", encoding="utf-8") as f:
            f.write(text)
    return warnings


def commit_mirror(cfg, state, stats, total):
    mgit(cfg, ["add", "-A", "-f", "--", "."])
    names = mgit(cfg, ["diff", "--cached", "--name-only", "-z", "--diff-filter=AM"]).stdout.split("\0")
    block, big = [], []
    for n in names:
        if not n:
            continue
        if is_secret(n):
            block.append(n)
            continue
        try:
            if os.path.getsize(os.path.join(cfg["mirror"], n)) > GITHUB_FILE_LIMIT:
                big.append(n)
        except OSError:
            pass
    for i in range(0, len(block + big), 100):
        chunk = (block + big)[i:i + 100]
        mgit(cfg, ["rm", "-r", "--cached", "-q", "-f", "--ignore-unmatch", "--"] + [":(literal)" + c for c in chunk])
    state["skipped_secrets"] = len(block)
    state["skipped_large"] = big
    pending = mgit(cfg, ["status", "--porcelain", "--untracked-files=no"]).stdout.strip()
    has_head = mgit(cfg, ["rev-parse", "--verify", "-q", "HEAD"], check=False).returncode == 0
    if has_head and not pending:
        return False
    msg = "backup %s: +%d ~%d -%d (%d files)" % (now(), stats["added"], stats["updated"], stats["deleted"], total)
    mgit(cfg, ["commit", "-q", "--no-verify", "--no-gpg-sign", "-m", msg])
    return True


# ------------------------------------------------------------- core backup
def clone_vault(url, dest):
    """Non-bare clone that also works when the remote's default branch is not `main`. Returns (ok, error)."""
    r = git(["clone", "-q", url, dest], check=False, timeout=3600)
    if r.returncode != 0:
        return False, r.stderr.strip()[-200:]
    if git(["-C", dest, "rev-parse", "--verify", "-q", "HEAD"], check=False).returncode != 0:
        git(["-C", dest, "checkout", "-q", "-B", "main", "origin/main"], check=False)
    if git(["-C", dest, "rev-parse", "--verify", "-q", "HEAD"], check=False).returncode != 0:
        return False, "the repository has no backup versions yet"
    return True, ""


def rehydrate_mirror(cfg):
    """Local backup folder lost but GitHub still has it: pull it back so history continues."""
    m = cfg["mirror"]
    if os.path.isdir(os.path.join(m, ".git")) or not cfg.get("remote"):
        return
    if os.path.isdir(m) and os.listdir(m):
        return
    tmp = m.rstrip(os.sep) + ".rehydrate"
    shutil.rmtree(tmp, ignore_errors=True)
    ok, _err = clone_vault(cfg["remote"], tmp)
    if ok:
        if os.path.isdir(m):
            os.rmdir(m)
        os.replace(tmp, m)
        say("local backup folder was missing - re-created from GitHub, history continues")
    else:
        shutil.rmtree(tmp, ignore_errors=True)


def backup_once(cfg, force=False, dry=False, push=True):
    """Return exit code: 0 ok, 1 error, 2 ok but some files failed, 3 ok but GitHub push failed."""
    project, mirror = cfg["project"], cfg["mirror"]
    if not os.path.isdir(project):
        warn("project folder is missing: %s (nothing was changed in the backup)" % project)
        return 1
    state = load_json(state_path(cfg["slug"]), {})
    rehydrate_mirror(cfg)
    ensure_repo(cfg)
    src, e1 = scan_tree(project, cfg["excludes"])
    dst, _ = scan_tree(mirror, [], True)
    dels, copies = make_plan(src, dst)
    src_files = sum(1 for v in src.values() if v[0] == "file")
    dst_files = sum(1 for v in dst.values() if v[0] == "file")
    del_files = sum(1 for r in dels if dst[r][0] == "file")
    if not force and dst_files >= 5 and (src_files == 0 or (del_files >= 5 and del_files > 0.3 * dst_files)):
        warn("SAFETY STOP: %d of %d backed-up files are missing from the project now. "
             "Looks like a mistaken delete or an unmounted drive. Backup NOT changed. "
             "If this is intentional run:  run --force   (old versions stay in git history anyway)." % (del_files, dst_files))
        state["blocked"] = "%d of %d backed-up files are missing from the project" % (del_files, dst_files)
        state["last_run_ts"] = time.time()
        save_json(state_path(cfg["slug"]), state)
        return 1
    if dry:
        say("dry run: %d to add/update, %d to delete" % (len(copies), len(dels)))
        for r, why in copies[:50]:
            say("  %s %s" % ("+" if why == "add" else "~", r))
        for r in dels[:50]:
            say("  - " + r)
        return 0
    stats, e2 = apply_plan(project, mirror, src, dst, dels, copies)
    errors = e1 + e2
    warns = write_manifest_and_bundle(cfg, state)
    for w in warns:
        warn(w)
    total = src_files
    committed = commit_mirror(cfg, state, stats, total)
    code = 0
    if cfg.get("remote") and push:
        if committed or state.get("pending_push") or not state.get("last_push_ok"):
            ok = do_push(cfg, state)
            state["last_push_ok"] = ok
            state["pending_push"] = not ok
            if ok:
                state["last_push_at"] = now()
            else:
                code = 3
        elif state.get("pending_push"):
            code = 3
    state.pop("blocked", None)
    state.update({"last_run": now(), "last_run_ts": time.time(), "last_stats": stats, "files": total,
                  "last_commit": mgit(cfg, ["rev-parse", "--short", "HEAD"], check=False).stdout.strip(),
                  "last_errors": errors[:20]})
    save_json(state_path(cfg["slug"]), state)
    say("backup ok %s: +%d ~%d -%d | %d files | %s%s" % (
        now(), stats["added"], stats["updated"], stats["deleted"], total,
        "new version " + state["last_commit"] if committed else "no changes",
        (" | GitHub: up to date" if code == 0 and cfg.get("remote") and state.get("last_push_ok") else "")))
    if state.get("skipped_secrets"):
        say("  %d secret file(s) (.env, keys...) are in the local backup but NOT sent to GitHub" % state["skipped_secrets"])
    if state.get("skipped_large"):
        warn("%d file(s) over 95 MB not sent to GitHub (kept in local backup): %s" % (
            len(state["skipped_large"]), ", ".join(state["skipped_large"][:3])))
    if errors:
        warn("%d file problem(s), e.g. %s" % (len(errors), errors[0]))
        return 2 if code == 0 else code
    return code


def run_with_lock(cfg, **kw):
    lock = Lock(cfg["slug"])
    if not lock.acquire():
        open(lock.flag, "w").close()
        say("another backup is running; queued a re-run")
        return 0
    try:
        code = 0
        for _ in range(3):
            if os.path.exists(lock.flag):
                try:
                    os.remove(lock.flag)
                except OSError:
                    pass
            code = backup_once(cfg, **kw)
            if not os.path.exists(lock.flag):
                break
        return code
    finally:
        lock.release()


# ---------------------------------------------------------------- commands
def cmd_init(a):
    project = os.path.realpath(a.project or os.getcwd())
    if not os.path.isdir(project):
        die("project folder does not exist: " + project)
    if project in (os.path.realpath(os.path.expanduser("~")), os.path.realpath(os.sep)):
        die("refusing to back up your whole home folder / drive. Point --project at one project folder")
    dest = os.path.realpath(os.path.expanduser(a.dest)) if a.dest else os.path.join(
        os.path.expanduser("~"), "ProjectBackups", os.path.basename(project))
    if is_within(dest, project) or is_within(project, dest):
        die("backup folder and project folder must be separate (not inside each other)")
    if os.path.isdir(dest) and os.listdir(dest) and not os.path.exists(os.path.join(dest, MANIFEST)):
        die("backup folder %s exists and is not an empty project-rescue folder. Choose another --dest" % dest)
    if not shutil.which("git"):
        die("git is required but was not found")
    excludes = [] if a.exact else list(DEFAULT_EXCLUDES)
    excludes += a.exclude or []
    slug = slug_for(project)
    old = load_json(cfg_path(slug), {})
    cfg = {"slug": slug, "project": project, "mirror": dest, "excludes": excludes, "branch": "main",
           "remote": old.get("remote"), "allow_public": bool(a.allow_public)}
    if a.github:
        cfg["remote"] = resolve_remote(a.github, a.create)
    os.makedirs(dest, exist_ok=True)
    save_json(cfg_path(slug), cfg)
    ensure_repo(cfg)
    say("project-rescue ready\n  project : %s\n  backup  : %s\n  GitHub  : %s\n  excluded: %s" % (
        project, dest, cfg.get("remote") or "(not set - local backup only)",
        ", ".join(excludes) or "nothing (exact mode)"))
    if cfg.get("remote") and parse_github(cfg["remote"]) and github_private(cfg["remote"]) is None:
        warn("could not verify that the GitHub repo is PRIVATE (gh missing/not logged in). Check it yourself.")
    if not a.no_run:
        return run_with_lock(cfg, force=True)
    return 0


def spawn_background(cfg, force=False):
    env = dict(os.environ, RESCUE_CHILD="1")
    os.makedirs(CFG_DIR, exist_ok=True)
    lp = os.path.join(CFG_DIR, cfg["slug"] + ".log")
    try:
        if os.path.getsize(lp) > 1024 * 1024:
            os.remove(lp)
    except OSError:
        pass
    log = open(lp, "a")
    kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000008 | 0x00000200}
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "run", "--project", cfg["project"]]
                     + (["--force"] if force else []),
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, close_fds=True, **kw)


def cmd_run(a):
    cfg = load_cfg(a.project)
    if a.min_interval:
        last = load_json(state_path(cfg["slug"]), {}).get("last_run_ts", 0)
        if time.time() - last < a.min_interval:
            return 0
    if a.background and not os.environ.get("RESCUE_CHILD"):
        spawn_background(cfg, a.force)
        return 0
    return run_with_lock(cfg, force=a.force, dry=a.dry_run, push=not a.no_push)


def project_fingerprint(cfg):
    items, _ = scan_tree(cfg["project"], cfg["excludes"])
    acc = 0
    for r, v in items.items():
        acc = (acc + hash((r, v))) & 0xFFFFFFFFFFFF
    return (len(items), acc)


def cmd_watch(a):
    cfg = load_cfg(a.project)
    say("watching %s every %ss - Ctrl+C to stop" % (cfg["project"], a.interval))
    run_with_lock(cfg)
    last = project_fingerprint(cfg)
    try:
        while True:
            time.sleep(a.interval)
            fp = project_fingerprint(cfg)
            if fp != last:
                time.sleep(a.settle)  # wait until saving/editing calms down
                fp = project_fingerprint(cfg)
                run_with_lock(cfg)
                last = fp
    except KeyboardInterrupt:
        say("stopped")
        return 0


def cmd_status(a):
    cfg = load_cfg(a.project)
    st = load_json(state_path(cfg["slug"]), {})
    src, _ = scan_tree(cfg["project"], cfg["excludes"]) if os.path.isdir(cfg["project"]) else ({}, [])
    dst, _ = scan_tree(cfg["mirror"], [], True)
    dels, copies = make_plan(src, dst)
    n = 0
    if os.path.isdir(os.path.join(cfg["mirror"], ".git")):
        r = mgit(cfg, ["rev-list", "--count", "HEAD"], check=False)
        n = int(r.stdout.strip() or 0) if r.returncode == 0 else 0
    print("project   : %s%s" % (cfg["project"], "" if os.path.isdir(cfg["project"]) else "  (MISSING!)"))
    print("backup    : %s  (%d saved versions)" % (cfg["mirror"], n))
    print("GitHub    : %s" % (cfg.get("remote") or "not set"))
    print("last run  : %s   last version: %s" % (st.get("last_run", "never"), st.get("last_commit", "-")))
    if cfg.get("remote"):
        print("last push : %s%s" % (st.get("last_push_at", "never"), "   ** PENDING - push failed last time **" if st.get("pending_push") else ""))
    print("in sync   : %s" % ("YES" if not copies and not dels else "NO - %d change(s) not backed up yet (run `run`)" % (len(copies) + len(dels))))
    if st.get("blocked"):
        print("PAUSED    : safety stop - %s. Check your project; if the deletes were intended run `run --force`." % st["blocked"])
    if st.get("last_errors"):
        print("problems  : " + "; ".join(st["last_errors"][:3]))
    return 0


def cmd_list(a):
    cs = all_cfgs()
    if not cs:
        print("no projects set up yet")
    for c in cs:
        print("%s\n   backup: %s\n   GitHub: %s" % (c["project"], c["mirror"], c.get("remote") or "-"))
    return 0


def _extract(source, rev, dest, tmpdirs):
    """Put project files from `source` (local mirror folder or git URL) into dest. Return git-dir used."""
    if os.path.isdir(source) and os.path.isdir(os.path.join(source, ".git")):
        gdir = os.path.join(source, ".git")
        local = True
    else:
        url = source
        if re.match(r"^[\w.-]+/[\w.-]+$", source) and not os.path.exists(source):
            url = "https://github.com/%s.git" % source
        t = tempfile.mkdtemp(prefix="rescue-clone-")
        tmpdirs.append(t)
        git(["clone", "-q", "--bare", url, t])
        gdir, local = t, False
    if not rev:
        # a remote's HEAD may point at a branch we never pushed (e.g. master vs main)
        for cand in ("HEAD", "refs/heads/main", "refs/heads/master"):
            if git(["--git-dir", gdir, "rev-parse", "--verify", "-q", cand + "^{commit}"], check=False).returncode == 0:
                if not local:
                    rev = cand
                break
        else:
            if not local:
                die("that source has no backup versions in it")
    if local and not rev:
        shutil.copytree(source, dest, symlinks=True, dirs_exist_ok=True,
                        ignore=lambda d, names: [".git"] if os.path.realpath(d) == os.path.realpath(source) else [])
        for junk in glob.glob(os.path.join(dest, ".pbk-*.tmp")):
            os.remove(junk)
    else:
        cmd = ["git", "--git-dir", gdir, "archive", "--format=tar", rev or "HEAD"]
        env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        try:
            with tarfile.open(fileobj=p.stdout, mode="r|") as tf:
                if hasattr(tarfile, "tar_filter"):
                    tf.extractall(dest, filter="tar")
                else:
                    tf.extractall(dest)
        except tarfile.ReadError:
            pass  # reported below from git's own exit code / stderr
        finally:
            err = p.stderr.read().decode("utf-8", "replace")
            p.stdout.close()
            p.stderr.close()
            rc = p.wait()
        if rc != 0:
            die("could not read that version: " + err.strip())
    return gdir


# ------------------------------------------------------------------ restore
def do_restore(source, dest, rev, tmpdirs):
    """Rebuild a project into dest (new/empty folder). Returns the manifest dict."""
    os.makedirs(dest, exist_ok=True)
    _extract(source, rev, dest, tmpdirs)
    mpath, bpath = os.path.join(dest, MANIFEST), os.path.join(dest, BUNDLE)
    man = load_json(mpath, {})
    if os.path.exists(bpath):
        git(["init", "-q", dest])
        fetch = git(["-C", dest, "fetch", "-q", "--update-head-ok", bpath,
                     "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*",
                     "+refs/remotes/*:refs/remotes/*"], check=False)
        if fetch.returncode != 0:
            warn("files restored but git history could not be restored: " + fetch.stderr.strip()[:200])
        else:
            git(["-C", dest, "fetch", "-q", "--update-head-ok", bpath, "+refs/stash:refs/stash"], check=False)
            if man.get("head_ref"):
                git(["-C", dest, "symbolic-ref", "HEAD", man["head_ref"]], check=False)
            elif man.get("head_commit"):
                git(["-C", dest, "update-ref", "--no-deref", "HEAD", man["head_commit"]], check=False)
            git(["-C", dest, "reset", "-q"], check=False)
            for name, url in (man.get("remotes") or {}).items():
                git(["-C", dest, "remote", "add", name, url], check=False)
        os.remove(bpath)
    if os.path.exists(mpath):
        os.remove(mpath)
    return man


def cmd_restore(a):
    source = a.source
    if not source:
        cs = [c for c in all_cfgs() if not a.project or is_within(a.project, c["project"])]
        if len(cs) != 1:
            die("say where to restore from: --source <backup folder | owner/repo | git URL>  (or run `list`)")
        source = cs[0]["mirror"]
    source = os.path.expanduser(source)
    tmpdirs = []
    try:
        if a.list:
            if os.path.isdir(os.path.join(source, ".git")):
                gdir = os.path.join(source, ".git")
            else:
                url = "https://github.com/%s.git" % source if re.match(r"^[\w.-]+/[\w.-]+$", source) and not os.path.exists(source) else source
                gdir = tempfile.mkdtemp(prefix="rescue-clone-")
                tmpdirs.append(gdir)
                git(["clone", "-q", "--bare", url, gdir])
            out = git(["--git-dir", gdir, "log", "-n", str(a.count), "--date=iso", "--format=%h  %ad  %s"]).stdout
            print(out.rstrip() or "no versions found")
            return 0
        if not a.to:
            die("--to <new empty folder> is required")
        dest = os.path.abspath(os.path.expanduser(a.to))
        if os.path.exists(dest) and os.listdir(dest):
            die("destination %s is not empty; restore only writes into a new/empty folder (nothing is overwritten)" % dest)
        do_restore(source, dest, a.rev, tmpdirs)
        say("restored into %s\n  (secret files like .env are only in the LOCAL backup; a GitHub restore does not contain them)" % dest)
        return 0
    finally:
        for t in tmpdirs:
            shutil.rmtree(t, ignore_errors=True)


# --------------------------------------------------------------- git hook
HOOK_BEGIN, HOOK_END = "# >>> project-rescue >>>", "# <<< project-rescue <<<"


def q(p):
    return '"%s"' % p


def ph():
    """Command prefix that runs this very script."""
    return "%s %s" % (q(sys.executable or "python3"), q(os.path.abspath(__file__)))


def cmd_hook(a):
    cfg = load_cfg(a.project)
    line = "%s run --project %s --quiet --background" % (ph(), q(cfg["project"]))
    if os.name == "nt":
        line = line.replace("\\", "/")
    if not os.path.exists(os.path.join(cfg["project"], ".git")):
        die("project is not a git repository")
    hooks = pgit(cfg, ["rev-parse", "--git-path", "hooks"]).stdout.strip()
    hooks = hooks if os.path.isabs(hooks) else os.path.join(cfg["project"], hooks)
    os.makedirs(hooks, exist_ok=True)
    hp = os.path.join(hooks, "post-commit")
    block = "%s\n%s\n%s\n" % (HOOK_BEGIN, line, HOOK_END)
    if os.path.exists(hp):
        txt = open(hp, encoding="utf-8").read()
        if HOOK_BEGIN in txt:
            txt = re.sub(re.escape(HOOK_BEGIN) + r".*?" + re.escape(HOOK_END) + r"\n?", lambda m: block, txt, flags=re.S)
        else:
            txt = txt.rstrip("\n") + "\n\n" + block
    else:
        txt = "#!/bin/sh\n" + block
    with open(hp, "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)
    os.chmod(hp, 0o755)
    say("git post-commit hook installed: every commit now triggers a background backup")
    return 0


# ------------------------------------------------------ global config / auto
GCONF = os.path.join(HOME_DIR, "config.json")
MARKERS = ["package.json", "pyproject.toml", "requirements.txt", "setup.py", "Pipfile", "Cargo.toml",
           "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile", "composer.json",
           "Makefile", "CMakeLists.txt", "Dockerfile", "docker-compose.yml", "index.html",
           "pubspec.yaml", "deno.json", "tsconfig.json", "CLAUDE.md", "AGENTS.md", ".claude"]
RISKY = re.compile(
    r"(\brm\s+(-\S+\s+)*-\w*[rRfF]|\brmdir\b|\bgit\s+(reset\s+--hard|clean\s+-\w*f|checkout\s+(--|\.)|restore\b|"
    r"stash\s+(drop|clear)|branch\s+-D|push\s+.*(--force|-f\b)|filter-)|\bshred\b|\bmkfs|\bdd\s+if=|"
    r"\bfind\b.*-delete|Remove-Item|\brd\s+/s|\bdel\s+/|\btruncate\b|\bsed\s+-i|\bperl\s+-pi|\brsync\b.*--delete)")


def gconf():
    return load_json(GCONF, {})


def backup_root():
    return os.path.realpath(os.path.expanduser(gconf().get("backup_root") or os.path.join(os.path.expanduser("~"), "ProjectRescue")))


def repo_name(name):
    n = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.").lower() or "project"
    return n + "-rescue-backup"


def gh_ready():
    if not shutil.which("gh"):
        return False
    try:
        return subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def create_private_repo(owner, name):
    """Return (url, note). Reuses an existing PRIVATE repo; never touches a public one."""
    full = "%s/%s" % (owner, name)
    try:
        v = subprocess.run(["gh", "repo", "view", full, "--json", "url,isPrivate"], capture_output=True, text=True, timeout=60)
        if v.returncode == 0:
            j = json.loads(v.stdout)
            if not j.get("isPrivate"):
                return None, "repo %s exists and is PUBLIC (refused)" % full
            return j["url"].rstrip("/") + ".git", ""
        r = subprocess.run(["gh", "repo", "create", full, "--private",
                            "--description", "Project Rescue automatic project backup"],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        return None, str(e)
    if r.returncode == 0:
        return "https://github.com/%s.git" % full, ""
    return None, (r.stderr or r.stdout).strip().splitlines()[-1][:160] if (r.stderr or r.stdout).strip() else "gh failed"


def unsafe_root(p):
    p = os.path.realpath(p)
    home = os.path.realpath(os.path.expanduser("~"))
    if p in (home, os.path.realpath(os.sep)) or len(p.strip(os.sep).split(os.sep)) < 2 or os.path.dirname(p) == p:
        return True
    for d in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Movies", "Videos", "Library", "AppData"):
        if p == os.path.join(home, d):
            return True
    if p in ("/usr", "/etc", "/var", "/opt", "/bin", "/sbin", "/lib", "/tmp", "/home", "/Users", "/mnt", "/Volumes"):
        return True
    if is_within(p, HOME_DIR) or is_within(p, backup_root()):
        return True
    return False


def detect_root(start):
    start = os.path.realpath(start)
    if not os.path.isdir(start):
        return None
    r = git(["-C", start, "rev-parse", "--show-toplevel"], check=False)
    root = None
    if r.returncode == 0 and r.stdout.strip():
        root = os.path.realpath(r.stdout.strip())
    elif any(os.path.exists(os.path.join(start, m)) for m in MARKERS):
        root = start
    if not root or unsafe_root(root):
        return None
    return root


def too_big(root):
    items, errs = scan_tree(root, DEFAULT_EXCLUDES, limit=150000)
    if "LIMIT" in errs:
        return "more than 150,000 files"
    total = sum(v[1] for v in items.values())
    if total > 3 * 1024 ** 3:
        return "larger than 3 GB"
    return None


def unique_dest(root):
    name = os.path.basename(root) or "project"
    d = os.path.join(backup_root(), name)
    if os.path.exists(d):
        man = load_json(os.path.join(d, MANIFEST), {})
        if man.get("original_path") != root:
            d = d + "-" + hashlib.sha1(root.encode("utf-8", "surrogateescape")).hexdigest()[:6]
    return d


def is_ignored(root):
    return root in (gconf().get("ignore") or [])


def auto_setup(start):
    """Make sure the project containing `start` is protected. Return (cfg or None, context text)."""
    root = detect_root(start)
    if not root or is_ignored(root):
        return None, ""
    cfg = find_cfg(root)
    g = gconf()
    first = False
    note = ""
    if cfg is None:
        reason = too_big(root)
        if reason:
            return None, ("[PROJECT RESCUE] %s is %s, so it was NOT auto-protected. Tell the user and run: %s init "
                          "--project %s --exclude <big folders>" % (root, reason, ph(), q(root)))
        remote = None
        dest = unique_dest(root)
        if g.get("github_owner"):
            if gh_ready():
                remote, err = create_private_repo(g["github_owner"], repo_name(os.path.basename(dest)))
                note = "" if remote else "GitHub skipped: " + err
            else:
                note = "GitHub not connected: user must run `gh auth login`"
        cfg = {"slug": slug_for(root), "project": root, "mirror": dest,
               "excludes": list(DEFAULT_EXCLUDES), "branch": "main", "remote": remote, "allow_public": False}
        os.makedirs(cfg["mirror"], exist_ok=True)
        save_json(cfg_path(cfg["slug"]), cfg)
        ensure_repo(cfg)
        first = True
    st = load_json(state_path(cfg["slug"]), {})
    if not first and not cfg.get("remote") and g.get("github_owner") and gh_ready():
        remote, err = create_private_repo(g["github_owner"], repo_name(os.path.basename(cfg["mirror"])))
        if remote:
            cfg["remote"] = remote
            save_json(cfg_path(cfg["slug"]), cfg)
            ensure_repo(cfg)
            st["pending_push"] = True
            save_json(state_path(cfg["slug"]), st)
        else:
            note = "GitHub skipped: " + err
    gh_line = cfg.get("remote") or ("local only. " + (note or "Tell the user to run: %s connect --github THEIR_USERNAME" % ph()))
    if cfg.get("remote") and st.get("pending_push"):
        gh_line += "  (WARNING: last push failed; retrying automatically)"
    last = "first backup is running now" if first else st.get("last_run", "never")
    warn_line = ""
    if st.get("blocked"):
        warn_line = ("\nWARNING: backups are PAUSED by the safety stop (%s). Tell the user NOW. "
                     "Never use --force unless they confirm the deletes were intended." % st["blocked"])
    text = ("[PROJECT RESCUE] ACTIVE - this project is under continuous backup.\n"
            "Project: %s\nLocal backup: %s\nGitHub: %s\nLast backup: %s\n"
            "Orders: after each batch of edits run `%s run`; before any destructive command run it first and wait for "
            "exit code 0; if the user says rescue/restore/recover run `%s rescue`." % (
                cfg["project"], cfg["mirror"], gh_line, last, ph(), ph())) + warn_line
    return cfg, text


def cmd_auto(a):
    cfg, text = auto_setup(a.project or os.getcwd())
    if not cfg:
        if text:
            print(text)
        else:
            say("not a project folder (or protection is off here); nothing to do")
        return 0
    if not a.no_run:
        spawn_background(cfg)
    print(text)
    return 0


def cmd_agent_hook(a):
    """Entry point for Claude Code hooks. Never fails, never blocks."""
    global QUIET
    QUIET = True
    try:
        data = {}
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
            data = json.loads(raw) if raw.strip() else {}
        ev = data.get("hook_event_name") or a.event or ""
        cwd = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        if ev == "SessionStart":
            cfg, text = auto_setup(cwd)
            if cfg:
                spawn_background(cfg)
            if text:
                print(text)
            return 0
        root = detect_root(cwd)
        cfg = find_cfg(root or cwd)
        if cfg is None and root and not is_ignored(root):
            cfg, _ = auto_setup(cwd)
            if cfg:
                spawn_background(cfg)
            return 0
        if cfg is None:
            return 0
        if ev == "PreToolUse":
            cmd = (data.get("tool_input") or {}).get("command", "") or ""
            if RISKY.search(cmd):
                run_with_lock(cfg)  # synchronous safety snapshot BEFORE the risky command runs
            return 0
        gap = {"PostToolUse": 15, "Stop": 2, "SessionEnd": 0}.get(ev, 5)
        if time.time() - load_json(state_path(cfg["slug"]), {}).get("last_run_ts", 0) >= gap:
            spawn_background(cfg)
        return 0
    except (Exception, SystemExit) as e:
        try:
            os.makedirs(CFG_DIR, exist_ok=True)
            with open(os.path.join(CFG_DIR, "hook-errors.log"), "a") as f:
                f.write("%s %r\n" % (now(), e))
        except OSError:
            pass
        return 0


def cmd_connect(a):
    g = gconf()
    if a.github:
        if not re.match(r"^[A-Za-z0-9-]+$", a.github):
            die("that does not look like a GitHub username")
        g["github_owner"] = a.github
    if a.backup_root:
        g["backup_root"] = os.path.abspath(os.path.expanduser(a.backup_root))
    save_json(GCONF, g)
    say("saved. GitHub user: %s | backup folder: %s" % (g.get("github_owner", "(none)"), backup_root()))
    if g.get("github_owner"):
        if gh_ready():
            say("GitHub CLI is logged in - every new project will get its own PRIVATE repo automatically.")
        elif not shutil.which("gh"):
            say("NEXT: install the GitHub CLI (https://cli.github.com), then run:  gh auth login")
        else:
            say("NEXT: run:  gh auth login   (choose GitHub.com > HTTPS > login with browser)")
    return 0


def cmd_off(a, on=False):
    root = detect_root(a.project or os.getcwd()) or os.path.realpath(a.project or os.getcwd())
    g = gconf()
    ign = [p for p in (g.get("ignore") or []) if p != root]
    if not on:
        ign.append(root)
    g["ignore"] = ign
    save_json(GCONF, g)
    say("auto-protection %s for %s" % ("ON" if on else "OFF", root))
    return 0


# ----------------------------------------------------------------- rescue
def lost_dir(p):
    return (not os.path.isdir(p)) or not os.listdir(p)


def next_steps(dest):
    tips = []
    for f, t in (("package.json", "npm install"), ("requirements.txt", "pip install -r requirements.txt"),
                 ("pyproject.toml", "pip install -e ."), ("Gemfile", "bundle install"), ("go.mod", "go mod download"),
                 ("Cargo.toml", "cargo build")):
        if os.path.exists(os.path.join(dest, f)):
            tips.append(t)
    return tips


def finish_rescue(target, source_desc, has_env):
    n = 0
    for dp, dn, fs in os.walk(target):
        dn[:] = [d for d in dn if d != ".git"]
        n += len(fs)
    say("RESCUED -> %s\n  from: %s\n  files: %d" % (target, source_desc, n))
    if not has_env:
        say("  note: .env / key files are never stored on GitHub - recreate them if the project needs them")
    tips = next_steps(target)
    if tips:
        say("  dependencies were not backed up (they are re-creatable). Run: " + " | ".join(tips))


def rescue_known(c, a):
    mirror = c["mirror"]
    local_ok = os.path.isdir(os.path.join(mirror, ".git")) and git(
        ["--git-dir", os.path.join(mirror, ".git"), "rev-parse", "--verify", "-q", "HEAD"], check=False).returncode == 0
    use_github = bool(a.from_github) or not local_ok
    if use_github and not c.get("remote"):
        die("the local backup is gone and no GitHub repo was configured. Try: project-rescue rescue NAME --github YOUR_USERNAME")
    if a.to:
        target = os.path.abspath(os.path.expanduser(a.to))
    elif lost_dir(c["project"]):
        target = c["project"]
    else:
        target = c["project"] + "-restored-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        say("the original folder still has files, so nothing is overwritten - restoring beside it")
    if os.path.exists(target) and os.listdir(target):
        die("destination %s is not empty" % target)
    source = c["remote"] if use_github else mirror
    tmp = []
    try:
        do_restore(source, target, a.rev, tmp)
    finally:
        for t in tmp:
            shutil.rmtree(t, ignore_errors=True)
    if target == c["project"]:
        run_with_lock(c)
    finish_rescue(target, "GitHub (" + c["remote"] + ")" if use_github else "local backup (" + mirror + ")", not use_github)
    return 0


def rescue_github(name, a, g):
    cwd = os.path.realpath(os.getcwd())
    if not name:
        if lost_dir(cwd):
            name = os.path.basename(cwd)
        else:
            die("which project? run:  project-rescue rescue NAME   (see names with: project-rescue list)")
    repo = a.repo
    if not repo:
        owner = a.github or g.get("github_owner")
        if not owner:
            die("no local record of '%s'. Tell me your GitHub username:  project-rescue rescue %s --github YOUR_USERNAME" % (name, name))
        repo = "%s/%s" % (owner, repo_name(name))
    url = repo if ("://" in repo or repo.startswith("git@") or os.path.isdir(repo)) else "https://github.com/%s.git" % repo
    dest = os.path.join(backup_root(), name)
    if os.path.exists(dest) and os.listdir(dest):
        dest += "-" + datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    ok, err = clone_vault(url, dest)
    if not ok:
        shutil.rmtree(dest, ignore_errors=True)
        die("could not download %s - %s. If it is private run `gh auth login` first" % (url, err))
    man = load_json(os.path.join(dest, MANIFEST), {})
    if not man:
        shutil.rmtree(dest, ignore_errors=True)
        die("%s is not a Project Rescue backup" % url)
    orig = man.get("original_path") or ""
    if a.to:
        target = os.path.abspath(os.path.expanduser(a.to))
    elif lost_dir(cwd) and os.path.basename(cwd).lower() == name.lower():
        target = cwd
    elif orig and lost_dir(orig):
        target = orig
    else:
        target = os.path.join(cwd, name)
        if not lost_dir(target):
            target += "-restored-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    if os.path.exists(target) and os.listdir(target):
        die("destination %s is not empty" % target)
    tmp = []
    try:
        do_restore(dest, target, a.rev, tmp)
    finally:
        for t in tmp:
            shutil.rmtree(t, ignore_errors=True)
    cfg = {"slug": slug_for(os.path.realpath(target)), "project": os.path.realpath(target), "mirror": dest,
           "excludes": man.get("excludes") or list(DEFAULT_EXCLUDES), "branch": "main", "remote": url, "allow_public": False}
    save_json(cfg_path(cfg["slug"]), cfg)
    ensure_repo(cfg)
    run_with_lock(cfg)
    finish_rescue(target, "GitHub (%s) - backups continue automatically" % url, False)
    return 0


def cmd_rescue(a):
    cfgs, g = all_cfgs(), gconf()
    cwd = os.path.realpath(os.getcwd())
    name = (a.name or "").strip()
    if name:
        low = name.lower()
        cands = [c for c in cfgs if os.path.basename(c["project"]).lower() == low] or \
                [c for c in cfgs if low in os.path.basename(c["project"]).lower()]
    else:
        cands = [c for c in cfgs if c["project"] == cwd] or [c for c in cfgs if is_within(cwd, c["project"])] \
            or [c for c in cfgs if lost_dir(c["project"])]
    if len(cands) > 1:
        print("several projects match:")
        for c in cands:
            print("  - %s   (backup: %s)" % (os.path.basename(c["project"]), c["mirror"]))
        die("run again with the project name:  project-rescue rescue NAME")
    if cands:
        return rescue_known(cands[0], a)
    return rescue_github(name or None, a, g)


# ---------------------------------------------- agent integration (install)
BLOCK_BEGIN = "<!-- PROJECT-RESCUE:BEGIN (managed by `project-rescue install`; edit outside this block) -->"
BLOCK_END = "<!-- PROJECT-RESCUE:END -->"
BLOCK = """{begin}
# Project Rescue - standing orders for EVERY project

**ROLE.** Besides the coding task, you are the user's data-safety engineer. Their biggest fear is losing project files by mistake or system failure. Every project must always exist in three places: the project folder, a local backup mirror, and a private GitHub repo.

**CONTEXT.** In Claude Code a hook prints a `[PROJECT RESCUE]` block at session start (paths, GitHub repo, last backup). Trust it. In any other agent, or if the block is missing, run `{ph} auto` as your first action.

**STATE GRAPH**
```
START --auto--> PROTECTED --edit batch--> DIRTY --run (exit 0)--> PROTECTED
PROTECTED --risky command--> CHECKPOINT (run, wait for exit 0) --> execute --> DIRTY
LOST --rescue--> RESTORED --run--> PROTECTED
```

**LOOP (every task).** OBSERVE the `[PROJECT RESCUE]` context -> ACT (do the user's work) -> CHECKPOINT: `{ph} run` after each meaningful batch of edits and BEFORE any destructive step (rm -rf, git reset --hard, git clean, force-push, mass move/rename, deleting data) -> VERIFY the exit code -> REPORT in one short line. Never claim a backup you did not verify.

**EXIT CODES.** 0 ok | 1 error or SAFETY STOP (stop and tell the user; never add --force without their OK) | 2 some files failed (read the warning) | 3 local backup ok but GitHub push failed (tell the user once).

**INVARIANTS.** Never delete, edit or overwrite the backup folder or any `.rescue-*` file. Never push a project to a public repo. Never print secrets. Never disable the hooks to save time. If a project is not protected (tool missing or erroring), say so in your first reply.

**RESCUE.** If the user says rescue / restore / recover / "I lost my project" (any language), do not ask for details first: run `{ph} rescue [NAME]`. If it lists several projects, ask which. It restores into the original path when that is empty or missing, otherwise into `<name>-restored-<time>`; it never overwrites. Then report the file count, the source (local or GitHub) and any next step it prints (for example reinstalling dependencies).
{end}
"""
CMD_RESCUE = """---
description: Rescue my lost project from its Project Rescue backup (local first, then GitHub)
argument-hint: [project name]
---
Run `{ph} rescue $ARGUMENTS` with the Bash tool and show me the result. If it lists several projects, ask me which one. It never overwrites existing files. When it finishes, tell me the file count, where it restored from and the next steps it printed. Then run `{ph} status`.
"""
CMD_VAULT = """---
description: Check and refresh the Project Rescue backup of this project
---
Run `{ph} run` and then `{ph} status` with the Bash tool. Report in at most three lines: whether the project is in sync, the last GitHub push time, and any warning.
"""
HOOK_EVENTS = [("SessionStart", None, 90), ("PreToolUse", "Bash", 180),
               ("PostToolUse", "Write|Edit|MultiEdit|NotebookEdit|Bash", 20), ("Stop", None, 20)]


def installed_script():
    return os.path.join(HOME_DIR, "project_rescue.py")


def inst_ph():
    return "%s %s" % (q(sys.executable or "python3"), q(installed_script()))


def upsert_block(path, block):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    txt = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    if BLOCK_BEGIN in txt and BLOCK_END in txt:
        txt = re.sub(re.escape(BLOCK_BEGIN) + r".*?" + re.escape(BLOCK_END) + r"\n?", lambda m: block, txt, flags=re.S)
    else:
        txt = (txt.rstrip("\n") + "\n\n" if txt.strip() else "") + block
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)


def remove_block(path):
    if not os.path.exists(path):
        return False
    txt = open(path, encoding="utf-8").read()
    if BLOCK_BEGIN not in txt:
        return False
    txt = re.sub(r"\n*" + re.escape(BLOCK_BEGIN) + r".*?" + re.escape(BLOCK_END) + r"\n?", "\n", txt, flags=re.S).strip("\n")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(txt + ("\n" if txt else ""))
    return True


def merge_settings(sp, add):
    s = {}
    if os.path.exists(sp):
        try:
            s = json.load(open(sp, encoding="utf-8"))
        except ValueError:
            die("%s is not valid JSON - fix it first; I will not overwrite it" % sp)
        bak = sp + ".rescue-bak"
        if not os.path.exists(bak):
            shutil.copy2(sp, bak)
    hooks = s.get("hooks") or {}
    for ev in list(hooks):
        groups = []
        for g in hooks[ev]:
            g["hooks"] = [h for h in g.get("hooks", []) if not ("project_rescue.py" in h.get("command", "") and "agent-hook" in h.get("command", ""))]
            if g["hooks"]:
                groups.append(g)
        if groups:
            hooks[ev] = groups
        else:
            del hooks[ev]
    if add:
        cmd = inst_ph() + " agent-hook"
        for ev, matcher, timeout in HOOK_EVENTS:
            grp = {"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}
            if matcher:
                grp["matcher"] = matcher
            hooks.setdefault(ev, []).append(grp)
    if hooks:
        s["hooks"] = hooks
    else:
        s.pop("hooks", None)
    save_json(sp, s)


def agent_targets(force_all):
    home = os.path.expanduser("~")
    t = [os.path.join(home, ".claude", "CLAUDE.md")]
    for d, f in ((".codex", "AGENTS.md"), (".gemini", "GEMINI.md")):
        if force_all or os.path.isdir(os.path.join(home, d)):
            t.append(os.path.join(home, d, f))
    return t


def cmd_install(a):
    os.makedirs(os.path.join(HOME_DIR, "bin"), exist_ok=True)
    me = os.path.abspath(__file__)
    if os.path.realpath(me) != os.path.realpath(installed_script()):
        shutil.copy2(me, installed_script())
    wrapper = os.path.join(HOME_DIR, "bin", "project-rescue")
    with open(wrapper, "w", newline="\n") as f:
        f.write('#!/bin/sh\nexec %s %s "$@"\n' % (q(sys.executable or "python3"), q(installed_script())))
    os.chmod(wrapper, 0o755)
    with open(os.path.join(HOME_DIR, "bin", "project-rescue.cmd"), "w", newline="\r\n") as f:
        f.write('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable or "python", installed_script()))
    block = BLOCK.format(begin=BLOCK_BEGIN, end=BLOCK_END, ph=inst_ph())
    done = []
    for t in agent_targets(a.all_agents):
        upsert_block(t, block)
        done.append(t)
    home = os.path.expanduser("~")
    for name, body in (("rescue.md", CMD_RESCUE), ("vault.md", CMD_VAULT)):
        p = os.path.join(home, ".claude", "commands", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(body.format(ph=inst_ph()))
    merge_settings(os.path.join(home, ".claude", "settings.json"), True)
    save_json(GCONF, gconf())
    say("Project Rescue installed.\n  engine   : %s\n  hooks    : %s\n  orders   : %s\n  commands : /rescue  /vault"
        % (installed_script(), os.path.join(home, ".claude", "settings.json"), ", ".join(done)))
    say("\nNEXT (once):\n  1) python3 ~/.project-rescue/project_rescue.py connect --github YOUR_GITHUB_USERNAME\n  2) gh auth login\n"
        "Then start a NEW Claude Code session in any project - it protects itself.")
    return 0


def cmd_uninstall(a):
    home = os.path.expanduser("~")
    merge_settings(os.path.join(home, ".claude", "settings.json"), False)
    for t in agent_targets(True):
        remove_block(t)
    for n in ("rescue.md", "vault.md"):
        p = os.path.join(home, ".claude", "commands", n)
        if os.path.exists(p) and "project-rescue" in open(p, encoding="utf-8").read():
            os.remove(p)
    say("Project Rescue hooks, orders and commands removed. Your backups are untouched in %s and on GitHub." % backup_root())
    return 0


def main():
    global QUIET
    ap = argparse.ArgumentParser(prog="project-rescue", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version="project-rescue " + VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("install", help="one-time: wire Project Rescue into Claude Code (and Codex/Gemini if present)")
    p.add_argument("--all-agents", action="store_true", help="also write standing orders for Codex and Gemini CLI")
    sub.add_parser("uninstall", help="remove hooks, orders and commands (backups are kept)")
    p = sub.add_parser("connect", help="one-time: tell Project Rescue your GitHub username (and optional backup folder)")
    p.add_argument("--github", help="your GitHub username")
    p.add_argument("--backup-root", help="where local backups live (default ~/ProjectRescue)")

    p = sub.add_parser("auto", help="protect the current project now (idempotent) and print its status")
    p.add_argument("--project")
    p.add_argument("--no-run", action="store_true")
    for nm, hp in (("off", "stop auto-protecting this project"), ("on", "resume auto-protecting this project")):
        p = sub.add_parser(nm, help=hp)
        p.add_argument("--project")

    p = sub.add_parser("init", help="advanced: set up one project by hand (custom folder / repo / excludes)")
    p.add_argument("--project", help="project folder (default: current folder)")
    p.add_argument("--dest", help="backup folder (default: ~/ProjectBackups/<name>)")
    p.add_argument("--github", "--remote", dest="github", help="owner/repo, repo name (with --create) or git URL")
    p.add_argument("--create", action="store_true", help="create the private GitHub repo with gh")
    p.add_argument("--exact", action="store_true", help="exclude nothing (also copies node_modules, .venv ...)")
    p.add_argument("--exclude", action="append", help="extra pattern to skip (repeatable)")
    p.add_argument("--allow-public", action="store_true", help="allow pushing to a public repo (not recommended)")
    p.add_argument("--no-run", action="store_true")

    p = sub.add_parser("run", help="back up now")
    p.add_argument("--project")
    p.add_argument("--force", action="store_true", help="allow a backup that deletes many files")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-push", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--background", action="store_true", help="return immediately, run detached")
    p.add_argument("--min-interval", type=int, default=0, help="skip if backed up less than N seconds ago")

    p = sub.add_parser("watch", help="back up automatically whenever files change")
    p.add_argument("--project")
    p.add_argument("--interval", type=int, default=30)
    p.add_argument("--settle", type=int, default=5)
    p.add_argument("--quiet", action="store_true")

    p = sub.add_parser("status", help="is everything backed up?")
    p.add_argument("--project")
    sub.add_parser("list", help="all protected projects")

    p = sub.add_parser("rescue", help="bring a lost project back (local backup first, then GitHub)")
    p.add_argument("name", nargs="?", help="project name (optional)")
    p.add_argument("--to", help="restore into this new/empty folder")
    p.add_argument("--github", help="your GitHub username (only needed on a fresh machine)")
    p.add_argument("--repo", help="owner/repo or URL of the vault repo")
    p.add_argument("--from-github", action="store_true", help="ignore the local backup, use GitHub")
    p.add_argument("--rev", help="older version id (see: restore --list)")

    p = sub.add_parser("restore", help="advanced: restore from any backup folder / owner/repo / URL")
    p.add_argument("--source")
    p.add_argument("--project")
    p.add_argument("--to")
    p.add_argument("--rev")
    p.add_argument("--list", action="store_true", help="show saved versions")
    p.add_argument("--count", type=int, default=30)

    p = sub.add_parser("hook", help="advanced: also back up on every git commit")
    p.add_argument("kind", choices=["git"])
    p.add_argument("--project")

    p = sub.add_parser("agent-hook", help=argparse.SUPPRESS)
    p.add_argument("--event")

    a = ap.parse_args()
    QUIET = bool(getattr(a, "quiet", False))
    fn = {"install": cmd_install, "uninstall": cmd_uninstall, "connect": cmd_connect, "auto": cmd_auto,
          "off": lambda x: cmd_off(x, False), "on": lambda x: cmd_off(x, True), "init": cmd_init, "run": cmd_run,
          "watch": cmd_watch, "status": cmd_status, "list": cmd_list, "rescue": cmd_rescue,
          "restore": cmd_restore, "hook": cmd_hook, "agent-hook": cmd_agent_hook}[a.cmd]
    try:
        sys.exit(fn(a) or 0)
    except RuntimeError as e:
        die(str(e))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
