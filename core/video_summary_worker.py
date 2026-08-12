from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
from http import HTTPStatus
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


LOGGER = logging.getLogger("video-summary-worker")
SUCCESS_SCHEMA = "discord_video_summary_worker_v1"
ERROR_SCHEMA = "discord_video_summary_worker_error_v1"
CLI_SUCCESS_SCHEMA = "content_enrichment_cli_summary_v1"
CLI_ERROR_SCHEMA = "content_enrichment_cli_error_v1"
ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
MAX_REQUEST_BYTES = 4096
MAX_CAPTURE_BYTES = 64 * 1024
MAX_MARKDOWN_BYTES = 128 * 1024
MAX_MARKDOWN_CHARS = 32_000
_JOB_SLOT = threading.BoundedSemaphore(1)


class WorkerError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _bounded_float(
    value: str | None, *, default: float, minimum: float, maximum: float
) -> float:
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError as error:
        raise WorkerError("worker_config_invalid") from error
    if not minimum <= parsed <= maximum:
        raise WorkerError("worker_config_invalid")
    return parsed


def _bounded_port(value: str | None, *, default: int) -> int:
    try:
        parsed = default if value is None else int(value)
    except ValueError as error:
        raise WorkerError("worker_config_invalid") from error
    if not 1 <= parsed <= 65535:
        raise WorkerError("worker_config_invalid")
    return parsed


def _resolve_executable(value: str | None) -> str:
    candidate = value or "/usr/local/bin/info-curator"
    if not candidate or len(candidate) > 4096:
        raise WorkerError("worker_config_invalid")
    if os.sep in candidate:
        path = Path(candidate)
        try:
            metadata = path.stat()
        except OSError as error:
            raise WorkerError("worker_unavailable") from error
        if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
            raise WorkerError("worker_unavailable")
        return str(path)
    resolved = shutil.which(candidate)
    if resolved is None:
        raise WorkerError("worker_unavailable")
    return resolved


def _last_json_line(raw: bytes) -> object | None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    for line in reversed(text.splitlines()[-10:]):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _cli_error(stderr: bytes) -> str:
    value = _last_json_line(stderr)
    if not isinstance(value, dict) or value.get("schema_version") != CLI_ERROR_SCHEMA:
        return "worker_subprocess_failed"
    code = value.get("error_code")
    if not isinstance(code, str) or ERROR_CODE_RE.fullmatch(code) is None:
        return "worker_subprocess_failed"
    allowed = {
        "unsupported_media_url",
        "invalid_media_url",
        "media_not_available",
        "media_contract_mismatch",
        "transcript_invalid",
        "summary_attempt_exhausted",
        "provider_unavailable",
        "provider_timeout",
        "provider_invalid_json",
        "summary_invalid",
        "summary_invalid_citation",
        "invalid_provider_config",
        "invalid_runtime_env",
        "curator_daily_error",
        "internal_error",
    }
    return code if code in allowed else "worker_subprocess_failed"


def _read_markdown(path: Path) -> str:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > MAX_MARKDOWN_BYTES
        ):
            raise WorkerError("worker_contract_mismatch")
        markdown = path.read_text(encoding="utf-8")
    except WorkerError:
        raise
    except (OSError, UnicodeDecodeError) as error:
        raise WorkerError("worker_contract_mismatch") from error
    if (
        not 1 <= len(markdown) <= MAX_MARKDOWN_CHARS
        or any(ord(character) < 32 and character not in "\n\r\t" for character in markdown)
    ):
        raise WorkerError("worker_contract_mismatch")
    return markdown


