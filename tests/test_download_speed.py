import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import download_speed


PAYLOAD = b"0123456789abcdef" * 12_345
BODY_DELAY = 0.02
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        with self.server.event_lock:
            self.server.paths.append(self.path)
            self.server.events.append("start")
            self.server.request_headers.append(dict(self.headers))

        if self.path == "/missing":
            self.send_error(404, "File not found")
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/payload")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(200)
        if self.path in ("/chunked", "/broken-chunked"):
            self.send_header("Transfer-Encoding", "chunked")
            # Transfer-Encoding must override Content-Length for framing.
            self.send_header("Content-Length", "1")
        elif self.path != "/no-length":
            self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()

        if self.path == "/timeout":
            time.sleep(0.15)
            return
        if self.path == "/truncated":
            self.wfile.write(PAYLOAD[:13])
            return
        if self.path in ("/chunked", "/broken-chunked"):
            for offset in range(0, len(PAYLOAD), 10_000):
                chunk = PAYLOAD[offset:offset + 10_000]
                self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
            if self.path == "/chunked":
                self.wfile.write(b"0\r\n\r\n")
            return

        # Send headers and part of the body immediately, then delay the rest.
        # A client measuring only headers or starting requests in parallel fails.
        self.wfile.write(PAYLOAD[:100])
        self.wfile.flush()
        time.sleep(BODY_DELAY)
        with self.server.event_lock:
            self.server.events.append("end")
            self.wfile.write(PAYLOAD[100:])


@contextlib.contextmanager
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    server.paths = []
    server.events = []
    server.request_headers = []
    server.event_lock = threading.Lock()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        with patch.dict(os.environ, {"no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}):
            yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def invoke_main(arguments):
    output, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        status = download_speed.main(arguments)
    return status, output.getvalue(), errors.getvalue()


class DownloadTests(unittest.TestCase):
    def test_cli_downloads_ten_complete_responses_sequentially(self):
        with local_server() as (server, base_url):
            result = subprocess.run(
                [sys.executable, "download_speed.py", f"{base_url}/payload"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(server.paths, ["/payload"] * 10)
            self.assertEqual(server.events, ["start", "end"] * 10)
            self.assertIn(f"Total downloaded: {len(PAYLOAD) * 10} bytes", result.stdout)
            self.assertIn("Requests: 10", result.stdout)
            self.assertIn("Download speed:", result.stdout)
            timings = [
                float(line.split(" in ")[1].split()[0])
                for line in result.stdout.splitlines()
                if line.startswith("Request ")
            ]
            self.assertEqual(len(timings), 10)
            self.assertTrue(all(elapsed >= BODY_DELAY * 0.9 for elapsed in timings))
            for headers in server.request_headers:
                self.assertEqual(headers["Accept-Encoding"], "identity")

    def test_counts_actual_bytes_for_chunked_and_unframed_bodies(self):
        for path in ("/chunked", "/no-length"):
            with self.subTest(path=path), local_server() as (_, base_url):
                downloaded, elapsed = download_speed.download(base_url + path, 2)
                self.assertEqual(downloaded, len(PAYLOAD))
                self.assertGreater(elapsed, 0)

    def test_follows_redirect_and_reads_final_body(self):
        with local_server() as (server, base_url):
            downloaded, _ = download_speed.download(f"{base_url}/redirect", 2)
            self.assertEqual(downloaded, len(PAYLOAD))
            self.assertEqual(server.paths, ["/redirect", "/payload"])

    def test_stops_on_http_error_without_printing_summary(self):
        with local_server() as (server, base_url):
            status, output, errors = invoke_main([f"{base_url}/missing"])
            self.assertEqual(status, 1)
            self.assertIn("404", errors)
            self.assertIn("request 1/10", errors)
            self.assertNotIn("Download speed:", output)
            self.assertEqual(server.paths, ["/missing"])

    def test_rejects_truncated_and_unfinished_chunked_bodies(self):
        for path in ("/truncated", "/broken-chunked"):
            with self.subTest(path=path), local_server() as (server, base_url):
                status, output, errors = invoke_main([base_url + path])
                self.assertEqual(status, 1)
                self.assertIn("request 1/10", errors)
                self.assertNotIn("Download speed:", output)
                self.assertEqual(server.paths, [path])

    def test_timeout_applies_while_reading_body(self):
        with local_server() as (server, base_url):
            status, output, errors = invoke_main(
                [f"{base_url}/timeout", "--timeout", "0.03"]
            )
            self.assertEqual(status, 1)
            self.assertIn("timed out", errors.lower())
            self.assertNotIn("Download speed:", output)
            self.assertEqual(server.paths, ["/timeout"])


class CalculationAndArgumentTests(unittest.TestCase):
    def test_speed_uses_total_bytes_divided_by_total_duration(self):
        measurements = [(1_000_000, 1.0)] + [(3_000_000, 2.0)] * 9
        with patch("download_speed.download", side_effect=measurements) as download:
            status, output, errors = invoke_main(["https://example.com/file"])
        self.assertEqual(status, 0)
        self.assertEqual(errors, "")
        self.assertEqual(download.call_count, 10)
        self.assertIn("Average request time: 1.900 s", output)
        self.assertIn("Total downloaded: 28000000 bytes (28.000 MB)", output)
        self.assertIn("Total request time: 19.000 s", output)
        self.assertIn("Download speed: 1.474 MB/s", output)

    def test_error_after_successful_requests_does_not_report_partial_result(self):
        measurements = [(1000, 1.0), (2000, 2.0), OSError("Connection lost")]
        with patch("download_speed.download", side_effect=measurements) as download:
            status, output, errors = invoke_main(["https://example.com/file"])
        self.assertEqual(status, 1)
        self.assertEqual(download.call_count, 3)
        self.assertIn("request 3/10", errors)
        self.assertNotIn("Download speed:", output)

    def test_rejects_invalid_arguments_before_network_access(self):
        invalid_arguments = [
            [],
            ["example.com/file"],
            ["ftp://example.com/file"],
            ["file:///tmp/file"],
            ["https:///file"],
            ["https://example.com:abc/file"],
            ["https://example.com:99999/file"],
            ["https://example.com/a b"],
            ["https://example.com/\nfile"],
            ["https://[broken/file"],
        ]
        invalid_arguments.extend(
            ["https://example.com/file", "--timeout", timeout]
            for timeout in ("0", "-1", "nan", "inf", "nope")
        )
        with patch("download_speed.download") as download:
            for arguments in invalid_arguments:
                with self.subTest(arguments=arguments), self.assertRaises(SystemExit) as error:
                    invoke_main(arguments)
                self.assertEqual(error.exception.code, 2)
            download.assert_not_called()

    def test_custom_timeout_is_used_for_every_request(self):
        with patch("download_speed.download", return_value=(10, 0.1)) as download:
            status, _, _ = invoke_main(["https://example.com/file", "--timeout", "2.5"])
        self.assertEqual(status, 0)
        self.assertEqual(download.call_count, 10)
        for call in download.call_args_list:
            self.assertEqual(call.args, ("https://example.com/file", 2.5))

    def test_interruption_returns_130_without_summary(self):
        with patch("download_speed.download", side_effect=KeyboardInterrupt):
            status, output, errors = invoke_main(["https://example.com/file"])
        self.assertEqual(status, 130)
        self.assertIn("interrupted", errors)
        self.assertNotIn("Download speed:", output)


if __name__ == "__main__":
    unittest.main()
