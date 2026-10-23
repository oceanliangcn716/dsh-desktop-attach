#!/usr/bin/env python3
"""DSH Desktop Attach - let a phone browser drive the running Desktop Host.

Why this exists
---------------
The DeepSeek Harness Desktop app serves its Web GUI from a host that is bound to
loopback only and authenticates every browser with a per-process launch token.
There is no supported way to make a second host share that live process, so a
separate `dsh --profile web` instance (the old 3080 entry) is a *different* host
with different tools and its own session writer locks.

This helper does not start another DSH host. It discovers the Desktop app's
loopback listener, forwards HTTP + WebSocket traffic to it, and supplies the
authority-bound browser cookie that the Desktop Host expects, minted from the
local DSH browser-session signing secret (`~/.dsh/.credentials.yaml`). The
phone therefore acts as a second browser client of the *same* host: the same
live session, the same profile, the same browser/desktop MCP tools.

This is an unsupported local workaround, not a product feature. It can break
when DSH changes its auth format or port discovery. It is intentionally gated
behind a random secret URL and binds only a concrete LAN address, never a
wildcard address.

Never print cookies, the gate secret, or credential file contents.
"""

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
GATE_PATH = BASE_DIR / "gate.secret"
LOG_DIR = BASE_DIR / "logs"

GATE_COOKIE = "dsh-attach-gate"
GATE_MAX_AGE_SECONDS = 365 * 24 * 3600
COOKIE_MAX_AGE_MS = 30 * 24 * 3600 * 1000
COOKIE_REFRESH_MS = 24 * 3600 * 1000
DISCOVERY_TTL_SECONDS = 4.0
CONNECT_TIMEOUT_SECONDS = 10.0
HEAD_TIMEOUT_SECONDS = 30.0
RESPONSE_HEAD_TIMEOUT_SECONDS = 120.0
MAX_HEAD_BYTES = 256 * 1024
MAX_BODY_BYTES = 128 * 1024 * 1024
STREAM_LIMIT = 1024 * 1024

DEFAULT_CONFIG = {"bind_host": "auto", "port": 3081, "mobile_ui": True}

_RUNTIME_CONFIG = {}

# Resources the web app serves without authentication upstream (static assets).
# The phone browser fetches some of them (manifest, favicon) without credentials,
# so they must not be stopped by this proxy's gate.
PUBLIC_PREFIXES = ("/assets/", "/plugins/", "/plugins?")
PUBLIC_EXACT = {
    "/manifest.webmanifest",
    "/favicon.svg",
    "/favicon-dark.svg",
    "/favicon.ico",
    "/apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png",
}


def is_public_path(path):
    return path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES)


# --------------------------------------------------------------------------
# Phone adaptation layer. Assets are local and injected only into HTML.
# Keep frontend changes separate from the authentication/transport implementation.
# --------------------------------------------------------------------------

