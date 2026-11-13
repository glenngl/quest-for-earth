"""Process entry point: ``quest-for-earth`` / ``python -m package``."""

import argparse

import uvicorn

from . import settings


def main() -> None:
    import os

    parser = argparse.ArgumentParser(description="Solar Scavengers server")
    # bind address is a deployment fact: HOST env (Docker sets 0.0.0.0),
    # loopback for local dev
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=settings.get_int("port"))
    args = parser.parse_args()
    uvicorn.run("quest_for_earth.web:app", host=args.host, port=args.port,
                log_level="info")


if __name__ == "__main__":
    main()
