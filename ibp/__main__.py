"""Script to run the IBP development server."""

import argparse
import uvicorn

from .base import app, get_log_level


def main():
    """Run the IBP development server."""

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    # Use uvicorn to run the FastAPI app
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level=get_log_level().lower(),
    )


if __name__ == "__main__":
    main()
