import unittest
from datetime import date

from timeline_protocol import (
    day_request, detail_request, download_request, parse_day_response,
    parse_detail_response, parse_download_done,
)


class TimelineProtocolTest(unittest.TestCase):
    def test_requests_match_apk_fields(self):
        self.assertEqual(day_request("user", "app-1")["body"], {})
        self.assertEqual(
            detail_request("user", date(2026, 9, 24), 2, "app-2"),
            {
                "head": {"reply": True, "req_id": "app-2", "origin": "app", "user_id": "user", "cmd": "list_record_detail"},
                "body": {"s_sec": "2026/09/24 00:00:00", "e_sec": "2026/09/24 23:59:59", "curPage": 2, "pageSize": 20},
            },
        )

    def test_page_validation(self):
        response = {"head": {"cmd": "list_record_detail"}, "code": 0,
                    "body": {"curPage": 1, "totalPage": 2,
                             "video_list": [{"video_id": 7, "s_sec": "start", "e_sec": "end", "json_url": "metadata.json"}]}}
        self.assertEqual(parse_detail_response(response, 1)[1], 2)
        with self.assertRaises(ValueError):
            parse_detail_response(response, 2)
        response["body"]["video_list"] = []
        with self.assertRaises(ValueError):
            parse_detail_response(response, 1)

    def test_day_response(self):
        response = {"head": {"cmd": "list_record_day"}, "body": {"date": ["2026/09/24"]}}
        self.assertEqual(parse_day_response(response), ["2026-09-24"])
        response["body"]["date"] = [1]
        with self.assertRaises(ValueError):
            parse_day_response(response)

    def test_device_download_path(self):
        self.assertEqual(
            download_request("user", "2026-09-24 10:00:00", "2026-09-24 10:05:00", "app-3"),
            {
                "head": {"reply": True, "req_id": "app-3", "origin": "app", "user_id": "user", "cmd": "req_download"},
                "body": {"type": "playback", "s_sec": "2026-09-24 10:00:00", "e_sec": "2026-09-24 10:05:00", "video_id": 2, "img_url": ""},
            },
        )
        response = {"head": {"cmd": "req_download_done"}, "body": {"status": True, "mp4url": "https://example.test/clip.mp4"}}
        self.assertEqual(parse_download_done(response), "https://example.test/clip.mp4")
        response["body"]["status"] = False
        with self.assertRaises(ValueError):
            parse_download_done(response)


if __name__ == "__main__":
    unittest.main()
