import json
import os
import shutil
import subprocess
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

    def make_source_repo(self, branch="main"):
        repo = self.root / f"source-{len(list(self.root.glob('source-*')))}"
        repo.mkdir()
        self.git(repo, "init")
        self.git(repo, "config", "user.email", "installer-test@example.invalid")
        self.git(repo, "config", "user.name", "GovernLoop Installer Test")
        self.git(repo, "checkout", "-b", branch)

        scripts = repo / "scripts"
        scripts.mkdir()
        shutil.copyfile(INSTALLER_SOURCE, scripts / "install.sh")
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
        self.assertEqual(payload["installer_version"], "phase2a-skeleton-v1")
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
        self.assertIn("injected interrupt after activation rename", failed.stderr)
        # Committed: current moved to the new install (not the stale target) and
        # the published version directory is kept rather than rolled back.
        self.assertTrue((home / "current").is_symlink())
        self.assertEqual(os.readlink(home / "current"), f"versions/{new_id}")
        self.assertNotEqual(os.readlink(home / "current"), old_target)
        self.assertTrue((home / "versions" / new_id).is_dir())

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


if __name__ == "__main__":
    unittest.main()