HOME_ASSETS = {
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/apple-touch-icon-precomposed.png": ("apple-touch-icon.png", "image/png"),
    "/dsh-mobile-icon-192.png": ("icon-192.png", "image/png"),
    "/dsh-mobile-icon-512.png": ("icon-512.png", "image/png"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
}
HOME_SCREEN_HEAD = b'''<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="DSH">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png?v=20261020">
'''

MOBILE_PATCH_MARKER = b'data-dsh-attach-mobile="2"'
MOBILE_PATCH_STYLE = '<style data-dsh-attach-mobile="2">' + (BASE_DIR / "mobile.css").read_text(encoding="utf-8") + '</style>'
MOBILE_PATCH_SCRIPT = '<script data-dsh-attach-mobile="2">' + (BASE_DIR / "mobile.js").read_text(encoding="utf-8") + '</script>'


def mobile_ui_enabled():
    return bool(_RUNTIME_CONFIG.get("mobile_ui", True))


def apply_mobile_patch(html):
    if not html or MOBILE_PATCH_MARKER in html:
        return html
    if b"</head>" not in html:
        return html
    patched = html.replace(b"</head>", HOME_SCREEN_HEAD + MOBILE_PATCH_STYLE.encode("utf-8") + b"</head>", 1)
    if b"</body>" in patched:
        patched = patched.replace(
            b"</body>", MOBILE_PATCH_SCRIPT.encode("utf-8") + b"</body>", 1
        )
    else:
        patched += MOBILE_PATCH_SCRIPT.encode("utf-8")
    return patched


DEBUG = os.environ.get("DSH_ATTACH_DEBUG") == "1"


def debug(message):
    if DEBUG:
        log("debug: " + message)


def log(message):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    print(f"{stamp} {message}", flush=True)


def b64url_encode(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def dsh_home():
    return Path(os.environ.get("DSH_HOME") or (Path.home() / ".dsh"))


# --------------------------------------------------------------------------
# config / gate / secret / discovery
# --------------------------------------------------------------------------

def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            config.update(data)
    except FileNotFoundError:
        pass
    except Exception as error:  # noqa: BLE001 - config is local and small
        raise SystemExit(f"attach: cannot read config.json: {error}")
    if not isinstance(config.get("port"), int) or not (1 <= config["port"] <= 65535):
        raise SystemExit("attach: config.json port must be 1..65535")
    if not isinstance(config.get("bind_host"), str) or not config["bind_host"]:
        raise SystemExit("attach: config.json bind_host must be a string")
    return config


def load_gate():
    try:
        gate = GATE_PATH.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        gate = ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", gate):
        gate = b64url_encode(secrets.token_bytes(32))
        GATE_PATH.write_text(gate + "\n", encoding="ascii")
        try:
            os.chmod(GATE_PATH, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        log("created a new gate secret")
    return gate


_SECRET_CACHE = {"mtime": None, "value": None}


def load_signing_secret():
    path = dsh_home() / ".credentials.yaml"
    try:
        st = path.stat()
    except FileNotFoundError:
        raise RuntimeError("DSH credential file not found; start the Desktop app first")
    if _SECRET_CACHE["mtime"] == st.st_mtime_ns and _SECRET_CACHE["value"] is not None:
        return _SECRET_CACHE["value"]

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    secret_text = None
    for index, line in enumerate(lines):
        if line.strip() == "client-connection/browser-session:":
            base_indent = len(line) - len(line.lstrip(" "))
            for follow in lines[index + 1: index + 50]:
                if follow.strip() and (len(follow) - len(follow.lstrip(" "))) <= base_indent:
                    break
                match = re.match(r"\s+secret:\s*([A-Za-z0-9_-]+)\s*$", follow)
                if match:
                    secret_text = match.group(1)
                    break
            if secret_text:
                break
    if not secret_text:
        raise RuntimeError("browser-session signing secret not found in DSH credentials")

    raw = b64url_decode(secret_text)
    if len(raw) != 32:
        raise RuntimeError("browser-session signing secret has an unexpected length")
    _SECRET_CACHE["mtime"] = st.st_mtime_ns
    _SECRET_CACHE["value"] = raw
    return raw


_PORT_CACHE = {"value": None, "at": 0.0}


def _run(command):
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=8
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _desktop_profile_dir():
    return str((dsh_home() / "profiles" / "desktop").resolve())


def discover_desktop_port(force=False):
    now = time.monotonic()
    cached = _PORT_CACHE["value"]
    if not force and cached is not None and now - _PORT_CACHE["at"] < DISCOVERY_TTL_SECONDS:
        return cached

    listening = _run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"])
    candidates = []
    for line in listening.splitlines()[1:]:
        match = re.search(r"\bTCP 127\.0\.0\.1:(\d+) \(LISTEN\)$", line)
        fields = line.split()
        if match and len(fields) >= 2:
            candidates.append((fields[1], int(match.group(1))))

    wanted = _desktop_profile_dir()
    found = None
    for pid, port in candidates:
        cwd_output = _run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"])
        for line in cwd_output.splitlines():
            if not line.startswith("n"):
                continue
            try:
                actual = str(Path(line[1:]).resolve())
            except OSError:
                continue
            if actual == wanted:
                found = port
                break
        if found is not None:
            break

    if found is None:
        if cached is not None:
            log("desktop host listener disappeared")
    elif found != cached:
        log("desktop host listener found on 127.0.0.1:%d" % found)
    _PORT_CACHE["value"] = found
    _PORT_CACHE["at"] = now
    return found


_COOKIE_CACHE = {"port": None, "slot": None, "value": None}


def mint_browser_cookie(port, secret):
    authority = f"127.0.0.1:{port}"
    now = int(time.time() * 1000)
    slot = now // COOKIE_REFRESH_MS
    if _COOKIE_CACHE["port"] == port and _COOKIE_CACHE["slot"] == slot:
        return _COOKIE_CACHE["value"]
    payload = {
        "version": 1,
        "authority": authority,
        "issuedAt": now,
        "expiresAt": now + COOKIE_MAX_AGE_MS,
    }
    body = b64url_encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    signature = hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest()
    value = "v1.%s.%s" % (body, b64url_encode(signature))
    name = "dsh-auth-" + b64url_encode(hashlib.sha256(authority.encode()).digest())
    cookie = name + "=" + value
    _COOKIE_CACHE.update({"port": port, "slot": slot, "value": cookie})
    return cookie


# --------------------------------------------------------------------------
# small HTTP parsing helpers
# --------------------------------------------------------------------------

def parse_request_head(raw):
    lines = raw.split(b"\r\n")
    if not lines:
        return None
    try:
        request_line = lines[0].decode("latin-1")
    except UnicodeDecodeError:
        return None
    parts = request_line.split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        return None
    method, target, version = parts
    headers = []
    for line in lines[1:]:
        if not line:
            break
        if b":" not in line:
            return None
        name, _, value = line.partition(b":")
        headers.append((name.decode("latin-1"), value.strip().decode("latin-1")))
    return method.upper(), target, version, headers


def header_get(headers, wanted):
    wanted = wanted.lower()
    for name, value in headers:
        if name.lower() == wanted:
            return value
    return None


def header_all(headers, wanted):
    wanted = wanted.lower()
    return [value for name, value in headers if name.lower() == wanted]


def cookie_has(cookie_header, name, expected):
    if not cookie_header:
        return False
    for segment in cookie_header.split(";"):
        key, sep, value = segment.strip().partition("=")
        if sep and key == name:
            return hmac.compare_digest(value.strip(), expected)
    return False


def origin_form(target):
    parsed = urlsplit(target)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    return path


async def read_chunked_body(reader):
    body = bytearray()
    while True:
        line = await reader.readuntil(b"\r\n")
        size_text = line.split(b";", 1)[0].strip()
        size = int(size_text, 16)
        if size == 0:
            while True:
                trailer = await reader.readuntil(b"\r\n")
                if trailer == b"\r\n":
                    return bytes(body)
        if len(body) + size > MAX_BODY_BYTES:
            raise ValueError("request body too large")
        body += await reader.readexactly(size)
        await reader.readexactly(2)


async def read_request_body(reader, headers):
    transfer_encoding = header_get(headers, "transfer-encoding")
    if transfer_encoding and "chunked" in transfer_encoding.lower():
        return await read_chunked_body(reader)
    content_length = header_get(headers, "content-length")
    if content_length is None:
        return b""
    try:
        length = int(content_length)
    except ValueError:
        raise ValueError("invalid content-length")
    if length < 0 or length > MAX_BODY_BYTES:
        raise ValueError("request body too large")
    return await reader.readexactly(length) if length else b""


async def write_simple(writer, status, reason, text, extra_headers=()):
    body = text.encode("utf-8")
    lines = [
        f"HTTP/1.1 {status} {reason}",
        "Content-Type: text/plain; charset=utf-8",
        f"Content-Length: {len(body)}",
        "Cache-Control: no-store",
        "Connection: close",
    ]
    lines.extend(extra_headers)
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body)
    try:
        await writer.drain()
    except (ConnectionError, OSError):
        pass


async def write_pairing_page(writer, head=False):
    body = (BASE_DIR / "pairing.html").read_bytes()
    header = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Referrer-Policy: no-referrer\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "Content-Security-Policy: default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    writer.write(header + (b"" if head else body))
    await writer.drain()


async def relay_chunked(reader, writer):
    total = 0
    while True:
        line = await reader.readuntil(b"\r\n")
        writer.write(line)
        await writer.drain()
        raw_size = line.split(b";", 1)[0].strip()
        try:
            size = int(raw_size, 16)
        except ValueError:
            return total
        if size == 0:
            while True:
                trailer = await reader.readuntil(b"\r\n")
                writer.write(trailer)
                await writer.drain()
                if trailer == b"\r\n":
                    return total
        remaining = size + 2
        while remaining > 0:
            chunk = await reader.read(min(65536, remaining))
            if not chunk:
                return total
            writer.write(chunk)
            await writer.drain()
            remaining -= len(chunk)
            total += len(chunk)


async def relay_fixed(reader, writer, length):
    remaining = length
    while remaining > 0:
        chunk = await reader.read(min(65536, remaining))
        if not chunk:
            return
        writer.write(chunk)
        await writer.drain()
        remaining -= len(chunk)


async def relay_until_eof(reader, writer):
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            return
        writer.write(chunk)
        await writer.drain()


# --------------------------------------------------------------------------
# request handling
# --------------------------------------------------------------------------

def upstream_headers(original, port, cookie, websocket, path=""):
    authority = f"127.0.0.1:{port}"
    document_request = (
        path == "/" or (header_get(original, "sec-fetch-dest") or "") == "document"
    )
    forwarded = []
    for name, value in original:
        lowered = name.lower()
        if lowered in {
            "host",
            "cookie",
            "origin",
            "referer",
            "connection",
            "keep-alive",
            "proxy-connection",
            "expect",
            "upgrade",
        }:
            continue
        if websocket and not lowered.startswith("sec-websocket-"):
            continue
        # Keep the index HTML uncompressed so the phone patch can be injected.
        if lowered == "accept-encoding" and document_request and mobile_ui_enabled():
            continue
        forwarded.append((name, value))
    forwarded.insert(0, ("Host", authority))
    forwarded.append(("Cookie", cookie))
    origin = header_get(original, "origin")
    if origin is not None:
        forwarded.append(("Origin", "http://" + authority))
    if websocket:
        forwarded.append(("Connection", "Upgrade"))
        forwarded.append(("Upgrade", "websocket"))
    else:
        forwarded.append(("Connection", "close"))
    return forwarded


async def handle_request(raw_head, reader, writer, gate):
    parsed = parse_request_head(raw_head)
    if parsed is None:
        await write_simple(writer, 400, "Bad Request", "attach: malformed request\n")
        return
    method, target, _version, headers = parsed

    # --- gate -----------------------------------------------------------
    try:
        parsed_target = urlsplit(target)
    except ValueError:
        await write_simple(writer, 400, "Bad Request", "attach: malformed target\n")
        return
    # Home-screen assets contain no session data and must also load without cookies.
    asset = HOME_ASSETS.get(parsed_target.path)
    if method in ("GET", "HEAD") and asset:
        filename, content_type = asset
        try:
            payload = (BASE_DIR / "home-icons" / filename).read_bytes()
        except OSError:
            await write_simple(writer, 404, "Not Found", "icon not found\n")
            return
        response = (
            "HTTP/1.1 200 OK\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Cache-Control: public, max-age=3600\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(response + (payload if method == "GET" else b""))
        await writer.drain()
        return

    path_only = origin_form(target)
    query_pairs = parse_qsl(parsed_target.query, keep_blank_values=True)
    gate_values = [value for key, value in query_pairs if key == "k"]
    if gate_values:
        if (
            method in ("GET", "HEAD")
            and len(gate_values) == 1
            and hmac.compare_digest(gate_values[0], gate)
        ):
            clean_pairs = [(key, value) for key, value in query_pairs if key != "k"]
            location = parsed_target.path or "/"
            if clean_pairs:
                location += "?" + urlencode(clean_pairs)
            cookie = (
                f"{GATE_COOKIE}={gate}; Max-Age={GATE_MAX_AGE_SECONDS}; "
                "Path=/; HttpOnly; SameSite=Lax"
            )
            await write_simple(
                writer,
                303,
                "See Other",
                "",
                extra_headers=[f"Location: {location}", f"Set-Cookie: {cookie}"],
            )
            return
        if method in ("GET", "HEAD") and parsed_target.path == "/":
            await write_pairing_page(writer, head=(method == "HEAD"))
        else:
            await write_simple(writer, 401, "Unauthorized", "attach: unauthorized\n")
        return
    if not cookie_has(header_get(headers, "cookie"), GATE_COOKIE, gate):
        if is_public_path(path_only):
            pass
        elif method in ("GET", "HEAD") and parsed_target.path == "/":
            await write_pairing_page(writer, head=(method == "HEAD"))
            return
        else:
            await write_simple(writer, 401, "Unauthorized", "attach: unauthorized\n")
            return

    # --- discovery ------------------------------------------------------
    try:
        port = discover_desktop_port()
    except Exception as error:  # noqa: BLE001
        log("discovery failed: %s" % type(error).__name__)
        port = None
    if port is None:
        await write_simple(
            writer,
            503,
            "Service Unavailable",
            "attach: DeepSeek Harness Desktop Host is not running\n",
        )
        return
    try:
        secret = load_signing_secret()
    except RuntimeError as error:
        log("credential error: %s" % error)
        await write_simple(
            writer,
            503,
            "Service Unavailable",
            "attach: local browser-session credential is unavailable\n",
        )
        return
    cookie = mint_browser_cookie(port, secret)

    websocket = (
        (header_get(headers, "upgrade") or "").lower() == "websocket"
        and "upgrade" in (header_get(headers, "connection") or "").lower()
    )

    if (header_get(headers, "expect") or "").lower() == "100-continue" and not websocket:
        writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            return

    try:
        body = b"" if websocket else await read_request_body(reader, headers)
    except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as error:
        log("bad request body: %s" % type(error).__name__)
        await write_simple(writer, 400, "Bad Request", "attach: invalid request body\n")
        return

    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port, limit=STREAM_LIMIT),
            CONNECT_TIMEOUT_SECONDS,
        )
    except (OSError, asyncio.TimeoutError):
        discover_desktop_port(force=True)
        await write_simple(writer, 502, "Bad Gateway", "attach: cannot reach Desktop Host\n")
        return

    path = origin_form(target)
    forwarded = upstream_headers(headers, port, cookie, websocket, path)
    request_head = f"{method} {path} HTTP/1.1\r\n"
    request_head += "".join(f"{name}: {value}\r\n" for name, value in forwarded)
    request_head += "\r\n"

    try:
        upstream_writer.write(request_head.encode("latin-1") + body)
        await upstream_writer.drain()
        response_head_raw = await asyncio.wait_for(
            upstream_reader.readuntil(b"\r\n\r\n"), RESPONSE_HEAD_TIMEOUT_SECONDS
        )
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError,
            asyncio.LimitOverrunError):
        _close(upstream_writer)
        discover_desktop_port(force=True)
        await write_simple(writer, 502, "Bad Gateway", "attach: no response from Desktop Host\n")
        return

    if websocket and response_head_raw.startswith(b"HTTP/1.1 101"):
        writer.write(response_head_raw)
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            _close(upstream_writer)
            return
        log("websocket attached")
        await _pipe_both(reader, upstream_reader, writer, upstream_writer)
        log("websocket closed")
        return

    await _relay_plain_response(
        method, response_head_raw, upstream_reader, writer, port, path
    )
    _close(upstream_writer)


