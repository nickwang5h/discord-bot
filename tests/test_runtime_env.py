import tempfile
import unittest
from pathlib import Path

from core.runtime_env import RuntimeEnvError, load_runtime_env_file


class RuntimeEnvironmentTests(unittest.TestCase):
    def test_private_env_does_not_override_process_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); root.chmod(0o700)
            path=root/"runtime.env"
            path.write_text("GROQ_API_KEY=file-key\nBOT_TIMEZONE='America/Toronto'\n",encoding="utf-8")
            path.chmod(0o600)
            environment={"GROQ_API_KEY":"process-key"}
            loaded=load_runtime_env_file(path,environ=environment)
        self.assertEqual(environment["GROQ_API_KEY"],"process-key")
        self.assertEqual(environment["BOT_TIMEZONE"],"America/Toronto")
        self.assertEqual(loaded,("GROQ_API_KEY","BOT_TIMEZONE"))

    def test_unsafe_file_and_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); root.chmod(0o700)
            path=root/"runtime.env"
            path.write_text("GROQ_API_KEY=secret\n",encoding="utf-8")
            path.chmod(0o644)
            with self.assertRaises(RuntimeEnvError):
                load_runtime_env_file(path,environ={})
            path.chmod(0o600)
            link=root/"linked.env"; link.symlink_to(path)
            with self.assertRaises(RuntimeEnvError):
                load_runtime_env_file(link,environ={})

if __name__=="__main__": unittest.main()
