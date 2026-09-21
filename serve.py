#!/usr/bin/env python3
"""Start the interview search web UI.

    python serve.py            # http://127.0.0.1:8765
    python serve.py --port 9000

The port comes from --port, else the PORT environment variable, else 8765.
Honouring PORT lets a supervisor assign a free port when 8765 is taken.
"""

import argparse
import os

from interview_monitor.web import serve

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Interview search web UI")
    parser.add_argument("--port", type=int, default=None,
                        help="port to bind (default: $PORT, else 8765)")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    args = parser.parse_args()

    port = args.port if args.port is not None else int(os.environ.get("PORT") or 8765)
    serve(host=args.host, port=port)
