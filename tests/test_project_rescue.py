"""End-to-end tests. Run:  python3 -m unittest discover -s tests -v
Everything happens in temp folders with a fake HOME; nothing touches your real files."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ENGINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "project_rescue.py")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="rescue-test-"))
        self.home = os.path.join(self.tmp, "home")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.home)
        os.makedirs(self.work)
        self.env = dict(os.environ, HOME=self.home, USERPROFILE=self.home,
                        RESCUE_HOME=os.path.join(self.home, ".project-rescue"),
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                        GIT_COMMITTER_EMAIL="t@t", PATH=os.environ["PATH"])
        for k in ("CLAUDE_PROJECT_DIR",):
            self.env.pop(k, None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ph(self, *args, cwd=None, inp=None, env=None):
        return subprocess.run([sys.executable, ENGINE] + list(args), cwd=cwd or self.work, env=env or self.env,
                              input=inp, capture_output=True, text=True, timeout=120)

    def sh(self, *cmd, cwd=None):
        return subprocess.run(cmd, cwd=cwd or self.work, env=self.env, capture_output=True, text=True, check=True)

    def make_project(self, name="proj"):
        p = os.path.join(self.work, name)
        os.makedirs(os.path.join(p, "src", "deep"))
        os.makedirs(os.path.join(p, "node_modules", "x"))
        for rel, txt in (("src/a.txt", "hello\n"), ("src/deep/b.txt", "deep\n"), (".env", "SECRET=1\n"),
                         (".env.example", "X=\n"), ("package.json", "{}\n"), ("node_modules/x/i.js", "nm\n")):
            with open(os.path.join(p, rel), "w") as f:
                f.write(txt)
        with open(os.path.join(p, "blob.bin"), "wb") as f:
            f.write(os.urandom(50000))
        os.symlink("src/a.txt", os.path.join(p, "link"))
        os.makedirs(os.path.join(p, "emptydir"))
        self.sh("git", "init", "-q", "-b", "main", cwd=p)
        self.sh("git", "add", ".", cwd=p)
        self.sh("git", "commit", "-qm", "c1", cwd=p)
        self.sh("git", "checkout", "-q", "-b", "feat", cwd=p)
        with open(os.path.join(p, "feat.txt"), "w") as f:
            f.write("f\n")
        self.sh("git", "add", "feat.txt", cwd=p)
        self.sh("git", "commit", "-qm", "c2", cwd=p)
        self.sh("git", "checkout", "-q", "main", cwd=p)
        with open(os.path.join(p, "src", "a.txt"), "a") as f:
            f.write("uncommitted\n")
        return p

    def tree(self, root):
        out = {}
        for dp, dn, fs in os.walk(root):
            dn[:] = [d for d in dn if d not in (".git", "node_modules")]
            for f in fs:
                full = os.path.join(dp, f)
                rel = os.path.relpath(full, root)
                if os.path.islink(full):
                    out[rel] = os.readlink(full)
                else:
                    with open(full, "rb") as fh:
                        out[rel] = fh.read()
        return out

    def wait_for(self, fn, secs=15):
        end = time.time() + secs
        while time.time() < end:
            if fn():
                return True
            time.sleep(0.2)
        return False


class TestBackupAndRestore(Base):
    def test_full_cycle(self):
        p = self.make_project()
        remote = os.path.join(self.tmp, "remote.git")
        self.sh("git", "init", "-q", "--bare", remote)
        bk = os.path.join(self.tmp, "bk")
        r = self.ph("init", "--project", p, "--dest", bk, "--github", remote)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.tree(p), {k: v for k, v in self.tree(bk).items() if not k.startswith(".rescue-")})
        self.assertTrue(os.path.exists(os.path.join(bk, ".env")))          # secret kept locally
        files = self.sh("git", "--git-dir", remote, "ls-tree", "-r", "--name-only", "main").stdout.split()
        self.assertNotIn(".env", files)                                     # ...but never on GitHub
        self.assertIn(".env.example", files)
        self.assertFalse(any(f.startswith("node_modules") for f in files))
        # change cycle
        with open(os.path.join(p, "src", "deep", "b.txt"), "a") as f:
            f.write("v2\n")
        os.remove(os.path.join(p, "src", "a.txt"))
        r = self.ph("run", "--project", p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.tree(p), {k: v for k, v in self.tree(bk).items() if not k.startswith(".rescue-")})
        self.assertIn("no changes", self.ph("run", "--project", p).stdout)
        self.assertIn("in sync   : YES", self.ph("status", "--project", p).stdout)
        # safety stop
        lost = p + ".lost"
        os.rename(p, lost)
        os.makedirs(p)
        r = self.ph("run", "--project", p)
        self.assertEqual(r.returncode, 1)
        self.assertIn("SAFETY STOP", r.stderr)
        self.assertTrue(os.path.exists(os.path.join(bk, "src", "deep", "b.txt")))
        shutil.rmtree(p)
        os.rename(lost, p)
        # restore keeps history, branches and uncommitted work
        dest = os.path.join(self.tmp, "r1")
        r = self.ph("restore", "--source", bk, "--to", dest)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.tree(p), self.tree(dest))
        branches = self.sh("git", "branch", "--format=%(refname:short)", cwd=dest).stdout.split()
        self.assertEqual(sorted(branches), ["feat", "main"])
        self.assertIn("src/deep/b.txt", self.sh("git", "status", "--short", cwd=dest).stdout)
        # never overwrites
        self.assertNotEqual(self.ph("restore", "--source", bk, "--to", dest).returncode, 0)
        # restore from the GitHub copy
        dest2 = os.path.join(self.tmp, "r2")
        self.assertEqual(self.ph("restore", "--source", remote, "--to", dest2).returncode, 0)
        t1, t2 = self.tree(p), self.tree(dest2)
        t1.pop(".env")
        self.assertEqual(t1, t2)

    def test_offline_push_recovers(self):
        p = self.make_project()
        remote = os.path.join(self.tmp, "remote.git")
        self.sh("git", "init", "-q", "--bare", remote)
        self.assertEqual(self.ph("init", "--project", p, "--dest", os.path.join(self.tmp, "bk"), "--github", remote).returncode, 0)
        os.rename(remote, remote + ".away")
        with open(os.path.join(p, "new.txt"), "w") as f:
            f.write("n")
        self.assertEqual(self.ph("run", "--project", p).returncode, 3)
        os.rename(remote + ".away", remote)
        self.assertEqual(self.ph("run", "--project", p).returncode, 0)
        files = self.sh("git", "--git-dir", remote, "ls-tree", "-r", "--name-only", "main").stdout.split()
        self.assertIn("new.txt", files)


class TestRescue(Base):
    def setUp(self):
        super().setUp()
        self.p = self.make_project()
        self.remote = os.path.join(self.tmp, "remote.git")
        self.sh("git", "init", "-q", "--bare", self.remote)
        self.bk = os.path.join(self.tmp, "bk")
        r = self.ph("init", "--project", self.p, "--dest", self.bk, "--github", self.remote)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.before = self.tree(self.p)

    def test_rescue_after_folder_deleted(self):
        shutil.rmtree(self.p)
        os.makedirs(self.p)                      # user opens the agent in the (now empty) folder
        r = self.ph("rescue", cwd=self.p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("RESCUED", r.stdout)
        self.assertEqual(self.before, self.tree(self.p))
        self.assertEqual(sorted(self.sh("git", "branch", "--format=%(refname:short)", cwd=self.p).stdout.split()), ["feat", "main"])
        self.assertIn("npm install", r.stdout)
        self.assertIn("in sync", self.ph("status", "--project", self.p).stdout)

    def test_rescue_by_name_from_parent_and_never_overwrites(self):
        r = self.ph("rescue", "proj")            # folder still has files -> restore beside it
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-restored-", r.stdout)
        self.assertEqual(self.before, self.tree(self.p))
        side = [d for d in os.listdir(self.work) if d.startswith("proj-restored-")]
        self.assertEqual(len(side), 1)

    def test_rescue_fresh_machine_from_github_only(self):
        shutil.rmtree(self.p)
        shutil.rmtree(self.bk)
        shutil.rmtree(self.env["RESCUE_HOME"])  # new computer: no config, no local backup
        os.makedirs(self.p)
        r = self.ph("rescue", "proj", "--repo", self.remote, cwd=self.p)
        self.assertEqual(r.returncode, 0, r.stderr)
        want = dict(self.before)
        want.pop(".env")
        self.assertEqual(want, self.tree(self.p))
        self.assertIn(".env", r.stdout)           # tells user secrets are not on GitHub
        with open(os.path.join(self.p, "again.txt"), "w") as f:
            f.write("x")
        self.assertEqual(self.ph("run", "--project", self.p).returncode, 0)   # backups continue
        files = self.sh("git", "--git-dir", self.remote, "ls-tree", "-r", "--name-only", "main").stdout.split()
        self.assertIn("again.txt", files)


class TestAgentIntegration(Base):
    def settings(self):
        return json.loads(read(os.path.join(self.home, ".claude", "settings.json")))

    def test_install_idempotent_preserves_and_uninstalls(self):
        cdir = os.path.join(self.home, ".claude")
        os.makedirs(cdir)
        mine = {"theme": "dark", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}
        write(os.path.join(cdir, "settings.json"), json.dumps(mine))
        write(os.path.join(cdir, "CLAUDE.md"), "# my own rules\nbe nice\n")
        for _ in range(2):
            r = self.ph("install")
            self.assertEqual(r.returncode, 0, r.stderr)
        s = self.settings()
        self.assertEqual(s["theme"], "dark")
        for ev in ("SessionStart", "PreToolUse", "PostToolUse", "Stop"):
            ours = [h for g in s["hooks"][ev] for h in g["hooks"] if "agent-hook" in h["command"]]
            self.assertEqual(len(ours), 1, ev)
        self.assertTrue(any(h["command"] == "echo mine" for g in s["hooks"]["Stop"] for h in g["hooks"]))
        md = read(os.path.join(cdir, "CLAUDE.md"))
        self.assertEqual(md.count("PROJECT-RESCUE:BEGIN"), 1)
        self.assertIn("be nice", md)
        self.assertTrue(os.path.exists(os.path.join(cdir, "commands", "rescue.md")))
        self.assertTrue(os.path.exists(os.path.join(self.home, ".project-rescue", "project_rescue.py")))
        self.assertEqual(self.ph("uninstall").returncode, 0)
        s = self.settings()
        self.assertEqual(s["hooks"], mine["hooks"])
        md = read(os.path.join(cdir, "CLAUDE.md"))
        self.assertNotIn("PHOENIX", md)
        self.assertIn("be nice", md)
        self.assertFalse(os.path.exists(os.path.join(cdir, "commands", "rescue.md")))

    def test_install_refuses_to_clobber_bad_json(self):
        cdir = os.path.join(self.home, ".claude")
        os.makedirs(cdir)
        write(os.path.join(cdir, "settings.json"), "{ not json")
        self.assertNotEqual(self.ph("install").returncode, 0)
        self.assertEqual(read(os.path.join(cdir, "settings.json")), "{ not json")

    def test_session_start_auto_protects_and_live_hooks(self):
        p = self.make_project()
        ev = json.dumps({"hook_event_name": "SessionStart", "cwd": p})
        r = self.ph("agent-hook", inp=ev, cwd=p)
        self.assertEqual(r.returncode, 0)
        self.assertIn("[PROJECT RESCUE] ACTIVE", r.stdout)
        mirror = os.path.join(self.home, "ProjectRescue", "proj")
        self.assertTrue(self.wait_for(lambda: os.path.exists(os.path.join(mirror, "src", "deep", "b.txt"))), "first backup")
        # live: a file edit + Stop event is mirrored
        time.sleep(2.2)
        with open(os.path.join(p, "live.txt"), "w") as f:
            f.write("live")
        self.ph("agent-hook", inp=json.dumps({"hook_event_name": "Stop", "cwd": p}), cwd=p)
        self.assertTrue(self.wait_for(lambda: os.path.exists(os.path.join(mirror, "live.txt"))), "Stop hook backup")
        # pre-flight snapshot before a destructive command (synchronous)
        with open(os.path.join(p, "precious.txt"), "w") as f:
            f.write("p")
        r = self.ph("agent-hook", inp=json.dumps({"hook_event_name": "PreToolUse", "cwd": p, "tool_name": "Bash",
                                                  "tool_input": {"command": "rm -rf src"}}), cwd=p)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(mirror, "precious.txt")), "snapshot must exist before rm runs")
        # harmless command does not trigger a snapshot
        with open(os.path.join(p, "later.txt"), "w") as f:
            f.write("l")
        self.ph("agent-hook", inp=json.dumps({"hook_event_name": "PreToolUse", "cwd": p, "tool_name": "Bash",
                                              "tool_input": {"command": "ls -la"}}), cwd=p)
        self.assertFalse(os.path.exists(os.path.join(mirror, "later.txt")))

    def test_hook_is_safe_everywhere(self):
        junk = self.ph("agent-hook", inp="not json at all")
        self.assertEqual(junk.returncode, 0)
        empty = os.path.join(self.work, "notaproject")
        os.makedirs(empty)
        r = self.ph("agent-hook", inp=json.dumps({"hook_event_name": "SessionStart", "cwd": empty}), cwd=empty)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))
        r = self.ph("agent-hook", inp=json.dumps({"hook_event_name": "SessionStart", "cwd": self.home}), cwd=self.home)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))   # never protects the whole home folder
        p = self.make_project("optout")
        self.assertEqual(self.ph("off", "--project", p).returncode, 0)
        r = self.ph("agent-hook", inp=json.dumps({"hook_event_name": "SessionStart", "cwd": p}), cwd=p)
        self.assertEqual(r.stdout.strip(), "")


FAKE_GH = """#!/bin/sh
# minimal fake of the GitHub CLI: repos live as bare repos under $FAKE_GH_BASE
BASE="$FAKE_GH_BASE"
case "$1 $2" in
  "auth status") exit 0;;
  "repo view")
    full="$3"; d="$BASE/$full.git"
    [ -d "$d" ] || exit 1
    priv=true; [ -n "${FAKE_GH_PUBLIC:-}" ] && priv=false
    case "$*" in *" -q "*) echo $priv;; *) echo "{\\"url\\":\\"https://github.com/$full\\",\\"isPrivate\\":$priv}";; esac
    exit 0;;
  "repo create")
    full="$3"; mkdir -p "$BASE/$full.git"; git init -q --bare "$BASE/$full.git"
    echo "https://github.com/$full"; exit 0;;
