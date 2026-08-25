import os
import pathlib
import subprocess
import tempfile
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "install-agent-skills.sh"


class AgentSkillInstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.tmp.name)
        self.governloop_home = self.home / ".governloop"
        self.skill = self.governloop_home / "current" / "skills" / "governloop"
        self.skill.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("---\nname: governloop\n---\n", encoding="utf-8")
        self.env = os.environ.copy()
        self.env.update(
            {
                "HOME": str(self.home),
                "GOVERLOOP_HOME": str(self.governloop_home),
                "XDG_CONFIG_HOME": str(self.home / ".config"),
                "WORKBUDDY_HOME": str(self.home / ".workbuddy"),
                "CLAUDE_HOME": str(self.home / ".claude"),
                "CODEX_HOME": str(self.home / ".codex"),
            }
        )

    def tearDown(self):
        self.tmp.cleanup()

    def run_installer(self, *args, input_text=None):
        return subprocess.run(
            ["sh", str(SCRIPT), *args],
            input=input_text,
            text=True,
            capture_output=True,
            env=self.env,
            check=False,
        )

    def assert_link(self, path):
        path = pathlib.Path(path)
        self.assertTrue(path.is_symlink(), path)
        self.assertEqual(os.readlink(path), str(self.skill))

    def test_installs_same_universal_skill_for_supported_skill_agents(self):
        result = self.run_installer(
            "--agents", "workbuddy,opencode,claude,codex"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_link(self.home / ".workbuddy" / "skills" / "governloop")
        self.assert_link(self.home / ".config" / "opencode" / "skills" / "governloop")
        self.assert_link(self.home / ".claude" / "skills" / "governloop")
        self.assert_link(self.home / ".codex" / "skills" / "governloop")

    def test_existing_matching_link_is_idempotent(self):
        first = self.run_installer("--agents", "codex")
        second = self.run_installer("--agents", "codex")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already linked", second.stdout)

    def test_refuses_to_overwrite_existing_user_owned_skill(self):
        destination = self.home / ".claude" / "skills" / "governloop"
        destination.mkdir(parents=True)
        (destination / "SKILL.md").write_text("user-owned", encoding="utf-8")
        result = self.run_installer("--agents", "claude")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to overwrite user-owned state", result.stderr)
        self.assertEqual(
            (destination / "SKILL.md").read_text(encoding="utf-8"), "user-owned"
        )

    def test_dsh_uses_native_adapter_instead_of_generic_skill_copy(self):
        result = self.run_installer("--agents", "dsh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "dsh plugin --profile <name> add governloop-dsh@0.1.1", result.stdout
        )
        self.assertFalse((self.home / ".dsh" / "skills" / "governloop").exists())

    def test_noninteractive_without_selection_skips_cleanly(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skipped (non-interactive)", result.stdout)

    def test_environment_selection_supports_automation(self):
        self.env["GOVERLOOP_INSTALL_AGENTS"] = "1,4"
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_link(self.home / ".workbuddy" / "skills" / "governloop")
        self.assert_link(self.home / ".codex" / "skills" / "governloop")


if __name__ == "__main__":
    unittest.main()
