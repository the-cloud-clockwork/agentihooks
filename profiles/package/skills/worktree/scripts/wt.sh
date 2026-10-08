#!/usr/bin/env bash
# wt.sh — the one worktree lifecycle for every repo and every agentic session.
#
# Every worktree lives at  $WORKTREE_ROOT/<repo>/<name>  (default ~/dev/worktrees),
# on branch <name>, cut from fresh origin/<base>, where <base> is WT_BASE_BRANCH (default dev). The repo is the primary checkout
# of the git repo containing the current directory (or --repo DIR), so running
# it from inside another worktree still resolves the real repo.
#
# Usage:
#   wt.sh new  [name] [--repo DIR] [--from REF]  # worktree + branch <name> off fresh origin/<base> (or REF); prints the path
#   wt.sh tmp  [name] [--repo DIR] [--from REF]  # throwaway detached worktree under <repo>/_tmp/; prints the path
#   wt.sh ls   [--repo DIR | --all]           # branch, dirty, ahead/behind origin/<base>
#   wt.sh done <name> [--repo DIR] [--force]  # remove a worktree + its local branch, fast-forward the primary checkout to origin/<base>
#              [--pushed]                    # parked work: drop a clean worktree and its local branch once origin holds its head; keep the remote branch
#                                             # (<name> may be a _tmp path printed by 'tmp'; those are removed even if dirty)
#   wt.sh root                                # print the worktree root
#
# new and tmp refuse when free space (the smaller of the worktree filesystem and, on WSL, /mnt/c) is below
# WT_MIN_FREE_GB (50) or the repo already has WT_MAX_PER_REPO (60) worktrees. Both record the calling agent
# session as owner through 'agentihooks lease' when agentihooks is installed.
#
# Names come from code: with agentihooks installed, new and tmp take the name 'agentihooks name' builds for the
# calling session when none is given, and refuse a given name it did not build. Without agentihooks or outside an
# agent session, the given name stands.
set -euo pipefail

die() { echo "wt: $*" >&2; exit 1; }

WORKTREE_ROOT="${WORKTREE_ROOT:-$HOME/dev/worktrees}"
WT_MIN_FREE_GB="${WT_MIN_FREE_GB:-50}"
WT_MAX_PER_REPO="${WT_MAX_PER_REPO:-60}"
BASE="${WT_BASE_BRANCH:-dev}"

cmd="${1:-ls}"; shift || true
NAME=""; BUILT=0; REPO_ARG=""; FORCE=0; PUSHED=0; ALL=0; FROM_REF="origin/${BASE}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO_ARG="${2:-}"; shift 2 ;;
    --from) FROM_REF="${2:-}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --pushed) PUSHED=1; shift ;;
    --all) ALL=1; shift ;;
    -*) die "unknown flag '$1'" ;;
    *) [[ -z "${NAME}" ]] || die "unexpected argument '$1'"; NAME="$1"; shift ;;
  esac
done
[[ "${FORCE}" -eq 1 && "${PUSHED}" -eq 1 ]] && die "--pushed and --force do not combine"

primary_of() {
  local dir="$1" common
  common="$(git -C "${dir}" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" \
    || die "not inside a git repo: ${dir}"
  [[ "$(basename "${common}")" == ".git" ]] || die "bare or unusual repo layout: ${common}"
  dirname "${common}"
}

resolve_repo() {
  REPO="$(primary_of "${REPO_ARG:-$PWD}")"
  REPO_NAME="$(basename "${REPO}")"
  WT_DIR="${WORKTREE_ROOT}/${REPO_NAME}"
}

resolve_name() {
  local out rc=0
  command -v agentihooks >/dev/null 2>&1 || return 0
  if [[ -n "${NAME}" ]]; then
    out="$(agentihooks name "$1" --repo "${REPO}" --check "${NAME}")" || rc=$?
  else
    out="$(agentihooks name "$1" --repo "${REPO}")" || rc=$?
  fi
  case "${rc}" in
    0) [[ -n "${NAME}" ]] || NAME="${out}"; BUILT=1 ;;
    1) die "REFUSED — names come from code; run 'wt.sh ${cmd}' without a name" ;;
  esac
  return 0
}

