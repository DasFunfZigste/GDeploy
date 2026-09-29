#!/usr/bin/env python3
"""Fail closed when a version is published or its state cannot be verified."""

import json
import os
import re
import urllib.error
import urllib.request


def main():
    tag = os.environ["RELEASE_TAG"]
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise SystemExit("Invalid release tag")
    repository = os.environ["GITHUB_REPOSITORY"]
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/releases/tags/{tag}",
        headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"], "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            release = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise SystemExit(f"Could not verify release state (HTTP {error.code}); stopping.") from None
        print("missing")
    else:
        if not release["draft"]:
            raise SystemExit("Published versions are never overwritten. Choose a new version.")
        print("draft")


if __name__ == "__main__":
    main()
