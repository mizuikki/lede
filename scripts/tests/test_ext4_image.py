"""Check that the built ext4 image generator produces a clean filesystem."""

from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
MKFS = ROOT / "staging_dir/host/bin/make_ext4fs"
FSCK = ROOT / "staging_dir/host/bin/e2fsck"


@unittest.skipUnless(MKFS.is_file() and FSCK.is_file(), "build the ext4 host tools first")
class Ext4ImageTests(unittest.TestCase):
    def test_inode_bitmap_padding_passes_read_only_filesystem_check(self):
        with tempfile.TemporaryDirectory(prefix="lede-ext4-image-") as directory:
            temporary = Path(directory)
            source = temporary / "root"
            source.mkdir()
            (source / "marker").write_bytes(bytes(range(256)) * 128)
            image = temporary / "root.ext4"
            create = subprocess.run(
                [str(MKFS), "-L", "rootfs", "-l", str(64 * 1024 * 1024),
                 "-b", "4096", "-J", "-T", "1", str(image), str(source)],
                capture_output=True, text=True,
            )
            self.assertEqual(create.returncode, 0, create.stdout + create.stderr)
            check = subprocess.run([str(FSCK), "-f", "-n", str(image)],
                                   capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)


if __name__ == "__main__":
    unittest.main()