check_name() {
  [[ -n "${NAME}" ]] || die "usage: wt.sh ${cmd} <name> [--repo DIR]"
  [[ "${NAME}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "invalid name '${NAME}' (letters, digits, . _ -)"
  case "${NAME}" in
    main|master|dev|"${BASE}") die "REFUSED — '${NAME}' is a protected branch name" ;;
  esac
}

free_gb() {
  local probe="$1" best="" kb target
  while [[ ! -d "${probe}" ]]; do probe="$(dirname "${probe}")"; done
  for target in "${probe}" /mnt/c; do
    [[ -d "${target}" ]] || continue
    kb="$(df -Pk "${target}" 2>/dev/null | awk 'NR==2{print $4}')" || continue
    [[ "${kb}" =~ ^[0-9]+$ ]] || continue
    if [[ -z "${best}" || "${kb}" -lt "${best}" ]]; then best="${kb}"; fi
  done
  [[ -n "${best}" ]] && echo $(( best / 1048576 ))
  return 0
}

preflight() {
  local free count
  free="$(free_gb "${WT_DIR}")"
  if [[ -n "${free}" && "${free}" -lt "${WT_MIN_FREE_GB}" ]]; then
    die "REFUSED — ${free} GB free, below the ${WT_MIN_FREE_GB} GB floor (WT_MIN_FREE_GB). Finish worktrees with 'wt.sh done <name>' and run 'agentihooks gc'."
  fi
  count="$(git -C "${REPO}" worktree list --porcelain | awk '/^worktree /{n++} END{print n-1}')"
  if [[ "${count}" -ge "${WT_MAX_PER_REPO}" ]]; then
    die "REFUSED — ${REPO_NAME} already has ${count} worktrees (cap ${WT_MAX_PER_REPO}, WT_MAX_PER_REPO). Finish some with 'wt.sh done <name>' or run 'agentihooks gc'."
  fi
}

lease() {
  command -v agentihooks >/dev/null 2>&1 || return 0
  agentihooks lease "$1" --kind "$2" >/dev/null 2>&1 || true
}

release() {
  command -v agentihooks >/dev/null 2>&1 || return 0
  agentihooks serena release "$1" >/dev/null 2>&1 || true
}

list_repo() {
  local repo="$1"
  git -C "${repo}" fetch origin "${BASE}" --quiet 2>/dev/null || true
  git -C "${repo}" worktree list --porcelain | awk '/^worktree /{print $2}' | while read -r wt; do
    [[ -d "${wt}" ]] || continue
    local br dirty ab tag=""
    br="$(git -C "${wt}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    if [[ -n "$(git -C "${wt}" status -s 2>/dev/null)" ]]; then dirty="yes"; else dirty="-"; fi
    ab="$(git -C "${wt}" rev-list --left-right --count "origin/${BASE}...HEAD" 2>/dev/null | awk '{print "+"$2"/-"$1}' || echo '?')"
    [[ "${wt}" == "${repo}" ]] && tag=" (primary)"
    printf '%-64s %-32s %-6s %s%s\n' "${wt}" "${br}" "${dirty}" "${ab}" "${tag}"
  done
}

case "${cmd}" in
  new)
    resolve_repo; resolve_name worktree; check_name
    DEST="${WT_DIR}/${NAME}"
    [[ -e "${DEST}" ]] && die "worktree path already exists: ${DEST}"
    preflight
    git -C "${REPO}" fetch origin "${BASE}" --quiet || die "cannot fetch origin/${BASE} in ${REPO}"
    if [[ "${FROM_REF}" == origin/* && "${FROM_REF}" != "origin/${BASE}" ]]; then
      git -C "${REPO}" fetch origin "${FROM_REF#origin/}" --quiet || die "cannot fetch ${FROM_REF} in ${REPO}"
    fi
    git -C "${REPO}" rev-parse --verify --quiet "${FROM_REF}^{commit}" >/dev/null || die "unknown ref '${FROM_REF}'"
    mkdir -p "${WT_DIR}"
    if git -C "${REPO}" show-ref --verify --quiet "refs/heads/${NAME}"; then
      git -C "${REPO}" worktree add "${DEST}" "${NAME}" >&2
    else
      git -C "${REPO}" worktree add --no-track -b "${NAME}" "${DEST}" "${FROM_REF}" >&2
    fi
    lease "${DEST}" worktree
    echo "${DEST}"
    ;;

  tmp)
    resolve_repo; resolve_name tmp; check_name
    preflight
    git -C "${REPO}" fetch origin "${BASE}" --quiet || die "cannot fetch origin/${BASE} in ${REPO}"
    mkdir -p "${WT_DIR}/_tmp"
    if [[ "${BUILT}" -eq 1 ]]; then
      DEST="${WT_DIR}/_tmp/${NAME}"
      mkdir "${DEST}" || die "tmp worktree path already exists: ${DEST}"
    else
      DEST="$(mktemp -d "${WT_DIR}/_tmp/${NAME}-XXXX")"
    fi
    if ! git -C "${REPO}" worktree add --detach "${DEST}" "${FROM_REF}" >&2; then
      rmdir "${DEST}"
      die "cannot create a worktree at ${FROM_REF}"
    fi
    lease "${DEST}" ephemeral
    echo "${DEST}"
    ;;

  ls|list)
    printf '%-64s %-32s %-6s %s\n' "PATH" "BRANCH" "DIRTY" "vs origin/${BASE}"
    if [[ "${ALL}" -eq 1 ]]; then
      [[ -d "${WORKTREE_ROOT}" ]] || exit 0
      for d in "${WORKTREE_ROOT}"/*/*/; do
        [[ -e "${d}.git" ]] || continue
        primary_of "${d}"
      done | sort -u | while read -r repo; do list_repo "${repo}"; done
    else
      resolve_repo; list_repo "${REPO}"
    fi
    ;;

  done|rm|remove)
    if [[ "${NAME}" == /* && -z "${REPO_ARG}" ]]; then REPO_ARG="${NAME}"; fi
    resolve_repo
    if [[ "${NAME}" == "${WT_DIR}/_tmp/"* ]]; then NAME="_tmp/${NAME#"${WT_DIR}"/_tmp/}"; fi
    case "${NAME}" in
      _tmp/*)
        [[ "${NAME#_tmp/}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "invalid name '${NAME}'"
        FORCE=1 ;;
      *) check_name ;;
    esac
    TARGET="${WT_DIR}/${NAME}"
    [[ -d "${TARGET}" ]] || die "no such worktree: ${TARGET}"
    BR="$(git -C "${TARGET}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    case "${BR}" in
      main|master|dev|"${BASE}") die "REFUSED — worktree is on protected branch '${BR}'" ;;
    esac
    if [[ -n "$(git -C "${TARGET}" status -s 2>/dev/null)" && "${FORCE}" -ne 1 ]]; then
      echo "wt: '${TARGET}' has uncommitted changes — ship them, or pass --force:" >&2
      git -C "${TARGET}" status -s | sed 's/^/  /' >&2
      exit 1
    fi
    if [[ "${PUSHED}" -eq 1 && "${NAME}" != _tmp/* ]]; then
      REMOTE_HEAD="$(git -C "${REPO}" ls-remote --heads origin "refs/heads/${BR}")" \
        || die "cannot read remote branch '${BR}' — worktree kept"
      [[ -n "${REMOTE_HEAD}" && "${REMOTE_HEAD%%[[:space:]]*}" == "$(git -C "${TARGET}" rev-parse HEAD)" ]] \
        || die "branch '${BR}' is not on origin at the worktree head — worktree kept"
    elif [[ "${NAME}" != _tmp/* ]]; then
      if command -v gh >/dev/null 2>&1; then
        PR_STATE="$(cd "${REPO}" && gh pr list --head "${BR}" --base "${BASE}" --state all --json state,url --jq '.[0] | if .state == "MERGED" then .state elif .state == "CLOSED" then "CLOSED " + .url else .url end')" \
          || die "cannot read pull requests for '${BR}' — worktree kept"
        if [[ "${PR_STATE}" == "CLOSED "* ]]; then
          [[ "${FORCE}" -eq 1 ]] \
            || die "pull request ${PR_STATE#CLOSED } was closed without merging — pass --force to drop the worktree and branch ${BR}"
        else
          [[ -z "${PR_STATE}" || "${PR_STATE}" == MERGED ]] \
            || die "pull request ${PR_STATE} is not merged — wait for it to land before worktree teardown"
          REMOTE_BRANCH="$(git -C "${REPO}" ls-remote --heads origin "refs/heads/${BR}")" \
            || die "cannot read remote branch '${BR}' — worktree kept"
          if [[ -n "${REMOTE_BRANCH}" ]]; then
            [[ "${PR_STATE}" == MERGED ]] \
              || die "published branch '${BR}' has no confirmed merged pull request — worktree kept"
            git -C "${REPO}" push origin --delete "${BR}" \
              || die "cannot delete remote branch '${BR}' — worktree kept"
          fi
        fi
      else
        PUBLISHED=0
        git -C "${REPO}" show-ref --verify --quiet "refs/remotes/origin/${BR}" || PUBLISHED=$?
        [[ "${PUBLISHED}" -eq 1 ]] \
          || die "cannot verify the published branch '${BR}' merged without gh — worktree kept"
      fi
    fi
    release "${TARGET}"
    if [[ "${FORCE}" -eq 1 ]]; then
      git -C "${REPO}" worktree remove --force "${TARGET}"
    else
      git -C "${REPO}" worktree remove "${TARGET}"
    fi
    git -C "${REPO}" worktree prune
    rmdir "${WT_DIR}/_tmp" 2>/dev/null || true
    rmdir "${WT_DIR}" 2>/dev/null || true
    PRIMARY_BR="$(git -C "${REPO}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    if [[ "${PRIMARY_BR}" != "${BASE}" ]]; then
      echo "wt: primary checkout ${REPO} is on '${PRIMARY_BR}', not ${BASE} — local ${BASE} NOT synced" >&2
    elif [[ -n "$(git -C "${REPO}" status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
      echo "wt: primary checkout ${REPO} has uncommitted changes — local ${BASE} NOT synced" >&2
    elif ! git -C "${REPO}" fetch --quiet origin "${BASE}"; then
      echo "wt: 'git fetch origin ${BASE}' failed in ${REPO} — local ${BASE} NOT synced" >&2
    elif git -C "${REPO}" merge --ff-only --quiet "origin/${BASE}"; then
      echo "wt: local ${BASE} synced to origin/${BASE} ($(git -C "${REPO}" rev-parse --short HEAD))"
    else
      echo "wt: 'git merge --ff-only origin/${BASE}' failed in ${REPO} — local ${BASE} NOT synced" >&2
    fi
    if git -C "${REPO}" show-ref --verify --quiet "refs/heads/${BR}"; then
      git -C "${REPO}" fetch origin "${BASE}" --quiet 2>/dev/null || true
      MERGED=0
      [[ -z "$(git -C "${REPO}" rev-list "origin/${BASE}..${BR}" 2>/dev/null)" ]] && MERGED=1
      if [[ "${MERGED}" -eq 0 ]] && command -v gh >/dev/null 2>&1; then
        [[ -n "$(cd "${REPO}" && gh pr list --head "${BR}" --base "${BASE}" --state merged --json number --jq '.[0].number' 2>/dev/null)" ]] && MERGED=1
      fi
      if [[ "${MERGED}" -eq 1 || "${FORCE}" -eq 1 || "${PUSHED}" -eq 1 ]]; then
        git -C "${REPO}" branch -D "${BR}" >/dev/null && echo "wt: removed ${TARGET} and branch ${BR}"
      else
        echo "wt: removed ${TARGET}; branch ${BR} has commits not on origin/${BASE} and no merged PR — kept (--force drops it)" >&2
      fi
    else
      echo "wt: removed ${TARGET}"
    fi
    ;;

  root)
    echo "${WORKTREE_ROOT}"
    ;;

  -h|--help|help)
    sed -n '2,20p' "${BASH_SOURCE[0]}"
    ;;

  *)
    die "unknown command '${cmd}' (new|tmp|ls|done|root|help)"
    ;;
esac