def _rebuild_response_head(response_head_raw, port, content_length=None, drop_headers=()):
    lines = response_head_raw.split(b"\r\n")
    if not lines:
        return b""
    status_line = lines[0]
    skip = {"connection", "keep-alive", "set-cookie", "alt-svc"}
    skip.update(name.lower() for name in drop_headers)
    if content_length is not None:
        skip.update({"content-length", "transfer-encoding"})
    kept = []
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(b":")
        lowered = name.strip().lower().decode("latin-1")
        if lowered in skip:
            continue
        kept.append(line)
    authority = f"127.0.0.1:{port}"
    rewritten = []
    for line in kept:
        name, _, value = line.partition(b":")
        lowered = name.strip().lower().decode("latin-1")
        if lowered == "location":
            location = value.strip().decode("latin-1")
            for prefix in ("http://" + authority, "https://" + authority):
                if location.startswith(prefix):
                    location = location[len(prefix):] or "/"
                    break
            line = name + b":" + b" " + location.encode("latin-1")
        rewritten.append(line)
    if content_length is not None:
        rewritten.append(b"Content-Length: %d" % content_length)
    rewritten.append(b"Connection: close")
    return status_line + b"\r\n" + b"\r\n".join(rewritten) + b"\r\n\r\n"


async def _read_full_body(reader, headers, limit=2 * 1024 * 1024):
    transfer_encoding = header_get(headers, "transfer-encoding")
    content_length = header_get(headers, "content-length")
    if transfer_encoding and "chunked" in transfer_encoding.lower():
        chunks = []
        total = 0
        while True:
            line = await reader.readuntil(b"\r\n")
            size = int(line.split(b";", 1)[0].strip(), 16)
            if size == 0:
                while True:
                    trailer = await reader.readuntil(b"\r\n")
                    if trailer == b"\r\n":
                        break
                break
            total += size
            if total > limit:
                raise ValueError("response body too large")
            chunks.append(await reader.readexactly(size))
            await reader.readexactly(2)
        return b"".join(chunks)
    if content_length is not None:
        length = int(content_length)
        if length < 0 or length > limit:
            raise ValueError("response body too large")
        return await reader.readexactly(length)
    buffered = bytearray()
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            break
        buffered += chunk
        if len(buffered) > limit:
            raise ValueError("response body too large")
    return bytes(buffered)


