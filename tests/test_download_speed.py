import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
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

        if self.path == "/missing" or (
            self.path == "/fail-after-two" and len(self.server.paths) > 2
        ):
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


class IsolatedDownloadsTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.project_root = Path(temporary_directory.name) / "project"
        self.project_root.mkdir()
        self.downloads_dir = self.project_root / "downloads"
        directory_patch = patch("download_speed.DOWNLOADS_DIR", self.downloads_dir)
        directory_patch.start()
        self.addCleanup(directory_patch.stop)


class DownloadTests(IsolatedDownloadsTests):
    def test_cli_downloads_ten_complete_responses_sequentially(self):
        script = self.project_root / "download_speed.py"
        shutil.copyfile(PROJECT_ROOT / "download_speed.py", script)
        with local_server() as (server, base_url):
            result = subprocess.run(
                [sys.executable, str(script), f"{base_url}/payload"],
                # The output directory must follow the script, not the working directory.
                cwd=self.project_root.parent,
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
            self.assertIn(f"Downloads directory: {self.downloads_dir}", result.stdout)
            saved_files = sorted(self.downloads_dir.iterdir())
            self.assertEqual(len(saved_files), 10)
            for number, saved_file in enumerate(saved_files, 1):
                self.assertTrue(saved_file.name.endswith(f"-{number:02d}-payload.bin"))
                self.assertEqual(saved_file.read_bytes(), PAYLOAD)
            self.assertFalse((self.project_root.parent / "downloads").exists())
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
        self.downloads_dir.mkdir()
        for path in ("/chunked", "/no-length"):
            with self.subTest(path=path), local_server() as (_, base_url):
                destination = self.downloads_dir / f"{path[1:]}.bin"
                downloaded, elapsed = download_speed.download(base_url + path, 2, destination)
                self.assertEqual(downloaded, len(PAYLOAD))
                self.assertGreater(elapsed, 0)
                self.assertEqual(destination.read_bytes(), PAYLOAD)

    def test_follows_redirect_and_reads_final_body(self):
        self.downloads_dir.mkdir()
        destination = self.downloads_dir / "redirect.bin"
        with local_server() as (server, base_url):
            downloaded, _ = download_speed.download(f"{base_url}/redirect", 2, destination)
            self.assertEqual(downloaded, len(PAYLOAD))
            self.assertEqual(server.paths, ["/redirect", "/payload"])
            self.assertEqual(destination.read_bytes(), PAYLOAD)

    def test_stops_on_http_error_without_printing_summary(self):
        with local_server() as (server, base_url):
            status, output, errors = invoke_main([f"{base_url}/missing"])
            self.assertEqual(status, 1)
            self.assertIn("404", errors)
            self.assertIn("request 1/10", errors)
            self.assertNotIn("Download speed:", output)
            self.assertEqual(server.paths, ["/missing"])
            self.assertEqual(list(self.downloads_dir.iterdir()), [])

    def test_rejects_truncated_and_unfinished_chunked_bodies(self):
        for path in ("/truncated", "/broken-chunked"):
            with self.subTest(path=path), local_server() as (server, base_url):
                status, output, errors = invoke_main([base_url + path])
                self.assertEqual(status, 1)
                self.assertIn("request 1/10", errors)
                self.assertNotIn("Download speed:", output)
                self.assertEqual(server.paths, [path])
                self.assertEqual(list(self.downloads_dir.iterdir()), [])

    def test_timeout_applies_while_reading_body(self):
        with local_server() as (server, base_url):
            status, output, errors = invoke_main(
                [f"{base_url}/timeout", "--timeout", "0.03"]
            )
            self.assertEqual(status, 1)
            self.assertIn("timed out", errors.lower())
            self.assertNotIn("Download speed:", output)
            self.assertEqual(server.paths, ["/timeout"])
            self.assertEqual(list(self.downloads_dir.iterdir()), [])

    def test_rerun_preserves_previous_files_and_original_extension(self):
        with local_server() as (_, base_url):
            arguments = [f"{base_url}/image.jpg"]
            self.assertEqual(invoke_main(arguments)[0], 0)
            previous_files = set(self.downloads_dir.iterdir())
            self.assertEqual(len(previous_files), 10)
            self.assertEqual(invoke_main(arguments)[0], 0)
        all_files = set(self.downloads_dir.iterdir())
        self.assertEqual(len(all_files), 20)
        self.assertTrue(previous_files.issubset(all_files))
        for saved_file in all_files:
            self.assertTrue(saved_file.name.endswith("-image.jpg"))
            self.assertEqual(saved_file.read_bytes(), PAYLOAD)

    def test_error_keeps_completed_files_without_reporting_partial_result(self):
        with local_server() as (server, base_url):
            status, output, errors = invoke_main([f"{base_url}/fail-after-two"])
            self.assertEqual(server.paths, ["/fail-after-two"] * 3)
        self.assertEqual(status, 1)
        self.assertIn("request 3/10", errors)
        self.assertNotIn("Download speed:", output)
        saved_files = list(self.downloads_dir.iterdir())
        self.assertEqual(len(saved_files), 2)
        for saved_file in saved_files:
            self.assertEqual(saved_file.read_bytes(), PAYLOAD)


class CalculationAndArgumentTests(IsolatedDownloadsTests):
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
        self.assertFalse(self.downloads_dir.exists())

    def test_custom_timeout_is_used_for_every_request(self):
        with patch("download_speed.download", return_value=(10, 0.1)) as download:
            status, _, _ = invoke_main(["https://example.com/file", "--timeout", "2.5"])
        self.assertEqual(status, 0)
        self.assertEqual(download.call_count, 10)
        for call in download.call_args_list:
            self.assertEqual(call.args[:2], ("https://example.com/file", 2.5))
            self.assertEqual(call.args[2].parent, self.downloads_dir)

    def test_interruption_removes_partial_file_and_returns_130(self):
        with patch("download_speed.urlopen") as open_url:
            response = open_url.return_value.__enter__.return_value
            response.status = 200
            response.read.side_effect = [b"partial body", KeyboardInterrupt]
            status, output, errors = invoke_main(["https://example.com/file"])
        self.assertEqual(status, 130)
        self.assertIn("interrupted", errors)
        self.assertNotIn("Download speed:", output)
        self.assertEqual(list(self.downloads_dir.iterdir()), [])

    def test_existing_destination_is_not_overwritten_or_deleted(self):
        destination = self.project_root / "existing.bin"
        destination.write_bytes(b"previous download")
        with patch("download_speed.urlopen") as open_url:
            with self.assertRaises(FileExistsError):
                download_speed.download("https://example.com/file", 2, destination)
            open_url.assert_not_called()
        self.assertEqual(destination.read_bytes(), b"previous download")

    def test_directory_creation_error_is_reported_before_downloading(self):
        self.downloads_dir.write_text("existing file", encoding="utf-8")
        with patch("download_speed.download") as download:
            status, output, errors = invoke_main(["https://example.com/file"])
            download.assert_not_called()
        self.assertEqual(status, 1)
        self.assertIn("Cannot create downloads directory", errors)
        self.assertNotIn("Download speed:", output)
        self.assertEqual(self.downloads_dir.read_text(encoding="utf-8"), "existing file")


if __name__ == "__main__":
    unittest.main()
