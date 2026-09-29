#!/usr/bin/env python3
"""Fail closed when a version is published or its state cannot be verified."""

import json
import os
import re
import urllib.error
import urllib.request


QUERY = """
query ReleaseStatus($owner: String!, $name: String!, $tag: String!) {
  repository(owner: $owner, name: $name) {
    release(tagName: $tag) {
      tagName
      isDraft
    }
  }
}
"""


def main():
    tag = os.environ["RELEASE_TAG"]
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise SystemExit("Invalid release tag")
    repository = os.environ["GITHUB_REPOSITORY"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise SystemExit("Invalid repository")
    owner, name = repository.split("/")
    # REST's tag lookup excludes drafts and its release listing can lag behind
    # creation. Direct GraphQL lookup also finds a newly created draft by tag.
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": QUERY, "variables": {"owner": owner, "name": name, "tag": tag}}).encode(),
        headers={
            "Authorization": "Bearer " + os.environ["GH_TOKEN"],
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise SystemExit(f"Could not verify release state (HTTP {error.code}); stopping.") from None
    except OSError:
        raise SystemExit("Could not verify release state (network error); stopping.") from None
    except (ValueError, UnicodeError):
        raise SystemExit("Could not verify release state (invalid JSON); stopping.") from None
    if not isinstance(result, dict) or "errors" in result:
        raise SystemExit("Could not verify release state (GraphQL error); stopping.")
    try:
        release = result["data"]["repository"]["release"]
    except (KeyError, TypeError):
        raise SystemExit("Could not verify release state (repository unavailable or invalid response); stopping.") from None
    if release is None:
        print("missing")
        return
    if not isinstance(release, dict) or release.get("tagName") != tag or type(release.get("isDraft")) is not bool:
        raise SystemExit("Could not verify release state (invalid release response); stopping.")
    if not release["isDraft"]:
        raise SystemExit("Published versions are never overwritten. Choose a new version.")
    print("draft")


if __name__ == "__main__":
    main()
