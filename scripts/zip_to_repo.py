#!/usr/bin/env python3
"""
Download a ZIP that was attached to a Salesforce Account, unpack it, and publish
its contents as a new public GitHub repo named after the ZIP's top-level folder.

This runs on a GitHub Actions runner. Everything Apex structurally cannot do --
hold a 20 MB+ file, stream it, unpack it, and push many files -- happens here.

Every failure is reported back onto the Account so nothing dies silently in a
workflow log. If Salesforce itself is unreachable we can't do that, so the run
fails loudly instead.
"""

import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

# --------------------------------------------------------------------- config

SF_LOGIN_URL = os.environ.get("SF_LOGIN_URL", "https://login.salesforce.com").rstrip("/")
SF_CLIENT_ID = os.environ.get("SF_CLIENT_ID", "")
SF_CLIENT_SECRET = os.environ.get("SF_CLIENT_SECRET", "")
SF_API_VERSION = os.environ.get("SF_API_VERSION", "v62.0")

# Alternative to the client-credentials pair: an SFDX auth URL, which reuses the
# connected app the Salesforce CLI already registered. Needed on orgs that forbid
# creating new connected apps. Obtain with:
#   sf org auth show-sfdx-auth-url --target-org <alias>
# Treat it like a password -- it is a long-lived refresh token.
SF_AUTH_URL = os.environ.get("SF_AUTH_URL", "").strip()

GH_TOKEN = os.environ.get("GH_TOKEN", "")
GH_OWNER = os.environ.get("GH_OWNER", "")

CONTENT_VERSION_ID = os.environ.get("CONTENT_VERSION_ID", "")
ACCOUNT_ID = os.environ.get("ACCOUNT_ID", "")
FILE_NAME = os.environ.get("FILE_NAME", "upload.zip")

WORK_DIR = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "zip-to-repo"
LOG_FILE = WORK_DIR / "run-log.txt"

# Zip junk that would otherwise defeat the "single top-level folder" check. A zip
# made on macOS is full of this, and it would look like a second root folder.
JUNK_PREFIXES = ("__MACOSX/",)
JUNK_NAMES = (".DS_Store", "Thumbs.db", "desktop.ini")

# How long to wait for a brand-new repo to become pushable.
REPO_READY_TIMEOUT_S = 60


class PublishError(Exception):
    """A failure worth reporting to the user on the Account record."""


# -------------------------------------------------------------------- logging

_log_handle = None


def log(message):
    stamped = f"{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z  {message}"
    print(stamped, flush=True)
    if _log_handle:
        _log_handle.write(stamped + "\n")
        _log_handle.flush()


# ------------------------------------------------------------------ salesforce


def token_from_auth_url(auth_url):
    """
    Exchange an SFDX auth URL for an access token.

        force://<clientId>:<clientSecret>:<refreshToken>@<instanceHost>

    This reuses the connected app the Salesforce CLI already registered, which is
    the only option on orgs that forbid creating new connected apps.
    """
    url = auth_url.strip()
    if not url.startswith("force://"):
        raise RuntimeError("SF_AUTH_URL must be an SFDX auth URL beginning with force://")

    creds, at_sign, host = url[len("force://"):].rpartition("@")
    # Split on at most two colons: a refresh token may itself contain one, and the
    # client secret is routinely empty (the CLI's own app does the refresh-token
    # grant with no secret), so a strict "one or more characters" pattern fails.
    parts = creds.split(":", 2)
    if not at_sign or len(parts) != 3:
        raise RuntimeError(
            "SF_AUTH_URL is not in the expected "
            "force://clientId:clientSecret:refreshToken@host form."
        )
    client_id, client_secret, refresh_token = parts
    host = host.strip().strip("/")

    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    # Only send the secret if there IS one. Sending it empty fails with
    # "invalid_client", because Salesforce treats an empty parameter as a wrong
    # secret rather than as an absent one.
    if client_secret:
        data["client_secret"] = client_secret

    resp = requests.post(f"https://{host}/services/oauth2/token", data=data, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Could not exchange SF_AUTH_URL for an access token (HTTP {resp.status_code}). "
            "The refresh token may have been revoked; re-run: sf org login web"
        )
    body = resp.json()
    return body["access_token"], body["instance_url"].rstrip("/")


