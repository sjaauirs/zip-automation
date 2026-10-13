#!/usr/bin/env python3
"""
Local end-to-end demo: upload a ZIP to a Salesforce Account, then run the real
publish pipeline against it.

Why this exists
---------------
The deployed workflow authenticates with a Connected App (client-credentials
flow). Standing that up takes a round of Setup clicking, and it is worth
proving the pipeline works before doing it. This script authenticates with the
Salesforce CLI's own stored token instead and calls exactly the same publish()
the workflow calls -- so what you see here is the real thing, minus the trigger.

Usage
-----
    python scripts/local_demo.py .tmp/demo-app.zip
    python scripts/local_demo.py .tmp/demo-app.zip --account 001...
    python scripts/local_demo.py .tmp/demo-app.zip --target-org rcm-dev

The GitHub token is read from $GH_TOKEN, or from salesforce/secrets/gh_token.txt
(which is gitignored). It is never printed.
"""

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
import zip_to_repo as z  # noqa: E402

HERE = Path(__file__).resolve().parent
SECRETS_FILE = HERE.parent.parent / "secrets" / "gh_token.txt"


def sf_cli_auth(target_org):
    """
    Borrow the CLI's stored access token. Never logs it.

    `sf org display --verbose` redacts the token -- it comes back literally as
    "[REDACTED] ..." -- so the CLI's dedicated command is used instead.
    """
    # On Windows `sf` is a .cmd shim, which subprocess cannot find without a
    # shell unless we resolve it against PATHEXT first.
    sf_bin = shutil.which("sf") or shutil.which("sf.cmd")
    if not sf_bin:
        raise RuntimeError("The Salesforce CLI ('sf') is not on PATH.")

    meta = subprocess.run(
        [sf_bin, "org", "display", "--target-org", target_org, "--json"],
        capture_output=True, text=True,
    )
    if meta.returncode != 0:
        raise RuntimeError(
            f"Could not read org details. Is the CLI logged in? Try: sf org login web --alias {target_org}\n"
            + meta.stderr.strip()[:300]
        )
    info = json.loads(meta.stdout[meta.stdout.index("{"):])["result"]

    token_result = subprocess.run(
        [sf_bin, "org", "auth", "show-access-token", "--target-org", target_org, "--json"],
        capture_output=True, text=True,
    )
    if token_result.returncode != 0:
        raise RuntimeError(
            "Could not read an access token from the CLI. "
            + token_result.stderr.strip()[:300]
        )
    raw = token_result.stdout
    token = json.loads(raw[raw.index("{"):])["result"]["accessToken"]

    return token, info["instanceUrl"].rstrip("/"), info.get("username")


def gh_token():
    token = os.environ.get("GH_TOKEN", "").strip()
    if token:
        return token
    if SECRETS_FILE.exists():
        token = SECRETS_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    raise RuntimeError(
        "No GitHub token found. Either set GH_TOKEN, or write the token into\n"
        f"  {SECRETS_FILE}\n"
        "(that path is gitignored). The token needs rights to create repos."
    )


