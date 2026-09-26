"""Export PawzZ storage-card recordings through the app's MQTT download flow.

Protocol details are recovered from Android APK 2.2.0 and need live validation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, Queue
import subprocess
import threading
import time
from urllib.request import urlopen

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from timeline_protocol import (
    day_request, detail_request, download_request, parse_day_response,
    parse_detail_response, parse_download_done,
)


HOST = "iot.papahuoban.cn"
PORT = 1883
INTERVALS = {"1h": 3600, "1d": 86400, "2d": 172800, "3d": 259200}


class AuthenticationError(RuntimeError):
    """Broker explicitly rejected credentials; do not retry without login."""


class DeviceClient:
    def __init__(self, user_id: str, serial: str, token: str):
        self.user_id = user_id
        self.serial = serial
        self.messages: Queue[dict] = Queue()
        self.ready = threading.Event()
        self.failure: str | None = None
        self.auth_rejected = False
        self.topic = f"haojing/pa/app/device/E0/{serial}/send"
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"app-{user_id}",
            protocol=mqtt.MQTTv5,
        )
        self.client.username_pw_set(user_id, token)
        self.client.tls_set()
        self.client.on_connect = self._connected
        self.client.on_subscribe = self._subscribed
        self.client.on_message = self._message
        self.client.on_disconnect = self._disconnected

    def _connected(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.auth_rejected = reason_code.value in (134, 135)
            self.failure = f"MQTT connection rejected: {reason_code}"
            self.ready.set()
            return
        topics = [
            (f"haojing/pa/app/user/{self.user_id}/recv", 1),
            (f"haojing/pa/app/device/E0/{self.serial}/recv", 1),
        ]
        result, _ = client.subscribe(topics)
        if result != mqtt.MQTT_ERR_SUCCESS:
            self.failure = "MQTT subscription could not be sent"
            self.ready.set()

    def _subscribed(self, client, userdata, mid, reason_codes, properties):
        if any(code.is_failure for code in reason_codes):
            self.auth_rejected = any(code.value == 135 for code in reason_codes)
            self.failure = "MQTT subscription rejected"
        self.ready.set()

    def _message(self, client, userdata, message):
        try:
            decoded = json.loads(message.payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if isinstance(decoded, dict):
            self.messages.put(decoded)

    def _disconnected(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.auth_rejected = reason_code.value == 135
            self.failure = f"MQTT disconnected: {reason_code}"
            self.messages.put({})

    def __enter__(self):
        props = Properties(PacketTypes.CONNECT)
        props.UserProperty = [("productId", "pa"), ("userAgent", "android"), ("version", "2.2.0")]
        self.client.connect(HOST, PORT, keepalive=30, clean_start=True, properties=props)
        self.client.loop_start()
        if not self.ready.wait(15):
            self.__exit__(None, None, None)
            raise TimeoutError("MQTT subscription timed out")
        if self.failure:
            self.__exit__(None, None, None)
            if self.auth_rejected:
                raise AuthenticationError("Account login rejected")
            raise RuntimeError(self.failure)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.client.disconnect()
        self.client.loop_stop()

    def request(self, payload: dict, expected: str, timeout: int = 30) -> dict:
        while not self.messages.empty():
            self.messages.get_nowait()
        result = self.client.publish(self.topic, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), qos=1)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError("MQTT publish failed")
        result.wait_for_publish(timeout=10)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.failure:
                if self.auth_rejected:
                    raise AuthenticationError("Account login rejected")
                raise RuntimeError(self.failure)
            try:
                message = self.messages.get(timeout=max(0, min(1, deadline - time.monotonic())))
            except Empty:
                continue
            head = message.get("head")
            if isinstance(head, dict) and head.get("cmd") == expected:
                return message
        raise TimeoutError(f"{expected} timed out")


def output_name(serial: str, record: dict) -> str:
    start, end = str(record["s_sec"]), str(record["e_sec"])
    start_time = datetime.strptime(start.replace("/", "-"), "%Y-%m-%d %H:%M:%S")
    datetime.strptime(end.replace("/", "-"), "%Y-%m-%d %H:%M:%S")
    digest = hashlib.sha256(f"{serial}\0{start}\0{end}".encode()).hexdigest()[:12]
    return f"{start_time:%Y-%m-%d_%H-%M-%S}_{digest}.mp4"


def save_mp4(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".mp4.part")
    try:
        with urlopen(url, timeout=60) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        if temporary.stat().st_size < 1024:
            raise ValueError("downloaded MP4 is too small")
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(temporary)],
            capture_output=True, text=True, timeout=60, check=True,
        )
        if not any(stream.get("codec_type") == "video" for stream in json.loads(probe.stdout).get("streams", [])):
            raise ValueError("downloaded MP4 has no video stream")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def export_once(user_id: str, serial: str, token: str, output_dir: Path, start_date: date | None) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    marker = output_dir / ".initial-scan-complete"
    cutoff = start_date
    if marker.exists():
        last_scan = date.fromisoformat(marker.read_text().strip())
        cutoff = max(cutoff or date.min, last_scan - timedelta(days=1))
    exported = 0
    with DeviceClient(user_id, serial, token) as device:
        days = parse_day_response(device.request(day_request(user_id), "list_record_day"))
        for day_text in sorted(days):
            day = date.fromisoformat(day_text)
            if cutoff and day < cutoff:
                continue
            page = 1
            while True:
                records, total = parse_detail_response(
                    device.request(detail_request(user_id, day, page), "list_record_detail"), page
                )
                for record in records:
                    target = output_dir / day_text / output_name(serial, record)
                    if target.exists() and target.stat().st_size > 1024:
                        continue
                    start, end = str(record["s_sec"]), str(record["e_sec"])
                    done = device.request(download_request(user_id, start, end), "req_download_done", timeout=900)
                    url = parse_download_done(done)
                    save_mp4(url, target)
                    exported += 1
                    print(f"saved {target.name}", flush=True)
                if page >= total:
                    break
                page += 1
    temporary_marker = marker.with_suffix(".tmp")
    temporary_marker.write_text(date.today().isoformat())
    temporary_marker.replace(marker)
    return exported


def read_session(path: Path) -> tuple[str, str]:
    if isinstance(path, int):
        if path < 3:
            raise ValueError('Invalid session descriptor')
        with os.fdopen(path) as pipe:
            raw = pipe.read(8193)
    else:
        if path.stat().st_size > 8192:
            raise ValueError('Session file too large')
        raw = path.read_text()
    if len(raw.encode()) > 8192:
        raise ValueError('Session too large')
    session = json.loads(raw)
    user_id = session.get("user_id") if isinstance(session, dict) else None
    token = session.get("token") if isinstance(session, dict) else None
    if not isinstance(user_id, str) or not user_id or not isinstance(token, str) or not token:
        raise ValueError("session file has no user_id or token")
    return user_id, token


def main() -> None:
    serial = os.environ["PAWZZ_SN"]
    session_file = Path(os.environ["PAWZZ_SESSION_FILE"])
    output_dir = Path(os.environ.get("PAWZZ_OUTPUT_DIR", "/videos"))
    interval = INTERVALS[os.environ.get("PAWZZ_INTERVAL", "1d")]
    start = os.environ.get("PAWZZ_START_DATE")
    start_date = date.fromisoformat(start) if start else None
    while True:
        try:
            user_id, token = read_session(session_file)
            count = export_once(user_id, serial, token, output_dir, start_date)
            print(f"cycle complete: {count} new videos", flush=True)
        except Exception as exc:
            print(f"cycle failed: {type(exc).__name__}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