async def _relay_plain_response(method, response_head_raw, upstream_reader, writer, port,
                                path=""):
    status_line = response_head_raw.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    try:
        status = int(status_line.split(" ")[1])
    except (IndexError, ValueError):
        status = 0

    headers = []
    for line in response_head_raw.split(b"\r\n")[1:]:
        if not line:
            break
        name, _, value = line.partition(b":")
        headers.append((name.decode("latin-1"), value.strip().decode("latin-1")))

    content_type = (header_get(headers, "content-type") or "").lower()
    content_encoding = (header_get(headers, "content-encoding") or "").lower()
    if (
        mobile_ui_enabled()
        and method == "GET"
        and status == 200
        and "text/html" in content_type
        and not content_encoding
    ):
        try:
            html_body = await _read_full_body(upstream_reader, headers)
        except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                ConnectionError, OSError):
            await write_simple(writer, 502, "Bad Gateway",
                               "attach: cannot read the page from Desktop Host\n")
            return
        patched = apply_mobile_patch(html_body)
        # The HTML carries the phone shell: always refresh it after an update.
        rebuilt = _rebuild_response_head(
            response_head_raw, port, content_length=len(patched),
            drop_headers=("cache-control", "etag", "last-modified"),
        )
        rebuilt = rebuilt.replace(b"Connection: close\r\n", b"Cache-Control: no-store\r\nConnection: close\r\n")
        writer.write(rebuilt)
        try:
            await writer.drain()
            if patched:
                writer.write(patched)
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        return

    writer.write(_rebuild_response_head(response_head_raw, port))
    try:
        await writer.drain()
    except (ConnectionError, OSError):
        return

    if method == "HEAD" or status in (204, 304) or status == 101:
        return

    transfer_encoding = header_get(headers, "transfer-encoding")
    content_length = header_get(headers, "content-length")
    try:
        if transfer_encoding and "chunked" in transfer_encoding.lower():
            await relay_chunked(upstream_reader, writer)
        elif content_length is not None:
            await relay_fixed(upstream_reader, writer, int(content_length))
        else:
            await relay_until_eof(upstream_reader, writer)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
            ConnectionError, OSError, ValueError):
        pass


