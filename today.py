#!/usr/bin/env python3
"""
today.py — GitHub profile stats updater for PR-HARIHARAN
Updates dark_mode.svg and light_mode.svg with live GitHub stats.
"""

from __future__ import annotations
import os
import time
import json
import logging
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, date
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dateutil import relativedelta
from lxml import etree

# ---------------------------
# Configuration (ENV-based)
# ---------------------------
ACCESS_TOKEN = os.environ['ACCESS_TOKEN']
USER_NAME    = os.environ['USER_NAME']

# ✅ Your real date of birth — uptime is calculated from this
DOB = date(2005, 11, 20)

GQL_URL   = "https://api.github.com/graphql"
CACHE_DIR = "cache"
SVG_FILES = ["dark_mode.svg", "light_mode.svg"]
VERBOSE   = True

QUERY_COUNT = {
    'user_getter': 0,
    'graph_repos_stars': 0,
    'recursive_loc': 0,
    'graph_commits': 0,
    'loc_query': 0,
}

logging.basicConfig(
    level=logging.DEBUG if VERBOSE else logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

HEADERS = {'authorization': 'token ' + ACCESS_TOKEN}


# ---------------------------
# Helper utilities
# ---------------------------

def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def cache_filename_for_user(username: str) -> str:
    return os.path.join(CACHE_DIR, sha256_hex(username) + ".json")


def query_count(key: str):
    global QUERY_COUNT
    QUERY_COUNT[key] = QUERY_COUNT.get(key, 0) + 1


def perf_counter(func, *args, **kwargs):
    start = time.perf_counter()
    result = func(*args, **kwargs)
    return result, time.perf_counter() - start


# ---------------------------
# Session + retries
# ---------------------------

def make_session(retries: int = 4, backoff: float = 0.4) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["POST", "GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    return session

SESSION = make_session()


# ---------------------------
# GraphQL helper
# ---------------------------

class GitHubAPIError(Exception):
    pass


def graphql_request(query: str, variables: Optional[dict] = None, max_attempts: int = 3) -> dict:
    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables

    for attempt in range(1, max_attempts + 1):
        try:
            resp = SESSION.post(GQL_URL, json=payload, timeout=30)
        except Exception as exc:
            if attempt == max_attempts:
                raise GitHubAPIError(f"Network error after {max_attempts} attempts: {exc}") from exc
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 200:
            return resp.json()

        if attempt < max_attempts:
            time.sleep(2 ** attempt)
        else:
            raise GitHubAPIError(f"HTTP {resp.status_code}: {resp.text[:300]}")

    raise GitHubAPIError("Exhausted retries")


# ---------------------------
# Data models / cache
# ---------------------------

@dataclass
class RepoCacheItem:
    name_with_owner: str
    repo_hash: str
    commit_count: int
    additions: int
    deletions: int
    my_commits: int

    def to_dict(self) -> dict:
        return {
            "name_with_owner": self.name_with_owner,
            "repo_hash": self.repo_hash,
            "commit_count": self.commit_count,
            "additions": self.additions,
            "deletions": self.deletions,
            "my_commits": self.my_commits,
        }

    @staticmethod
    def from_dict(d: dict) -> "RepoCacheItem":
        return RepoCacheItem(
            name_with_owner=d.get("name_with_owner", ""),
            repo_hash=d.get("repo_hash", ""),
            commit_count=int(d.get("commit_count", 0)),
            additions=int(d.get("additions", 0)),
            deletions=int(d.get("deletions", 0)),
            my_commits=int(d.get("my_commits", 0)),
        )


def load_cache(username: str) -> Dict[str, RepoCacheItem]:
    path = cache_filename_for_user(username)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return {k: RepoCacheItem.from_dict(v) for k, v in raw.items()}
    except Exception as e:
        logging.warning("Could not load cache (%s): %s", path, e)
        return {}


def save_cache(username: str, cache: Dict[str, RepoCacheItem]):
    ensure_cache_dir()
    path = cache_filename_for_user(username)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({k: v.to_dict() for k, v in cache.items()}, fh, indent=2)
    except Exception as e:
        logging.exception("Failed to write cache file %s: %s", path, e)


# ---------------------------
# GitHub API queries
# ---------------------------

def get_user_info(username: str) -> dict:
    """Fetch user id, followers, repo count, contributed repos count."""
    query_count('user_getter')
    query = """
    query($login: String!) {
      user(login: $login) {
        id
        createdAt
        followers { totalCount }
        repositoriesContributedTo(
          first: 1,
          includeUserRepositories: false,
          contributionTypes: [COMMIT, PULL_REQUEST, ISSUE, REPOSITORY]
        ) { totalCount }
        repositories(ownerAffiliations: OWNER, isFork: false, first: 1) {
          totalCount
        }
      }
    }
    """
    result = graphql_request(query, {"login": username})
    return (result.get("data") or {}).get("user") or {}


def graph_repos_stars(username: str) -> Tuple[int, int, List[dict]]:
    """
    Paginate all owned non-fork repos.
    Returns (total_stars, repo_count, edges_list)
    """
    query_count('graph_repos_stars')
    edges = []
    total_stars = 0
    cursor = None

    star_query = """
    query($login: String!, $after: String) {
      user(login: $login) {
        repositories(
          ownerAffiliations: OWNER,
          isFork: false,
          first: 50,
          after: $after,
          orderBy: {field: UPDATED_AT, direction: DESC}
        ) {
          pageInfo { hasNextPage endCursor }
          edges {
            node {
              nameWithOwner
              stargazerCount
              defaultBranchRef {
                target {
                  ... on Commit {
                    history { totalCount }
                  }
                }
              }
            }
          }
        }
      }
    }
    """

    while True:
        variables: dict = {"login": username}
        if cursor:
            variables["after"] = cursor
        result = graphql_request(star_query, variables)
        repos_data = ((result.get("data") or {}).get("user") or {}).get("repositories") or {}
        page_edges = repos_data.get("edges") or []
        for edge in page_edges:
            if not edge:
                continue
            node = edge.get("node") or {}
            if node:
                total_stars += node.get("stargazerCount", 0)
        edges.extend(page_edges)
        page_info = repos_data.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    return total_stars, len(edges), edges


def graph_commits(username: str, created_at_str: str) -> int:
    """
    Count total commits (public + private) across all years since GitHub account creation.
    """
    query_count('graph_commits')
    try:
        created_year = int(created_at_str[:4])
    except Exception:
        created_year = datetime.now().year - 1

    current_year = datetime.now().year
    total_commits = 0

    commit_q = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          totalCommitContributions
          restrictedContributionsCount
        }
      }
    }
    """
    for year in range(created_year, current_year + 1):
        from_dt = f"{year}-01-01T00:00:00Z"
        to_dt   = f"{year}-12-31T23:59:59Z"
        try:
            r = graphql_request(commit_q, {"login": username, "from": from_dt, "to": to_dt})
            cc = (((r.get("data") or {}).get("user") or {}).get("contributionsCollection") or {})
            # ✅ Include both public commits and private/restricted commits
            total_commits += cc.get("totalCommitContributions", 0)
            total_commits += cc.get("restrictedContributionsCount", 0)
        except Exception as e:
            logging.warning("Could not fetch commits for year %d: %s", year, e)

    return total_commits


def recursive_loc_for_repo(owner: str, repo_name: str, owner_id: str) -> Tuple[int, int, int]:
    """
    Paginate all commits in a repo, sum additions/deletions for commits by owner_id.
    Returns (additions, deletions, commit_count).
    """
    query_count('recursive_loc')
    additions = deletions = commit_count = 0
    cursor = None

    loc_query = """
    query($owner: String!, $repo: String!, $after: String) {
      repository(owner: $owner, name: $repo) {
        defaultBranchRef {
          target {
            ... on Commit {
              history(first: 50, after: $after) {
                pageInfo { hasNextPage endCursor }
                edges {
                  node {
                    additions
                    deletions
                    author { user { id } }
                  }
                }
              }
            }
          }
        }
      }
    }
    """

    while True:
        variables: dict = {"owner": owner, "repo": repo_name}
        if cursor:
            variables["after"] = cursor
        try:
            result = graphql_request(loc_query, variables)
        except GitHubAPIError as e:
            logging.warning("LOC query failed for %s/%s: %s", owner, repo_name, e)
            break

        query_count('loc_query')
        target = (
            (((result.get("data") or {}).get("repository") or {})
             .get("defaultBranchRef") or {})
            .get("target") or {}
        )
        history = target.get("history") or {}
        commit_edges = history.get("edges") or []

        for edge in commit_edges:
            if not edge:
                continue
            node = edge.get("node") or {}
            if not node:
                continue
            author_user = ((node.get("author") or {}).get("user") or {})
            if author_user.get("id") == owner_id:
                additions    += node.get("additions", 0)
                deletions    += node.get("deletions", 0)
                commit_count += 1

        page_info = history.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    return additions, deletions, commit_count


def build_loc_from_edges(edges: List[dict], owner_id: str, username: str, force_refresh: bool = False) -> Tuple[int, int, int, bool]:
    """
    For each repo edge, compute or retrieve cached LOC.
    Returns (total_additions, total_deletions, net_loc, was_cached)
    """
    cache = load_cache(username)
    new_cache: Dict[str, RepoCacheItem] = {}
    total_add = total_del = 0
    changed = False

    for edge in edges:
        if not edge:
            continue
        node = edge.get('node') or {}
        if not node:
            continue
        name = node.get('nameWithOwner')
        if not name:
            continue
        repo_hash = sha256_hex(name)
        commit_total = 0
        try:
            commit_total = int(
                (node.get('defaultBranchRef') or {})
                .get('target', {})
                .get('history', {})
                .get('totalCount', 0) or 0
            )
        except Exception:
            commit_total = 0

        cached_item = cache.get(repo_hash)
        if cached_item and not force_refresh and cached_item.commit_count == commit_total:
            new_cache[repo_hash] = cached_item
        else:
            owner_login, repo_name = name.split('/', 1)
            try:
                adds, dels, my_commits = recursive_loc_for_repo(owner_login, repo_name, owner_id)
            except Exception as e:
                logging.exception("Failed LOC for %s: %s. Falling back.", name, e)
                if cached_item:
                    new_cache[repo_hash] = cached_item
                    adds = cached_item.additions
                    dels = cached_item.deletions
                    my_commits = cached_item.my_commits
                else:
                    adds = dels = my_commits = 0
            new_cache[repo_hash] = RepoCacheItem(
                name_with_owner=name,
                repo_hash=repo_hash,
                commit_count=commit_total,
                additions=adds,
                deletions=dels,
                my_commits=my_commits,
            )
            changed = changed or (not cached_item) or (cached_item and cached_item.commit_count != commit_total)

        total_add += new_cache[repo_hash].additions
        total_del += new_cache[repo_hash].deletions

    try:
        save_cache(username, new_cache)
    except Exception:
        logging.exception("Failed to persist cache.")

    net = total_add - total_del
    return total_add, total_del, net, not changed


# ---------------------------
# Uptime from DOB
# ---------------------------

def compute_age_from_dob(dob: date) -> str:
    """
    ✅ Calculates age from real date of birth (DOB = 20 Nov 2005).
    Returns string like '20 years, 3 months, 20 days'
    """
    now = date.today()
    delta = relativedelta.relativedelta(now, dob)
    parts = []
    if delta.years:
        parts.append(f"{delta.years} year{'s' if delta.years != 1 else ''}")
    if delta.months:
        parts.append(f"{delta.months} month{'s' if delta.months != 1 else ''}")
    if delta.days:
        parts.append(f"{delta.days} day{'s' if delta.days != 1 else ''}")
    if not parts:
        parts.append("0 days")
    return ", ".join(parts)


def compute_dots(label_text: str, value_text: str, total_width: int = 48) -> str:
    """
    Compute the dot-padding string to keep alignment consistent in the SVG.
    """
    used = len(label_text) + len(value_text)
    dots = max(3, total_width - used)
    return " " + ("." * dots) + " "


# ---------------------------
# SVG helpers
# ---------------------------

def find_and_replace(root: etree._ElementTree, element_id: str, new_text: Any):
    """Replace the text content of an SVG element by its id."""
    elem = root.find(f".//*[@id='{element_id}']")
    if elem is not None:
        elem.text = str(new_text)
    else:
        logging.warning("SVG element id='%s' not found!", element_id)


def update_svg(
    svg_path: str,
    age_str: str,
    commits: int,
    stars: int,
    followers: int,
    repo_count: int,
    contrib_count: int,
    loc_add: int,
    loc_del: int,
    loc_net: int,
):
    """
    Updates SVG elements by their IDs with live stats.
      - age_data        → uptime/age string
      - star_data       → total stars
      - commit_data     → total commits
      - follower_data   → followers
      - repo_data       → owned repo count
      - contrib_data    → contributed-to repo count
      - loc_data        → net lines of code
      - loc_add         → total additions
      - loc_del         → total deletions
    """
    try:
        parser = etree.XMLParser(remove_blank_text=False)
        tree = etree.parse(svg_path, parser)
    except Exception as e:
        logging.error("Failed to parse SVG %s: %s", svg_path, e)
        return

    find_and_replace(tree, "age_data",      age_str)
    find_and_replace(tree, "star_data",     f"{stars:,}")
    find_and_replace(tree, "commit_data",   f"{commits:,}")
    find_and_replace(tree, "follower_data", f"{followers:,}")
    find_and_replace(tree, "repo_data",     f"{repo_count:,}")
    find_and_replace(tree, "contrib_data",  f"{contrib_count:,}")
    find_and_replace(tree, "loc_data",      f"{loc_net:,}")
    find_and_replace(tree, "loc_add",       f"{loc_add:,}")
    find_and_replace(tree, "loc_del",       f"{loc_del:,}")

    try:
        tree.write(svg_path, xml_declaration=True, encoding="utf-8", pretty_print=False)
        logging.info("✅ Updated SVG: %s", svg_path)
    except Exception as e:
        logging.error("Failed to write SVG %s: %s", svg_path, e)


# ---------------------------
# Main entry point
# ---------------------------

def main():
    logging.info("=== Starting GitHub stats update for: %s ===", USER_NAME)

    # 1. User info
    user_info     = get_user_info(USER_NAME)
    owner_id      = user_info.get("id", "")
    created_at    = user_info.get("createdAt", "")
    followers     = (user_info.get("followers") or {}).get("totalCount", 0)
    repo_count    = ((user_info.get("repositories") or {}).get("totalCount", 0))
    contrib_count = ((user_info.get("repositoriesContributedTo") or {}).get("totalCount", 0))

    logging.info("owner_id=%s | followers=%d | repos=%d | contrib=%d",
                 owner_id, followers, repo_count, contrib_count)

    # 2. ✅ Age from DOB (20 Nov 2005), NOT GitHub account creation date
    age_str = compute_age_from_dob(DOB)
    logging.info("Age (from DOB): %s", age_str)

    # 3. Stars + repo edges for LOC
    total_stars, fetched_repos, edges = graph_repos_stars(USER_NAME)
    logging.info("Stars: %d | Repos fetched: %d", total_stars, fetched_repos)

    # 4. Commits — public + private (from GitHub account creation year)
    total_commits, t = perf_counter(graph_commits, USER_NAME, created_at)
    logging.info("Commits: %d (%.2fs)", total_commits, t)

    # 5. Lines of code
    loc_add, loc_del, loc_net, was_cached = build_loc_from_edges(edges, owner_id, USER_NAME)
    logging.info("LOC add=%d del=%d net=%d cached=%s", loc_add, loc_del, loc_net, was_cached)

    # 6. Update both SVGs
    for svg_path in SVG_FILES:
        if os.path.exists(svg_path):
            update_svg(
                svg_path,
                age_str=age_str,
                commits=total_commits,
                stars=total_stars,
                followers=followers,
                repo_count=repo_count,
                contrib_count=contrib_count,
                loc_add=loc_add,
                loc_del=loc_del,
                loc_net=loc_net,
            )
        else:
            logging.warning("SVG not found: %s", svg_path)

    logging.info("Query counts: %s", QUERY_COUNT)
    logging.info("=== Done ===")


if __name__ == "__main__":
    main()
