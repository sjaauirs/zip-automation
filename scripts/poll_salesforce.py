#!/usr/bin/env python3
"""
Poll Salesforce for ZIPs attached to Accounts, and publish each new one as a repo.

Why polling exists
------------------
On an org that cannot run Apex or Flow there is no way for Salesforce to tell us
that something was uploaded, so we ask instead. The workflow runs on a schedule;
each run looks for ZIPs it has not seen before and processes them.

The trade-off is latency: a repo appears within one polling interval rather than
immediately. On an org with Apex you would dispatch instead and skip this.

State
-----
Processed ContentVersion Ids are kept in a JSON file, which the workflow commits
back to the repo. Committing is what makes it durable across runs -- there is no
database here.

Usage
-----
    python scripts/poll_salesforce.py            # process anything new
    python scripts/poll_salesforce.py --dry-run  # list what it would do
    python scripts/poll_salesforce.py --limit 1  # cap files per run
"""

import argparse
import fnmatch
import json
import os
import sys
import tempfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import zip_to_repo as z  # noqa: E402

# Bounded so a single run cannot wander into the workflow timeout.
DEFAULT_LIMIT = 5
# Enough history to avoid re-processing, without growing without limit.
MAX_REMEMBERED = 500
# SOQL has a practical ceiling on IN-list size, so chunk the link lookups.
ID_CHUNK = 100

STATE_PATH = Path(os.environ.get("STATE_FILE", ".state/processed.json"))
# Names in here are never published. Committed to the repo so it is editable from
# the GitHub UI without touching code.
IGNORE_PATH = Path(os.environ.get("IGNORE_FILE", "ignore.txt"))


# ------------------------------------------------------------------- ignore list


def load_ignores():
    """
    Filename patterns that must never become a public repo.

    Matching is case-insensitive and accepts substrings or globs, so both
    "front-end-rcm" and "*.zip" do what you would expect. The name checked is the
    ZIP's filename (Salesforce calls it Title), not the extracted folder.
    """
    if not IGNORE_PATH.exists():
        return []
    patterns = []
    for line in IGNORE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            patterns.append(line)
    return patterns


def is_ignored(file_name, patterns):
    name = file_name.lower()
    return any(p in name or fnmatch.fnmatch(name, p) for p in patterns)


# ------------------------------------------------------------------ salesforce


def soql(token, instance_url, query):
    """Query and return records. Raises with the real message on failure."""
    resp = requests.get(
        f"{instance_url}/services/data/{z.SF_API_VERSION}/query",
        headers={"Authorization": f"Bearer {token}"},
        params={"q": query},
        timeout=60,
    )
    body = resp.json()
    # Salesforce returns a LIST of error objects on failure, not a dict.
    if isinstance(body, list):
        raise RuntimeError("; ".join(e.get("message", str(e)) for e in body))
    if resp.status_code != 200:
        raise RuntimeError(f"SOQL failed (HTTP {resp.status_code}): {str(body)[:300]}")
    return body.get("records", [])


def quote_ids(ids):
    return ", ".join("'" + i + "'" for i in ids)


def find_zip_uploads(token, instance_url):
    """
    Return [(contentVersionId, accountId, fileName)] for every ZIP attached to an
    Account, newest first.

    Two queries, not one per file: ContentDocumentLink refuses to be queried
    without an Id filter, so the documents are collected first and looked up in
    batches.
    """
    versions = soql(
        token, instance_url,
        "SELECT Id, Title, ContentDocumentId, CreatedDate, IsLatest "
        "FROM ContentVersion WHERE FileExtension = 'zip' "
        "ORDER BY CreatedDate DESC LIMIT 200",
    )
    # A ContentDocument accumulates versions; only the newest is interesting.
    versions = [v for v in versions if v.get("IsLatest")]
    if not versions:
        return []

    doc_ids = sorted({v["ContentDocumentId"] for v in versions if v.get("ContentDocumentId")})
    account_by_doc = {}
    for start in range(0, len(doc_ids), ID_CHUNK):
        chunk = doc_ids[start:start + ID_CHUNK]
        links = soql(
            token, instance_url,
            "SELECT ContentDocumentId, LinkedEntityId FROM ContentDocumentLink "
            f"WHERE ContentDocumentId IN ({quote_ids(chunk)})",
        )
        for link in links:
            # 001 is the Account key prefix.
            if str(link["LinkedEntityId"]).startswith("001"):
                account_by_doc.setdefault(link["ContentDocumentId"], link["LinkedEntityId"])

    found = []
    for version in versions:
        account_id = account_by_doc.get(version["ContentDocumentId"])
        if account_id:
            found.append((
                version["Id"],
                account_id,
                (version.get("Title") or "upload") + ".zip",
            ))
    return found