async def _pipe(src, dst, label):
    try:
        while True:
            chunk = await src.read(65536)
            if not chunk:
                debug("pipe %s eof" % label)
                break
            dst.write(chunk)
            await dst.drain()
    except asyncio.CancelledError:
        debug("pipe %s cancelled" % label)
        raise
    except (ConnectionError, OSError) as error:
        debug("pipe %s error: %s" % (label, type(error).__name__))
    finally:
        try:
            dst.close()
        except (ConnectionError, OSError):
            pass


async def _pipe_both(client_reader, upstream_reader, client_writer, upstream_writer):
    client_to_upstream = asyncio.ensure_future(
        _pipe(client_reader, upstream_writer, "client->host")
    )
    upstream_to_client = asyncio.ensure_future(
        _pipe(upstream_reader, client_writer, "host->client")
    )
    try:
        done, pending = await asyncio.wait(
            {client_to_upstream, upstream_to_client},
            return_when=asyncio.FIRST_COMPLETED,
        )
        debug("pipe finished: %s" % ", ".join(t.get_name() for t in done))
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    finally:
        for task in (client_to_upstream, upstream_to_client):
            if not task.done():
                task.cancel()


def _close(writer):
    try:
        writer.close()
    except (ConnectionError, OSError):
        pass


async def handle_connection(reader, writer, gate):
    try:
        raw_head = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_SECONDS
        )
    except (asyncio.IncompleteReadError, asyncio.TimeoutError,
            asyncio.LimitOverrunError, ConnectionError, OSError):
        _close(writer)
        return
    if len(raw_head) > MAX_HEAD_BYTES:
        await write_simple(writer, 431, "Request Header Fields Too Large",
                           "attach: request header too large\n")
        _close(writer)
        return
    try:
        await handle_request(raw_head, reader, writer, gate)
    except Exception as error:  # noqa: BLE001 - never leak request details
        log("request failed: %s" % type(error).__name__)
        try:
            await write_simple(writer, 500, "Internal Server Error",
                               "attach: internal error\n")
        except Exception:  # noqa: BLE001
            pass
    finally:
        _close(writer)


