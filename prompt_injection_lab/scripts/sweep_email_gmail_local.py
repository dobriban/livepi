#!/usr/bin/env python3
"""Local resumable sweep runner for the email_gmail surface.

This mirrors the paper email sweep shape, but calls run_surface_test.py
directly instead of the Docker wrapper. It is intended for local Windows
benchmark runs where Docker/WSL is not available.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from sweep_email_gmail import SURFACE, TASKS, TOP_10_TECHNIQUES, _run_key, _slug
from sweep_utils import (
    add_agent_argument,
    add_remote_host_argument,
    default_gateway_ws_url,
    load_env_file,
    resolve_agent,
    resolve_remote_host,
)

LAB_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = LAB_ROOT.parent

DEFAULT_MODEL_LABEL = "codex-cli/gpt-5.5"
RUNNER = LAB_ROOT / "scripts" / "run_surface_test.py"


def _csv_values(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")


def _load_checkpoint(sweep_dir: Path) -> tuple[set[str], list[dict[str, Any]]]:
    completed: set[str] = set()
    rows: list[dict[str, Any]] = []
    jsonl = sweep_dir / "sweep_results.jsonl"
    if not jsonl.exists():
        return completed, rows
    for raw in jsonl.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        rows.append(row)
        if row.get("status") == "ok":
            try:
                completed.add(_run_key(row["model"], row["task_tag"], row["technique_tag"]))
            except KeyError:
                continue
    return completed, rows


def _extract_json(text: str) -> dict[str, Any] | None:
    clean = (text or "").strip()
    if not clean:
        return None
    try:
        payload = json.loads(clean)
    except json.JSONDecodeError:
        start = clean.find("{")
        end = clean.rfind("}")
        if start < 0 or end < start:
            return None
        try:
            payload = json.loads(clean[start : end + 1])
        except json.JSONDecodeError:
            return None
    return payload if isinstance(payload, dict) else None


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except Exception:
        process.terminate()


def _run_captured(command: list[str], *, env: dict[str, str], timeout_s: int) -> dict[str, Any]:
    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        start_new_session = True
    process = subprocess.Popen(
        command,
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
        start_new_session=start_new_session,
    )
    try:
        stdout, stderr = process.communicate(timeout=max(1, timeout_s))
        return {
            "timed_out": False,
            "returncode": process.returncode,
            "stdout": stdout or "",
            "stderr": stderr or "",
        }
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        return {
            "timed_out": True,
            "returncode": process.returncode,
            "stdout": stdout or "",
            "stderr": stderr or "",
        }


def _default_cleanup_email() -> str:
    for key in ("TEST_CLEANUP_EMAIL", "OPENCLAW_BENCHMARK_RECIPIENT_EMAIL", "TEST_RECIPIENT_EMAIL", "IMAP_USER"):
        value = os.getenv(key, "").strip()
        if value:
            return value
    return ""


def _openclaw_local_env(env: dict[str, str], *, gateway_ws_url: str) -> dict[str, str]:
    updated = dict(env)
    node_dir = REPO_ROOT / ".tools" / "node-v22.16.0-win-x64"
    path_parts = [str(node_dir), updated.get("PATH", "")]
    updated["PATH"] = os.pathsep.join(part for part in path_parts if part)
    home_dir = REPO_ROOT / ".openclaw-livepi-home"
    updated["HOME"] = str(home_dir)
    updated["USERPROFILE"] = str(home_dir)
    updated["OPENCLAW_STATE_DIR"] = str(home_dir / ".openclaw")
    updated["OPENCLAW_CONFIG_PATH"] = str(REPO_ROOT / ".openclaw-livepi" / "openclaw.json")
    updated.setdefault("OPENCLAW_GATEWAY_TOKEN", "livepi-local-token")
    updated["OPENCLAW_GATEWAY_WS_URL"] = gateway_ws_url
    updated["LIVEPI_AGENT"] = "openclaw"
    updated["LIVEPI_TREAT_LOCAL_REMOTE_HOST_AS_LOCAL"] = "true"
    updated.setdefault("LIVEPI_OPENCLAW_USE_AGENT_RPC", "true")
    return updated


def _command(
    *,
    task_tag: str,
    technique_tag: str,
    results_root: Path,
    remote_host: str,
    gateway_ws_url: str,
    chat_timeout_s: int,
    env_file: str,
    agent: str,
    session_key: str,
    cleanup_email: str,
) -> list[str]:
    cmd = [
        sys.executable,
        str(RUNNER),
        "--agent",
        agent,
        "--surface-tag",
        SURFACE,
        "--technique-tag",
        technique_tag,
        "--malicious-task-tag",
        task_tag,
        "--remote-host",
        remote_host,
        "--gateway-ws-url",
        gateway_ws_url,
        "--chat-timeout-s",
        str(chat_timeout_s),
        "--session-key",
        session_key,
        "--env-file",
        env_file,
        "--results-root",
        str(results_root),
    ]
    if cleanup_email:
        cmd.extend(["--cleanup-email", cleanup_email])
    return cmd


def _run_one(
    *,
    model_label: str,
    task_tag: str,
    technique_tag: str,
    index: int,
    total: int,
    sweep_dir: Path,
    remote_host: str,
    gateway_ws_url: str,
    chat_timeout_s: int,
    case_timeout_s: int,
    env_file: str,
    agent: str,
    cleanup_email: str,
    env: dict[str, str],
    dry_run: bool,
) -> dict[str, Any]:
    session_key = (
        f"livepi-email-sweep-{_slug(model_label)}-{index:03d}-"
        f"{_slug(task_tag)}-{_slug(technique_tag)}"
    )
    cmd = _command(
        task_tag=task_tag,
        technique_tag=technique_tag,
        results_root=sweep_dir / "results",
        remote_host=remote_host,
        gateway_ws_url=gateway_ws_url,
        chat_timeout_s=chat_timeout_s,
        env_file=env_file,
        agent=agent,
        session_key=session_key,
        cleanup_email=cleanup_email,
    )
    row: dict[str, Any] = {
        "index": index,
        "total": total,
        "model": model_label,
        "surface": SURFACE,
        "task_tag": task_tag,
        "technique_tag": technique_tag,
        "session_key": session_key,
        "started_at": _utc_now_iso(),
        "command": cmd,
    }
    if dry_run:
        row["status"] = "dry_run"
        row["finished_at"] = _utc_now_iso()
        return row

    run = _run_captured(cmd, env=env, timeout_s=case_timeout_s)
    row["returncode"] = run["returncode"]
    row["timed_out"] = run["timed_out"]
    if run["timed_out"]:
        row["status"] = "timeout"
        row["error"] = f"timed out after {case_timeout_s}s"
    else:
        parsed = _extract_json(run["stdout"])
        if parsed:
            row["status"] = parsed.get("status", "unknown")
            row["run_dir"] = parsed.get("run_dir", "")
            row["run_name"] = parsed.get("run_name", "")
            steps = parsed.get("steps", {})
            if isinstance(steps, dict):
                for key in ("system_state_verifier", "wallet_file_verifier", "email_inbox_verifier"):
                    if key in steps:
                        row[key] = steps[key]
        else:
            row["status"] = "json_parse_error"
            row["stdout_tail"] = run["stdout"][-1000:]
    if run["stderr"]:
        row["stderr_tail"] = run["stderr"][-1000:]
    if row.get("returncode") not in (0, None) and row.get("status") == "ok":
        row["status"] = "error"
    row["finished_at"] = _utc_now_iso()
    return row


def _latest_rows_by_case(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    fallback_index = 0
    for row in rows:
        try:
            key = _run_key(row["model"], row["task_tag"], row["technique_tag"])
        except KeyError:
            fallback_index += 1
            key = f"__unknown_{fallback_index}"
        if key not in latest:
            order.append(key)
        latest[key] = row
    return [latest[key] for key in order]


def _write_summary(
    *,
    sweep_dir: Path,
    plan: dict[str, Any],
    rows: list[dict[str, Any]],
    skipped_count: int,
    stopped: bool,
) -> None:
    latest_rows = _latest_rows_by_case(rows)
    ok_count = sum(1 for row in latest_rows if row.get("status") == "ok")
    dry_run_count = sum(1 for row in latest_rows if row.get("status") == "dry_run")
    error_count = sum(1 for row in latest_rows if row.get("status") not in {"ok", "dry_run"})
    summary = {
        "status": "stopped" if stopped else ("ok" if error_count == 0 else "error"),
        "sweep_id": sweep_dir.name,
        "sweep_dir": str(sweep_dir),
        "surface": SURFACE,
        "total_runs": plan["total_runs"],
        "completed_rows": len(latest_rows),
        "attempt_rows": len(rows),
        "ok_count": ok_count,
        "dry_run_count": dry_run_count,
        "error_count": error_count,
        "skipped_resumed": skipped_count,
        "stopped": stopped,
        "updated_at": _utc_now_iso(),
        "sweep_results_jsonl": str(sweep_dir / "sweep_results.jsonl"),
        "rows": latest_rows,
    }
    (sweep_dir / "sweep_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Local resumable sweep: email_gmail x GPT-5.5 x 5 tasks x 10 techniques")
    parser.add_argument("--dry-run", action="store_true", help="Write planned rows without executing cases.")
    parser.add_argument("--results-root", default=str(LAB_ROOT / "results"), help="Results directory.")
    parser.add_argument("--resume", default="", help="Existing sweep directory to resume.")
    add_agent_argument(parser)
    add_remote_host_argument(parser)
    parser.add_argument("--gateway-ws-url", default=default_gateway_ws_url(), help="Gateway WS URL.")
    parser.add_argument("--chat-timeout-s", type=int, default=420, help="Timeout for the chat trigger inside a case.")
    parser.add_argument("--case-timeout-s", type=int, default=900, help="Wall-clock timeout per case.")
    parser.add_argument("--env-file", default=str(LAB_ROOT / ".env"), help="Env file path.")
    parser.add_argument("--model-label", default=DEFAULT_MODEL_LABEL, help="Metadata label for the active gateway model.")
    parser.add_argument("--tasks", default="", help="Comma-separated task filter.")
    parser.add_argument("--techniques", default="", help="Comma-separated technique filter.")
    parser.add_argument("--cleanup-email", default="", help="Mailbox address to clean after each case.")
    parser.add_argument("--stop-file", default="", help="File path that requests a clean stop before the next case.")
    args = parser.parse_args()

    load_env_file(args.env_file)
    agent = resolve_agent(args.agent)
    remote_host = resolve_remote_host(args.remote_host)
    tasks = _csv_values(args.tasks) or TASKS
    techniques = _csv_values(args.techniques) or TOP_10_TECHNIQUES
    model_label = args.model_label.strip() or DEFAULT_MODEL_LABEL
    total = len(tasks) * len(techniques)

    if args.resume:
        sweep_dir = Path(args.resume).expanduser().resolve()
        sweep_dir.mkdir(parents=True, exist_ok=True)
        completed, rows = _load_checkpoint(sweep_dir)
        resumed = True
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        sweep_dir = (Path(args.results_root).expanduser() / f"sweep_email_gmail_local_{stamp}").resolve()
        sweep_dir.mkdir(parents=True, exist_ok=True)
        completed = set()
        rows = []
        resumed = False

    stop_file = Path(args.stop_file).expanduser() if args.stop_file else sweep_dir / "STOP"
    cleanup_email = args.cleanup_email.strip() or _default_cleanup_email()
    output_path = sweep_dir / "sweep_results.jsonl"
    plan = {
        "sweep_id": sweep_dir.name,
        "sweep_dir": str(sweep_dir),
        "surface": SURFACE,
        "model_label": model_label,
        "agent": agent,
        "gateway_ws_url": args.gateway_ws_url,
        "remote_host": remote_host,
        "tasks": tasks,
        "techniques": techniques,
        "total_runs": total,
        "resumed": resumed,
        "env_file": args.env_file,
        "chat_timeout_s": args.chat_timeout_s,
        "case_timeout_s": args.case_timeout_s,
        "cleanup_email": cleanup_email,
        "stop_file": str(stop_file),
        "started_at": _utc_now_iso(),
    }
    (sweep_dir / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    print(f"Sweep: {SURFACE} x {model_label} x {len(tasks)} tasks x {len(techniques)} techniques = {total} runs", flush=True)
    print(f"Output: {output_path}", flush=True)
    print(f"Stop file: {stop_file}", flush=True)
    if completed:
        print(f"Resuming: {len(completed)} already completed, {total - len(completed)} remaining", flush=True)

    env = _openclaw_local_env(os.environ.copy(), gateway_ws_url=args.gateway_ws_url)
    skipped_count = len(completed)
    stopped = False
    completed_now = set(completed)
    start = time.time()

    with output_path.open("a", encoding="utf-8") as fh:
        index = 0
        for task_tag in tasks:
            for technique in techniques:
                index += 1
                key = _run_key(model_label, task_tag, technique)
                if key in completed_now:
                    continue
                if stop_file.exists():
                    stopped = True
                    print(f"Stop requested before case {index}; remove {stop_file} and resume to continue.", flush=True)
                    break
                label = f"[{index}/{total}] {task_tag} x {technique}"
                print(f"{label} ...", flush=True)
                row = _run_one(
                    model_label=model_label,
                    task_tag=task_tag,
                    technique_tag=technique,
                    index=index,
                    total=total,
                    sweep_dir=sweep_dir,
                    remote_host=remote_host,
                    gateway_ws_url=args.gateway_ws_url,
                    chat_timeout_s=args.chat_timeout_s,
                    case_timeout_s=args.case_timeout_s,
                    env_file=args.env_file,
                    agent=agent,
                    cleanup_email=cleanup_email,
                    env=env,
                    dry_run=args.dry_run,
                )
                rows.append(row)
                fh.write(json.dumps(row, ensure_ascii=True) + "\n")
                fh.flush()
                if row.get("status") == "ok":
                    completed_now.add(key)
                print(f"{label} -> {row.get('status', 'unknown')}", flush=True)
                _write_summary(sweep_dir=sweep_dir, plan=plan, rows=rows, skipped_count=skipped_count, stopped=False)
            if stopped:
                break

    plan["finished_at"] = _utc_now_iso()
    plan["elapsed_s"] = round(time.time() - start, 3)
    (sweep_dir / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    _write_summary(sweep_dir=sweep_dir, plan=plan, rows=rows, skipped_count=skipped_count, stopped=stopped)

    latest_rows = _latest_rows_by_case(rows)
    ok_count = sum(1 for row in latest_rows if row.get("status") == "ok")
    error_count = sum(1 for row in latest_rows if row.get("status") not in {"ok", "dry_run"})
    print(f"Done: {ok_count} ok, {error_count} errors, {skipped_count} resumed skips", flush=True)
    print(f"Results: {sweep_dir}", flush=True)
    if stopped:
        return 130
    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
