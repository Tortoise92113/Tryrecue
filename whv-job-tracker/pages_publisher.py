"""
pages_publisher.py — Generate daily Pages HTMLs and push to GitHub main.

publish_today() generates two reports each run:
  • jobs_YYYY-MM-DD_today.html  — jobs fetched today
  • jobs_YYYY-MM-DD.html        — all active WHV jobs
Previous day's all-jobs file (jobs_YYYY-MM-DD.html, no _today suffix) is
deleted on each run to keep the Pages/ folder tidy.
"""

import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

_log = logging.getLogger("pages_publisher")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PAGES_DIR = _REPO_ROOT / "Pages"

# Matches the all-jobs filename pattern (no _today suffix)
_ALL_FNAME_RE = re.compile(r'^jobs_(\d{4}-\d{2}-\d{2})\.html$')


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(cmd: list[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd), timeout=timeout)
    out = (r.stdout + r.stderr).strip()
    _log.info("[%s] rc=%d  %s", " ".join(cmd[:3]), r.returncode, out or "(no output)")
    return r


def _github_pages_base() -> str:
    """Derive GitHub Pages base URL from the origin remote."""
    try:
        r = _run(["git", "remote", "get-url", "origin"], cwd=_REPO_ROOT, timeout=10)
        url = r.stdout.strip()
        if url.startswith("https://github.com/"):
            path = url.removeprefix("https://github.com/").removesuffix(".git")
        elif url.startswith("git@github.com:"):
            path = url.removeprefix("git@github.com:").removesuffix(".git")
        else:
            _log.warning("unrecognised remote URL: %s", url)
            return ""
        user, repo = path.split("/", 1)
        return f"https://{user}.github.io/{repo}"
    except Exception as exc:
        _log.warning("could not derive GitHub Pages base URL: %s", exc)
        return ""


def _current_branch() -> str:
    try:
        r = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=_REPO_ROOT, timeout=10)
        return r.stdout.strip()
    except Exception:
        return "unknown"


def _find_old_all_jobs_files(pages_dir: Path, today: str) -> list[str]:
    """Find all-jobs snapshot files (no _today suffix) in pages_dir that
    aren't today's, returning their repo-relative paths ("Pages/xxx.html").

    Must be called against the actual push target's Pages/ directory (the
    freshly-checked-out worktree, or _REPO_ROOT when already on main) — NOT
    a local working copy that may have drifted from what's really on GitHub.
    A local-disk-based scan can silently miss files that only exist on the
    remote, permanently orphaning them (see: jobs_2026-07-02.html, which
    never got cleaned up because the local Pages/ folder had lost track of
    it by the time the next run's push happened).
    """
    old_rels: list[str] = []
    if not pages_dir.exists():
        return old_rels
    for p in pages_dir.glob("jobs_*.html"):
        m = _ALL_FNAME_RE.match(p.name)
        if m and m.group(1) != today:
            old_rels.append(f"Pages/{p.name}")
    return old_rels


def _push_via_worktree(
    files_to_write: list[tuple[Path, str]],
    today: str,
    commit_msg: str,
) -> bool:
    """Commit multiple files to remote main using a temporary git worktree.

    Works correctly when the caller is on a feature branch: the worktree is
    based on origin/main so only the data files are pushed.
    """
    wt_dir = Path(tempfile.mkdtemp(prefix="whv-pages-"))
    try:
        r = _run(
            ["git", "worktree", "add", "--detach", str(wt_dir), "origin/main"],
            cwd=_REPO_ROOT, timeout=30,
        )
        if r.returncode != 0:
            _log.error("git worktree add failed — push aborted")
            return False

        # Scan the actual checked-out state of origin/main (not local disk)
        # so stale all-jobs files are always found and cleaned up, even if
        # the local Pages/ folder has lost track of them.
        files_to_delete = _find_old_all_jobs_files(wt_dir / "Pages", today)
        for repo_rel in files_to_delete:
            _log.info("queuing old all-jobs file for remote deletion: %s", repo_rel)

        for src_file, repo_rel in files_to_write:
            dst = wt_dir / repo_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src_file), str(dst))

        for repo_rel in files_to_delete:
            old = wt_dir / repo_rel
            if old.exists():
                old.unlink()

        for cmd, label in [
            (["git", "add", "-A", "Pages/"],                      "git add"),
            (["git", "commit", "-m", commit_msg],                 "git commit"),
            (["git", "push", "origin", "HEAD:refs/heads/main"],   "git push"),
        ]:
            r = _run(cmd, cwd=wt_dir, timeout=60)
            if r.returncode != 0:
                out = r.stdout + r.stderr
                if label == "git commit" and "nothing to commit" in out:
                    _log.info("pages unchanged — no new commit")
                    return True
                _log.error("[%s] failed (rc=%d)", label, r.returncode)
                return False
        return True
    except Exception as exc:
        _log.error("_push_via_worktree error: %s", exc)
        return False
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt_dir)],
            cwd=str(_REPO_ROOT), capture_output=True, timeout=15,
        )
        shutil.rmtree(str(wt_dir), ignore_errors=True)


