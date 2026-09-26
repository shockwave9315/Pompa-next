"""Run the operator smoke script over local HTTP against the actual test application."""

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from conftest import Api
from pompa import report

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "smoke.sh"


@pytest.fixture
def runtime():
    # UTC Jan 14, Warsaw Jan 15: the host's date/timezone must not select the report day.
    period = report.resolve_period("day", date="2027-01-15")
    api = Api(start=period.start + 30 * 60 + 17)
    requests, errors = [], {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            response = api.client.get(self.path)
            code = errors.get(self.path.split("?")[0], response.status_code)
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response.content)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        yield api, f"http://127.0.0.1:{server.server_port}", requests, errors
    finally:
        server.shutdown()
        worker.join(5)
        server.server_close()


@pytest.mark.parametrize("mode", ["default", "url", "json", "json-url"])
def test_smoke_modes_and_warsaw_day_from_status(runtime, mode):
    api, base, requests, _ = runtime
    args = (["--json"] if mode.startswith("json") else [])
    if mode.endswith("url"):
        args.append(base)
    result = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                            env={**os.environ, "POMPA_URL": base}, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    if mode.startswith("json"):
        assert requests == ["/api/v1/status"]
        assert json.loads(result.stdout) == api.body("/api/v1/status")
    else:
        assert "/api/v1/report?period=day&date=2027-01-15" in requests
        assert "report period" in result.stdout
        assert "closed_until=" in result.stdout and "effective_to=" in result.stdout
        for key in ("recorded_minutes", "gap_minutes", "settled_minutes", "unsettled_minutes",
                    "future_minutes", "coverage_percent"):
            assert f"{key}=" in result.stdout


def test_smoke_fails_for_non_200_report(runtime):
    _, base, _, errors = runtime
    errors["/api/v1/report"] = 503
    result = subprocess.run(["bash", str(SCRIPT), base], capture_output=True, text=True, timeout=20)
    assert result.returncode == 1
    assert "FAIL /api/v1/report HTTP 503" in result.stdout