# --------------------------------------------------------------------------
# bind address detection + service
# --------------------------------------------------------------------------

def detect_lan_ip():
    output = _run(["/sbin/ifconfig"])
    blocks = re.split(r"\n(?=\S)", output)
    for wanted in ("en0:", "en1:", "en2:"):
        for block in blocks:
            if not block.startswith(wanted):
                continue
            match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", block)
            if match and not match.group(1).startswith("169.254."):
                return match.group(1)
    for block in blocks:
        if block.startswith(("lo0:", "utun", "bridge", "gif", "stf")):
            continue
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", block)
        if match and not match.group(1).startswith(("127.", "169.254.")):
            return match.group(1)
    return None


def resolve_bind_host(config):
    bind_host = config["bind_host"]
    if bind_host != "auto":
        return bind_host
    detected = detect_lan_ip()
    if detected is None:
        raise RuntimeError("cannot detect a LAN IPv4 address")
    return detected


async def serve(config, gate):
    while True:
        try:
            bind_host = resolve_bind_host(config)
            port = config["port"]
            server = await asyncio.start_server(
                lambda reader, writer: handle_connection(reader, writer, gate),
                host=bind_host,
                port=port,
                limit=STREAM_LIMIT,
                reuse_address=True,
            )
            break
        except (OSError, RuntimeError) as error:
            log("cannot bind listener: %s" % type(error).__name__)
            await asyncio.sleep(10)
    sockets = ", ".join(str(sock.getsockname()[:2]) for sock in server.sockets)
    log("listening on %s" % sockets)
    async with server:
        await server.serve_forever()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def phone_url(config, gate):
    bind_host = config["bind_host"]
    if bind_host == "auto":
        try:
            bind_host = detect_lan_ip() or "<this-mac-lan-ip>"
        except Exception:  # noqa: BLE001
            bind_host = "<this-mac-lan-ip>"
    return f"http://{bind_host}:{config['port']}/?k={gate}"


