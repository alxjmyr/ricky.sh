#!/usr/bin/env bash

set -euo pipefail

command -v git >/dev/null 2>&1 || {
    echo "Error: git is required but was not found." >&2
    exit 1
}

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)" || {
    echo "Error: this script must be run from a Git checkout." >&2
    exit 1
}
readonly REPO_ROOT

cd "$REPO_ROOT"

release_changes_pending=false

cleanup() {
    local status=$?
    if [[ $status -ne 0 && $release_changes_pending == true ]]; then
        echo >&2
        echo "Release preparation did not complete; restoring version files." >&2
        git restore --source=HEAD --staged --worktree -- pyproject.toml uv.lock
    fi
}
trap cleanup EXIT

fail() {
    echo "Error: $*" >&2
    exit 1
}

confirm() {
    local prompt=$1
    local answer
    read -r -p "$prompt [y/N] " answer
    [[ $answer == "y" || $answer == "Y" ]]
}

command -v uv >/dev/null 2>&1 || fail "uv is required but was not found."
[[ -t 0 ]] || fail "release preparation requires an interactive terminal."

[[ -z $(git status --porcelain) ]] || fail "the working tree is not clean. Commit or stash changes first."

branch="$(git symbolic-ref --quiet --short HEAD)" || fail "releases cannot be made from a detached HEAD."
upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>/dev/null)" ||
    fail "branch '$branch' does not have an upstream."
remote="$(git config --get "branch.${branch}.remote")"
[[ -n $remote && $remote != "." ]] || fail "branch '$branch' does not have a push remote."
merge_ref="$(git config --get "branch.${branch}.merge")"
[[ $merge_ref == refs/heads/* ]] || fail "branch '$branch' does not track a remote branch."

echo "Checking $remote for upstream changes..."
git fetch "$remote"
behind_count="$(git rev-list --count "HEAD..${upstream}")"
[[ $behind_count == "0" ]] || fail "branch '$branch' is behind '$upstream'; update it before releasing."

current_version="$(uv version --short)"
[[ $current_version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
    fail "project version '$current_version' is not a stable MAJOR.MINOR.PATCH version."

echo "Current version: $current_version"
echo
echo "Select the version component to bump:"
echo "  1) major"
echo "  2) minor"
echo "  3) patch"
echo "  4) cancel"

while true; do
    read -r -p "Choice [1-4]: " choice
    case "$choice" in
        1 | major)
            bump="major"
            break
            ;;
        2 | minor)
            bump="minor"
            break
            ;;
        3 | patch)
            bump="patch"
            break
            ;;
        4 | cancel)
            echo "Release cancelled."
            exit 0
            ;;
        *) echo "Please enter 1, 2, 3, 4, major, minor, patch, or cancel." ;;
    esac
done

proposed_version="$(uv version --bump "$bump" --dry-run --short)"
echo "Proposed release: v${proposed_version} (${bump} bump)"
confirm "Continue?" || {
    echo "Release cancelled."
    exit 0
}

uv version --bump "$bump"
release_changes_pending=true
uv sync

new_version="$(uv version --short)"
[[ $new_version == "$proposed_version" ]] ||
    fail "expected version '$proposed_version' after bump, found '$new_version'."
tag="v${new_version}"

git rev-parse --verify --quiet "refs/tags/${tag}" >/dev/null &&
    fail "tag '$tag' already exists locally."

# A pathspec exclusion is exact: unlike splitting porcelain columns it survives
# quoted paths, paths containing spaces, and rename entries.
unexpected_changes="$(git status --porcelain -- ':(exclude)pyproject.toml' ':(exclude)uv.lock')"
[[ -z $unexpected_changes ]] || {
    echo "Unexpected files changed during release preparation:" >&2
    echo "$unexpected_changes" >&2
    fail "refusing to commit changes outside pyproject.toml and uv.lock."
}

echo
echo "Running release validation..."
uv run pytest
uv run ruff check .
uv run pyright

git add -- pyproject.toml uv.lock
git commit -m "${tag} release prep"
release_changes_pending=false
# Only tracked content matters here. The validation gates above may leave
# untracked artifacts, and aborting on those after the commit would strand an
# untagged release commit.
[[ -z $(git status --porcelain --untracked-files=no) ]] ||
    fail "the working tree is not clean after the release commit; inspect hook changes before tagging."

default_tag_message="Release ${tag}"
# The commit already exists, so an end of input must fall back to the default
# rather than abort under `set -e` and leave the release untagged.
read -r -p "Annotated tag message [${default_tag_message}]: " tag_message || tag_message=""
tag_message="${tag_message:-$default_tag_message}"
git tag -a "$tag" -m "$tag_message"

echo
echo "Release preparation complete:"
echo "  branch: $branch"
echo "  commit: $(git rev-parse --short HEAD) (${tag} release prep)"
echo "  tag:    $tag ($tag_message)"
echo "  remote: $remote"
echo

if confirm "Push the commit and tag to '$remote' now?"; then
    git push --atomic "$remote" "HEAD:${merge_ref}" "refs/tags/${tag}"
    echo "Pushed $branch and $tag to $remote. The release workflow should now run."
else
    echo "Push skipped. The release commit and tag remain local."
    echo "To publish later, run: git push --atomic $remote HEAD:${merge_ref} refs/tags/${tag}"
fi