esac
exit 1
"""


class TestGitHubFlow(Base):
    """Exercises the gh code paths offline: fake gh + git url rewrite (https://github.com/ -> local folder)."""

    def setUp(self):
        super().setUp()
        self.ghbase = os.path.join(self.tmp, "ghbase")
        os.makedirs(self.ghbase)
        bindir = os.path.join(self.tmp, "fakebin")
        os.makedirs(bindir)
        with open(os.path.join(bindir, "gh"), "w") as f:
            f.write(FAKE_GH)
        os.chmod(os.path.join(bindir, "gh"), 0o755)
        with open(os.path.join(self.home, ".gitconfig"), "w") as f:
            f.write('[url "%s/"]\n\tinsteadOf = https://github.com/\n' % self.ghbase)
        self.fake_env = dict(self.env, FAKE_GH_BASE=self.ghbase, PATH=bindir + os.pathsep + self.env["PATH"])
        self.p = self.make_project()
        self.repo = os.path.join(self.ghbase, "tester", "proj-rescue-backup.git")

    def hook(self, ev, env=None, cwd=None):
        cwd = cwd or self.p
        return self.ph("agent-hook", inp=json.dumps({"hook_event_name": ev, "cwd": cwd}), cwd=cwd, env=env or self.fake_env)

    def remote_files(self):
        return self.sh("git", "--git-dir", self.repo, "ls-tree", "-r", "--name-only", "main").stdout.split()

    def test_auto_creates_private_repo_and_pushes_without_secrets(self):
        self.assertEqual(self.ph("connect", "--github", "tester", env=self.fake_env).returncode, 0)
        r = self.hook("SessionStart")
        self.assertIn("github.com/tester/proj-rescue-backup", r.stdout)
        self.assertTrue(self.wait_for(lambda: os.path.isdir(self.repo) and subprocess.run(
            ["git", "--git-dir", self.repo, "rev-parse", "--verify", "-q", "main"], capture_output=True).returncode == 0), "pushed")
        files = self.remote_files()
        self.assertIn("src/a.txt", files)
        self.assertNotIn(".env", files)

    def test_public_repo_is_refused(self):
        os.makedirs(os.path.dirname(self.repo))
        self.sh("git", "init", "-q", "--bare", self.repo)
        self.ph("connect", "--github", "tester", env=self.fake_env)
        r = self.hook("SessionStart", env=dict(self.fake_env, FAKE_GH_PUBLIC="1"))
        self.assertIn("local only", r.stdout)
        self.assertIn("PUBLIC", r.stdout)
        time.sleep(2)
        self.assertEqual(subprocess.run(["git", "--git-dir", self.repo, "rev-parse", "--verify", "-q", "main"],
                                        capture_output=True).returncode, 1)

    def test_github_attached_later_when_gh_login_happens(self):
        self.ph("connect", "--github", "tester")                       # no working gh yet
        r = self.hook("SessionStart", env=self.env)
        self.assertIn("local only", r.stdout)
        mirror = os.path.join(self.home, "ProjectRescue", "proj")
        self.assertTrue(self.wait_for(lambda: os.path.exists(os.path.join(mirror, "src", "a.txt"))))
        time.sleep(1.5)
        r = self.hook("SessionStart")                                   # user ran `gh auth login` meanwhile
        self.assertIn("github.com/tester/proj-rescue-backup", r.stdout)
        self.assertTrue(self.wait_for(lambda: os.path.isdir(self.repo) and "src/a.txt" in self.remote_files_safe()), "late push")

    def remote_files_safe(self):
        r = subprocess.run(["git", "--git-dir", self.repo, "ls-tree", "-r", "--name-only", "main"], capture_output=True, text=True)
        return r.stdout.split()


class TestResilience(Base):
    def test_lost_backup_folder_is_rehydrated_from_github(self):
        p = self.make_project()
        remote = os.path.join(self.tmp, "remote.git")
        self.sh("git", "init", "-q", "--bare", remote)
        bk = os.path.join(self.tmp, "bk")
        self.assertEqual(self.ph("init", "--project", p, "--dest", bk, "--github", remote).returncode, 0)
        shutil.rmtree(bk)                                               # backup disk died, project still fine
        with open(os.path.join(p, "after.txt"), "w") as f:
            f.write("after")
        r = self.ph("run", "--project", p)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("re-created from GitHub", r.stdout)
        log = self.sh("git", "--git-dir", remote, "log", "--oneline", "main").stdout.strip().splitlines()
        self.assertEqual(len(log), 2)                                   # history continued, no rejected push
        self.assertTrue(os.path.exists(os.path.join(bk, ".env")))       # secrets re-copied locally

    def test_safety_stop_is_visible_to_the_agent(self):
        p = self.make_project()
        self.assertEqual(self.ph("init", "--project", p, "--dest", os.path.join(self.tmp, "bk")).returncode, 0)
        for rel in ("src/a.txt", "src/deep/b.txt", ".env", ".env.example", "blob.bin"):
            os.remove(os.path.join(p, rel))
        self.assertEqual(self.ph("run", "--project", p).returncode, 1)
        self.assertIn("PAUSED", self.ph("status", "--project", p).stdout)
        r = self.ph("agent-hook", inp=json.dumps({"hook_event_name": "SessionStart", "cwd": p}), cwd=p)
        self.assertIn("PAUSED", r.stdout)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "bk", "src", "a.txt")))   # good copy untouched


class TestDocs(unittest.TestCase):
    def test_agent_protocol_doc_matches_code(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("project-rescue", ENGINE)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        block = m.BLOCK.format(begin=m.BLOCK_BEGIN, end=m.BLOCK_END, ph="project-rescue")
        doc = os.path.join(os.path.dirname(ENGINE), "docs", "AGENT_PROTOCOL.md")
        with open(doc, encoding="utf-8") as f:
            self.assertIn(block, f.read(), "docs/AGENT_PROTOCOL.md is out of date; regenerate it")


if __name__ == "__main__":
    unittest.main()