def salesforce_token():
    """
    Get a Salesforce access token by whichever route is configured.

    SF_AUTH_URL wins if present. Otherwise the client-credentials flow is used,
    which requires a Connected App with that flow enabled.
    """
    if SF_AUTH_URL:
        return token_from_auth_url(SF_AUTH_URL)

    resp = requests.post(
        f"{SF_LOGIN_URL}/services/oauth2/token",
        data={
            "grant_type": "client_credentials",
            "client_id": SF_CLIENT_ID,
            "client_secret": SF_CLIENT_SECRET,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        # Deliberately does not echo the response body wholesale -- it can contain
        # client id material.
        raise RuntimeError(
            "Could not get a Salesforce token "
            f"(HTTP {resp.status_code}). Check SF_CLIENT_ID / SF_CLIENT_SECRET, that the "
            "Connected App has Client Credentials enabled and a run-as user, and that "
            f"SF_LOGIN_URL ({SF_LOGIN_URL}) is right."
        )
    body = resp.json()
    return body["access_token"], body["instance_url"].rstrip("/")


def report(instance_url, token, account_id, status, message, repo_url=None):
    """
    Write the outcome onto the Account. Best-effort: never masks the real error.

    account_id is a parameter rather than read from the environment, because the
    polling and local entry points do not carry one in ACCOUNT_ID. Passing an
    empty id would PATCH the Account *collection* URL, which Salesforce rejects
    with a confusing "PATCH not allowed, allowed are HEAD, GET, POST".
    """
    if not account_id:
        log("WARNING: no Account id to report to; skipping status write-back.")
        return

    payload = {
        "GitHub_Sync_Status__c": status,
        "GitHub_Sync_Message__c": message[:32000],
        "GitHub_Last_Synced__c": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
    }
    if repo_url:
        payload["GitHub_Repo_URL__c"] = repo_url

    try:
        resp = requests.patch(
            f"{instance_url}/services/data/{SF_API_VERSION}/sobjects/Account/{account_id}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if resp.status_code not in (200, 204):
            log(f"WARNING: could not write status back to Salesforce (HTTP {resp.status_code}): {resp.text[:300]}")
        else:
            log(f"Reported '{status}' to Account {account_id}.")
    except requests.RequestException as exc:
        log(f"WARNING: could not write status back to Salesforce: {exc}")


# ---------------------------------------------------------------- zip handling


def is_junk(name):
    if name.startswith(JUNK_PREFIXES):
        return True
    base = name.rsplit("/", 1)[-1]
    return base in JUNK_NAMES or base.startswith("._")


def top_level_folder(names):
    """
    Return the single top-level folder shared by every entry.

    Raises if the zip has files at its root or more than one root folder -- in
    either case there is no unambiguous folder name to call the repo, and guessing
    would put a wrongly-named public repo on the account.
    """
    roots = set()
    stray_files = []
    for raw in names:
        name = raw.replace("\\", "/")
        if is_junk(name):
            continue

        # Checked before filtering empty segments: archives almost always carry an
        # explicit directory entry for the wrapper folder ("my-app/"), and that
        # trailing slash is the only thing distinguishing it from a root-level file.
        is_dir_entry = name.endswith("/")

        parts = [p for p in name.split("/") if p]
        if not parts:
            continue
        if len(parts) == 1:
            if is_dir_entry:
                roots.add(parts[0])   # the wrapper folder's own entry
            else:
                roots.add(None)       # a file genuinely sitting at the zip root
                stray_files.append(parts[0])
        else:
            roots.add(parts[0])

    if not roots:
        raise PublishError("The ZIP is empty, or contains only system files.")
    if None in roots:
        listed = ", ".join(sorted(stray_files)[:10])
        raise PublishError(
            f"The ZIP has files at its root ({listed}), so there is no folder name to "
            "use. Re-zip it with everything inside a single top-level folder."
        )
    if len(roots) > 1:
        listed = ", ".join(sorted(r for r in roots if r)[:10])
        raise PublishError(
            f"The ZIP has more than one top-level folder ({listed}), so the repo name "
            "is ambiguous. Re-zip it with a single top-level folder."
        )
    return roots.pop()


def slugify_repo_name(name):
    """Coerce a folder name into something GitHub will accept as a repo name."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-.")
    if not cleaned or cleaned in (".", ".."):
        raise PublishError(f"'{name}' cannot be turned into a valid GitHub repo name.")
    if len(cleaned) > 100:
        cleaned = cleaned[:100].strip("-.")
    return cleaned


def safe_target(root, relative):
    """Resolve an entry path, refusing anything that escapes the extraction root."""
    target = (root / relative).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and not str(target).startswith(str(root_resolved) + os.sep):
        raise PublishError(f"The ZIP contains an unsafe path and was not extracted: {relative}")
    return target


def extract(zip_path, prefix, destination):
    """
    Extract, stripping the shared top-level folder so files land at the repo root.

    Also guards against zip-slip and preserves any executable bit the archive
    recorded, which matters for scripts in a build artifact.
    """
    extracted = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if is_junk(name):
                continue

            parts = [p for p in name.split("/") if p]
            if len(parts) < 2:
                continue  # the root folder entry itself

            relative = "/".join(parts[1:])  # drop the wrapper folder
            if not relative:
                continue

            target = safe_target(destination, relative)

            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)

            mode = info.external_attr >> 16
            if mode and mode & stat.S_IXUSR:
                target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

            extracted += 1

    if extracted == 0:
        raise PublishError(
            f"The ZIP contained nothing outside the '{prefix}' folder."
        )
    return extracted


# -------------------------------------------------------------------- github


def gh_headers():
    return {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        # GitHub rejects requests with no User-Agent outright.
        "User-Agent": "zip-to-repo-workflow",
    }


def create_repo(repo_name):
    resp = requests.post(
        "https://api.github.com/user/repos",
        headers=gh_headers(),
        json={"name": repo_name, "private": False, "auto_init": False},
        timeout=30,
    )
    if resp.status_code == 201:
        return resp.json()["html_url"]
    if resp.status_code == 401:
        raise PublishError("GitHub rejected the token (401). GH_REPO_PAT is invalid or expired.")
    if resp.status_code == 403:
        raise PublishError(
            "GitHub refused to create the repo (403). GH_REPO_PAT likely lacks "
            "'Administration: write' (fine-grained) or 'repo' (classic) rights."
        )
    if resp.status_code == 422:
        raise PublishError(
            f"A repo named '{repo_name}' already exists on {GH_OWNER}. Refusing to "
            "overwrite or auto-rename it -- delete it or rename the folder in the ZIP."
        )
    raise PublishError(f"Creating the repo failed with HTTP {resp.status_code}: {resp.text[:300]}")


def run_git(args, cwd):
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise PublishError(f"git {' '.join(args)} failed:\n{result.stderr.strip()[:600]}")
    return result.stdout


def push(repo_name, repo_dir, source_dir, file_name):
    """Commit the extracted files and push them as the repo's first commit."""
    shutil.copytree(source_dir, repo_dir, dirs_exist_ok=True)

    run_git(["init", "-b", "main"], cwd=repo_dir)
    run_git(["config", "user.name", "zip-to-repo"], cwd=repo_dir)
    run_git(["config", "user.email", "zip-to-repo@users.noreply.github.com"], cwd=repo_dir)
    run_git(["add", "-A"], cwd=repo_dir)
    run_git(["commit", "-m", f"Import {file_name} from Salesforce"], cwd=repo_dir)

    # The token rides in the remote URL. Actions masks registered secrets in logs,
    # so a failure here prints *** rather than the token.
    run_git(
        ["remote", "add", "origin", f"https://x-access-token:{GH_TOKEN}@github.com/{GH_OWNER}/{repo_name}.git"],
        cwd=repo_dir,
    )

    # A repo that has just been created is occasionally not immediately pushable.
    deadline = time.time() + REPO_READY_TIMEOUT_S
    while True:
        try:
            run_git(["push", "-u", "origin", "main"], cwd=repo_dir)
            return
        except PublishError as exc:
            if time.time() >= deadline:
                raise
            log(f"Push not accepted yet, retrying: {str(exc)[:200]}")
            time.sleep(5)


# ---------------------------------------------------------------------- main


def init_log():
    """Point the logger at a file the workflow can upload as an artifact."""
    global _log_handle
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    _log_handle = open(LOG_FILE, "w", encoding="utf-8")


def require_env(names):
    values = {
        "SF_CLIENT_ID": SF_CLIENT_ID,
        "SF_CLIENT_SECRET": SF_CLIENT_SECRET,
        "SF_AUTH_URL": SF_AUTH_URL,
        "GH_TOKEN": GH_TOKEN,
        "GH_OWNER": GH_OWNER,
        "CONTENT_VERSION_ID": CONTENT_VERSION_ID,
        "ACCOUNT_ID": ACCOUNT_ID,
    }
    # An auth URL stands in for the whole client-credentials pair.
    if SF_AUTH_URL:
        names = [n for n in names if n not in ("SF_CLIENT_ID", "SF_CLIENT_SECRET")]
    missing = [name for name in names if not values.get(name)]
    if missing:
        raise RuntimeError(
            "Missing required configuration: " + ", ".join(missing)
            + ". Set them as repo secrets/variables on the dispatcher repo."
        )


def publish(content_version_id, file_name, token, instance_url, work_dir):
    """
    Download one ZIP, unpack it, and publish it as a new public repo.

    Returns (repo_url, file_count). Raises PublishError carrying a message meant
    to be written onto the Account.

    Reporting is deliberately left to the caller: the dispatch entry point
    reports once, while the polling entry point reports per file as it goes.
    """
    zip_path = work_dir / "upload.zip"
    extract_dir = work_dir / "contents"
    repo_dir = work_dir / "repo"

    # Polling reuses this directory across several ZIPs in one run, so leftovers
    # from the previous archive would otherwise leak into the next repo.
    for directory in (extract_dir, repo_dir):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)

    url = (
        f"{instance_url}/services/data/{SF_API_VERSION}"
        f"/sobjects/ContentVersion/{content_version_id}/VersionData"
    )
    with requests.get(
        url, headers={"Authorization": f"Bearer {token}"}, stream=True, timeout=120
    ) as resp:
        if resp.status_code == 404:
            raise PublishError(
                "Salesforce could not return the file (404). The run-as user on the "
                "Connected App probably cannot see this ContentVersion."
            )
        if resp.status_code != 200:
            raise PublishError(f"Downloading the ZIP failed with HTTP {resp.status_code}.")
        with open(zip_path, "wb") as handle:
            for chunk in resp.iter_content(chunk_size=1024 * 256):
                handle.write(chunk)

    log(f"Downloaded {zip_path.stat().st_size / (1024 * 1024):.1f} MB")

    if not zipfile.is_zipfile(zip_path):
        raise PublishError("The uploaded file is not a valid ZIP archive.")

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    prefix = top_level_folder(names)
    repo_name = slugify_repo_name(prefix)
    log(f"Top-level folder '{prefix}' -> repo name '{repo_name}'")

    count = extract(zip_path, prefix, extract_dir)
    log(f"Extracted {count} files")

    repo_url = create_repo(repo_name)
    log(f"Created {repo_url}")
    push(repo_name, repo_dir, extract_dir, file_name)
    log(f"Pushed {count} files to {repo_url}")

    return repo_url, count


def main():
    """Dispatch entry point: publish the single ZIP named in the event payload."""
    init_log()
    require_env(["SF_CLIENT_ID", "SF_CLIENT_SECRET", "GH_TOKEN", "GH_OWNER",
                 "CONTENT_VERSION_ID", "ACCOUNT_ID"])

    log(f"Publishing {FILE_NAME} (ContentVersion {CONTENT_VERSION_ID}) to {GH_OWNER}")

    # Authenticate first: without this we have no way to report a failure at all.
    token, instance_url = salesforce_token()
    log(f"Authenticated to Salesforce at {instance_url}")

    try:
        repo_url, count = publish(
            CONTENT_VERSION_ID, FILE_NAME, token, instance_url, WORK_DIR
        )
        report(instance_url, token, ACCOUNT_ID, "Complete",
               f"Published {count} files from {FILE_NAME}.", repo_url)

    except PublishError as exc:
        log(f"FAILED: {exc}")
        report(instance_url, token, ACCOUNT_ID, "Failed", str(exc))
        return 1
    except Exception as exc:  # unexpected -- still tell the user something useful
        log(f"FAILED (unexpected): {exc}")
        report(instance_url, token, ACCOUNT_ID, "Failed", f"Unexpected error: {exc}")
        return 1
    finally:
        if _log_handle:
            _log_handle.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