def run_info_curator(
    url: str,
    *,
    executable: str | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, object]:
    if not isinstance(url, str) or not 1 <= len(url) <= 2048:
        raise WorkerError("invalid_media_url")
    command = _resolve_executable(executable or os.getenv("INFO_CURATOR_CLI"))
    timeout = (
        timeout_seconds
        if timeout_seconds is not None
        else _bounded_float(
            os.getenv("VIDEO_SUMMARY_JOB_TIMEOUT_SECONDS"),
            default=190.0,
            minimum=30.0,
            maximum=300.0,
        )
    )
    if not 30.0 <= timeout <= 300.0:
        raise WorkerError("worker_config_invalid")
    with tempfile.TemporaryDirectory(prefix="video-summary-") as directory:
        work_root = Path(directory)
        work_root.chmod(0o700)
        output = work_root / "summary.md"
        try:
            process = subprocess.Popen(
                [command, "summarize-video", url, "--output", str(output)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                env=os.environ.copy(),
                start_new_session=True,
            )
        except OSError as error:
            raise WorkerError("worker_unavailable") from error
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()
            raise WorkerError("worker_timeout") from error
        if len(stdout) > MAX_CAPTURE_BYTES or len(stderr) > MAX_CAPTURE_BYTES:
            raise WorkerError("worker_contract_mismatch")
        if process.returncode != 0:
            raise WorkerError(_cli_error(stderr))
        value = _last_json_line(stdout)
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "status",
            "reused",
            "media_reused",
            "provider",
            "model",
            "output",
        }:
            raise WorkerError("worker_contract_mismatch")
        provider = value.get("provider")
        model = value.get("model")
        if (
            value.get("schema_version") != CLI_SUCCESS_SCHEMA
            or value.get("status") != "complete"
            or value.get("output") != str(output)
            or not isinstance(value.get("reused"), bool)
            or not isinstance(value.get("media_reused"), bool)
            or not isinstance(provider, str)
            or not 1 <= len(provider) <= 80
            or any(ord(character) < 32 for character in provider)
            or not isinstance(model, str)
            or not 1 <= len(model) <= 160
            or any(ord(character) < 32 for character in model)
        ):
            raise WorkerError("worker_contract_mismatch")
        return {
            "schema_version": SUCCESS_SCHEMA,
            "status": "complete",
            "markdown": _read_markdown(output),
            "provider": provider,
            "model": model,
            "reused": value["reused"],
            "media_reused": value["media_reused"],
        }


class VideoSummaryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class VideoSummaryHandler(BaseHTTPRequestHandler):
    server_version = "VideoSummaryWorker/1"
    sys_version = ""

    def log_message(self, format_string: str, *args: object) -> None:
        LOGGER.info("request: " + format_string, *args)

    def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, status: HTTPStatus, code: str) -> None:
        self._send_json(
            status,
            {"schema_version": ERROR_SCHEMA, "error_code": code},
        )

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/healthz":
            self._error(HTTPStatus.NOT_FOUND, "not_found")
            return
        self._send_json(
            HTTPStatus.OK,
            {"schema_version": "discord_video_summary_worker_health_v1", "status": "ok"},
        )

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path != "/v1/video-summary":
            self._error(HTTPStatus.NOT_FOUND, "not_found")
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            content_length = -1
        if content_type != "application/json" or not 1 <= content_length <= MAX_REQUEST_BYTES:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request")
            return
        raw = self.rfile.read(content_length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request")
            return
        if not isinstance(value, dict) or set(value) != {"url"} or not isinstance(value["url"], str):
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request")
            return
        if not _JOB_SLOT.acquire(blocking=False):
            self._error(HTTPStatus.CONFLICT, "worker_busy")
            return
        try:
            result = run_info_curator(value["url"])
        except WorkerError as error:
            status = (
                HTTPStatus.UNPROCESSABLE_ENTITY
                if error.code not in {"worker_unavailable", "worker_timeout"}
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            self._error(status, error.code)
        except Exception:
            LOGGER.exception("unexpected video summary worker failure")
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "worker_internal_error")
        else:
            self._send_json(HTTPStatus.OK, result)
        finally:
            _JOB_SLOT.release()


def create_server(host: str, port: int) -> VideoSummaryHTTPServer:
    if host not in {"0.0.0.0", "127.0.0.1"}:
        raise WorkerError("worker_config_invalid")
    return VideoSummaryHTTPServer((host, port), VideoSummaryHandler)


def healthcheck(port: int) -> int:
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        raw = response.read(4097)
        status = response.status
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return 1
    finally:
        connection.close()
    return 0 if (
        status == 200
        and len(raw) <= 4096
        and value == {
            "schema_version": "discord_video_summary_worker_health_v1",
            "status": "ok",
        }
    ) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="video-summary-worker")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args(argv)
    try:
        port = _bounded_port(os.getenv("VIDEO_SUMMARY_WORKER_PORT"), default=8080)
        if args.healthcheck:
            return healthcheck(port)
        host = os.getenv("VIDEO_SUMMARY_WORKER_HOST", "0.0.0.0")
        logging.basicConfig(
            level=os.getenv("LOG_LEVEL", "INFO").upper(),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        server = create_server(host, port)
    except WorkerError as error:
        LOGGER.error("video summary worker configuration failed: %s", error.code)
        return 2
    LOGGER.info("video summary worker listening on %s:%d", host, port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
