# Scripts

## `oss_leaderboard.py`

Generate an OSS PR impact leaderboard for any GitHub repository.

The script uses the GitHub REST API directly. It has no external dependencies and does not require the GitHub CLI.

## Usage

Generate a full leaderboard:

```bash
python3 scripts/oss_leaderboard.py \
  --repo owner/repo \
  --output-dir .
```

Generate a single contributor report:

```bash
python3 scripts/oss_leaderboard.py \
  --repo owner/repo \
  --user username \
  --output-dir reports
```

## Authentication

Set `GITHUB_TOKEN` to avoid low unauthenticated GitHub API limits:

```bash
export GITHUB_TOKEN="..."
```

## Output

For a repository named `owner/example-project`, leaderboard mode writes:

```text
example-project-leaderboard.md
example-project-leaderboard.json
contributors/
```

The GitHub Actions workflow in this repository runs the script daily for Floresta and publishes the generated leaderboard as the root `README.md`.
