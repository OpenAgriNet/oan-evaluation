#!/usr/bin/env python3
"""
Beckn mock proxy — single entry point for the entire mock_server stack.

  python3 server.py   starts two services:
    • beckn_search_proxy  http://HOST:5689/{search,init,status}
    • vistaar_grievance_mock  http://127.0.0.1:5691/Grievance*

/search with no matching rule → forwarded to real BAP (mandi fallthrough).
/init and /status with no matching rule → empty {"responses":[]} (no BAP call).

Environment (mock_server/.env or shell):
  PROXY_UPSTREAM_BASE   — upstream base URL
  PROXY_CONFIG          — path to config.json (default: beside this file)
  PROXY_HOST / PROXY_PORT — bind address (default 0.0.0.0:5689)
  BPP_URI               — mock on_search bpp_uri fallback
  BECKN_PROXY_LOG_FILE  — optional request log path
  GRIEVANCE_HOST / GRIEVANCE_PORT — grievance mock bind (default 127.0.0.1:5691)
  GRIEVANCE_KEY_1 / GRIEVANCE_KEY_2 — AES-GCM keys for grievance mock

Point bharat-oan-api at this service:
  BAP_ENDPOINT=http://127.0.0.1:5689
  GRIEVANCE_BASE_URL=http://127.0.0.1:5691
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

from engine import apply_rules, substitute_env
from env_loader import load_proxy_env

_DEFAULT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = _DEFAULT_DIR / "config.json"


def _request_log_path() -> Path:
    p = os.environ.get("BECKN_PROXY_LOG_FILE", "").strip()
    return Path(p).expanduser().resolve() if p else (_DEFAULT_DIR / "request_log.txt")


def _shell_single_quoted(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


def _append_request_txt(
    *,
    host_header: str,
    request_path: str,
    raw_in: bytes,
    in_label: str,
    out_label: str,
    out_body: str,
    forwarded_url: str | None = None,
    upstream_status: int | None = None,
    upstream_body: str | None = None,
) -> None:
    path = _request_log_path()
    try:
        raw_txt = raw_in.decode("utf-8", errors="replace")
    except Exception:
        raw_txt = str(raw_in)
    in_url = f"http://{host_header}{request_path}"
    in_curl = (
        f"curl -sS -X POST { _shell_single_quoted(in_url) } "
        f"-H 'Content-Type: application/json' "
        f"-d { _shell_single_quoted(raw_txt) }"
    )
    lines = [
        "",
        "=" * 80,
        datetime.now(timezone.utc).isoformat(),
        in_label,
        in_curl,
        "",
        out_label,
        out_body,
    ]
    if forwarded_url is not None:
        fwd_curl = (
            f"curl -sS -X POST { _shell_single_quoted(forwarded_url) } "
            f"-H 'Content-Type: application/json' "
            f"-d { _shell_single_quoted(raw_txt) }"
        )
        lines.extend(
            [
                "",
                "forwarded (same -d body as [in to proxy] above)",
                fwd_curl,
                "",
                f"upstream HTTP {upstream_status}" if upstream_status is not None else "upstream (connection failed)",
            ]
        )
        if upstream_body:
            lines.extend(["", upstream_body])
    lines.append("")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except OSError:
        pass


def load_config() -> tuple[dict, Path]:
    cfg_path = Path(os.environ.get("PROXY_CONFIG", str(DEFAULT_CONFIG))).resolve()
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    cfg = substitute_env(raw)
    return cfg, cfg_path.parent


class Handler(BaseHTTPRequestHandler):
    _cfg: dict
    _config_dir: Path

    @classmethod
    def set_config(cls, cfg: dict, config_dir: Path) -> None:
        cls._cfg = cfg
        cls._config_dir = config_dir

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))

    def _send_raw(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, obj: dict):
        self._send_raw(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        if self.path in ("/", "/health"):
            up = self._cfg.get("upstream", {})
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": "beckn_search_proxy",
                    "upstream_base": up.get("base_url"),
                    "upstream_search": up.get("base_url", "").rstrip("/") + (up.get("search_path") or "/search"),
                    "rules": [r.get("id") for r in self._cfg.get("rules", [])],
                },
            )
            return
        self.send_error(404)

    _ALLOWED_POST_PATHS = {"/search", "/init", "/status"}

    def do_POST(self):
        path = self.path.rstrip("/") or "/"
        if path not in self._ALLOWED_POST_PATHS:
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length) if length else b"{}"

        host_header = self.headers.get("Host", "127.0.0.1:5689")

        in_label = f"[in to proxy — {path}]"

        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            err = json.dumps({"error": "invalid_json"})
            _append_request_txt(
                host_header=host_header,
                request_path=path,
                raw_in=raw,
                in_label=in_label,
                out_label="[out from proxy]",
                out_body=err,
            )
            self._send_json(400, {"error": "invalid_json"})
            return

        try:
            mocked = apply_rules(body, self._cfg.get("rules", []), self._config_dir)
        except RuntimeError as e:
            err = json.dumps({"error": "rule_engine", "detail": str(e)})
            _append_request_txt(
                host_header=host_header,
                request_path=path,
                raw_in=raw,
                in_label=in_label,
                out_label="[out from proxy]",
                out_body=err,
            )
            self._send_json(500, {"error": "rule_engine", "detail": str(e)})
            return

        if mocked is not None:
            out_txt = json.dumps(mocked, ensure_ascii=False, indent=2)
            _append_request_txt(
                host_header=host_header,
                request_path=path,
                raw_in=raw,
                in_label=in_label,
                out_label="[out from proxy — mock]",
                out_body=out_txt,
            )
            self._send_json(200, mocked)
            return

        # /init and /status are fully mocked — no BAP fallthrough.
        # /search falls through to BAP (covers mandi queries for non-mocked cities).
        if path != "/search":
            empty = {"responses": []}
            out_txt = json.dumps(empty)
            _append_request_txt(
                host_header=host_header,
                request_path=path,
                raw_in=raw,
                in_label=in_label,
                out_label="[out from proxy — no rule matched, empty response]",
                out_body=out_txt,
            )
            self._send_json(200, empty)
            return

        up = self._cfg.get("upstream", {})
        base = (up.get("base_url") or "").rstrip("/")
        url = base + path

        fwd_headers = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in ("host", "content-length", "connection"):
                continue
            fwd_headers[k] = v
        if "Content-Type" not in fwd_headers:
            fwd_headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=raw, method="POST", headers=fwd_headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                resp_body = resp.read()
                ct = resp.headers.get("Content-Type", "application/json")
                up_txt = resp_body.decode("utf-8", errors="replace")
                _append_request_txt(
                    host_header=host_header,
                    request_path=path,
                    raw_in=raw,
                    in_label=in_label,
                    out_label="[out from proxy — upstream relay]",
                    out_body=up_txt,
                    forwarded_url=url,
                    upstream_status=resp.status,
                    upstream_body=None,
                )
                self.send_response(resp.status)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
        except urllib.error.HTTPError as e:
            err_body = e.read()
            up_txt = err_body.decode("utf-8", errors="replace")
            _append_request_txt(
                host_header=host_header,
                request_path=path,
                raw_in=raw,
                in_label=in_label,
                out_label="[out from proxy — upstream relay]",
                out_body=up_txt,
                forwarded_url=url,
                upstream_status=e.code,
                upstream_body=None,
            )
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(err_body)))
            self.end_headers()
            self.wfile.write(err_body)
        except urllib.error.URLError as e:
            err = json.dumps({"error": "upstream_unavailable", "detail": str(e.reason)})
            _append_request_txt(
                host_header=host_header,
                request_path=path,
                raw_in=raw,
                in_label=in_label,
                out_label="[out from proxy — upstream failed]",
                out_body=err,
                forwarded_url=url,
                upstream_status=None,
                upstream_body=str(e.reason),
            )
            self._send_json(502, {"error": "upstream_unavailable", "detail": str(e.reason)})


def _start_grievance_mock() -> None:
    """Load and run vistaar_grievance_mock in a daemon thread."""
    grievance_dir = _DEFAULT_DIR.parent / "vistaar_grievance_mock"
    spec = importlib.util.spec_from_file_location(
        "vistaar_grievance_mock", grievance_dir / "server.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    g_host = os.environ.get("GRIEVANCE_HOST", "127.0.0.1")
    g_port = int(os.environ.get("GRIEVANCE_PORT", "5691"))
    httpd = ThreadingHTTPServer((g_host, g_port), mod.GrievanceHandler)
    print(f"vistaar_grievance_mock  http://{g_host}:{g_port}/{{GrievanceAadhaarToken,LodgeGrievance,GrievanceStatusCheck}}", flush=True)
    httpd.serve_forever()


def _start_sathi_mock() -> None:
    """Load and run vistaar_sathi_mock in a daemon thread."""
    sathi_dir = _DEFAULT_DIR.parent / "vistaar_sathi_mock"
    spec = importlib.util.spec_from_file_location(
        "vistaar_sathi_mock", sathi_dir / "server.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    s_host = os.environ.get("SATHI_HOST", "127.0.0.1")
    s_port = int(os.environ.get("SATHI_PORT", "5692"))
    httpd = ThreadingHTTPServer((s_host, s_port), mod.SATHIHandler)
    print(f"vistaar_sathi_mock      http://{s_host}:{s_port}/{{get-crop-group,get-crops-list,search-dealers}}", flush=True)
    httpd.serve_forever()


def main():
    loaded = load_proxy_env(_DEFAULT_DIR)
    if loaded:
        print("beckn_search_proxy: loaded env from " + ", ".join(str(p) for p in loaded), flush=True)
    cfg, config_dir = load_config()
    Handler.set_config(cfg, config_dir)

    t = threading.Thread(target=_start_grievance_mock, daemon=True, name="grievance-mock")
    t.start()

    t = threading.Thread(target=_start_sathi_mock, daemon=True, name="sathi-mock")
    t.start()

    host = os.environ.get("PROXY_HOST", "127.0.0.1")
    port = int(os.environ.get("PROXY_PORT", "5689"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    up = cfg.get("upstream", {})
    upstream_base = (up.get("base_url") or "").rstrip("/")
    print(f"beckn_search_proxy      http://{host}:{port}/{{search,init,status}}  (/search → {upstream_base} if no rule matches)", flush=True)
    print(f"request log: {_request_log_path()}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
