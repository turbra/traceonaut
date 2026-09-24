#!/usr/bin/env python3
"""Create a private scrape token without printing it or replacing an existing file."""

import argparse
import os
from pathlib import Path
import secrets

from traceonaut.observability_exporter import read_credential, validate_credential_parent


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credential-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        path = validate_credential_parent(args.credential_file)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            try:
                read_credential(path)
            except (OSError, ValueError):
                parser.exit(1, "The existing path is not a valid private token. Check its type, owner, permissions and contents.\n")
            parser.exit(1, "A valid token already exists. Reuse it and continue with the next setup step; it was not changed.\n")
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(secrets.token_urlsafe(32) + "\n")
    except (OSError, ValueError):
        parser.exit(1, "Token creation failed: use a trusted directory and a new file path.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
