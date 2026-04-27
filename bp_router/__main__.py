"""bp_router entry point — `python -m bp_router`.

Starts uvicorn against the FastAPI app produced by `create_app()`.
For production use a process supervisor (systemd, kubernetes) and
optionally `gunicorn -k uvicorn.workers.UvicornWorker` for multi-worker.
"""

from __future__ import annotations


def main() -> None:
    import uvicorn

    from bp_router.app import create_app
    from bp_router.settings import load_settings

    settings = load_settings()
    uvicorn.run(
        create_app,
        factory=True,
        host=settings.bind_host,
        port=settings.bind_port,
        log_config=None,  # we configure logging ourselves
    )


if __name__ == "__main__":
    main()