# ----------------------------------------------------------------------- state


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            z.log(f"WARNING: {STATE_PATH} is unreadable; starting from empty state.")
    return {"processed": []}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["processed"] = state["processed"][-MAX_REMEMBERED:]
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# ------------------------------------------------------------------------ auth


def authenticate():
    """
    An auth URL or client-credentials pair in CI; the CLI's own session locally.

    All routes yield an (access_token, instance_url) pair.
    """
    if z.SF_AUTH_URL or (z.SF_CLIENT_ID and z.SF_CLIENT_SECRET):
        return z.salesforce_token()

    import local_demo  # local-only convenience; not used on a runner
    token, instance_url, _ = local_demo.sf_cli_auth(os.environ.get("SF_TARGET_ORG", "rcm-dev"))
    return token, instance_url


# ------------------------------------------------------------------------ main


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="list candidates without publishing")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"max ZIPs per run (default {DEFAULT_LIMIT})")
    args = parser.parse_args()

    z.init_log()

    # In CI the token arrives as a secret. Locally it comes from the gitignored
    # file, so fall back to that rather than failing outright.
    if not z.GH_TOKEN:
        try:
            import local_demo
            z.GH_TOKEN = local_demo.gh_token()
            z.GH_OWNER = z.GH_OWNER or local_demo.resolve_owner(z.GH_TOKEN)
        except Exception as exc:
            z.log(f"WARNING: no GitHub token available locally: {exc}")

    z.require_env(["GH_TOKEN", "GH_OWNER"])

    token, instance_url = authenticate()
    z.log(f"Authenticated to Salesforce at {instance_url}")

    state = load_state()
    already = set(state["processed"])

    candidates = find_zip_uploads(token, instance_url)
    z.log(f"Found {len(candidates)} ZIP(s) attached to Accounts.")

    patterns = load_ignores()
    fresh = []
    skipped = set()
    for candidate in candidates:
        if candidate[0] in already:
            continue
        if is_ignored(candidate[2], patterns):
            # Deliberately NOT marked as processed: they stay visible here, and
            # removing the pattern later makes them publish on the next poll.
            skipped.add(candidate[2])
            continue
        fresh.append(candidate)

    z.log(f"{len(fresh)} not yet processed.")
    if skipped:
        z.log(f"{len(skipped)} skipped by {IGNORE_PATH.name}: {', '.join(sorted(skipped))}")

    if args.dry_run:
        for version_id, account_id, file_name in fresh[:args.limit]:
            z.log(f"  would publish {file_name} (version {version_id}) to Account {account_id}")
        return 0

    work = Path(tempfile.mkdtemp(prefix="zip-to-repo-poll-"))
    published = 0

    for version_id, account_id, file_name in fresh[:args.limit]:
        z.log(f"--- {file_name} (Account {account_id})")
        try:
            repo_url, count = z.publish(version_id, file_name, token, instance_url, work)
            z.report(instance_url, token, account_id, "Complete",
                     f"Published {count} files from {file_name}.", repo_url)
            z.log(f"SUCCESS: {count} files -> {repo_url}")
        except z.PublishError as exc:
            z.log(f"FAILED: {exc}")
            z.report(instance_url, token, account_id, "Failed", str(exc))
        except Exception as exc:
            z.log(f"FAILED (unexpected): {exc}")
            z.report(instance_url, token, account_id, "Failed", f"Unexpected error: {exc}")

        # Remembered either way. A file that fails repeatedly would otherwise be
        # retried on every poll, and a permanently-bad ZIP would block the queue.
        state["processed"].append(version_id)
        already.add(version_id)
        published += 1

    save_state(state)
    z.log(f"Done. {published} processed this run.")

    if z._log_handle:
        z._log_handle.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
