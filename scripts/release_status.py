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
    # The release-by-tag endpoint can return 404 for an existing draft. The
    # authenticated releases listing includes drafts visible to this publisher.
    page = 1
    while True:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repository}/releases?per_page=100&page={page}",
            headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"], "Accept": "application/vnd.github+json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                releases = json.load(response)
        except urllib.error.HTTPError as error:
            raise SystemExit(f"Could not verify release state (HTTP {error.code}); stopping.") from None
        for release in releases:
            if release["tag_name"] == tag:
                if not release["draft"]:
                    raise SystemExit("Published versions are never overwritten. Choose a new version.")
                print("draft")
                return
        if len(releases) < 100:
            print("missing")
            return
        page += 1


if __name__ == "__main__":
    main()
