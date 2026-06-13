#!/usr/bin/env python3
"""
Generate an OSS PR impact leaderboard for a GitHub repository.

No dependencies. No GitHub CLI. Uses GitHub REST API directly.

Leaderboard:
  python3 oss_leaderboard.py --repo getfloresta/Floresta
  python3 oss_leaderboard.py --repo getfloresta/Floresta --output-dir docs

Single contributor report:
  python3 oss_leaderboard.py --repo getfloresta/Floresta --user JoseSK999

Recommended:
  export GITHUB_TOKEN="..."
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

API = "https://api.github.com"


def github_get(path: str, token: str | None, retries: int = 6) -> Any:
    req = urllib.request.Request(API + path)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")

    if token:
        req.add_header("Authorization", f"Bearer {token}")

    last_error: BaseException | None = None

    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return json.loads(res.read().decode("utf-8"))

        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")

            retry_after = e.headers.get("Retry-After")
            reset = e.headers.get("X-RateLimit-Reset")
            remaining = e.headers.get("X-RateLimit-Remaining")

            should_retry = e.code in {429, 500, 502, 503, 504}

            # GitHub can use 403 for primary or secondary rate limits.
            if e.code == 403 and (
                retry_after
                or remaining == "0"
                or "rate limit" in body.lower()
                or "secondary rate limit" in body.lower()
            ):
                should_retry = True

            if should_retry and attempt < retries:
                if retry_after:
                    wait = int(retry_after)
                elif remaining == "0" and reset:
                    wait = max(1, int(reset) - int(time.time()) + 5)
                else:
                    wait = min(60, 2**attempt)

                print(
                    f"GitHub HTTP {e.code}. Retrying in {wait}s "
                    f"({attempt}/{retries})...",
                    file=sys.stderr,
                )
                time.sleep(wait)
                continue

            raise SystemExit(f"GitHub API error {e.code}: {body}") from e

        except (
            urllib.error.URLError,
            TimeoutError,
            socket.timeout,
            socket.gaierror,
            ConnectionError,
        ) as e:
            last_error = e

            if attempt < retries:
                wait = min(60, 2**attempt)
                print(
                    f"Network error: {e}. Retrying in {wait}s ({attempt}/{retries})...",
                    file=sys.stderr,
                )
                time.sleep(wait)
                continue

    raise SystemExit(f"Network error after {retries} attempts: {last_error}")


def github_get_pages(path: str, token: str | None) -> list[Any]:
    all_items: list[Any] = []

    for page in range(1, 10_000):
        sep = "&" if "?" in path else "?"
        data = github_get(f"{path}{sep}per_page=100&page={page}", token)

        if not isinstance(data, list):
            raise SystemExit(f"Expected list response from GitHub for: {path}")

        all_items.extend(data)

        if len(data) < 100:
            break

    return all_items


def fetch_prs(owner: str, repo_name: str, token: str | None) -> list[dict[str, Any]]:
    print("Fetching PRs...", file=sys.stderr)

    prs = github_get_pages(
        f"/repos/{owner}/{repo_name}/pulls?state=all",
        token,
    )

    return [pr for pr in prs if isinstance(pr, dict)]


def fetch_reviewers(
    owner: str,
    repo_name: str,
    prs: list[dict[str, Any]],
    token: str | None,
) -> dict[int, set[str]]:
    reviewers_by_pr: dict[int, set[str]] = {}

    ordered_prs = sorted(prs, key=lambda pr: int(pr["number"]))

    for i, pr in enumerate(ordered_prs, start=1):
        number = int(pr["number"])
        print(
            f"Fetching reviews for PR #{number} ({i}/{len(ordered_prs)})...",
            file=sys.stderr,
        )

        reviews = github_get_pages(
            f"/repos/{owner}/{repo_name}/pulls/{number}/reviews",
            token,
        )

        reviewers: set[str] = set()

        for review in reviews:
            if not isinstance(review, dict):
                continue

            # GitHub can return null users for deleted/ghost accounts.
            # Skip them instead of attributing them to a fake aggregate user.
            login = (review.get("user") or {}).get("login")
            if login:
                reviewers.add(login)

        reviewers_by_pr[number] = reviewers

    return reviewers_by_pr


def day(value: str | None) -> str:
    return value[:10] if value else ""


def is_merged(pr: dict[str, Any]) -> bool:
    return bool(pr.get("merged_at"))


def is_bot(login: str) -> bool:
    return login.endswith("[bot]")


def pr_author(pr: dict[str, Any]) -> str:
    # Deleted/ghost users may appear as null in GitHub API responses.
    return (pr.get("user") or {}).get("login", "")


def pr_link(pr: dict[str, Any]) -> str:
    title = str(pr["title"]).replace("|", "\\|").replace("\n", " ")
    return f"[#{pr['number']} — {title}]({pr['html_url']})"


def pr_status(pr: dict[str, Any]) -> str:
    if pr.get("draft"):
        return "📝 draft"
    if is_merged(pr):
        return "✅ merged"
    if pr.get("state") == "closed":
        return "❌ closed"
    return "🟢 open"


def relation(pr: dict[str, Any], user: str, reviewed_numbers: set[int]) -> str:
    if pr_author(pr).lower() == user.lower():
        return "✍️ author"

    if int(pr["number"]) in reviewed_numbers:
        return "👀 reviewed"

    return "—"


def current_attention_streak(
    merged_prs: list[dict[str, Any]],
    attention_numbers: set[int],
) -> int:
    streak = 0

    for pr in merged_prs:
        if int(pr["number"]) in attention_numbers:
            streak += 1
        else:
            break

    return streak


def current_impact_streak_for_user(
    user: str,
    merged_prs: list[dict[str, Any]],
    reviewers_by_pr: dict[int, set[str]],
) -> int:
    user_lc = user.lower()
    streak = 0

    for pr in merged_prs:
        number = int(pr["number"])
        is_author = pr_author(pr).lower() == user_lc
        is_reviewer = any(
            reviewer.lower() == user_lc
            for reviewer in reviewers_by_pr.get(number, set())
        )

        if is_author or is_reviewer:
            streak += 1
        else:
            break

    return streak


def project_name(repo: str) -> str:
    return repo.split("/", 1)[1]


def project_slug(repo: str) -> str:
    name = project_name(repo).lower()
    return re.sub(r"[^a-z0-9]+", "-", name).strip("-") or "repo"


def write_personal_report(
    output: Path,
    repo: str,
    user: str,
    prs: list[dict[str, Any]],
    reviewers_by_pr: dict[int, set[str]],
) -> None:
    user_lc = user.lower()

    authored_numbers = {
        int(pr["number"]) for pr in prs if pr_author(pr).lower() == user_lc
    }

    reviewed_numbers = {
        number
        for number, reviewers in reviewers_by_pr.items()
        if any(reviewer.lower() == user_lc for reviewer in reviewers)
    }

    attention_numbers = authored_numbers | reviewed_numbers

    attention_prs = [pr for pr in prs if int(pr["number"]) in attention_numbers]

    merged_prs = sorted(
        [pr for pr in prs if is_merged(pr)],
        key=lambda pr: pr.get("merged_at") or "",
        reverse=True,
    )

    merged_with_attention = [
        pr for pr in merged_prs if int(pr["number"]) in attention_numbers
    ]

    streak = current_attention_streak(merged_prs, attention_numbers)

    lines: list[str] = []

    lines.append(f"# @{user} impact report")
    lines.append("")
    lines.append(f"- Repository: [`{repo}`](https://github.com/{repo})")
    lines.append(f"- Generated: {date.today().isoformat()}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Count |")
    lines.append("|---|---:|")
    lines.append(f"| ✅ Project merged PRs | {len(merged_prs)} |")
    lines.append(f"| 🎯 Merged PRs with my impact | {len(merged_with_attention)} |")
    lines.append(f"| 🔥 [Current impact streak](#all-merged-prs) | **{streak}** |")
    lines.append("")
    lines.append(
        "> Streak definition: starting from the newest merged PR, count consecutive merged PRs that were authored or reviewed by me. The streak stops at the first merged PR without my impact."
    )
    lines.append("")

    lines.append("## PRs with my attention")
    lines.append("")
    lines.append("Ordered newest first by PR number.")
    lines.append("")
    lines.append("| # | Status | Relation | PR | Created | Closed / merged |")
    lines.append("|---:|---|---|---|---|---|")

    for pr in sorted(attention_prs, key=lambda p: int(p["number"]), reverse=True):
        closed_or_merged = day(pr.get("merged_at")) or day(pr.get("closed_at"))
        lines.append(
            f"| {pr['number']} | {pr_status(pr)} | {relation(pr, user, reviewed_numbers)} | "
            f"{pr_link(pr)} | {day(pr.get('created_at'))} | {closed_or_merged} |"
        )

    lines.append("")
    lines.append("## All merged PRs")
    lines.append("")
    lines.append(
        "Ordered newest merged first, because this is the order used for the streak."
    )
    lines.append("")
    lines.append("| Merged | # | My attention? | Relation | PR |")
    lines.append("|---|---:|---|---|---|")

    for pr in merged_prs:
        number = int(pr["number"])
        has_attention = number in attention_numbers
        lines.append(
            f"| {day(pr.get('merged_at'))} | {number} | {'✅ yes' if has_attention else '— no'} | "
            f"{relation(pr, user, reviewed_numbers)} | {pr_link(pr)} |"
        )

    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_leaderboard(
    prs: list[dict[str, Any]],
    reviewers_by_pr: dict[int, set[str]],
) -> list[dict[str, Any]]:
    eligible_users = {
        pr_author(pr)
        for pr in prs
        if is_merged(pr) and pr_author(pr) and not is_bot(pr_author(pr))
    }

    stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "user": "",
            "avatar_url": "",
            "authored": 0,
            "authored_merged": 0,
            "reviewed": 0,
            "reviewed_merged": 0,
            "merged_impact": 0,
            "current_streak": 0,
        }
    )

    for user in eligible_users:
        stats[user]["user"] = user

    for pr in prs:
        number = int(pr["number"])
        author = pr_author(pr)
        merged = is_merged(pr)

        if author in eligible_users:
            if not stats[author]["avatar_url"]:
                # Same null-user guard as pr_author(); normally present for eligible authors.
                stats[author]["avatar_url"] = (pr.get("user") or {}).get(
                    "avatar_url", ""
                )

            stats[author]["authored"] += 1

            if merged:
                stats[author]["authored_merged"] += 1
                stats[author]["merged_impact"] += 1

        for reviewer in reviewers_by_pr.get(number, set()):
            if reviewer not in eligible_users:
                continue

            if reviewer == author:
                continue

            stats[reviewer]["reviewed"] += 1

            if merged:
                stats[reviewer]["reviewed_merged"] += 1
                stats[reviewer]["merged_impact"] += 1

    merged_prs = sorted(
        [pr for pr in prs if is_merged(pr)],
        key=lambda pr: pr.get("merged_at") or "",
        reverse=True,
    )

    rows = list(stats.values())

    for row in rows:
        row["current_streak"] = current_impact_streak_for_user(
            row["user"],
            merged_prs,
            reviewers_by_pr,
        )

    rows.sort(
        key=lambda row: (
            row["merged_impact"],
            row["authored_merged"],
            row["reviewed_merged"],
            row["authored"],
            row["reviewed"],
        ),
        reverse=True,
    )

    return rows


def rank_label(rank: int) -> str:
    if rank == 1:
        return "🏆"
    if rank == 2:
        return "🥈"
    if rank == 3:
        return "🥉"
    return str(rank)


def contributor_cell(user: str, avatar_url: str, report_url: str | None = None) -> str:
    github_url = f"https://github.com/{user}"
    name_url = report_url or github_url

    if avatar_url:
        sep = "&" if "?" in avatar_url else "?"
        small_avatar = f"{avatar_url}{sep}s=20"

        return (
            f'<a href="{github_url}">'
            f'<img src="{small_avatar}" width="20" height="20"></a> '
            f"[@{user}]({name_url})"
        )

    return f"[@{user}]({name_url})"


def write_leaderboard(
    output_md: Path,
    output_json: Path,
    repo: str,
    rows: list[dict[str, Any]],
) -> None:
    lines: list[str] = []

    lines.append(f"# {project_name(repo)} impact leaderboard")
    lines.append("")
    lines.append(f"- Repository: [`{repo}`](https://github.com/{repo})")
    lines.append(f"- Generated: {date.today().isoformat()}")
    lines.append("")
    lines.append(
        "> Merged impact 🎯 = merged PRs *authored* ✍️ + others' merged PRs *reviewed* 👀"
    )
    lines.append(">")
    lines.append(
        "> Note: only formal GitHub reviews are counted; regular PR discussion comments are excluded."
    )
    lines.append("")
    lines.append(
        "Click a contributor name to open their detailed report. Click the avatar to open their GitHub profile."
    )
    lines.append("")

    streak_rows = sorted(
        [row for row in rows if row["current_streak"] > 0],
        key=lambda row: (
            row["current_streak"],
            row["merged_impact"],
            row["authored_merged"],
            row["reviewed_merged"],
        ),
        reverse=True,
    )

    lines.append("## Current impact streaks")
    lines.append("")
    lines.append(
        "Consecutive newest merged PRs authored or formally reviewed by each contributor."
    )
    lines.append("")
    lines.append("| Rank | Contributor | Current streak | Merged impact |")
    lines.append("|---:|---|---:|---:|")

    for rank, row in enumerate(streak_rows, start=1):
        user = row["user"]
        report_url = f"contributors/{user}.md"
        lines.append(
            f"| {rank_label(rank)} | {contributor_cell(user, row['avatar_url'], report_url)} | "
            f"🔥 **{row['current_streak']}** | "
            f"{row['merged_impact']} |"
        )

    lines.append("")
    lines.append("## Full history leaderboard")
    lines.append("")
    lines.append(
        "| Rank | Contributor | Merged impact | Merged authored | Merged reviewed | Total authored | Total reviewed |"
    )
    lines.append("|---:|---|---:|---:|---:|---:|---:|")

    for rank, row in enumerate(rows, start=1):
        user = row["user"]
        report_url = f"contributors/{user}.md"
        lines.append(
            f"| {rank_label(rank)} | {contributor_cell(user, row['avatar_url'], report_url)} | "
            f"{row['merged_impact']} | "
            f"{row['authored_merged']} | "
            f"{row['reviewed_merged']} | "
            f"{row['authored']} | "
            f"{row['reviewed']} |"
        )

    output_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output_json.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repo",
        required=True,
        help="GitHub repository in owner/name format, for example getfloresta/Floresta.",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="Generate a single contributor report instead of the full leaderboard.",
    )
    parser.add_argument("--output-dir", default="reports")
    args = parser.parse_args()

    if "/" not in args.repo:
        raise SystemExit(
            "--repo must look like owner/name, for example getfloresta/Floresta"
        )

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            "Warning: GITHUB_TOKEN is not set. You may hit GitHub rate limits.",
            file=sys.stderr,
        )

    owner, repo_name = args.repo.split("/", 1)

    prs = fetch_prs(owner, repo_name, token)
    reviewers_by_pr = fetch_reviewers(owner, repo_name, prs, token)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    slug = project_slug(args.repo)

    if args.user:
        output = (
            output_dir
            / f"{slug}-{args.user}-impact-report-{date.today().isoformat()}.md"
        )

        write_personal_report(
            output=output,
            repo=args.repo,
            user=args.user,
            prs=prs,
            reviewers_by_pr=reviewers_by_pr,
        )

        print(f"Wrote {output}")
        return

    rows = build_leaderboard(prs, reviewers_by_pr)

    output_md = output_dir / f"{slug}-leaderboard.md"
    output_json = output_dir / f"{slug}-leaderboard.json"

    write_leaderboard(output_md, output_json, args.repo, rows)

    contributors_dir = output_dir / "contributors"
    contributors_dir.mkdir(parents=True, exist_ok=True)

    for row in rows:
        user = row["user"]
        report_path = contributors_dir / f"{user}.md"
        write_personal_report(
            output=report_path,
            repo=args.repo,
            user=user,
            prs=prs,
            reviewers_by_pr=reviewers_by_pr,
        )

    print(f"Wrote {output_md}")
    print(f"Wrote {output_json}")
    print(f"Wrote contributor reports to {contributors_dir}")


if __name__ == "__main__":
    main()
