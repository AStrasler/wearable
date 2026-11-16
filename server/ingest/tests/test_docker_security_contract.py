"""Test the Docker subprocess boundary without running Docker."""
import ast
import importlib.util
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


FIXTURE = Path(__file__).with_name("conftest.py")


def load_fixture(executable):
    fake_pytest = types.ModuleType("pytest")
    fake_pytest.mark = types.SimpleNamespace(skipif=lambda *args, **kwargs: None)
    fake_pytest.fixture = lambda *args, **kwargs: (lambda func: func)
    spec = importlib.util.spec_from_file_location("isolated_docker_conftest", FIXTURE)
    fixture = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"pytest": fake_pytest}):
        with patch("shutil.which", return_value=executable) as which:
            with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
                spec.loader.exec_module(fixture)
    return fixture, which, run


class DockerFixtureSecurityTests(unittest.TestCase):
    def test_docker_is_resolved_once_and_used_as_absolute_path(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "docker.exe"
            binary.write_bytes(b"placeholder")
            fixture, which, run = load_fixture(str(binary))
            which.assert_called_once_with("docker")
            self.assertEqual(fixture._DOCKER, str(binary.resolve()))
            self.assertEqual(run.call_args.args[0], [str(binary.resolve()), "info"])
            self.assertEqual(fixture._docker_argv("port", "safe-name", "5432/tcp"),
                             [str(binary.resolve()), "port", "safe-name", "5432/tcp"])
            self.assertIs(run.call_args.kwargs["shell"], False)

    def test_docker_missing_skips_without_executing_anything(self):
        fixture, which, run = load_fixture(None)
        which.assert_called_once_with("docker")
        run.assert_not_called()
        self.assertFalse(fixture._docker_available())
        with self.assertRaises(FileNotFoundError):
            fixture._docker_argv("run")

    def test_every_subprocess_invocation_uses_resolved_executable(self):
        tree = ast.parse(FIXTURE.read_text(encoding="utf-8"))
        runs = [node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr == "run"]
        self.assertEqual(len(runs), 4)
        for call in runs:
            self.assertIsInstance(call.args[0], ast.Call)
            self.assertEqual(call.args[0].func.id, "_docker_argv")
            self.assertTrue(any(keyword.arg == "shell" and
                                isinstance(keyword.value, ast.Constant) and
                                keyword.value.value is False for keyword in call.keywords))


if __name__ == "__main__":
    unittest.main()
