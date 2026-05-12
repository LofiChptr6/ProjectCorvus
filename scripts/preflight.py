#!/usr/bin/env python3
"""Preflight check for a fresh ProjectCorvus install.

Verifies each external dependency the desk needs to start. Designed to be
run with the repo's .venv interpreter:

    .venv/bin/python scripts/preflight.py

Exit codes: 0 on full pass (errors=0), 1 if any required check fails.
Warnings don't fail the run but are surfaced in the summary.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------- pretty printing ---------------------------------------------------

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"


def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


_USE_COLOR = _supports_color()


def _c(text: str, color: str) -> str:
    return f"{color}{text}{RESET}" if _USE_COLOR else text


class Result:
    def __init__(self, name: str) -> None:
        self.name = name
        self.status = "pending"  # pass | fail | warn | skip
        self.message = ""

    def pass_(self, msg: str = "") -> "Result":
        self.status = "pass"; self.message = msg; return self

    def fail(self, msg: str) -> "Result":
        self.status = "fail"; self.message = msg; return self

    def warn(self, msg: str) -> "Result":
        self.status = "warn"; self.message = msg; return self

    def skip(self, msg: str) -> "Result":
        self.status = "skip"; self.message = msg; return self

    def print(self) -> None:
        tag = {
            "pass": _c("PASS", GREEN),
            "fail": _c("FAIL", RED),
            "warn": _c("WARN", YELLOW),
            "skip": _c("SKIP", DIM),
        }[self.status]
        line = f"  [{tag}] {self.name}"
        if self.message:
            line += f"  {_c('— ' + self.message, DIM)}"
        print(line)


RESULTS: list[Result] = []


def check(name: str):
    """Decorator-style: create a Result, register it, return it for the body to mutate."""
    r = Result(name)
    RESULTS.append(r)
    return r


# ---------- .env / config loaders --------------------------------------------

def load_dotenv() -> dict[str, str]:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'").strip('"')
    return out


def load_yaml_config() -> dict:
    try:
        import yaml  # type: ignore
    except ImportError:
        return {}
    cfg_path = REPO_ROOT / "config.yaml"
    if not cfg_path.exists():
        return {}
    with cfg_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------- individual checks ------------------------------------------------

def check_python_version() -> None:
    r = check("Python version (>= 3.12)")
    v = sys.version_info
    if v >= (3, 12):
        r.pass_(f"{v.major}.{v.minor}.{v.micro}")
    else:
        r.fail(f"got {v.major}.{v.minor}, need 3.12+")


def check_venv() -> None:
    r = check("Main venv (.venv) populated")
    py = REPO_ROOT / ".venv" / "bin" / "python"
    if not py.exists():
        r.fail("missing .venv — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt")
        return
    # Probe a few key imports.
    probe = subprocess.run(
        [str(py), "-c", "import asyncpg, yaml, anthropic, httpx, ib_async"],
        capture_output=True, text=True,
    )
    if probe.returncode == 0:
        r.pass_(str(py))
    else:
        r.fail(f"venv present but missing packages: {probe.stderr.strip().splitlines()[-1] if probe.stderr else 'unknown'}")


def check_dotenv(env: dict[str, str]) -> None:
    r = check(".env present + required keys")
    if not env:
        r.fail("no .env at repo root — copy .env.example and fill in")
        return
    required = ["TELEGRAM_BOT_TOKEN", "MASSIVE_API_KEY"]
    missing = [k for k in required if not env.get(k) or env[k].startswith("123456789:ABC")]
    if missing:
        r.fail(f"missing/placeholder: {', '.join(missing)}")
    else:
        r.pass_(f"{len(env)} keys loaded")


def check_config_yaml(cfg: dict) -> None:
    r = check("config.yaml present")
    if not cfg:
        r.fail("missing or empty — copy config.example.yaml and fill in")
        return
    pw = (cfg.get("postgres") or {}).get("password", "")
    if pw == "CHANGE_ME":
        r.warn("postgres.password is still 'CHANGE_ME' (PG_PASSWORD env var may override)")
        return
    r.pass_()


def check_claude_cli() -> None:
    r = check("claude CLI on PATH")
    path = shutil.which("claude")
    if not path:
        r.fail("not found — install Claude Code: https://docs.anthropic.com/en/docs/claude-code/quickstart")
        return
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
        r.pass_(f"{path} ({out.stdout.strip() or out.stderr.strip()})")
    except subprocess.TimeoutExpired:
        r.warn(f"{path} (--version timed out)")


def check_postgres(cfg: dict, env: dict[str, str]) -> None:
    r = check("Postgres reachable + can SELECT")
    pg_cfg = cfg.get("postgres", {})
    host = env.get("PG_HOST") or pg_cfg.get("host", "localhost")
    port = int(env.get("PG_PORT") or pg_cfg.get("port", 5432))
    db = env.get("PG_DATABASE") or pg_cfg.get("database", "trading")
    user = env.get("PG_USER") or pg_cfg.get("user", "trading")
    pw = env.get("PG_PASSWORD") or pg_cfg.get("password", "")

    try:
        import asyncpg  # type: ignore
    except ImportError:
        r.fail("asyncpg not installed in current interpreter")
        return

    async def _probe() -> tuple[bool, str]:
        try:
            conn = await asyncpg.connect(host=host, port=port, database=db, user=user, password=pw, timeout=5)
            try:
                v = await conn.fetchval("SELECT version()")
            finally:
                await conn.close()
            return True, str(v).split(",", 1)[0]
        except Exception as exc:  # noqa: BLE001
            return False, repr(exc)

    ok, msg = asyncio.run(_probe())
    if ok:
        r.pass_(f"{user}@{host}:{port}/{db} — {msg}")
    else:
        r.fail(f"{user}@{host}:{port}/{db} — {msg}")


def check_schema_bootstrapped(cfg: dict, env: dict[str, str]) -> None:
    r = check("Postgres schema bootstrapped (audit_log table exists)")
    pg_cfg = cfg.get("postgres", {})
    host = env.get("PG_HOST") or pg_cfg.get("host", "localhost")
    port = int(env.get("PG_PORT") or pg_cfg.get("port", 5432))
    db = env.get("PG_DATABASE") or pg_cfg.get("database", "trading")
    user = env.get("PG_USER") or pg_cfg.get("user", "trading")
    pw = env.get("PG_PASSWORD") or pg_cfg.get("password", "")

    try:
        import asyncpg  # type: ignore
    except ImportError:
        r.skip("asyncpg not installed"); return

    async def _probe() -> tuple[bool, str]:
        try:
            conn = await asyncpg.connect(host=host, port=port, database=db, user=user, password=pw, timeout=5)
            try:
                exists = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'audit_log')"
                )
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'"
                )
            finally:
                await conn.close()
            return bool(exists), f"{count} public tables"
        except Exception as exc:  # noqa: BLE001
            return False, repr(exc)

    ok, msg = asyncio.run(_probe())
    if ok:
        r.pass_(msg)
    else:
        r.fail(f"audit_log missing — run: .venv/bin/python -c 'import asyncio; from db.schema import init_db, close_pool; asyncio.run(init_db()); asyncio.run(close_pool())'")


def _http_get_json(url: str, timeout: float = 5.0) -> tuple[int, Optional[dict]]:
    req = urllib.request.Request(url, headers={"User-Agent": "preflight/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
            return e.code, json.loads(body)
        except Exception:
            return e.code, None
    except Exception as exc:
        raise exc


def check_telegram(env: dict[str, str]) -> None:
    r = check("Telegram bot token valid (getMe)")
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token or token.startswith("123456789:ABC"):
        r.skip("TELEGRAM_BOT_TOKEN not set")
        return
    try:
        status, body = _http_get_json(f"https://api.telegram.org/bot{token}/getMe")
        if status == 200 and body and body.get("ok"):
            username = body.get("result", {}).get("username", "?")
            r.pass_(f"@{username}")
        else:
            r.fail(f"HTTP {status} — {body.get('description') if body else 'no body'}")
    except Exception as exc:  # noqa: BLE001
        r.fail(f"network error — {exc}")


def check_massive(env: dict[str, str]) -> None:
    r = check("Massive API key valid (snapshot SPY)")
    key = env.get("MASSIVE_API_KEY", "")
    if not key:
        r.skip("MASSIVE_API_KEY not set")
        return
    url = f"https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers/SPY?apiKey={key}"
    try:
        status, body = _http_get_json(url, timeout=8.0)
        if status == 200 and body and body.get("status") in ("OK", "DELAYED"):
            r.pass_("snapshot OK")
        elif status == 401 or status == 403:
            r.fail(f"unauthorized (HTTP {status}) — check MASSIVE_API_KEY")
        else:
            r.warn(f"HTTP {status} — {body.get('error') if body else 'no body'}")
    except Exception as exc:  # noqa: BLE001
        r.fail(f"network error — {exc}")


def check_ibkr_gateway(cfg: dict) -> None:
    r = check("IBKR Gateway TCP reachable")
    ibkr_cfg = cfg.get("ibkr", {})
    host = ibkr_cfg.get("host", "127.0.0.1")
    port = int(ibkr_cfg.get("port", 4002))
    try:
        with socket.create_connection((host, port), timeout=3):
            r.pass_(f"{host}:{port}")
    except OSError as exc:
        r.fail(f"{host}:{port} — {exc}")


def check_postgres_role_password(cfg: dict, env: dict[str, str]) -> None:
    r = check("PG password not 'CHANGE_ME'")
    pw = env.get("PG_PASSWORD") or (cfg.get("postgres") or {}).get("password", "")
    if pw == "CHANGE_ME":
        r.fail("postgres password is still the literal 'CHANGE_ME' from setup_trading_role.sql")
    else:
        r.pass_()


def check_local_llm(env: dict[str, str]) -> None:
    """Only on local-llm branch — checks vLLM is responding on LOCAL_LLM_BASE_URL."""
    r = check("Local LLM (vLLM) reachable")
    url = env.get("LOCAL_LLM_BASE_URL", "")
    if not url:
        r.skip("LOCAL_LLM_BASE_URL not set (main branch?)")
        return
    models_url = url.rstrip("/") + "/models"
    try:
        status, body = _http_get_json(models_url, timeout=3.0)
        if status == 200 and body and "data" in body:
            names = [m.get("id", "?") for m in body["data"]]
            r.pass_(f"{len(names)} models")
        else:
            r.warn(f"HTTP {status}")
    except Exception as exc:  # noqa: BLE001
        r.warn(f"{models_url} — {exc} (start with: systemctl --user start trading-vllm)")


def check_systemd_units_installed() -> None:
    r = check("systemd user units installed")
    try:
        out = subprocess.run(
            ["systemctl", "--user", "list-unit-files", "trading-*", "--no-legend", "--no-pager"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            r.skip("systemd --user not available")
            return
        lines = [l for l in out.stdout.splitlines() if l.strip()]
        if not lines:
            r.warn("no trading-* user units found — run scripts/install_schedules.sh")
        else:
            r.pass_(f"{len(lines)} units")
    except FileNotFoundError:
        r.skip("systemctl not on PATH")
    except subprocess.TimeoutExpired:
        r.warn("systemctl timed out")


# ---------- main -------------------------------------------------------------

def main() -> int:
    print()
    print(_c("ProjectCorvus preflight", "\033[1m"))
    print(_c(f"  repo: {REPO_ROOT}", DIM))
    print()

    env = load_dotenv()
    cfg = load_yaml_config()

    # Order matters: env/config first so later checks have credentials.
    check_python_version()
    check_venv()
    check_dotenv(env)
    check_config_yaml(cfg)
    check_postgres_role_password(cfg, env)
    check_claude_cli()
    check_postgres(cfg, env)
    check_schema_bootstrapped(cfg, env)
    check_telegram(env)
    check_massive(env)
    check_ibkr_gateway(cfg)
    check_local_llm(env)
    check_systemd_units_installed()

    print()
    for r in RESULTS:
        r.print()
    print()

    fails = [r for r in RESULTS if r.status == "fail"]
    warns = [r for r in RESULTS if r.status == "warn"]
    skips = [r for r in RESULTS if r.status == "skip"]

    summary = (
        f"{len([r for r in RESULTS if r.status == 'pass'])} pass · "
        f"{_c(str(len(fails)) + ' fail', RED if fails else DIM)} · "
        f"{_c(str(len(warns)) + ' warn', YELLOW if warns else DIM)} · "
        f"{_c(str(len(skips)) + ' skip', DIM)}"
    )
    print(summary)
    print()

    if fails:
        print(_c("Fix the FAILs above before running scheduled skills.", RED))
        return 1
    if warns:
        print(_c("Preflight passed with warnings — review above.", YELLOW))
    else:
        print(_c("All required checks passed.", GREEN))
    return 0


if __name__ == "__main__":
    sys.exit(main())
