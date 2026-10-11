"""Verify tracked Git blobs and optionally the latest release manifest/ZIP metadata.

No ZIP execution, extraction or network access is performed by this checker.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def parse_manifest(data):
    entries = {}
    folded = set()
    for line in data.decode("utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            raise ValueError("Invalid checksum line")
        digest, name = match.groups()
        parts = name.split("/")
        if (
            any(p in ("", ".", "..") for p in parts)
            or "\\" in name or ":" in name
            or any(ord(c) < 32 for c in name)
            or name.casefold() in folded
        ):
            raise ValueError("Unsafe or duplicate checksum path")
        entries[name] = digest
        folded.add(name.casefold())
    if not entries:
        raise ValueError("Empty checksum list")
    return entries


def verify(repository, checksum_file=None, release_metadata=None, release_manifest=None):
    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(repository), *args], stderr=subprocess.PIPE
        )

    def blob(name):
        return git("show", "HEAD:" + name)

    committed = blob("SHA256SUMS")
    data = checksum_file.read_bytes() if checksum_file else committed
    entries = parse_manifest(data)
    tracked = set(git("ls-tree", "-r", "--name-only", "HEAD").decode("utf-8").splitlines())
    tracked.remove("SHA256SUMS")
    required = {"README.md", "LICENSE.txt", "SUPPORT.md", "model-notices.md",
                ".github/ISSUE_TEMPLATE/bug.yml", ".github/ISSUE_TEMPLATE/config.yml"}
    if not required <= tracked:
        raise ValueError("Required distribution documents are missing")
    archives = set(entries) - tracked
    if len(archives) != 1:
        raise ValueError("Exactly one release ZIP must be listed")
    archive = archives.pop()
    match = re.fullmatch(r"YUHITSU-free-windows-x64-(v[0-9.]+-free\.[0-9]+)\.zip", archive)
    if not match:
        raise ValueError("Unexpected archive name")
    if set(entries) - {archive} != tracked:
        raise ValueError("Every tracked file except SHA256SUMS must be covered")
    mismatches = []
    for name in sorted(tracked):
        actual = hashlib.sha256(blob(name)).hexdigest()
        if actual != entries[name]:
            mismatches.append({"file": name, "expected": entries[name], "actual": actual})
    result = {"tracked_files_checked": len(tracked), "mismatches": mismatches,
              "archive": archive, "archive_bytes_verified": False,
              "release_checked": False}
    if (release_metadata is None) != (release_manifest is None):
        raise ValueError("Both release metadata and manifest are required")
    if release_metadata is not None:
        release = json.loads(release_metadata.read_text(encoding="utf-8"))
        if release.get("draft") is not False or release.get("prerelease") is not False:
            raise ValueError("Expected a published stable release")
        if release.get("tag_name") != match.group(1):
            raise ValueError("Release tag and archive name differ")
        attachment = release_manifest.read_bytes()
        parse_manifest(attachment)
        if attachment != committed or data != committed:
            mismatches.append({"file": "release/SHA256SUMS", "reason": "bytes differ from Git manifest"})
        assets = release.get("assets", [])
        for name, expected in [(archive, entries[archive]),
                               ("SHA256SUMS", hashlib.sha256(attachment).hexdigest())]:
            found = [a for a in assets if a.get("name") == name]
            if len(found) != 1:
                raise ValueError("Missing or duplicate release asset: " + name)
            asset = found[0]
            if asset.get("state") != "uploaded" or not isinstance(asset.get("size"), int) or asset["size"] <= 0:
                raise ValueError("Incomplete release asset: " + name)
            if asset.get("digest") != "sha256:" + expected:
                mismatches.append({"file": "release/" + name, "reason": "asset digest differs"})
        result["release_checked"] = True
        result["release_tag"] = release["tag_name"]
        result["archive_digest_metadata_verified"] = not any(
            m["file"] == "release/" + archive for m in mismatches
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("--checksum-file", type=Path)
    parser.add_argument("--release-metadata", type=Path)
    parser.add_argument("--release-manifest", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.repository, args.checksum_file, args.release_metadata, args.release_manifest)
        print(json.dumps(result, indent=2))
        return 1 if result["mismatches"] else 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
