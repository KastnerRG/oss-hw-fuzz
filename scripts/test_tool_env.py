"""Exercise discovery using fake installations without running vendor commands."""

from contextlib import redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import shlex
import shutil
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().with_name("tool-env.py")
SPEC = importlib.util.spec_from_file_location("tool_env", SOURCE)
tool_env = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool_env)


class ToolEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="tool-env-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.executed_marker = self.root / "unexpected-command-execution"
        self.addCleanup(lambda: self.assertFalse(self.executed_marker.exists(),
                                                "Discovery executed a fake vendor command"))
        self.image_bin = self.root / "image" / "bin"
        self.image_bin.mkdir(parents=True)

    def executable(self, directory, name):
        directory.mkdir(parents=True, exist_ok=True)
        program = directory / name
        program.write_text("#!/bin/sh\nprintf called > " +
                           shlex.quote(str(self.executed_marker)) + "\nexit 99\n")
        program.chmod(0o755)
        return program

    def discover(self, roots=(), extra_paths=(), base_path=None):
        return tool_env.discover(list(roots), list(extra_paths),
                                 str(self.image_bin) if base_path is None else base_path)

    def test_first_root_wins_and_numeric_versions_sort_within_root(self):
        preferred = self.root / "preferred"
        fallback = self.root / "fallback"
        old = preferred / "vcs" / "V-2022.06-SP2-9" / "bin"
        newest = preferred / "vcs" / "V-2022.06-SP2-10" / "bin"
        other_root = fallback / "vcs" / "V-2025.12" / "bin"
        for directory in (old, newest, other_root):
            self.executable(directory, "vcs")
        fallback_verdi = fallback / "verdi" / "V-2025.12" / "bin"
        self.executable(fallback_verdi, "verdi")

        result = self.discover([preferred, fallback])
        self.assertEqual(result["tools"]["vcs"]["executable"], str(newest / "vcs"))
        self.assertEqual(result["environment"]["VCS_HOME"], str(newest.parent))
        self.assertEqual(result["tools"]["verdi"]["executable"],
                         str(fallback_verdi / "verdi"))
        self.assertNotIn(str(old), result["environment"]["PATH"].split(":"))
        self.assertNotIn(str(other_root), result["environment"]["PATH"].split(":"))

    def test_explicit_path_overrides_version_and_updates_both_homes(self):
        installs = self.root / "installs"
        newest = installs / "Vivado" / "2025.2" / "bin"
        chosen = installs / "Vivado" / "2023.2" / "bin"
        second_override = installs / "Vivado" / "2024.2" / "bin"
        for directory in (newest, chosen, second_override):
            self.executable(directory, "vivado")

        result = self.discover([installs], [chosen, second_override])
        self.assertEqual(result["tools"]["vivado"]["executable"], str(chosen / "vivado"))
        for name in ("VIVADO_HOME", "XILINX_VIVADO"):
            self.assertEqual(result["environment"][name], str(chosen.parent))
        self.assertEqual(result["environment"]["PATH"].split(":")[:3],
                         [str(self.image_bin), str(chosen), str(second_override)])

    def test_directory_symlink_keeps_public_launcher_and_install_home(self):
        installs = self.root / "installs"
        home = installs / "XCELIUM2403"
        payload = self.executable(home / "tools.lnx86" / "inca" / "bin", "xrun")
        public_bin = home / "tools.lnx86" / "bin"
        public_bin.mkdir()
        (public_bin / "xrun").symlink_to(Path("..") / "inca" / "bin" / "xrun")
        (home / "tools").symlink_to("tools.lnx86", target_is_directory=True)

        result = self.discover([installs])
        launcher = Path(result["tools"]["xcelium"]["executable"])
        self.assertEqual(launcher.name, "xrun")
        self.assertIn(launcher.parent, (home / "tools" / "bin", public_bin))
        self.assertEqual(launcher.resolve(), payload)
        self.assertEqual(result["environment"]["XCELIUM_HOME"], str(home))

    def test_shared_wrapper_does_not_replace_public_command_name(self):
        home = self.root / "installs" / "GENUS231"
        wrapper = self.executable(home / "bin", ".cdnWrapperIndep")
        launcher = home / "bin" / "genus"
        launcher.symlink_to(wrapper.name)

        result = self.discover([home.parent])
        self.assertEqual(result["tools"]["genus"]["executable"], str(launcher))
        self.assertEqual(result["environment"]["GENUS_HOME"], str(home))

    def test_directory_cycle_does_not_repeat_candidates(self):
        installs = self.root / "installs"
        selected = installs / "V-2025.12" / "bin"
        self.executable(selected, "vcs")
        (installs / "a-cycle").symlink_to(installs, target_is_directory=True)

        candidates = list(tool_env.bin_directories(installs))
        self.assertEqual([path for path in candidates if tool_env.executable(path / "vcs")],
                         [selected])
        self.assertEqual(len(candidates), len({path.resolve() for path in candidates}))
        result = self.discover([installs])
        self.assertIn("vcs", result["tools"], "Symlink cycle hid a valid sibling installation")
        self.assertEqual(result["tools"]["vcs"]["executable"], str(selected / "vcs"))

    def test_unresolvable_symlink_cycle_does_not_hide_valid_sibling_install(self):
        installs = self.root / "installs"
        selected = installs / "V-2025.12" / "bin"
        self.executable(selected, "vcs")
        (installs / "self-cycle").symlink_to("self-cycle")
        (installs / "cycle-a").symlink_to("cycle-b")
        (installs / "cycle-b").symlink_to("cycle-a")

        result = self.discover([installs])
        self.assertIn("vcs", result["tools"], "Symlink cycle hid a valid sibling installation")
        self.assertEqual(result["tools"]["vcs"]["executable"], str(selected / "vcs"))

    def test_image_java_and_compilers_stay_before_explicit_vendor_path(self):
        vendor_bin = self.root / "vendor" / "bin"
        for command in ("java", "gcc", "g++"):
            self.executable(self.image_bin, command)
            self.executable(vendor_bin, command)
        self.executable(vendor_bin, "vcs")

        result = self.discover([vendor_bin.parent], [vendor_bin])
        path = result["environment"]["PATH"]
        for command in ("java", "gcc", "g++"):
            self.assertEqual(shutil.which(command, path=path), str(self.image_bin / command))
        self.assertEqual(shutil.which("vcs", path=path), str(vendor_bin / "vcs"))

    def test_homes_match_effective_path_when_a_bin_contains_multiple_tools(self):
        installs = self.root / "installs"
        combined = installs / "combined2024" / "bin"
        newer_verdi = installs / "verdi2025" / "bin"
        self.executable(combined, "vcs")
        self.executable(combined, "verdi")
        self.executable(newer_verdi, "verdi")

        result = self.discover([installs])
        executable = shutil.which("verdi", path=result["environment"]["PATH"])
        self.assertEqual(result["tools"]["verdi"]["executable"], executable)
        self.assertEqual(result["environment"]["VERDI_HOME"],
                         str(Path(executable).parent.parent))

    def test_missing_non_directory_and_relative_explicit_paths_are_rejected(self):
        ordinary_file = self.root / "not-a-directory"
        ordinary_file.write_text("not a directory")
        for directory in (self.root / "missing" / "bin", ordinary_file, Path("relative/bin")):
            with self.subTest(directory=directory):
                with self.assertRaisesRegex(ValueError, "TOOL_PATHS"):
                    self.discover(extra_paths=[directory])

    def test_spaces_survive_discovery_and_nul_environment_output(self):
        installs = self.root / "host tool installs"
        chosen = installs / "Vivado 2025.2" / "Vivado" / "bin"
        self.executable(chosen, "vivado")
        image_bin = self.root / "image tools" / "bin"
        image_bin.mkdir(parents=True)
        environment = {"TOOL_ROOTS": str(installs), "TOOL_PATHS": str(chosen),
                       "PATH": str(image_bin)}
        output = io.StringIO()
        with patch.dict(os.environ, environment), \
             patch("sys.argv", [str(SOURCE), "--format", "env"]), \
             redirect_stdout(output):
            tool_env.main()

        self.assertTrue(output.getvalue().endswith("\0"))
        records = dict(record.split("=", 1) for record in output.getvalue().split("\0")[:-1])
        self.assertEqual(records["TOOL_ROOTS"], str(installs))
        self.assertEqual(records["TOOL_PATHS"], str(chosen))
        self.assertEqual(records["VIVADO_HOME"], str(chosen.parent))
        self.assertEqual(records["PATH"], f"{image_bin}:{chosen}")

    def test_no_roots_or_missing_optional_roots_preserve_image_path(self):
        for roots in ([], [self.root / "missing-install"]):
            with self.subTest(roots=roots):
                result = self.discover(roots)
                self.assertEqual(result["tools"], {})
                self.assertEqual(set(result["missing_tools"]), set(tool_env.TOOLS))
                self.assertEqual(result["environment"]["PATH"], str(self.image_bin))
                self.assertEqual(result["environment"]["TOOL_PATHS"], "")

    def test_empty_path_components_are_ignored_and_duplicates_removed(self):
        value = f":{self.image_bin}::{self.image_bin}:"
        self.assertEqual(tool_env.split_paths(value), [self.image_bin])


if __name__ == "__main__":
    unittest.main()
