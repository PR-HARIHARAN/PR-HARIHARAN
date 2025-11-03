#!/usr/bin/env python3
"""
Optimized GitHub profile summary script.

Requirements:
 - requests
 - lxml
 - python-dateutil

Set environment:
 - ACCESS_TOKEN
 - USER_NAME

Produces: updates to SVGs (dark_mode.svg / light_mode.svg)
"""

from __future__ import annotations
import os
import time
import json
import hashlib
import logging
import datetime
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dateutil import relativedelta
from lxml import etree

# ---------------------------
# Config & Logging
# ---------------------------
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")
USER_NAME = os.environ.get("USER_NAME")
CACHE_DIR = "cache"
SVG_FILES = ["dark_mode.svg", "light_mode.svg"]
# set to True to write more verbose logs
VERBOSE = True

logging.basicConfig(
    level=logging.DEBUG if VERBOSE else logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)

if not ACCESS_TOKEN or not USER_NAME:
    raise RuntimeError("Please set ACCESS_TOKEN and USER_NAME environment variables.")

HEADERS = {"Authorization": f"token {ACCESS_TOKEN}"}
GQL_URL = "https://api.github.com/graphql"

# ---------------------------
# Utilities
# ---------------------------


def make_session(retries: int = 5, backoff_factor: float = 0.6) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["POST", "GET"]),
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.headers.update(HEADERS)
    return s


SESSION = make_session()


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def cache_path_for_user(username: str) -> str:
    return os.path.join(CACHE_DIR, f"{sha256_hex(username)}.json")


# ---------------------------
# GraphQL helper
# ---------------------------


class GitHubAPIError(Exception):
    pass


