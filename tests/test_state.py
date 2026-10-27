import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from macdesk import state

QEMU_IMG = os.environ.get("MACDESK_TEST_QEMU_IMG") or shutil.which("qemu-img")


@unittest.skipUnless(QEMU_IMG, "set MACDESK_TEST_QEMU_IMG to exercise real overlays")
class GenerationTests(unittest.TestCase):
    def test_reset_replaces_root_and_aux_together_and_retains_previous_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base.raw"
            base.write_bytes(b"immutable base" + bytes(4096))
            aux = root / "aux.img"
            aux.write_bytes(b"paired auxiliary state")
            base.chmod(0o400)
            aux.chmod(0o400)
            base_hash = state.sha256(base)
            image = {
                "disk": str(base),
                "format": "raw",
                "aux": str(aux),
                "dependencies": [{"path": str(base), "sha256": base_hash}],
            }
            instance = root / "instance"
            instance.mkdir()
            first = state.new_generation(instance, image, QEMU_IMG)
            (first / "aux.img").write_bytes(b"working auxiliary changes")
            second = state.new_generation(instance, image, QEMU_IMG)
            active = state.load(instance / "active.json")
            self.assertEqual(active["generation"], second.name)
            self.assertEqual(active["previous"], first.name)
            self.assertFalse(active["enrolled"])
            self.assertEqual((second / "aux.img").read_bytes(), aux.read_bytes())
            self.assertEqual((first / "aux.img").read_bytes(), b"working auxiliary changes")
            subprocess.run(
                [
                    QEMU_IMG,
                    "compare",
                    "-f",
                    "qcow2",
                    "-F",
                    "raw",
                    str(second / "root.qcow2"),
                    str(base),
                ],
                check=True,
                capture_output=True,
            )
            self.assertEqual(state.sha256(base), base_hash)
            old_pointer = (instance / "active.json").read_bytes()
            generations = set(instance.iterdir())
            with self.assertRaises(subprocess.CalledProcessError):
                state.new_generation(instance, image, "/bin/false")
            self.assertEqual((instance / "active.json").read_bytes(), old_pointer)
            self.assertEqual(set(instance.iterdir()), generations)


class StateTests(unittest.TestCase):
    def test_names_cannot_escape_storage_scope(self):
        for name in ("../other", "", "x/y", ".", "a" * 49):
            with self.subTest(name=name), self.assertRaises(ValueError):
                state.name(name)

    def test_atomic_replace_does_not_leave_staging_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state.save(path, {"generation": "first"})
            state.save(path, {"generation": "second"})
            self.assertEqual(state.load(path), {"generation": "second"})
            self.assertEqual(list(path.parent.iterdir()), [path])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
