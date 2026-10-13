#!/usr/bin/env python3
"""
Tests for the pure logic in zip_to_repo.py -- the ZIP inspection, naming, and
extraction rules. No credentials or network needed.

Run:  python scripts/test_zip_to_repo.py
"""

import os
import stat
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import zip_to_repo as z  # noqa: E402

PASSED = []
FAILED = []


def check(label, condition, detail=""):
    if condition:
        PASSED.append(label)
        print(f"  PASS  {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label}  {detail}")


def expect_error(label, fn, fragment=""):
    """Assert fn() raises PublishError mentioning fragment."""
    try:
        fn()
    except z.PublishError as exc:
        if fragment and fragment.lower() not in str(exc).lower():
            FAILED.append(label)
            print(f"  FAIL  {label}  message lacked '{fragment}': {exc}")
        else:
            PASSED.append(label)
            print(f"  PASS  {label}")
        return
    except Exception as exc:  # wrong exception type
        FAILED.append(label)
        print(f"  FAIL  {label}  raised {type(exc).__name__}, wanted PublishError: {exc}")
        return
    FAILED.append(label)
    print(f"  FAIL  {label}  expected a PublishError, none raised")


def make_zip(path, entries):
    """entries: list of (name, content_or_None_for_dir)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries:
            if content is None:
                zf.writestr(zipfile.ZipInfo(name), "")
            else:
                zf.writestr(name, content)
    return path


def test_top_level_folder(tmp):
    print("\ntop_level_folder")

    p = make_zip(tmp / "normal.zip", [
        ("my-app/", None),
        ("my-app/index.html", "<html>"),
        ("my-app/assets/app.js", "js"),
        ("my-app/assets/img/logo.png", "png"),
    ])
    with zipfile.ZipFile(p) as zf:
        check("single wrapper folder is detected", z.top_level_folder(zf.namelist()) == "my-app")

    p = make_zip(tmp / "root-files.zip", [
        ("index.html", "<html>"),
        ("app.js", "js"),
    ])
    with zipfile.ZipFile(p) as zf:
        expect_error(
            "files at zip root are rejected",
            lambda: z.top_level_folder(zf.namelist()),
            "no folder name",
        )

    p = make_zip(tmp / "two-folders.zip", [
        ("app-one/index.html", "a"),
        ("app-two/index.html", "b"),
    ])
    with zipfile.ZipFile(p) as zf:
        expect_error(
            "two top-level folders are rejected",
            lambda: z.top_level_folder(zf.namelist()),
            "more than one top-level folder",
        )

    # A macOS zip carries a parallel __MACOSX tree. Without filtering, that looks
    # like a second root folder and every mac-made zip would be rejected.
    p = make_zip(tmp / "macos.zip", [
        ("my-app/index.html", "<html>"),
        ("__MACOSX/my-app/._index.html", "junk"),
        ("my-app/.DS_Store", "junk"),
    ])
    with zipfile.ZipFile(p) as zf:
        check("__MACOSX and .DS_Store are ignored", z.top_level_folder(zf.namelist()) == "my-app")

    p = make_zip(tmp / "only-junk.zip", [
        ("__MACOSX/._thing", "junk"),
    ])
    with zipfile.ZipFile(p) as zf:
        expect_error("a zip of only junk is rejected", lambda: z.top_level_folder(zf.namelist()), "empty")


def test_slugify():
    print("\nslugify_repo_name")
    check("spaces become dashes", z.slugify_repo_name("my app") == "my-app")
    check("case is preserved", z.slugify_repo_name("MyApp") == "MyApp")
    check("dots and dashes survive", z.slugify_repo_name("my.app-2") == "my.app-2")
    check("runs collapse", z.slugify_repo_name("a   b") == "a-b")
    check("edges are trimmed", z.slugify_repo_name("-app-") == "app")
    check("unicode is replaced", z.slugify_repo_name("café-app") == "caf-app")
    check("long names are capped", len(z.slugify_repo_name("x" * 250)) <= 100)
    expect_error("a name of pure punctuation is rejected", lambda: z.slugify_repo_name("///"), "valid")


def test_extract(tmp):
    print("\nextract")

    p = make_zip(tmp / "normal.zip", [
        ("my-app/index.html", "<html>"),
        ("my-app/assets/app.js", "js"),
        ("my-app/deep/a/b/c.txt", "c"),
        ("__MACOSX/my-app/._index.html", "junk"),
    ])
    dest = tmp / "out-normal"
    dest.mkdir()
    count = z.extract(p, "my-app", dest)

    check("all real files extracted", count == 3, f"got {count}")
    check("wrapper folder is stripped", (dest / "index.html").exists())
    check("nesting is preserved", (dest / "deep" / "a" / "b" / "c.txt").exists())
    check("the wrapper folder itself is not recreated", not (dest / "my-app").exists())
    check("macos junk is not extracted", not (dest / "__MACOSX").exists())

    # A zip whose entries try to escape the extraction root must be refused.
    p = make_zip(tmp / "slip.zip", [
        ("my-app/ok.txt", "ok"),
        ("my-app/../../escaped.txt", "bad"),
    ])
    dest = tmp / "out-slip"
    dest.mkdir()
    expect_error("zip-slip is blocked", lambda: z.extract(p, "my-app", dest), "unsafe path")

    # Executable bit should survive, since build artifacts contain scripts.
    # Windows cannot express POSIX permission bits at all -- os.chmod there only
    # toggles the read-only flag -- so this check is only meaningful on the Linux
    # runner the workflow actually uses.
    with zipfile.ZipFile(tmp / "exec.zip", "w") as zf:
        info = zipfile.ZipInfo("my-app/run.sh")
        info.external_attr = (stat.S_IFREG | 0o755) << 16
        zf.writestr(info, "#!/bin/sh\necho hi\n")
    dest = tmp / "out-exec"
    dest.mkdir()
    z.extract(tmp / "exec.zip", "my-app", dest)

    if sys.platform == "win32":
        print("  SKIP  executable bit is preserved  (Windows cannot store POSIX mode bits; "
              "verified on the Linux runner instead)")
    else:
        mode = (dest / "run.sh").stat().st_mode
        check("executable bit is preserved", bool(mode & stat.S_IXUSR), oct(mode))


def test_safe_target(tmp):
    print("\nsafe_target")
    root = tmp / "root"
    root.mkdir()
    check("a normal path resolves", z.safe_target(root, "a/b.txt") == (root / "a/b.txt").resolve())
    expect_error("traversal is rejected", lambda: z.safe_target(root, "../evil.txt"), "unsafe path")
    expect_error("nested traversal is rejected", lambda: z.safe_target(root, "a/../../evil.txt"), "unsafe path")


def main():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_top_level_folder(tmp)
        test_slugify()
        test_extract(tmp)
        test_safe_target(tmp)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for label in FAILED:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
