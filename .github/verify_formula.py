"""Decides whether a pull request to this tap may merge.

A formula is Ruby that runs on the machine of everyone who installs it, so
a change is only let through when it is a plain version bump: the formula
on main with a higher version and new checksums, and nothing else. Every
file the formula names is then downloaded and its checksum compared.

This runs from main, never from the pull request, and treats the pull
request as text. It executes none of it.
"""

import base64
import hashlib
import json
import os
import re
import sys
import urllib.request

API = "https://api.github.com"
OWNER = "ultradotdev"
FORMULA = re.compile(r"Formula/([a-z][a-z0-9-]*)\.rb")
VERSION = re.compile(r'^  version "(\d+)\.(\d+)\.(\d+)"$', re.MULTILINE)
SHA = re.compile(r'sha256 "[0-9a-f]{64}"')
PAIR = re.compile(r'^ +url "([^"\n]*)"\n +sha256 "([0-9a-f]{64})"$', re.MULTILINE)
FORMULA_LIMIT = 64 * 1024
DOWNLOAD_LIMIT = 500 * 1024 * 1024


class Refused(Exception):
    pass


def api(path):
    request = urllib.request.Request(
        f"{API}/{path}",
        headers={"Authorization": f"Bearer {os.environ['GH_TOKEN']}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=60) as reply:
        return json.load(reply)


def tree(repo, sha):
    listing = api(f"repos/{repo}/git/trees/{sha}?recursive=1")
    if listing.get("truncated"):
        raise Refused("the file listing was cut short, so the change cannot be judged")
    return {entry["path"]: (entry["mode"], entry["type"], entry["sha"]) for entry in listing["tree"] if entry["type"] != "tree"}


def blob(repo, sha):
    found = api(f"repos/{repo}/git/blobs/{sha}")
    if found["size"] > FORMULA_LIMIT:
        raise Refused("a formula is larger than any formula should be")
    return base64.b64decode(found["content"]).decode("utf-8")


def version_of(text, where):
    found = VERSION.findall(text)
    if len(found) != 1:
        raise Refused(f"{where} must have exactly one line of the form: version \"1.2.3\"")
    return tuple(int(part) for part in found[0])


def shape(text, version):
    """The formula with its version and checksums blanked out."""
    return SHA.sub('sha256 "@"', text).replace(".".join(map(str, version)), "@")


def sha256_of(url):
    digest, size = hashlib.sha256(), 0
    with urllib.request.urlopen(url, timeout=120) as reply:
        while chunk := reply.read(1 << 20):
            size += len(chunk)
            if size > DOWNLOAD_LIMIT:
                raise Refused(f"{url} is larger than any release file should be")
            digest.update(chunk)
    return digest.hexdigest()


def judge(path, before, after, download=sha256_of):
    """Raises Refused unless `after` is `before` with a higher version and
    checksums that match what its addresses serve."""
    name = FORMULA.fullmatch(path).group(1)
    old, new = version_of(before, f"{path} on main"), version_of(after, path)
    if new <= old:
        raise Refused(f"{path}: version {'.'.join(map(str, new))} is not higher than {'.'.join(map(str, old))} on main")
    if shape(before, old) != shape(after, new):
        raise Refused(f"{path}: something other than the version and the checksums differs from main")
    dotted = ".".join(map(str, new))
    wanted = re.compile(
        rf"https://github\.com/{OWNER}/{name}/releases/download/v{re.escape(dotted)}/{name}-v{re.escape(dotted)}-[a-z0-9-]+\.(zip|tar\.gz)"
    )
    pairs = PAIR.findall(after)
    if not pairs or len(pairs) != after.count("url ") or len(pairs) != len(SHA.findall(after)):
        raise Refused(f"{path}: every address needs its checksum on the next line, and there must be at least one")
    for url, expected in pairs:
        if not wanted.fullmatch(url):
            raise Refused(f"{path}: {url} is not a file of the {OWNER}/{name} v{dotted} release")
        actual = download(url)
        if actual != expected:
            raise Refused(f"{path}: {url} has checksum {actual}, not the {expected} the formula gives")
        print(f"ok  {url}")


def changes(base, head):
    """Paths that differ between two listings, refusing anything that is
    not an existing formula kept as an ordinary file."""
    changed = sorted(path for path in base.keys() | head.keys() if base.get(path) != head.get(path))
    if not changed:
        raise Refused("this pull request changes nothing")
    for path in changed:
        if not FORMULA.fullmatch(path):
            raise Refused(f"{path}: only formulas may change in a pull request; anything else needs the owner")
        if path not in base or path not in head:
            raise Refused(f"{path}: adding or removing a formula needs the owner")
        if (base[path][0], base[path][1]) != ("100644", "blob") or (head[path][0], head[path][1]) != ("100644", "blob"):
            raise Refused(f"{path}: a formula must stay an ordinary file")
    return changed


def main():
    repo, head_sha = os.environ["REPO"], os.environ["HEAD_SHA"]
    if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise Refused("the pull request's commit is not a commit id")
    base_sha = api(f"repos/{repo}/git/ref/heads/main")["object"]["sha"]
    base, head = tree(repo, base_sha), tree(repo, head_sha)
    for path in changes(base, head):
        judge(path, blob(repo, base[path][2]), blob(repo, head[path][2]))
    print("This pull request is a plain version bump and its checksums match.")


if __name__ == "__main__":
    try:
        main()
    except Refused as why:
        print(f"::error::{why}")
        sys.exit(1)
