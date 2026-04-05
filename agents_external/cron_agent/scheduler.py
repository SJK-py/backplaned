"""
cron_agent/scheduler.py — Background scheduler that triggers cron jobs.

Scans all users' jobs every ``check_interval`` seconds, finds jobs whose
next_run has passed, and invokes the autonomous agent loop for each.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Awaitable, Optional
from zoneinfo import ZoneInfo

from croniter import croniter

from db import CronDB

logger = logging.getLogger("cron_agent.scheduler")


def _is_nighttime(now: datetime, start_str: str, end_str: str) -> bool:
    """Check whether ``now`` falls within the nighttime suppression window."""
    try:
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
    except Exception:
        return False
    cur = now.hour * 60 + now.minute
    ns = sh * 60 + sm
    ne = eh * 60 + em
    if ns <= ne:
        return ns <= cur < ne
    return cur >= ns or cur < ne


async def _process_job(
    user_id: str,
    job: dict[str, Any],
    settings: dict[str, Any],
    db: CronDB,
    run_autonomous: Callable[..., Awaitable[Optional[str]]],
    default_model_id: str,
) -> dict[str, Any]:
    """
    Execute a single cron job's autonomous agent loop.

    Returns a log entry dict.
    """
    job_id = job["id"]
    model_id = job.get("model_id") or settings.get("model_id") or default_model_id or None

    logger.info("Executing job %s for user %s: %s", job_id, user_id, job.get("description", ""))

    try:
        report = await run_autonomous(
            user_id=user_id,
            job=job,
            settings=settings,
            model_id=model_id,
        )
        await db.mark_job_run(user_id, job_id)
        return {
            "user_id": user_id,
            "job_id": job_id,
            "action": "executed",
            "reported": report is not None,
        }
    except Exception as exc:
        logger.exception("Job %s failed for user %s: %s", job_id, user_id, exc)
        await db.mark_job_run(user_id, job_id)
        return {
            "user_id": user_id,
            "job_id": job_id,
            "action": "error",
            "error": str(exc),
        }


async def scheduler_loop(
    db: CronDB,
    run_autonomous: Callable[..., Awaitable[Optional[str]]],
    check_interval: int,
    default_model_id: str,
    log_dir: str = "data/logs",
) -> None:
    """
    Background loop that checks all cron jobs and triggers due ones.

    Runs every ``check_interval`` seconds.
    """
    logger.info("Scheduler started: check_interval=%ds", check_interval)
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    # Track running jobs to avoid concurrent execution of the same job.
    running_jobs: set[str] = set()  # "{user_id}:{job_id}"

    first_run = True
    cycle_count = 0

    while True:
        if first_run:
            await asyncio.sleep(5)
            first_run = False
        else:
            await asyncio.sleep(check_interval)

        cycle_count += 1
        now_utc = datetime.now(timezone.utc)

        try:
            user_ids = db.list_user_ids()
        except Exception as exc:
            logger.error("Failed to list users: %s", exc)
            continue

        tasks_to_run: list[tuple[str, dict, dict]] = []  # (user_id, job, settings)

        for user_id in user_ids:
            try:
                settings = await db.get_settings(user_id)
                tz_str = settings.get("timezone", "UTC")
                try:
                    tz = ZoneInfo(tz_str)
                except Exception:
                    tz = ZoneInfo("UTC")

                now_local = datetime.now(tz)

                # Nighttime suppression
                ns = settings.get("nighttime_start", "22:00")
                ne = settings.get("nighttime_end", "07:00")
                if _is_nighttime(now_local, ns, ne):
                    continue

                jobs = await db.get_all_jobs(user_id)
                for job_id, job in jobs.items():
                    if not job.get("enabled", True):
                        continue

                    # Check start/end bounds.
                    start_at = job.get("start_at")
                    if start_at:
                        try:
                            sa_dt = datetime.fromisoformat(start_at)
                            if sa_dt.tzinfo is None:
                                sa_dt = sa_dt.replace(tzinfo=timezone.utc)
                            if now_utc < sa_dt:
                                continue
                        except Exception:
                            pass

                    end_at = job.get("end_at")
                    if end_at:
                        try:
                            ea_dt = datetime.fromisoformat(end_at)
                            if ea_dt.tzinfo is None:
                                ea_dt = ea_dt.replace(tzinfo=timezone.utc)
                            if now_utc > ea_dt:
                                # Auto-disable expired jobs.
                                await db.modify_job(user_id, job_id, {"enabled": False})
                                continue
                        except Exception:
                            pass

                    # Check if the job is due.
                    cron_expr = job.get("cron_expression", "")
                    if not cron_expr:
                        continue

                    last_run = job.get("last_run")
                    base_time = now_local
                    if last_run:
                        try:
                            lr_dt = datetime.fromisoformat(last_run)
                            if lr_dt.tzinfo is None:
                                lr_dt = lr_dt.replace(tzinfo=timezone.utc)
                            base_time_for_check = lr_dt.astimezone(tz)
                        except Exception:
                            base_time_for_check = now_local - timedelta(hours=24)
                    else:
                        # Never run — check if it should have run by now.
                        base_time_for_check = now_local - timedelta(hours=24)

                    try:
                        it = croniter(cron_expr, base_time_for_check)
                        next_run_dt = it.get_next(datetime)
                        # Store next_run in UTC for consistency with other timestamp fields.
                        next_run_utc = next_run_dt.astimezone(timezone.utc) if next_run_dt.tzinfo else next_run_dt
                        await db.modify_job(user_id, job_id, {"next_run": next_run_utc.isoformat()})
                    except Exception:
                        continue

                    if next_run_dt <= now_local:
                        run_key = f"{user_id}:{job_id}"
                        if run_key not in running_jobs:
                            tasks_to_run.append((user_id, job, settings))

            except Exception as exc:
                logger.error("Error checking user %s: %s", user_id, exc)

        # Execute due jobs concurrently (but not the same job twice).
        if tasks_to_run:
            logger.info("Scheduler cycle %d: %d job(s) to execute", cycle_count, len(tasks_to_run))

        for user_id, job, settings in tasks_to_run:
            run_key = f"{user_id}:{job['id']}"
            running_jobs.add(run_key)

            async def _run_and_cleanup(uid: str, j: dict, s: dict, rk: str) -> None:
                try:
                    result = await _process_job(uid, j, s, db, run_autonomous, default_model_id)
                    # Write log
                    log_path = Path(log_dir) / f"cron_{j['id']}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json"
                    log_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
                finally:
                    running_jobs.discard(rk)

            asyncio.create_task(_run_and_cleanup(user_id, job, settings, run_key))