def resolve_owner(token):
    """Ask GitHub who owns this token, so the username need not be configured."""
    resp = requests.get(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "zip-to-repo-local",
        },
        timeout=30,
    )
    if resp.status_code == 401:
        raise RuntimeError(
            "GitHub rejected the token (401). Check the token is valid and not expired."
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Could not identify the GitHub user (HTTP {resp.status_code}).")
    return resp.json()["login"]


def api(token, instance_url, method, path, **kwargs):
    return requests.request(
        method,
        f"{instance_url}/services/data/{z.SF_API_VERSION}{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=120,
        **kwargs,
    )


def soql(token, instance_url, query):
    """
    Run a query and return its records.

    On failure Salesforce returns a *list* of error objects rather than a dict,
    so the shape is checked explicitly -- otherwise a permission problem
    surfaces as a confusing "'list' object has no attribute 'get'".
    """
    resp = api(token, instance_url, "GET", "/query", params={"q": query})
    body = resp.json()
    if isinstance(body, list):
        message = "; ".join(e.get("message", str(e)) for e in body)
        raise RuntimeError(f"SOQL failed: {message}")
    if resp.status_code != 200:
        raise RuntimeError(f"SOQL failed (HTTP {resp.status_code}): {str(body)[:300]}")
    return body.get("records", [])


def find_or_create_account(token, instance_url, account_id):
    if account_id:
        resp = api(token, instance_url, "GET", f"/sobjects/Account/{account_id}")
        if resp.status_code != 200:
            raise RuntimeError(f"Account {account_id} not found (HTTP {resp.status_code}).")
        return account_id, resp.json().get("Name")

    records = soql(
        token, instance_url,
        "SELECT Id, Name FROM Account WHERE Name = 'ZIP Repo Test' LIMIT 1",
    )
    if records:
        return records[0]["Id"], records[0]["Name"]

    resp = api(token, instance_url, "POST", "/sobjects/Account", json={"Name": "ZIP Repo Test"})
    if resp.status_code != 201:
        raise RuntimeError(f"Could not create a test Account: {resp.text[:300]}")
    return resp.json()["id"], "ZIP Repo Test"


def upload_zip(token, instance_url, account_id, zip_path):
    """
    Upload the ZIP the same way the UI does: a ContentVersion whose
    FirstPublishLocationId makes Salesforce create the ContentDocumentLink to the
    Account for us.
    """
    blob = base64.b64encode(zip_path.read_bytes()).decode("ascii")
    payload = {
        "Title": zip_path.stem,
        "PathOnClient": zip_path.name,
        "VersionData": blob,
        "FirstPublishLocationId": account_id,
    }
    resp = api(token, instance_url, "POST", "/sobjects/ContentVersion", json=payload)
    if resp.status_code != 201:
        raise RuntimeError(f"Upload failed (HTTP {resp.status_code}): {resp.text[:300]}")

    version_id = resp.json()["id"]
    detail = api(token, instance_url, "GET", f"/sobjects/ContentVersion/{version_id}").json()
    return version_id, detail.get("ContentDocumentId")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("zip", type=Path, help="path to a .zip with a single top-level folder")
    parser.add_argument("--account", help="Account Id to attach to (default: find or create 'ZIP Repo Test')")
    parser.add_argument("--target-org", default="rcm-dev", help="Salesforce CLI alias (default: rcm-dev)")
    parser.add_argument("--skip-upload", action="store_true", help="reuse an already-uploaded --content-version-id")
    parser.add_argument("--content-version-id", help="ContentVersion Id to publish, with --skip-upload")
    args = parser.parse_args()

    z.init_log()
    z.GH_TOKEN = gh_token()
    z.GH_OWNER = os.environ.get("GH_OWNER", "").strip() or resolve_owner(z.GH_TOKEN)
    z.log(f"GitHub target: {z.GH_OWNER}")

    token, instance_url, username = sf_cli_auth(args.target_org)
    z.log(f"Authenticated via CLI as {username} at {instance_url}")

    work = Path(tempfile.mkdtemp(prefix="zip-to-repo-"))
    z.log(f"Work dir: {work}")

    # Bound before the try so the failure paths can still report to the Account.
    account_id = args.account

    try:
        if args.skip_upload:
            if not args.content_version_id:
                raise SystemExit("--skip-upload needs --content-version-id")
            version_id = args.content_version_id
            file_name = args.zip.name
            account_id, account_name = find_or_create_account(token, instance_url, args.account)
            z.log(f"Reusing ContentVersion {version_id} on Account '{account_name}'")
        else:
            if not args.zip.exists():
                raise SystemExit(f"No such file: {args.zip}")
            account_id, account_name = find_or_create_account(token, instance_url, args.account)
            z.log(f"Target Account: {account_name} ({account_id})")
            version_id, doc_id = upload_zip(token, instance_url, account_id, args.zip)
            file_name = args.zip.name
            z.log(f"Uploaded as ContentVersion {version_id} (ContentDocument {doc_id})")

        repo_url, count = z.publish(version_id, file_name, token, instance_url, work)
        z.report(instance_url, token, account_id, "Complete",
                 f"Published {count} files from {file_name}.", repo_url)
        z.log(f"SUCCESS: {count} files -> {repo_url}")
        return 0

    except z.PublishError as exc:
        z.log(f"FAILED: {exc}")
        z.report(instance_url, token, account_id, "Failed", str(exc))
        return 1
    except Exception as exc:
        z.log(f"FAILED (unexpected): {exc}")
        try:
            z.report(instance_url, token, account_id, "Failed", f"Unexpected error: {exc}")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
