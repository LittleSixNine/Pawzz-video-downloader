import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from synology_exporter import export_once, read_session


class FakeDevice:
    downloads = 0
    day = date.today().strftime("%Y/%m/%d")

    def __init__(self, *_):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def request(self, payload, expected, timeout=30):
        command = payload["head"]["cmd"]
        if command == "list_record_day":
            return {"head": {"cmd": expected}, "body": {"date": [self.day]}}
        if command == "list_record_detail":
            return {"head": {"cmd": expected}, "body": {"curPage": 1, "totalPage": 1, "video_list": [
                {"video_id": 9, "s_sec": f"{self.day} 10:00:00", "e_sec": f"{self.day} 10:05:00"}
            ]}}
        FakeDevice.downloads += 1
        return {"head": {"cmd": expected}, "body": {"status": True, "mp4url": "https://example.test/clip.mp4"}}


class SynologyExporterTest(unittest.TestCase):
    def test_reads_private_login_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            path.write_text('{"user_id":"user","token":"secret"}')
            self.assertEqual(read_session(path), ("user", "secret"))

    def test_completed_file_is_not_requested_again(self):
        FakeDevice.downloads = 0
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch("synology_exporter.DeviceClient", FakeDevice), patch(
                "synology_exporter.save_mp4", lambda url, path: path.parent.mkdir(exist_ok=True) or path.write_bytes(b"x" * 2048)
            ):
                self.assertEqual(export_once("user", "serial", "token", output, date.today()), 1)
                self.assertEqual(export_once("user", "serial", "token", output, date.today()), 0)
            self.assertEqual(FakeDevice.downloads, 1)
            self.assertEqual(next(output.rglob("*.mp4")).parent, output / date.today().isoformat())
            self.assertEqual((output / ".initial-scan-complete").read_text(), date.today().isoformat())


if __name__ == "__main__":
    unittest.main()
