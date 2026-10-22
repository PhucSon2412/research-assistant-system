"""Synthetic checks for the demo's web-only rollout; never connect to Docker."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("m9", Path(__file__).resolve().parents[1] / "m9_lan_demo.py")
m9 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m9)


class WebImageTest(unittest.TestCase):
    def test_rollout_preserves_env_and_recreates_only_web(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "pilot").mkdir()
            original = b"RAS_API_IMAGE=ras-pilot-api:rc2\nRAS_WEB_IMAGE=ras-pilot-web:rc2\nOPAQUE_VALUE=synthetic-unchanged\n"
            (root / "pilot/pilot.env").write_bytes(original)
            state = {"root": str(root)}
            m9.save_state(root, state)
            calls = []

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                if cmd[:3] == ["docker", "image", "inspect"]:
                    return SimpleNamespace(stdout="sha256:synthetic", returncode=0)
                if cmd[-2:] == ["ps", "-aq"]:
                    return SimpleNamespace(stdout="synthetic-container", returncode=0)
                if cmd[:2] == ["docker", "inspect"]:
                    labels = {m9.LABEL: m9.PROJECT, "com.docker.compose.project": m9.PROJECT}
                    return SimpleNamespace(stdout=json.dumps([{"Config": {"Labels": labels}}]), returncode=0)
                return SimpleNamespace(stdout="", returncode=0)

            with patch.object(m9, "run", side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(m9.web_image(SimpleNamespace(root=root, image="ras-pilot-web:ui1", apply=True)), 0)
            self.assertEqual((root / "pilot/pilot.env").read_bytes(), original.replace(b"ras-pilot-web:rc2", b"ras-pilot-web:ui1"))
            self.assertEqual(m9.load_state(root)["web_image"], "ras-pilot-web:ui1")
            self.assertEqual(calls[-1][-8:], ["up", "-d", "--no-deps", "--wait", "--pull", "never", "--force-recreate", "web"])

    def test_regeneration_keeps_web_choice_but_drill_keeps_rc2(self):
        state = {"root": "synthetic-root", "web_image": "ras-pilot-web:ui1", "host": "synthetic.test", "port": 8443,
                 "lan_ip": "192.168.1.2", "subnet": "172.30.64.0/24", "drill_subnet": "172.30.48.0/24",
                 "drill_ports": [18443, 18444], "passwords": {k: "synthetic" for k in ["pg", "runtime", "recovery", "redis", "minio"]}}
        self.assertEqual(m9.pilot_env(state, "pilot", "synthetic@example.org")["RAS_WEB_IMAGE"], "ras-pilot-web:ui1")
        self.assertEqual(m9.pilot_env(state, "drill", "synthetic@example.org")["RAS_WEB_IMAGE"], "ras-pilot-web:rc2")
        self.assertEqual(m9.pilot_env(state, "pilot", "synthetic@example.org")["RAS_API_IMAGE"], "ras-pilot-api:rc2")

    def test_foreign_or_missing_local_tag_cannot_write_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            m9.save_state(root, {"root": folder})
            before = (root / "state.json").read_bytes()
            with self.assertRaises(SystemExit):
                m9.web_image(SimpleNamespace(root=root, image="unrelated:latest", apply=True))
            with patch.object(m9, "run", side_effect=SystemExit("not found")), self.assertRaises(SystemExit):
                m9.web_image(SimpleNamespace(root=root, image="ras-pilot-web:missing", apply=True))
            self.assertEqual((root / "state.json").read_bytes(), before)

    def test_foreign_container_stops_before_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "pilot").mkdir()
            env = root / "pilot/pilot.env"
            env.write_bytes(b"RAS_WEB_IMAGE=ras-pilot-web:rc2\n")
            m9.save_state(root, {"root": folder})
            replies = [SimpleNamespace(stdout="sha256:synthetic"), SimpleNamespace(stdout="foreign"),
                       SimpleNamespace(stdout=json.dumps([{"Config": {"Labels": {}}}]))]
            with patch.object(m9, "run", side_effect=replies), self.assertRaises(SystemExit):
                m9.web_image(SimpleNamespace(root=root, image="ras-pilot-web:ui1", apply=True))
            self.assertEqual(env.read_bytes(), b"RAS_WEB_IMAGE=ras-pilot-web:rc2\n")
            self.assertNotIn("web_image", m9.load_state(root))


if __name__ == "__main__":
    unittest.main()