def _push_direct(
    files_to_write: list[tuple[Path, str]],
    today: str,
    commit_msg: str,
) -> bool:
    """Add, commit, and push when already on the main branch.

    New/updated files are picked up by git add -A Pages/; old all-jobs files
    are staged for deletion via git rm --cached (local copies are kept).
    """
    files_to_delete = _find_old_all_jobs_files(_REPO_ROOT / "Pages", today)
    for repo_rel in files_to_delete:
        _log.info("queuing old all-jobs file for remote deletion: %s", repo_rel)

    cmds: list[tuple[list[str], str]] = [
        (["git", "add", "-A", "Pages/"], "git add"),
    ]
    for rel in files_to_delete:
        cmds.append((["git", "rm", "--cached", "--ignore-unmatch", rel], "git rm"))
    cmds += [
        (["git", "commit", "-m", commit_msg], "git commit"),
        (["git", "push", "origin", "main"],   "git push"),
    ]
    for cmd, label in cmds:
        r = _run(cmd, cwd=_REPO_ROOT, timeout=60)
        if r.returncode != 0:
            out = r.stdout + r.stderr
            if label == "git commit" and "nothing to commit" in out:
                _log.info("pages unchanged — no new commit")
                return True
            _log.error("[%s] failed (rc=%d)", label, r.returncode)
            return False
    return True


# ── public API ────────────────────────────────────────────────────────────────

@dataclass
class PublishResult:
    today_url: str
    all_url: str
    push_ok: bool
    today_count: int
    all_count: int
    today_cities: list[str] = field(default_factory=list)


def publish_today() -> PublishResult:
    """Generate today's WHV job HTMLs, save to Pages/, and push to GitHub main.

    Writes two files:
      - jobs_YYYY-MM-DD_today.html  (jobs fetched today)
      - jobs_YYYY-MM-DD.html        (all active WHV jobs)
    Deletes any previous day's all-jobs file found in Pages/.

    push_ok=False means the git push failed; URLs are still returned so callers
    can display them with a "may not be updated" caveat.
    """
    import storage
    from report import generate_static_report

    today = date.today().strftime("%Y-%m-%d")

    today_fname    = f"jobs_{today}_today.html"
    all_fname      = f"jobs_{today}.html"
    today_repo_rel = f"Pages/{today_fname}"
    all_repo_rel   = f"Pages/{all_fname}"
    today_out      = _PAGES_DIR / today_fname
    all_out        = _PAGES_DIR / all_fname

    pages_base = _github_pages_base()
    today_url  = f"{pages_base}/{today_repo_rel}" if pages_base else ""
    all_url    = f"{pages_base}/{all_repo_rel}" if pages_base else ""

    today_jobs = storage.get_todays_whv_jobs()
    all_jobs   = storage.get_all_whv_jobs()

    _PAGES_DIR.mkdir(parents=True, exist_ok=True)
    today_out.write_text(generate_static_report(today_jobs), encoding="utf-8")
    all_out.write_text(generate_static_report(all_jobs), encoding="utf-8")
    _log.info(
        "pages written: %s (%d jobs), %s (%d jobs)",
        today_fname, len(today_jobs), all_fname, len(all_jobs),
    )

    today_count  = len(today_jobs)
    all_count    = len(all_jobs)
    today_cities = sorted({j["city"] for j in today_jobs if j.get("city")})
    commit_msg   = f"data: jobs report {today}"

    # Which old all-jobs files to delete is decided inside _push_direct /
    # _push_via_worktree, scanning the actual push target — not local disk
    # (see _find_old_all_jobs_files docstring for why).
    files_to_write = [(today_out, today_repo_rel), (all_out, all_repo_rel)]
    branch  = _current_branch()
    push_ok = (
        _push_direct(files_to_write, today, commit_msg)
        if branch == "main"
        else _push_via_worktree(files_to_write, today, commit_msg)
    )

    return PublishResult(
        today_url=today_url,
        all_url=all_url,
        push_ok=push_ok,
        today_count=today_count,
        all_count=all_count,
        today_cities=today_cities,
    )