def graphql_request(
    query: str, variables: Optional[dict] = None, max_retries: int = 3, sleep_on_secondary: bool = True
) -> dict:
    """
    Run a GraphQL POST and handle common failures.
    Retries are handled by the session adapter, but we also handle 403 secondary rate limits.
    """
    payload = {"query": query, "variables": variables or {}}
    for attempt in range(1, max_retries + 1):
        resp = SESSION.post(GQL_URL, json=payload, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if "errors" in data:
                # detect secondary rate limit / abuse block
                for err in data["errors"]:
                    msg = err.get("message", "").lower()
                    if "secondary rate limit" in msg or "abuse" in msg:
                        wait = 10 * attempt
                        logging.warning("Secondary rate limit detected: sleeping %s seconds (attempt %d)", wait, attempt)
                        time.sleep(wait)
                        break
                else:
                    # non-rate-limit GraphQL error
                    raise GitHubAPIError(f"GraphQL errors: {data['errors']}")
                # go to next attempt after backoff
                continue
            return data
        elif resp.status_code in (401, 403):
            # 403 might be real permission issue or rate limit -- try to extract info
            text = resp.text
            if resp.status_code == 403:
                # GitHub sometimes returns 403 on abuse; back off longer
                if sleep_on_secondary:
                    wait = 15 * attempt
                    logging.warning("HTTP 403 from GitHub. Backing off for %d seconds (attempt %d).", wait, attempt)
                    time.sleep(wait)
                    continue
            raise GitHubAPIError(f"HTTP {resp.status_code}: {text}")
        elif resp.status_code >= 500:
            # server error, backoff and retry
            wait = 5 * attempt
            logging.warning("Server error %d. Sleeping %d seconds (attempt %d).", resp.status_code, wait, attempt)
            time.sleep(wait)
            continue
        else:
            raise GitHubAPIError(f"Unexpected status {resp.status_code}: {resp.text}")
    raise GitHubAPIError("Max retries exceeded for GraphQL request.")


# ---------------------------
# GitHub data functions
# ---------------------------


def get_user_id_and_created_at(login: str) -> Tuple[str, str]:
    query = """
    query($login: String!) {
      user(login: $login) {
        id
        createdAt
      }
    }
    """
    res = graphql_request(query, {"login": login})
    user = res.get("data", {}).get("user")
    if not user:
        raise GitHubAPIError("User not found in response.")
    return user["id"], user["createdAt"]


def get_follower_count(login: str) -> int:
    query = """
    query($login: String!) {
      user(login: $login) { followers { totalCount } }
    }
    """
    res = graphql_request(query, {"login": login})
    return int(res["data"]["user"]["followers"]["totalCount"])


def graph_repos_list(
    login: str, owner_affiliation: List[str], per_page: int = 60
) -> List[Dict[str, Any]]:
    """
    Returns list of repositories (edges) the user can access according to affiliations.
    Uses cursor pagination.
    """
    query = """
    query($login: String!, $owner_affiliation: [RepositoryAffiliation], $cursor: String, $perPage: Int!) {
      user(login: $login) {
        repositories(first: $perPage, after: $cursor, ownerAffiliations: $owner_affiliation) {
          edges {
            node {
              nameWithOwner
              defaultBranchRef {
                target {
                  ... on Commit {
                    history {
                      totalCount
                    }
                  }
                }
              }
              stargazers { totalCount }
            }
          }
          pageInfo { endCursor hasNextPage }
        }
      }
    }
    """
    all_edges: List[Dict[str, Any]] = []
    cursor = None
    while True:
        variables = {"login": login, "owner_affiliation": owner_affiliation, "cursor": cursor, "perPage": per_page}
        res = graphql_request(query, variables)
        block = res["data"]["user"]["repositories"]
        edges = block["edges"]
        all_edges.extend(edges)
        if block["pageInfo"]["hasNextPage"]:
            cursor = block["pageInfo"]["endCursor"]
        else:
            break
    return all_edges


def count_stars_from_edges(edges: List[Dict[str, Any]]) -> int:
    return sum(int(edge["node"]["stargazers"]["totalCount"]) for edge in edges)


def count_repos_from_edges(edges: List[Dict[str, Any]]) -> int:
    return len(edges)


# ---------------------------
# LOC / commit history
# ---------------------------


def repo_commit_history_totals(owner: str, repo_name: str) -> int:
    """
    Quickly get total commit count on the default branch (used to compare cache).
    We already retrieve this in graph_repos_list; keep this function if needed separately.
    """
    query = """
    query($owner: String!, $name: String!) {
      repository(owner: $owner, name: $name) {
        defaultBranchRef {
          target {
            ... on Commit {
              history {
                totalCount
              }
            }
          }
        }
      }
    }
    """
    res = graphql_request(query, {"owner": owner, "name": repo_name})
    repo = res.get("data", {}).get("repository")
    if not repo or repo.get("defaultBranchRef") is None:
        return 0
    return repo["defaultBranchRef"]["target"]["history"]["totalCount"]


def recursive_loc_for_repo(owner: str, repo_name: str, owner_id: str) -> Tuple[int, int, int]:
    """
    Paginate commit history (100 per request) and sum additions/deletions authored by owner_id.
    Returns (additions, deletions, commits_by_user)
    """
    query = """
    query($owner: String!, $repo_name: String!, $cursor: String) {
      repository(owner: $owner, name: $repo_name) {
        defaultBranchRef {
          target {
            ... on Commit {
              history(first: 100, after: $cursor) {
                totalCount
                edges {
                  node {
                    ... on Commit {
                      committedDate
                    }
                    author { user { id } }
                    additions
                    deletions
                  }
                }
                pageInfo { endCursor hasNextPage }
              }
            }
          }
        }
      }
    }
    """
    additions = deletions = commits_by_user = 0
    cursor = None
    while True:
        res = graphql_request(query, {"owner": owner, "repo_name": repo_name, "cursor": cursor})
        repo = res.get("data", {}).get("repository")
        if not repo or repo.get("defaultBranchRef") is None:
            break
        history = repo["defaultBranchRef"]["target"]["history"]
        for edge in history.get("edges", []):
            node = edge["node"]
            author = node.get("author", {}).get("user")
            if author and isinstance(author, dict) and author.get("id") == owner_id:
                commits_by_user += 1
                additions += int(node.get("additions") or 0)
                deletions += int(node.get("deletions") or 0)
        if not history["pageInfo"]["hasNextPage"]:
            break
        cursor = history["pageInfo"]["endCursor"]
    return additions, deletions, commits_by_user


# ---------------------------
# Caching (JSON)
# ---------------------------


@dataclass
class RepoCacheItem:
    name_with_owner: str
    repo_hash: str
    commit_count: int = 0
    additions: int = 0
    deletions: int = 0
    my_commits: int = 0
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "name_with_owner": self.name_with_owner,
            "repo_hash": self.repo_hash,
            "commit_count": self.commit_count,
            "additions": self.additions,
            "deletions": self.deletions,
            "my_commits": self.my_commits,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RepoCacheItem":
        return cls(
            name_with_owner=d["name_with_owner"],
            repo_hash=d["repo_hash"],
            commit_count=int(d.get("commit_count", 0)),
            additions=int(d.get("additions", 0)),
            deletions=int(d.get("deletions", 0)),
            my_commits=int(d.get("my_commits", 0)),
            updated_at=float(d.get("updated_at", time.time())),
        )


def load_cache(username: str) -> Dict[str, RepoCacheItem]:
    ensure_cache_dir()
    path = cache_path_for_user(username)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return {k: RepoCacheItem.from_dict(v) for k, v in raw.items()}
    except Exception as e:
        logging.warning("Failed to load cache (%s): %s. Recreating cache.", path, e)
        return {}


def save_cache(username: str, cache: Dict[str, RepoCacheItem]):
    ensure_cache_dir()
    path = cache_path_for_user(username)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({k: v.to_dict() for k, v in cache.items()}, fh, indent=2)


def build_loc_from_repo_edges(edges: List[Dict[str, Any]], owner_id: str, username: str, force_refresh: bool = False) -> Tuple[int, int, int, bool]:
    """
    For each repo edge, check cache and run recursive_loc_for_repo if necessary.
    Returns (total_additions, total_deletions, net, cached_flag)
    """
    cache = load_cache(username)
    new_cache: Dict[str, RepoCacheItem] = {}
    total_add = total_del = 0
    changed = False

    for edge in edges:
        node = edge["node"]
        name = node["nameWithOwner"]
        repo_hash = sha256_hex(name)
        # commit total for default branch (may be missing for empty repos)
        commit_total = 0
        default_ref = node.get("defaultBranchRef")
        if default_ref and default_ref.get("target"):
            commit_total = int(default_ref["target"]["history"].get("totalCount", 0) or 0)

        cached_item = cache.get(repo_hash)
        if cached_item and not force_refresh and cached_item.commit_count == commit_total:
            # reuse
            new_cache[repo_hash] = cached_item
        else:
            # compute recursively
            owner, repo_name = name.split("/", 1)
            try:
                adds, dels, my_commits = recursive_loc_for_repo(owner, repo_name, owner_id)
            except Exception as e:
                logging.exception("Failed to compute LOC for %s: %s", name, e)
                # keep previous cache if present, else zero
                if cached_item:
                    new_cache[repo_hash] = cached_item
                    adds, dels, my_commits = cached_item.additions, cached_item.deletions, cached_item.my_commits
                else:
                    adds, dels, my_commits = 0, 0, 0
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

    # persist new cache
    try:
        save_cache(username, new_cache)
    except Exception as e:
        logging.warning("Failed to save cache: %s", e)

    net = total_add - total_del
    return total_add, total_del, net, not changed


# ---------------------------
# SVG update
# ---------------------------


def find_and_replace(root: etree._ElementTree, element_id: str, text: Any):
    elem = root.find(f".//*[@id='{element_id}']")
    if elem is not None:
        elem.text = str(text)


def justify_format(root: etree._ElementTree, element_id: str, new_text: Any, length: int = 0):
    if isinstance(new_text, int):
        new_text = f"{new_text:,}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: "", 1: " ", 2: ". "}
        dot_string = dot_map[just_len]
    else:
        dot_string = " " + ("." * just_len) + " "
    find_and_replace(root, f"{element_id}_dots", dot_string)


def svg_overwrite(filename: str, age_data: str, commit_data: int, star_data: int, repo_data: int, contrib_data: int, follower_data: int, loc_data: Tuple[int, int, int]):
    """
    Update SVG IDs. If file missing, log and continue.
    loc_data = (additions, deletions, net)
    """
    try:
        tree = etree.parse(filename)
        root = tree.getroot()
        find_and_replace(root, "age_data", age_data)
        justify_format(root, "commit_data", commit_data, 22)
        justify_format(root, "star_data", star_data, 14)
        justify_format(root, "repo_data", repo_data, 6)
        justify_format(root, "contrib_data", contrib_data)
        justify_format(root, "follower_data", follower_data, 10)
        justify_format(root, "loc_data", loc_data[2], 9)
        justify_format(root, "loc_add", loc_data[0])
        justify_format(root, "loc_del", loc_data[1], 7)
        tree.write(filename, encoding="utf-8", xml_declaration=True)
        logging.info("Wrote %s", filename)
    except FileNotFoundError:
        logging.warning("SVG file not found: %s", filename)
    except Exception:
        logging.exception("Failed to update SVG %s", filename)


# ---------------------------
# Misc helpers
# ---------------------------


def daily_readme(birthday: datetime.datetime) -> str:
    diff = relativedelta.relativedelta(datetime.datetime.today(), birthday)
    parts = []
    if diff.years:
        parts.append(f"{diff.years} year{'s' if diff.years != 1 else ''}")
    if diff.months:
        parts.append(f"{diff.months} month{'s' if diff.months != 1 else ''}")
    if diff.days or not parts:
        parts.append(f"{diff.days} day{'s' if diff.days != 1 else ''}")
    tail = " 🎂" if (diff.months == 0 and diff.days == 0) else ""
    return ", ".join(parts) + tail


def perf_counter(fn, *args, **kwargs):
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, (time.perf_counter() - start)


# ---------------------------
# Main workflow
# ---------------------------


def main():
    start_total = time.perf_counter()
    logging.info("Starting summary for %s", USER_NAME)

    # 1) get user id and created at
    (owner_id, created_at), t = perf_counter(get_user_id_and_created_at, USER_NAME)
    logging.info("Fetched user id in %.3fs", t)
    OWNER_ID = owner_id  # for legacy naming

    # age string
    age_str, age_time = perf_counter(daily_readme, datetime.datetime.fromisoformat("2005-11-20"))
    logging.info("Age computed in %.3fs", age_time)

    # 2) get repository edges for OWNER affiliation to compute stars & repos quickly
    edges_owner, edges_time = perf_counter(graph_repos_list, USER_NAME, ["OWNER"])
    logging.info("Fetched repositories (owner) in %.3fs / %d repos", edges_time, len(edges_owner))

    # 3) stars, repos
    star_count = count_stars_from_edges(edges_owner)
    repo_count = count_repos_from_edges(edges_owner)

    # 4) contributions (repos including collaborator / organization member) for contrib_data
    edges_all, edges_all_time = perf_counter(graph_repos_list, USER_NAME, ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"])
    contrib_count = count_repos_from_edges(edges_all)

    # 5) follower count
    follower_count, follower_time = perf_counter(get_follower_count, USER_NAME)
    logging.info("Followers fetched in %.3fs", follower_time)

    # 6) LOC calculations using cache & owner id
    loc_add, loc_del, loc_net, was_cached = build_loc_from_repo_edges(edges_all, OWNER_ID, USER_NAME, force_refresh=False)
    logging.info("LOC: +%d -%d net %d  (cached=%s)", loc_add, loc_del, loc_net, was_cached)

    # 7) commit total (sum of my_commits from cache file)
    cache = load_cache(USER_NAME)
    commit_total = sum(item.my_commits for item in cache.values())

    # 8) overwrite svgs
    try:
        svg_loc_tuple = (loc_add, loc_del, loc_net)
        for svg in SVG_FILES:
            svg_overwrite(svg, age_str, commit_total, star_count, repo_count, contrib_count, follower_count, svg_loc_tuple)
    except Exception:
        logging.exception("SVG update failed.")

    total_time = time.perf_counter() - start_total
    logging.info("Total runtime: %.3f s", total_time)

    # Print summary to stdout (concise)
    print("Summary:")
    print(f"User: {USER_NAME}")
    print(f"Followers: {follower_count:,}")
    print(f"Repos (owner): {repo_count:,}")
    print(f"Stars (owner): {star_count:,}")
    print(f"Contributed repo count: {contrib_count:,}")
    print(f"LOC additions: {loc_add:,}, deletions: {loc_del:,}, net: {loc_net:,}")
    print(f"My commits (cached): {commit_total:,}")
    print(f"Cache hit: {was_cached}")
    print(f"Total runtime: {total_time:.3f}s")


if __name__ == "__main__":
    main()
