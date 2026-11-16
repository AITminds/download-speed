"""Measure download speed with ten sequential HTTP requests."""

import argparse
import math
import sys
from datetime import datetime
from http.client import HTTPException
from pathlib import Path
from time import perf_counter
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen


REQUEST_COUNT = 10
CHUNK_SIZE = 64 * 1024
DEFAULT_TIMEOUT = 30.0
BYTES_PER_MB = 1_000_000
DOWNLOADS_DIR = Path(__file__).resolve().parent / "downloads"


def http_url(value: str) -> str:
    """Validate a CLI URL before starting any requests."""
    try:
        parsed = urlsplit(value)
        # Accessing port also validates its format and range.
        parsed.port
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"Invalid URL: {error}") from error
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise argparse.ArgumentTypeError(
            "Use an HTTP(S) URL with a hostname and no whitespace."
        )
    return value


def positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Timeout must be a number.") from error
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError("Timeout must be finite and greater than 0.")
    return timeout


def download_filename(url: str) -> str:
    """Keep the URL's filename while replacing characters unsuitable for a path."""
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    name = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in name
    )
    name = name[:100].strip(".") or "download"
    return name if Path(name).suffix else f"{name}.bin"


def download(url: str, timeout: float, destination: Path) -> tuple[int, float]:
    """Save the complete response and return body bytes and elapsed seconds."""
    request = Request(
        url,
        headers={
            "User-Agent": "download-speed/1.0",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
        },
    )
    downloaded_bytes = 0
    started = perf_counter()
    # Exclusive creation preserves existing files, even if a name collides.
    output = destination.open("xb")
    try:
        with output, urlopen(request, timeout=timeout) as response:
            if response.status is None or not 200 <= response.status < 300:
                raise ValueError(f"Expected a successful HTTP response, got {response.status}.")

            while chunk := response.read(CHUNK_SIZE):
                output.write(chunk)
                downloaded_bytes += len(chunk)

            # Count actual bytes; Content-Length only helps detect a truncated body.
            # Transfer-Encoding takes precedence over Content-Length.
            content_length = response.headers.get("Content-Length")
            if content_length is not None and not response.headers.get("Transfer-Encoding"):
                expected_bytes = int(content_length)
                if downloaded_bytes != expected_bytes:
                    raise OSError(
                        f"Incomplete response: expected {expected_bytes} bytes, "
                        f"received {downloaded_bytes}."
                    )
    except BaseException:
        # Context managers close the file before cleanup, including on Ctrl+C.
        destination.unlink(missing_ok=True)
        raise

    return downloaded_bytes, perf_counter() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Save a URL 10 times sequentially to the project's downloads folder "
            "and measure speed in MB/s."
        )
    )
    parser.add_argument("url", type=http_url, help="HTTP(S) URL of a file to download")
    parser.add_argument(
        "--timeout",
        type=positive_timeout,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help="Socket operation timeout, not a total download deadline (default: 30 s)",
    )
    args = parser.parse_args(argv)

    try:
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        print(f"Cannot create downloads directory: {error}", file=sys.stderr)
        return 1

    run_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    filename = download_filename(args.url)
    print(f"Downloads directory: {DOWNLOADS_DIR}", flush=True)
    total_bytes = 0
    total_seconds = 0.0
    for number in range(1, REQUEST_COUNT + 1):
        destination = DOWNLOADS_DIR / f"{run_id}-{number:02d}-{filename}"
        try:
            downloaded_bytes, elapsed = download(args.url, args.timeout, destination)
        except (OSError, HTTPException, ValueError) as error:
            # HTTPError can own an open response even when urlopen raises.
            if isinstance(error, HTTPError):
                error.close()
            print(f"Error on request {number}/{REQUEST_COUNT}: {error}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print("Measurement interrupted.", file=sys.stderr)
            return 130

        total_bytes += downloaded_bytes
        total_seconds += elapsed
        print(
            f"Request {number}/{REQUEST_COUNT}: {downloaded_bytes} bytes in {elapsed:.3f} s"
            f" -> {destination.name}",
            flush=True,
        )

    average_seconds = total_seconds / REQUEST_COUNT
    speed_mb_per_second = total_bytes / total_seconds / BYTES_PER_MB
    print(f"\nRequests: {REQUEST_COUNT}")
    print(f"Average request time: {average_seconds:.3f} s")
    print(f"Total downloaded: {total_bytes} bytes ({total_bytes / BYTES_PER_MB:.3f} MB)")
    print(f"Total request time: {total_seconds:.3f} s")
    print(f"Download speed: {speed_mb_per_second:.3f} MB/s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
