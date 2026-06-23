"""
pages_publisher.py — Generate the daily Pages HTML and push it to GitHub main.

publish_today() is called from both the getdata pipeline (main.py) and the
dashboard send-report flow (web/app.py _dash_pipeline).  It returns a
PublishResult so callers can embed the Pages URL in the email and warn the
user if the push failed.

The commit always targets the remote main branch, regardless of which local
branch the caller is on.  When running on main the path is direct; when on a
feature branch a temporary git worktree based on origin/main is used so that
only the data file is pushed without carrying over any unmerged feature code.
"""

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

_log = logging.getLogger("pages_publisher")

# pages_publisher.py lives in whv-job-tracker/; repo root is one level up.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_PAGES_DIR = _REPO_ROOT / "Pages"


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


def _push_via_worktree(src_file: Path, repo_rel: str, commit_msg: str) -> bool:
    """Commit src_file to remote main using a temporary git worktree.

    Works correctly even when the caller is on a feature branch: we base the
    worktree on origin/main (detached HEAD), so only the data file is pushed.
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

        dst = wt_dir / repo_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src_file), str(dst))

        for cmd, label in [
            (["git", "add", repo_rel],           "git add"),
            (["git", "commit", "-m", commit_msg], "git commit"),
            (["git", "push", "origin", "HEAD:refs/heads/main"], "git push"),
        ]:
            r = _run(cmd, cwd=wt_dir, timeout=60)
            if r.returncode != 0:
                out = (r.stdout + r.stderr)
                if label == "git commit" and "nothing to commit" in out:
                    _log.info("pages file unchanged — no new commit")
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


def _push_direct(repo_rel: str, commit_msg: str) -> bool:
    """Add, commit, and push when already on the main branch."""
    for cmd, label in [
        (["git", "add", repo_rel],           "git add"),
        (["git", "commit", "-m", commit_msg], "git commit"),
        (["git", "push", "origin", "main"],   "git push"),
    ]:
        r = _run(cmd, cwd=_REPO_ROOT, timeout=60)
        if r.returncode != 0:
            out = (r.stdout + r.stderr)
            if label == "git commit" and "nothing to commit" in out:
                _log.info("pages file unchanged — no new commit")
                return True
            _log.error("[%s] failed (rc=%d)", label, r.returncode)
            return False
    return True


# ── public API ────────────────────────────────────────────────────────────────

@dataclass
class PublishResult:
    pages_url: str
    push_ok: bool
    today_count: int
    today_cities: list[str] = field(default_factory=list)


def publish_today() -> PublishResult:
    """Generate today's WHV job HTML, save to Pages/, and push to GitHub main.

    Returns a PublishResult.  push_ok=False means the push failed; the URL is
    still returned so callers can display it with a "may not be updated" caveat.
    """
    import storage
    from report import generate_static_report

    today    = date.today().strftime("%Y-%m-%d")
    fname    = f"jobs_{today}.html"
    repo_rel = f"Pages/{fname}"
    out_path = _PAGES_DIR / fname

    pages_base = _github_pages_base()
    pages_url  = f"{pages_base}/{repo_rel}" if pages_base else ""

    # Generate HTML from today's WHV jobs
    jobs = storage.get_todays_whv_jobs()
    html = generate_static_report(jobs)

    _PAGES_DIR.mkdir(parents=True, exist_ok=True)
    is_update = out_path.exists()
    out_path.write_text(html, encoding="utf-8")
    _log.info(
        "pages report written: %s  (%d bytes, %d jobs)",
        out_path.name, len(html), len(jobs),
    )

    today_count  = len(jobs)
    today_cities = sorted({j["city"] for j in jobs if j.get("city")})
    commit_msg   = f"data: {'update' if is_update else 'add'} {fname}"

    # Push to main — use worktree if we're not on main
    branch   = _current_branch()
    push_ok  = (
        _push_direct(repo_rel, commit_msg)
        if branch == "main"
        else _push_via_worktree(out_path, repo_rel, commit_msg)
    )

    return PublishResult(
        pages_url=pages_url,
        push_ok=push_ok,
        today_count=today_count,
        today_cities=today_cities,
    )
