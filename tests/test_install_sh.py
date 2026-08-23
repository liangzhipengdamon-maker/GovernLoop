import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_SOURCE = REPO_ROOT / "scripts" / "install.sh"


class InstallerSkeletonTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def git(self, repo, *args, check=True):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=check,
            text=True,
            capture_output=True,
        )

    # Canonical runtime sources the Phase 2B installer packages into the bundle.
    RUNTIME_SOURCE_FILES = {
        "scripts/install.sh": INSTALLER_SOURCE,
        "skills/governloop/SKILL.md": REPO_ROOT / "skills/governloop/SKILL.md",
        "skills/workbuddy/governloop/scripts/governloop_session.py": (
            REPO_ROOT / "skills/workbuddy/governloop/scripts/governloop_session.py"
        ),
        "skills/workbuddy/governloop/references/policy.md": (
            REPO_ROOT / "skills/workbuddy/governloop/references/policy.md"
        ),
        "tools/neutral-relay/neutral_relay.py": (
            REPO_ROOT / "tools/neutral-relay/neutral_relay.py"
        ),
        "docs/architecture/neutral-relay-checkpoint-delivery.md": (
            REPO_ROOT / "docs/architecture/neutral-relay-checkpoint-delivery.md"
        ),
        "docs/ops/AGENT_SAFETY_CONTRACT.md": (
            REPO_ROOT / "docs/ops/AGENT_SAFETY_CONTRACT.md"
        ),
    }

    def make_source_repo(self, branch="main"):
        repo = self.root / f"source-{len(list(self.root.glob('source-*')))}"
        repo.mkdir()
        self.git(repo, "init")
        self.git(repo, "config", "user.email", "installer-test@example.invalid")
        self.git(repo, "config", "user.name", "GovernLoop Installer Test")
        self.git(repo, "checkout", "-b", branch)

        for rel, src in self.RUNTIME_SOURCE_FILES.items():
            dst = repo / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        (repo / "payload.txt").write_text("one\n", encoding="utf-8")
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-m", "initial")
        return repo

    def source_sha(self, repo):
        return self.git(repo, "rev-parse", "HEAD").stdout.strip()

    def source_short_sha(self, repo):
        return self.git(repo, "rev-parse", "--short=8", "HEAD").stdout.strip()

    def installer(self, repo, home, *args, check=True):
        env = os.environ.copy()
        env["GOVERLOOP_HOME"] = str(home)
        return subprocess.run(
            ["sh", str(repo / "scripts" / "install.sh"), *args],
            check=check,
            text=True,
            capture_output=True,
            env=env,
        )

    def test_install_id_generation_for_tag_main_and_local_checkout(self):
        tagged = self.make_source_repo()
        self.git(tagged, "tag", "v0.2.0")
        result = self.installer(tagged, self.root / "tag-home", "--print-install-id")
        self.assertEqual(result.stdout.strip(), f"v0.2.0.{self.source_short_sha(tagged)}")

        main = self.make_source_repo()
        result = self.installer(main, self.root / "main-home", "--print-install-id")
        self.assertEqual(result.stdout.strip(), f"main.{self.source_short_sha(main)}")

        local = self.make_source_repo(branch="feature/local-test")
        result = self.installer(local, self.root / "local-home", "--print-install-id")
        self.assertEqual(result.stdout.strip(), f"local.{self.source_short_sha(local)}")

    def test_duplicate_install_is_rejected_without_overwrite(self):
        repo = self.make_source_repo()
        home = self.root / "home"

        self.installer(repo, home)
        install_id = f"main.{self.source_short_sha(repo)}"
        metadata_before = (home / "versions" / install_id / "metadata.json").read_bytes()
        current_before = os.readlink(home / "current")

        second = self.installer(repo, home, check=False)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already exists and is immutable", second.stderr)
        self.assertEqual((home / "versions" / install_id / "metadata.json").read_bytes(), metadata_before)
        self.assertEqual(os.readlink(home / "current"), current_before)

    def test_metadata_creation_records_required_provenance(self):
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)

        install_id = f"main.{self.source_short_sha(repo)}"
        version_metadata = home / "versions" / install_id / "metadata.json"
        index_metadata = home / "metadata" / f"{install_id}.json"

        self.assertTrue(version_metadata.is_file())
        self.assertTrue(index_metadata.is_file())
        self.assertEqual(version_metadata.read_bytes(), index_metadata.read_bytes())

        payload = json.loads(version_metadata.read_text(encoding="utf-8"))
        self.assertEqual(payload["install_id"], install_id)
        self.assertEqual(payload["source_commit"], self.source_sha(repo))
        self.assertEqual(payload["source_type"], "untagged_checkout")
        self.assertEqual(payload["source_ref"], "main")
        self.assertEqual(payload["installer_version"], "phase2b-runtime-bundle-v1")
        self.assertRegex(payload["install_timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(os.readlink(home / "current"), f"versions/{install_id}")
        self.assertTrue((home / "bin").is_dir())

    def test_failed_install_does_not_corrupt_current_pointer(self):
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        old_target = os.readlink(home / "current")

        # Produce a new immutable source identity.
        (repo / "payload.txt").write_text("two\n", encoding="utf-8")
        self.git(repo, "add", "payload.txt")
        self.git(repo, "commit", "-m", "second")

        # Force a deterministic pre-activation failure: mkdir -p must reject a
        # regular file where the canonical bin/ directory belongs.
        shutil.rmtree(home / "bin")
        (home / "bin").write_text("block directory creation\n", encoding="utf-8")

        failed = self.installer(repo, home, check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertTrue((home / "current").is_symlink())
        self.assertEqual(os.readlink(home / "current"), old_target)

    def test_dirty_tracked_checkout_fails_closed(self):
        repo = self.make_source_repo()
        (repo / "payload.txt").write_text("dirty\n", encoding="utf-8")
        result = self.installer(repo, self.root / "home", "--print-install-id", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tracked working tree is dirty", result.stderr)

    def test_failed_publication_cleanup(self):
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)  # baseline activation

        # Produce a new immutable identity (new INSTALL_ID).
        (repo / "payload.txt").write_text("two\n", encoding="utf-8")
        self.git(repo, "add", "payload.txt")
        self.git(repo, "commit", "-m", "second")
        new_id = self.installer(repo, home, "--print-install-id").stdout.strip()

        # Block the metadata index publication so the install fails AFTER the
        # immutable version_dir is published but BEFORE current activation.
        os.chmod(home / "metadata", 0o500)
        try:
            failed = self.installer(repo, home, check=False)
            self.assertNotEqual(failed.returncode, 0)
            # Failed install leaves no partial immutable version behind.
            self.assertFalse((home / "versions" / new_id).exists())
            self.assertFalse((home / "metadata" / f"{new_id}.json").exists())
            # Previous current pointer is preserved (never touched).
            self.assertTrue((home / "current").is_symlink())
        finally:
            os.chmod(home / "metadata", 0o700)

        # INSTALL_ID is retryable: a clean re-run completes successfully.
        retry = self.installer(repo, home, check=False)
        self.assertEqual(retry.returncode, 0)
        self.assertTrue((home / "versions" / new_id).exists())
        self.assertTrue((home / "metadata" / f"{new_id}.json").is_file())

    def test_current_regular_file_rejected(self):
        repo = self.make_source_repo()
        home = self.root / "home"
        home.mkdir(parents=True)
        (home / "current").write_text("not a symlink\n", encoding="utf-8")

        result = self.installer(repo, home, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("regular file", result.stderr)
        # Fail-closed: pre-existing regular file is preserved, nothing published.
        self.assertTrue((home / "current").is_file())
        self.assertFalse((home / "versions").exists())

    def test_current_directory_rejected(self):
        repo = self.make_source_repo()
        home = self.root / "home"
        home.mkdir(parents=True)
        (home / "current").mkdir()

        result = self.installer(repo, home, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("directory", result.stderr)
        # Fail-closed: pre-existing directory is preserved, nothing published.
        self.assertTrue((home / "current").is_dir())
        self.assertFalse((home / "versions").exists())

    def test_failed_install_preserves_existing_current(self):
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        old_target = os.readlink(home / "current")

        # A tracked modification (uncommitted) forces a pre-publication failure
        # in detect_identity. The already-activated current must remain untouched.
        (repo / "payload.txt").write_text("dirty tracked\n", encoding="utf-8")

        failed = self.installer(repo, home, check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("tracked working tree is dirty", failed.stderr)
        self.assertTrue((home / "current").is_symlink())
        self.assertEqual(os.readlink(home / "current"), old_target)

    def test_activation_interrupt_preserves_committed_install(self):
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        old_target = os.readlink(home / "current")

        # New immutable identity -> new INSTALL_ID.
        (repo / "payload.txt").write_text("two\n", encoding="utf-8")
        self.git(repo, "add", "payload.txt")
        self.git(repo, "commit", "-m", "second")
        new_id = f"main.{self.source_short_sha(repo)}"

        # Simulate a termination between the atomic rename of current and the
        # in-memory activated flag being set. cleanup must detect that current
        # already resolves exactly to this run's version and treat activation as
        # committed (do NOT roll back the published artifacts).
        env = os.environ.copy()
        env["GOVERLOOP_HOME"] = str(home)
        env["GOVERLOOP_FAIL_AFTER_ACTIVATE"] = "1"
        failed = subprocess.run(
            ["sh", str(repo / "scripts" / "install.sh")],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("injected interrupt after activation replace", failed.stderr)
        # Committed: current moved to the new install (not the stale target) and
        # the published version directory is kept rather than rolled back.
        self.assertTrue((home / "current").is_symlink())
        self.assertEqual(os.readlink(home / "current"), f"versions/{new_id}")
        self.assertNotEqual(os.readlink(home / "current"), old_target)
        self.assertTrue((home / "versions" / new_id).is_dir())

    def test_activation_interrupt_before_replace_keeps_old_current(self):
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        old_target = os.readlink(home / "current")

        # New immutable identity -> new INSTALL_ID.
        (repo / "payload.txt").write_text("two\n", encoding="utf-8")
        self.git(repo, "add", "payload.txt")
        self.git(repo, "commit", "-m", "second")
        new_id = f"main.{self.source_short_sha(repo)}"

        # Simulate a termination BEFORE the atomic replacement is committed. The
        # previously active current must remain intact and this run's published
        # (but not activated) artifacts must roll back.
        env = os.environ.copy()
        env["GOVERLOOP_HOME"] = str(home)
        env["GOVERLOOP_FAIL_BEFORE_ACTIVATE"] = "1"
        failed = subprocess.run(
            ["sh", str(repo / "scripts" / "install.sh")],
            text=True,
            capture_output=True,
            env=env,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("injected interrupt before activation replace", failed.stderr)
        # Old current is untouched (still points at the prior install).
        self.assertTrue((home / "current").is_symlink())
        self.assertEqual(os.readlink(home / "current"), old_target)
        # This run's published-but-not-activated artifacts are rolled back.
        self.assertFalse((home / "versions" / new_id).exists())
        self.assertFalse((home / "metadata" / f"{new_id}.json").exists())

    def test_current_symlink_traversal_rejected(self):
        repo = self.make_source_repo()
        home = self.root / "home"
        home.mkdir(parents=True)
        # A symlink whose target uses traversal / nested path: not the exact
        # canonical "versions/<INSTALL_ID>" shape.
        os.symlink("versions/../escape", home / "current")

        result = self.installer(repo, home, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("malformed or uses traversal", result.stderr)
        # Fail-closed: pre-existing symlink is preserved, nothing published.
        self.assertTrue((home / "current").is_symlink())
        self.assertFalse((home / "versions").exists())

    def test_current_symlink_missing_target_rejected(self):
        repo = self.make_source_repo()
        home = self.root / "home"
        home.mkdir(parents=True)
        # A well-shaped but dangling installer-style symlink (target absent).
        os.symlink("versions/does-not-exist", home / "current")

        result = self.installer(repo, home, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing version directory", result.stderr)
        # Fail-closed: pre-existing symlink is preserved, nothing published.
        self.assertTrue((home / "current").is_symlink())
        self.assertFalse((home / "versions").exists())


# ---------------------------------------------------------------------------
# Phase 2B: minimal installed runtime bundle + checkout independence
# ---------------------------------------------------------------------------
INSTALLED_BUNDLE_FILES = {
    "metadata.json",
    "runtime/governloop_session.py",
    "runtime/neutral_relay.py",
    "skills/governloop/SKILL.md",
    "skills/governloop/contracts/neutral-relay-checkpoint-delivery.md",
    "skills/governloop/contracts/AGENT_SAFETY_CONTRACT.md",
    "skills/governloop/contracts/policy.md",
    "bin/governloop",
}


class Phase2BRuntimeBundleTests(InstallerSkeletonTests):
    """Phase 2B: installed runtime bundle, stable entrypoint, checkout independence."""

    def installed_id(self, repo, home):
        return f"main.{self.source_short_sha(repo)}"

    def _relative_files(self, root):
        return {str(p.relative_to(root)) for p in Path(root).rglob("*") if p.is_file()}

    def _assert_installed_skill_refs_resolve(self, version_dir, home):
        """Every backticked local path reference in the installed skill resolves
        inside the installed bundle (or the effective GOVERLOOP_HOME).

        contracts/ refs are relative to the installed skill directory
        (skills/governloop/); runtime/ + bin/ refs are relative to the installed
        version root. Placeholder/prose tokens (owner/repo, /governloop,
        <SESSION_ID>, canonical relay config path) are not bundle artifacts and
        are skipped.
        """
        skill = (version_dir / "skills/governloop/SKILL.md").read_text(encoding="utf-8")
        unresolved = []
        for m in re.finditer(r"`([^`\n]+)`", skill):
            ref = m.group(1).strip()
            if "<" in ref or ">" in ref:
                continue  # template placeholder
            if ref.endswith("config.json"):
                continue  # runtime routing config path, not a bundle artifact
            if not (ref.startswith(("contracts/", "runtime/", "bin/", "skills/"))
                    or ref.startswith("~")):
                continue  # prose / command token, not a bundle artifact
            if ref.startswith("~"):
                mapped = ref.replace("~/.governloop", str(home), 1)
                if not Path(os.path.expanduser(mapped)).exists():
                    unresolved.append(ref)
                continue
            base = version_dir
            if ref.startswith("contracts/"):
                base = version_dir / "skills/governloop"
            if not (base / ref).exists():
                unresolved.append(ref)
        self.assertEqual(unresolved, [], f"unresolved installed-skill refs: {unresolved}")

    def test_bundle_contains_exactly_required_artifacts(self):
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        version_dir = home / "versions" / self.installed_id(repo, home)
        self.assertEqual(self._relative_files(version_dir), INSTALLED_BUNDLE_FILES)

    def test_installed_paths_exist_and_wrapper_executable(self):
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        version_dir = home / "versions" / self.installed_id(repo, home)
        for rel in (
            "runtime/governloop_session.py",
            "runtime/neutral_relay.py",
            "skills/governloop/SKILL.md",
            "skills/governloop/contracts/neutral-relay-checkpoint-delivery.md",
            "skills/governloop/contracts/AGENT_SAFETY_CONTRACT.md",
            "skills/governloop/contracts/policy.md",
            "bin/governloop",
        ):
            p = version_dir / rel
            self.assertTrue(p.is_file(), rel)
            self.assertGreater(p.stat().st_size, 0, rel)
        self.assertTrue(os.access(version_dir / "bin/governloop", os.X_OK))
        self.assertTrue(os.access(home / "bin/governloop", os.X_OK))

    def test_stable_command_resolution_through_current(self):
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        install_id = self.installed_id(repo, home)
        # stable -> current -> version wrapper chain
        self.assertEqual(os.readlink(home / "current"), f"versions/{install_id}")
        target = self.root / "target-project"
        target.mkdir()
        env = os.environ.copy()
        env["GOVERLOOP_HOME"] = str(home)
        env["GOVERLOOP_STATE_DIR"] = str(self.root / "state")
        r = subprocess.run(
            [str(home / "bin/governloop"), "status"],
            cwd=str(target), env=env, text=True, capture_output=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no active session for target-project", r.stdout)

    def test_no_source_checkout_path_leakage(self):
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        version_dir = home / "versions" / self.installed_id(repo, home)
        # The fixture checkout path must not appear anywhere in the bundle.
        for p in version_dir.rglob("*"):
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn(str(repo), text, f"source-checkout leak in {p}")
        # Installed skill/contracts must not keep checkout-relative references.
        skill = (version_dir / "skills/governloop/SKILL.md").read_text(encoding="utf-8")
        for forbidden in ("docs/", "tools/", "skills/workbuddy", "README.md"):
            self.assertNotIn(forbidden, skill)
        for contract in (version_dir / "skills/governloop/contracts").glob("*.md"):
            text = contract.read_text(encoding="utf-8")
            self.assertNotIn("tools/", text)
            self.assertNotIn("docs/", text)

    def test_installed_skill_normative_refs_resolve_inside_bundle(self):
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        self._assert_installed_skill_refs_resolve(
            home / "versions" / self.installed_id(repo, home), home)

    def test_checkout_independence_after_removing_source(self):
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        install_id = self.installed_id(repo, home)
        version_dir = home / "versions" / install_id

        # Remove the original checkout.
        shutil.rmtree(repo)

        # Installed skill normative references still resolve inside the bundle.
        self._assert_installed_skill_refs_resolve(version_dir, home)

        # Installed runtime files still exist and are invokable.
        self.assertTrue((version_dir / "runtime/governloop_session.py").is_file())
        self.assertTrue((version_dir / "runtime/neutral_relay.py").is_file())
        self.assertTrue(os.access(version_dir / "bin/governloop", os.X_OK))

        # Stable entrypoint still functions from an arbitrary cwd (status).
        target = self.root / "target-project"
        target.mkdir()
        env = os.environ.copy()
        env["GOVERLOOP_HOME"] = str(home)
        env["GOVERLOOP_STATE_DIR"] = str(self.root / "state")
        r = subprocess.run(
            [str(home / "bin/governloop"), "status"],
            cwd=str(target), env=env, text=True, capture_output=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no active session for target-project", r.stdout)

        # new (non-destructive session creation; exits 3 without a bound URL).
        r = subprocess.run(
            [str(home / "bin/governloop"), "new"],
            cwd=str(target), env=env, text=True, capture_output=True,
        )
        self.assertEqual(r.returncode, 3)
        self.assertIn("USER_CONVERSATION_SELECTION_REQUIRED", r.stdout)

        # Relay resolution: the installed session manager finds the sibling relay.
        saved = os.environ.pop("GOVERLOOP_RELAY_PATH", None)
        try:
            spec = importlib.util.spec_from_file_location(
                "installed_gsm", str(version_dir / "runtime/governloop_session.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            if saved is not None:
                os.environ["GOVERLOOP_RELAY_PATH"] = saved
        self.assertEqual(
            os.path.realpath(mod.RELAY_DEFAULT),
            os.path.realpath(version_dir / "runtime/neutral_relay.py"),
        )

    def test_target_project_cwd_preserved(self):
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)

        target = self.root / "target-repo"
        target.mkdir()
        self.git(target, "init")
        self.git(target, "config", "user.email", "target@example.invalid")
        self.git(target, "config", "user.name", "Target")
        self.git(target, "remote", "add", "origin",
                 "https://github.com/fakeowner/fakeproject.git")
        (target / "x.txt").write_text("x\n", encoding="utf-8")
        self.git(target, "add", ".")
        self.git(target, "commit", "-m", "init")

        state_dir = self.root / "state"
        state_dir.mkdir()
        env = os.environ.copy()
        env["GOVERLOOP_HOME"] = str(home)
        env["GOVERLOOP_STATE_DIR"] = str(state_dir)
        r = subprocess.run(
            [str(home / "bin/governloop"), "new", "--title", "phase2b-check"],
            cwd=str(target), env=env, text=True, capture_output=True,
        )
        self.assertEqual(r.returncode, 3)
        self.assertIn("USER_CONVERSATION_SELECTION_REQUIRED", r.stdout)
        # Session was created for the TARGET project, proving cwd preservation.
        self.assertIn("fakeowner/fakeproject", r.stdout)
        states = list(state_dir.glob("governloop-session-*.json"))
        self.assertEqual(len(states), 1)
        state = json.loads(states[0].read_text(encoding="utf-8"))
        self.assertEqual(state["repo"], "fakeowner/fakeproject")
        self.assertNotIn(str(home), state["repo"])

    def test_failed_staging_preserves_previous_current(self):
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        old_id = self.installed_id(repo, home)
        old_target = os.readlink(home / "current")

        # New immutable identity -> new INSTALL_ID.
        (repo / "payload.txt").write_text("two\n", encoding="utf-8")
        self.git(repo, "add", "payload.txt")
        self.git(repo, "commit", "-m", "second")
        new_id = self.installed_id(repo, home)

        # Block bundle staging: the stage dir cannot be created inside versions/.
        os.chmod(home / "versions", 0o500)
        try:
            failed = self.installer(repo, home, check=False)
            self.assertNotEqual(failed.returncode, 0)
        finally:
            os.chmod(home / "versions", 0o700)

        # Previous current + version preserved; no partial new version.
        self.assertEqual(os.readlink(home / "current"), old_target)
        self.assertTrue((home / "versions" / old_id).is_dir())
        self.assertFalse((home / "versions" / new_id).exists())
        self.assertEqual([p for p in (home / "versions").glob(".*.stage.*")], [])

        # Retryable after the blocker is cleared.
        retry = self.installer(repo, home, check=False)
        self.assertEqual(retry.returncode, 0)
        self.assertTrue((home / "versions" / new_id).is_dir())
        self.assertEqual(os.readlink(home / "current"), f"versions/{new_id}")

    def test_dispatcher_publication_failure_keeps_previous_state(self):
        """P1 fix: stable entrypoint publication is inside the activation
        transaction. A deterministic dispatcher-publication failure must leave
        the previous current + previous version + previous dispatcher intact
        and must not leave a poisoned/half-committed active state."""
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        old_id = self.installed_id(repo, home)
        old_target = os.readlink(home / "current")
        dispatcher_before = (home / "bin/governloop").read_bytes()

        # New immutable identity -> new INSTALL_ID.
        (repo / "payload.txt").write_text("two\n", encoding="utf-8")
        self.git(repo, "add", "payload.txt")
        self.git(repo, "commit", "-m", "second")
        new_id = self.installed_id(repo, home)

        # Deterministic dispatcher-publication failure (env-gated injection).
        env = os.environ.copy()
        env["GOVERLOOP_HOME"] = str(home)
        env["GOVERLOOP_FAIL_PUBLISH_DISPATCHER"] = "1"
        failed = subprocess.run(
            ["sh", str(repo / "scripts" / "install.sh")],
            text=True, capture_output=True, env=env,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("injected interrupt: stable entrypoint publication", failed.stderr)
        # Previous current unchanged; previous known-good version intact.
        self.assertEqual(os.readlink(home / "current"), old_target)
        self.assertTrue((home / "versions" / old_id).is_dir())
        # Existing stable entrypoint remains byte-identical.
        self.assertEqual((home / "bin/governloop").read_bytes(), dispatcher_before)
        # Failed new install leaves no poisoned active state: the new version
        # was published but not committed, so it rolled back and current never
        # pointed at it.
        self.assertFalse((home / "versions" / new_id).exists())
        self.assertFalse((home / "metadata" / f"{new_id}.json").exists())
        # No leftover stage artifacts.
        self.assertEqual(list((home / "versions").glob(".*.stage.*")), [])
        self.assertEqual(list(home.glob(".bin.governloop.stage.*")), [])

        # Retry after the injected failure is removed succeeds and activates.
        retry = self.installer(repo, home, check=False)
        self.assertEqual(retry.returncode, 0)
        self.assertTrue((home / "versions" / new_id).is_dir())
        self.assertEqual(os.readlink(home / "current"), f"versions/{new_id}")

    def test_first_install_rollback_removes_created_dispatcher(self):
        """P1 fix: a failed FIRST install must not leave the stable dispatcher
        behind. Initial state has no current / version / metadata / dispatcher;
        dispatcher publication succeeds, then GOVERLOOP_FAIL_BEFORE_ACTIVATE
        fails the run before current activation -> rollback must remove the
        dispatcher this run created."""
        repo = self.make_source_repo()
        home = self.root / "fresh-home"
        install_id = self.installed_id(repo, home)
        # Initial state: nothing exists.
        self.assertFalse(home.exists())

        env = os.environ.copy()
        env["GOVERLOOP_HOME"] = str(home)
        env["GOVERLOOP_FAIL_BEFORE_ACTIVATE"] = "1"
        failed = subprocess.run(
            ["sh", str(repo / "scripts" / "install.sh")],
            text=True, capture_output=True, env=env,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("injected interrupt before activation replace", failed.stderr)
        # No partial failed install: current, version, metadata, dispatcher all
        # absent, and no stage artifacts remain.
        self.assertFalse((home / "current").exists())
        self.assertFalse((home / "versions" / install_id).exists())
        self.assertFalse((home / "metadata" / f"{install_id}.json").exists())
        self.assertFalse((home / "bin/governloop").exists())
        self.assertEqual(list((home / "versions").glob(".*.stage.*")), [])
        self.assertEqual(list(home.glob(".bin.governloop.stage.*")), [])
        self.assertEqual(list(home.glob(".current.stage.*")), [])

        # Retry without the injection succeeds (INSTALL_ID still retryable).
        retry = self.installer(repo, home, check=False)
        self.assertEqual(retry.returncode, 0)
        self.assertTrue((home / "versions" / install_id).is_dir())
        self.assertTrue((home / "bin/governloop").is_file())
        self.assertEqual(os.readlink(home / "current"), f"versions/{install_id}")


# ---------------------------------------------------------------------------
# Phase 2C: stable CLI + read-only doctor
# ---------------------------------------------------------------------------
class Phase2CDiagnosticsTests(Phase2BRuntimeBundleTests):
    """Phase 2C: stable CLI routes, doctor subcommand, read-only guarantees."""

    def test_doctor_subcommand_routed_by_stable_cli(self):
        """stable CLI forwards `doctor` to the active version without cd."""
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        target = self.root / "target-project"
        target.mkdir()
        env = os.environ.copy()
        env["GOVERLOOP_HOME"] = str(home)
        env.pop("GOVERLOOP_RELAY_PATH", None)
        r = subprocess.run(
            [str(home / "bin/governloop"), "doctor"],
            cwd=str(target), env=env, text=True, capture_output=True,
        )
        # doctor is read-only and must always exit 0 (PASS) or 1 (FAIL), never crash
        self.assertIn(r.returncode, (0, 1), r.stderr)
        output = r.stdout + r.stderr
        self.assertIn("[PASS]", output)

    def test_doctor_is_readonly_no_filesystem_mutation(self):
        """doctor must not create, delete, or modify any files."""
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)

        # Snapshot all files before running doctor.
        def all_files(root):
            return {str(p.relative_to(root)) for p in Path(root).rglob("*") if p.is_file()}

        before = all_files(home)

        env = os.environ.copy()
        env["GOVERLOOP_HOME"] = str(home)
        env.pop("GOVERLOOP_RELAY_PATH", None)
        r = subprocess.run(
            [str(home / "bin/governloop"), "doctor"],
            env=env, text=True, capture_output=True,
        )
        self.assertIn(r.returncode, (0, 1))

        after = all_files(home)
        created = after - before
        deleted = before - after
        self.assertEqual(created, set(), f"doctor created files: {created}")
        self.assertEqual(deleted, set(), f"doctor deleted files: {deleted}")

    def test_doctor_works_after_source_checkout_removed(self):
        """doctor works from an installed runtime after the source checkout is gone."""
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        install_id = self.installed_id(repo, home)
        version_dir = home / "versions" / install_id

        # Remove the original checkout (simulates production after install).
        shutil.rmtree(repo)

        env = os.environ.copy()
        env["GOVERLOOP_HOME"] = str(home)
        env.pop("GOVERLOOP_RELAY_PATH", None)
        r = subprocess.run(
            [str(home / "bin/governloop"), "doctor"],
            env=env, text=True, capture_output=True,
        )
        self.assertIn(r.returncode, (0, 1), r.stderr)
        output = r.stdout + r.stderr
        self.assertIn("[PASS]", output)
        # Neutral Relay check should PASS (sibling resolution inside version dir).
        relay_lines = [l for l in output.splitlines() if "Neutral Relay" in l]
        self.assertTrue(relay_lines, "doctor must report Neutral Relay status")
        self.assertTrue(any("PASS" in l for l in relay_lines),
                        f"Neutral Relay should PASS but got: {relay_lines}")

    def test_doctor_caller_cwd_preserved(self):
        """stable CLI does not change the caller's cwd; doctor runs from target project."""
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        target = self.root / "target-repo"
        target.mkdir()
        self.git(target, "init")
        self.git(target, "config", "user.email", "t@example.invalid")
        self.git(target, "config", "user.name", "Target")
        self.git(target, "remote", "add", "origin",
                 "https://github.com/owner/project.git")
        (target / "x.txt").write_text("x\n", encoding="utf-8")
        self.git(target, "add", ".")
        self.git(target, "commit", "-m", "init")

        env = os.environ.copy()
        env["GOVERLOOP_HOME"] = str(home)
        env.pop("GOVERLOOP_RELAY_PATH", None)
        r = subprocess.run(
            [str(home / "bin/governloop"), "doctor"],
            cwd=str(target), env=env, text=True, capture_output=True,
        )
        self.assertIn(r.returncode, (0, 1), r.stderr)
        # doctor must report the target repo detection using the caller's cwd.
        output = r.stdout + r.stderr
        self.assertTrue(any("Target repo detection" in l for l in output.splitlines()),
                        "doctor must include Target repo detection line")
        # The cwd must be reported as the target directory, not the home.
        target_lines = [l for l in output.splitlines() if "Target repo detection" in l]
        for line in target_lines:
            self.assertIn(str(target), line,
                          f"doctor cwd detection should mention target path: {line}")
            self.assertNotIn(str(home), line,
                             f"doctor cwd detection must not mention home path: {line}")

    def test_doctor_detects_missing_current(self):
        """doctor reports FAIL when current pointer is absent."""
        home = self.root / "broken-home"
        home.mkdir(parents=True)
        (home / "bin").mkdir()

        # Call the session manager Python directly (the stable CLI dispatcher
        # requires current to exist; doctor must detect the broken state).
        # Use the installed runtime from a different home as the Python binary.
        repo = self.make_source_repo()
        working_home = self.root / "working-home"
        self.installer(repo, working_home)
        install_id = self.installed_id(repo, working_home)
        version_dir = working_home / "versions" / install_id
        python_bin = version_dir / "runtime" / "governloop_session.py"

        env = os.environ.copy()
        env["GOVERLOOP_HOME"] = str(home)
        env.pop("GOVERLOOP_RELAY_PATH", None)
        r = subprocess.run(
            [sys.executable, str(python_bin), "doctor"],
            env=env, text=True, capture_output=True,
        )
        # Must not crash; must report FAIL.
        self.assertIn(r.returncode, (0, 1))
        output = r.stdout + r.stderr
        self.assertIn("[FAIL]", output)

    def test_doctor_detects_missing_runtime_files(self):
        """doctor reports FAIL/WARN when runtime files are missing."""
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        install_id = self.installed_id(repo, home)
        version_dir = home / "versions" / install_id

        # Remove a required runtime file.
        runtime_file = version_dir / "runtime" / "governloop_session.py"
        self.assertTrue(runtime_file.is_file())
        backup = runtime_file.read_bytes()
        runtime_file.unlink()
        try:
            env = os.environ.copy()
            env["GOVERLOOP_HOME"] = str(home)
            env.pop("GOVERLOOP_RELAY_PATH", None)
            # Use the version wrapper: its guard detects missing runtime files
            # and exits with [FAIL] before Python doctor can even start.
            # This is correct: the wrapper is the outer safety net.
            r = subprocess.run(
                [str(version_dir / "bin/governloop"), "doctor"],
                env=env, text=True, capture_output=True,
            )
            self.assertIn(r.returncode, (0, 1))
            output = r.stdout + r.stderr
            self.assertIn("[FAIL]", output)
        finally:
            runtime_file.write_bytes(backup)

    def test_doctor_detects_missing_contracts(self):
        """doctor reports WARN when normative contracts are absent."""
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        install_id = self.installed_id(repo, home)
        version_dir = home / "versions" / install_id

        contracts_dir = version_dir / "skills" / "governloop" / "contracts"
        policy = contracts_dir / "policy.md"
        self.assertTrue(policy.is_file())
        backup = policy.read_bytes()
        policy.unlink()
        try:
            env = os.environ.copy()
            env["GOVERLOOP_HOME"] = str(home)
            env.pop("GOVERLOOP_RELAY_PATH", None)
            r = subprocess.run(
                [str(home / "bin/governloop"), "doctor"],
                env=env, text=True, capture_output=True,
            )
            self.assertIn(r.returncode, (0, 1))
            output = r.stdout + r.stderr
            self.assertIn("[WARN]", output)
            self.assertTrue(any("Contract: policy.md" in l and "missing" in l
                                for l in output.splitlines()),
                            f"doctor should WARN on missing contract: {output}")
        finally:
            policy.write_bytes(backup)

    def test_stable_cli_all_subcommands_routed(self):
        """stable CLI routes all Phase 2C subcommands (new, bind, status, checkpoint, end, doctor)."""
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        target = self.root / "target-project"
        target.mkdir()
        self.git(target, "init")
        self.git(target, "config", "user.email", "t@example.invalid")
        self.git(target, "config", "user.name", "Target")
        self.git(target, "remote", "add", "origin",
                 "https://github.com/owner/project.git")
        (target / "x.txt").write_text("x\n", encoding="utf-8")
        self.git(target, "add", ".")
        self.git(target, "commit", "-m", "init")

        state_dir = self.root / "state"
        state_dir.mkdir()
        env = os.environ.copy()
        env["GOVERLOOP_HOME"] = str(home)
        env["GOVERLOOP_STATE_DIR"] = str(state_dir)
        env.pop("GOVERLOOP_RELAY_PATH", None)

        for subcmd in ("status", "doctor"):
            r = subprocess.run(
                [str(home / "bin/governloop"), subcmd],
                cwd=str(target), env=env, text=True, capture_output=True,
            )
            # Must not crash (exit 255) — valid exit codes are 0, 1, 3.
            self.assertIn(r.returncode, (0, 1, 3),
                          f"`{subcmd}` crashed: {r.stderr}")

        # new creates a session (exits 3 without a bound URL).
        r = subprocess.run(
            [str(home / "bin/governloop"), "new"],
            cwd=str(target), env=env, text=True, capture_output=True,
        )
        self.assertEqual(r.returncode, 3, f"new should exit 3 (no URL): {r.stdout}")
        self.assertIn("USER_CONVERSATION_SELECTION_REQUIRED", r.stdout)

        # bind without a session fails with a clear error.
        r = subprocess.run(
            [str(home / "bin/governloop"), "bind", "https://chatgpt.com/c/test"],
            cwd=str(target), env=env, text=True, capture_output=True,
        )
        self.assertIn(r.returncode, (0, 1, 3))

        # checkpoint with no session fails gracefully.
        r = subprocess.run(
            [str(home / "bin/governloop"), "checkpoint", "NEW_BLOCKER"],
            cwd=str(target), env=env, text=True, capture_output=True,
        )
        self.assertIn(r.returncode, (1, 3))

    def test_doctor_not_mutate_state_dir(self):
        """doctor must not create session state files."""
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        state_dir = self.root / "state"
        state_dir.mkdir()

        before = set(os.listdir(str(state_dir)))

        env = os.environ.copy()
        env["GOVERLOOP_HOME"] = str(home)
        env["GOVERLOOP_STATE_DIR"] = str(state_dir)
        env.pop("GOVERLOOP_RELAY_PATH", None)
        r = subprocess.run(
            [str(home / "bin/governloop"), "doctor"],
            env=env, text=True, capture_output=True,
        )
        self.assertIn(r.returncode, (0, 1))
        after = set(os.listdir(str(state_dir)))
        created = after - before
        self.assertEqual(created, set(), f"doctor created state files: {created}")

    def test_doctor_does_not_create_state_dir(self):
        """doctor must NOT create a nonexistent GOVERLOOP_STATE_DIR (strictly
        read-only, before any session-state mutation)."""
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)

        state_dir = self.root / "nonexistent-state"
        self.assertFalse(state_dir.exists())

        env = os.environ.copy()
        env["GOVERLOOP_HOME"] = str(home)
        env["GOVERLOOP_STATE_DIR"] = str(state_dir)
        env.pop("GOVERLOOP_RELAY_PATH", None)
        r = subprocess.run(
            [str(home / "bin/governloop"), "doctor"],
            env=env, text=True, capture_output=True,
        )
        self.assertIn(r.returncode, (0, 1), r.stderr)
        self.assertFalse(
            state_dir.exists(),
            "doctor must not create GOVERLOOP_STATE_DIR (read-only)",
        )

    def test_doctor_current_target_validation(self):
        """doctor rejects non-canonical current targets exactly like the
        installer: only versions/<single-install-id> is valid."""
        repo = self.make_source_repo()
        home = self.root / "home"
        self.installer(repo, home)
        install_id = self.installed_id(repo, home)
        version_dir = home / "versions" / install_id
        session_manager = version_dir / "runtime" / "governloop_session.py"

        # Invoke the session manager directly so doctor's own validation runs
        # (the stable CLI wrapper guards on current/bin/governloop first).
        def run_doctor():
            env = os.environ.copy()
            env["GOVERLOOP_HOME"] = str(home)
            env.pop("GOVERLOOP_RELAY_PATH", None)
            return subprocess.run(
                [sys.executable, str(session_manager), "doctor"],
                env=env, text=True, capture_output=True,
            )

        # Canonical target passes.
        r = run_doctor()
        self.assertIn("[PASS]", r.stdout + r.stderr)
        self.assertIn("current pointer", r.stdout)

        invalid_targets = [
            "versions/",         # empty component
            "versions/.",        # current directory
            "versions/..",       # parent directory
            "versions/../x",     # traversal
            "versions/a/b",      # nested path
            "not-versions/x",    # non-versions target
        ]
        for tgt in invalid_targets:
            os.unlink(home / "current")
            os.symlink(tgt, home / "current")
            r = run_doctor()
            output = r.stdout + r.stderr
            self.assertIn("[FAIL]", output, f"target {tgt!r} must FAIL doctor")
            self.assertIn("current pointer", output, f"target {tgt!r}")
        # Restore canonical state for the dangling check.
        os.unlink(home / "current")
        os.symlink(f"versions/{install_id}", home / "current")

        # Dangling (well-shaped but missing target) must FAIL.
        os.unlink(home / "current")
        os.symlink("versions/does-not-exist", home / "current")
        r = run_doctor()
        output = r.stdout + r.stderr
        self.assertIn("[FAIL]", output)
        self.assertIn("dangling", output)


if __name__ == "__main__":
    unittest.main()
