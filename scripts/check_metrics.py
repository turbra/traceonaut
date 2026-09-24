#!/usr/bin/env python3
"""Check authenticated session metrics without displaying the token or payload."""

import argparse
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from traceonaut.observability_exporter import read_credential


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:9464/metrics")
    parser.add_argument("--credential-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        url = urlsplit(args.url)
        if (url.scheme not in ("http", "https") or not url.hostname
                or url.username is not None or url.password is not None or url.fragment):
            raise ValueError("invalid URL")
        _ = url.port
        token = read_credential(args.credential_file).decode("ascii")
        request = Request(args.url, headers={"Authorization": "Bearer " + token})
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=4) as response:
            payload = response.read(65536)
    except HTTPError as error:
        status = error.code
        error.close()
        parser.exit(1, f"Metrics HTTP {status}; check authentication and collector readiness.\n")
    except (OSError, ValueError, URLError):
        parser.exit(1, "Metrics check failed: check the URL, private token, network and certificate trust.\n")
    if b"cwo_codex_collector_scan_timestamp_seconds" not in payload:
        parser.exit(1, "Expected collector health metrics are absent.\n")
    print("Authenticated metrics: HTTP 200; collector health metrics present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
