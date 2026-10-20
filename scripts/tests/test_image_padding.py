"""Exercise the image padding recipe with input reads shorter than its block size."""

from pathlib import Path
import os
import re
import subprocess
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[2]
BLOCK_SIZE = 1024 * 1024


class ImagePaddingTests(unittest.TestCase):
    def pad_stream(self, payload: bytes) -> bytes:
        source = (ROOT / "include/image.mk").read_text()
        macro = re.search(r"^define Image/pad-to\n.*?^endef$", source, re.M | re.S)
        self.assertIsNotNone(macro)
        makefile = macro.group(0) + (
            "\n.PHONY: pad\npad:\n"
            f"\t$(call Image/pad-to,$(PAD_INPUT),{BLOCK_SIZE})\n"
        )
        with tempfile.TemporaryDirectory(prefix="lede-image-padding-") as directory:
            path = Path(directory) / "image"
            os.mkfifo(path)
            failures = []

            def write_input():
                try:
                    with path.open("wb", buffering=0) as output:
                        for offset in range(0, len(payload), 8192):
                            output.write(payload[offset:offset + 8192])
                except OSError as exc:
                    failures.append(exc)

            writer = threading.Thread(target=write_input, daemon=True)
            process = subprocess.Popen(
                ["make", "--no-print-directory", "-f", "-", f"PAD_INPUT={path}", "pad"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            writer.start()
            try:
                stdout, stderr = process.communicate(makefile, timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                self.fail("image padding did not consume the input stream")
            writer.join(timeout=5)
            self.assertFalse(writer.is_alive())
            self.assertFalse(failures, failures)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            return path.read_bytes()

    def test_short_reads_are_combined_before_padding_the_last_block(self):
        payload = bytes(range(256)) * (3 * BLOCK_SIZE // 2 // 256)
        expected = payload + bytes(2 * BLOCK_SIZE - len(payload))
        self.assertEqual(self.pad_stream(payload), expected)

    def test_aligned_input_has_no_inserted_padding(self):
        payload = bytes(range(256)) * (2 * BLOCK_SIZE // 256)
        self.assertEqual(self.pad_stream(payload), payload)


if __name__ == "__main__":
    unittest.main()