def command_url(config, gate, copy):
    url = phone_url(config, gate)
    if copy:
        try:
            subprocess.run(["/usr/bin/pbcopy"], input=url.encode(), check=True,
                           timeout=5)
            print("已复制本次授权访问地址到剪贴板；在 iPhone Safari 中粘贴打开。")
        except (OSError, subprocess.SubprocessError):
            raise SystemExit("复制到剪贴板失败，请重新运行 DSH手机连接。")
    else:
        print(url)


def command_status(config, gate):
    print(json.dumps({
        "config": config,
        "gate_configured": bool(gate),
        "desktop_host_port": discover_desktop_port(force=True),
        "cookie_ready": _COOKIE_CACHE["value"] is not None,
    }, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description="DSH Desktop Attach")
    parser.add_argument("command", nargs="?", default="serve",
                        choices=["serve", "url", "status"])
    parser.add_argument("--copy", action="store_true",
                        help="copy the phone URL to the macOS clipboard")
    args = parser.parse_args(argv)

    config = load_config()
    _RUNTIME_CONFIG.update(config)
    if args.command == "serve":
        gate = load_gate()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            asyncio.run(serve(config, gate))
        except KeyboardInterrupt:
            pass
        return 0
    gate = load_gate()
    if args.command == "url":
        command_url(config, gate, args.copy)
    else:
        command_status(config, gate)
    return 0


if __name__ == "__main__":
    sys.exit(main())
