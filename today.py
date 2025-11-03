#!/usr/bin/env python3
"""
today.py — Optimized GitHub summary script (uses env vars ACCESS_TOKEN and USER_NAME)

Features:
 - GraphQL requests with retries + backoff
 - Graceful handling of "Resource not accessible by personal access token"
 - JSON cache per-user in ./cache/
 - LOC counting (commits additions/deletions authored by the user)
 - SVG overwrite (expects IDs used in original SVG templates)
"""

from __future__ import annotations
import os
import time
import json
import logging
import hashlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dateutil import relativedelta
from lxml import etree

# ---------------------------
# Configuration (ENV-based)
# ---------------------------
ACCESS_TOKEN = os.environ['ACCESS_TOKEN']     # must be present
USER_NAME = os.environ['USER_NAME']           # must be present

GQL_URL = "https://api.github.com/graphql"
CACHE_DIR = "cache"
SVG_FILES = ["dark_mode.svg", "light_mode.svg"]
VERBOSE = True

# Query counters for diagnostics (keeps parity with original)
QUERY_COUNT = {
    'user_getter': 0,
    'follower_getter': 0,
    'graph_repos_stars': 0,
    'recursive_loc': 0,
    'graph_commits': 0,
    'loc_query': 0,
}

# ---------------------------
# Logging
# ---------------------------
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
    if key in QUERY_COUNT:
        QUERY_COUNT[key] += 1
    else:
        QUERY_COUNT[key] = 1


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
# GraphQL helper (robust)
# ---------------------------

class GitHubAPIError(Exception):
    pass


def graphql_request(query: str, variables: Optional[dict] = None, max_attempts: int = 3) -> dict:
    """
    Run a GraphQL request with retries and handle 'Resource not accessible...' GraphQL errors gracefully.
    Returns the 'data' dict on success. If a forbidden resource error for some nodes is present,
    it logs and returns the data portion (partial) so the script can continue.
    """
    payload = {"query": query, "variables": variables or {}}
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        try:
            resp = SESSION.post(GQL_URL, json=payload, timeout=30)
        except Exception as e:
            logging.warning("GraphQL connection error (attempt %d): %s", attempt, e)
            time.sleep(1.0 * attempt)
            continue

        # If we get a valid JSON body
        try:
            body = resp.json()
        except Exception:
            body = {"errors": [{"message": f"Invalid JSON response: {resp.text[:200]}"}]}

        if resp.status_code == 200:
            # If GraphQL-level errors present
            if "errors" in body:
                errors = body["errors"]
                # If errors are "Resource not accessible by personal access token" -> skip restricted repos
                forbidden = [e for e in errors if isinstance(e.get("message", ""), str) and "Resource not accessible" in e.get("message")]
                if forbidden:
                    logging.warning("Some GraphQL nodes are inaccessible due to token permissions. Skipping those nodes and continuing.")
                    # Return whatever data exists (partial) if present
                    return body.get("data", {})
                # If other GraphQL error, maybe retry a bit (but most GraphQL errors are fatal)
                logging.error("GraphQL errors encountered: %s", errors)
                raise GitHubAPIError(f"GraphQL errors: {errors}")
            # success
            return body.get("data", {})
        elif resp.status_code in (401, 403):
            # 403 could be secondary rate limit or permission problem
            text = resp.text or ""
            logging.warning("HTTP %d from GitHub: %s", resp.status_code, text[:200])
            # If 403 and message indicates abuse or rate-limit, back off
            if "abuse" in text.lower() or "rate limit" in text.lower() or resp.status_code == 429:
                wait = 5 * attempt
                logging.warning("Backing off for %d seconds due to rate limit / abuse.", wait)
                time.sleep(wait)
                continue
            # permission issue - raise
            raise GitHubAPIError(f"HTTP {resp.status_code}: {text}")
        elif resp.status_code >= 500:
            # server error -> backoff and retry
            wait = 2 * attempt
            logging.warning("Server error %d. Retrying after %d seconds.", resp.status_code, wait)
            time.sleep(wait)
            continue
        else:
            raise GitHubAPIError(f"Unexpected HTTP status {resp.status_code}: {resp.text}")

    raise GitHubAPIError("Exceeded maximum GraphQL attempts.")


# ---------------------------
# GitHub-specific helpers
# ---------------------------

def user_getter(username: str) -> Tuple[str, str]:
    """
    Returns (user_id, createdAt)
    """
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    data = graphql_request(query, {"login": username})
    user = data.get('user')
    if not user:
        raise GitHubAPIError("Could not fetch user data.")
    return user['id'], user['createdAt']


def follower_getter(username: str) -> int:
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    data = graphql_request(query, {"login": username})
    followers = data.get('user', {}).get('followers', {}).get('totalCount', 0)
    return int(followers)


def graph_repos_list(login: str, owner_affiliation: List[str], per_page: int = 60) -> List[dict]:
    """
    Returns a flat list of repository edges accessible to the user for the given affiliations.
    We request limited fields (nameWithOwner, defaultBranchRef info, stargazers) to reduce payload.
    """
    query_count('graph_repos_stars')
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String, $perPage: Int!) {
        user(login: $login) {
            repositories(first: $perPage, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            stargazers { totalCount }
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
                pageInfo { endCursor hasNextPage }
            }
        }
    }'''
    edges: List[dict] = []
    cursor = None
    while True:
        variables = {'owner_affiliation': owner_affiliation, 'login': login, 'cursor': cursor, 'perPage': per_page}
        data = graphql_request(query, variables)
        # data might be partial if some nodes were forbidden; guard carefully
        repo_block = data.get('user', {}).get('repositories')
        if not repo_block:
            # No repo data returned; stop
            break
        block_edges = repo_block.get('edges', [])
        edges.extend(block_edges)
        page_info = repo_block.get('pageInfo', {})
        if page_info.get('hasNextPage'):
            cursor = page_info.get('endCursor')
        else:
            break
    return edges


def stars_counter(edges: List[dict]) -> int:
    total = 0
    for e in edges:
        try:
            total += int(e['node']['stargazers']['totalCount'])
        except Exception:
            continue
    return total


# ---------------------------
# LOC counting (per-repo)
# ---------------------------

def recursive_loc_for_repo(owner: str, repo_name: str, owner_id: str) -> Tuple[int, int, int]:
    """
    Paginate commit history (100 per request) and sum additions/deletions plus commit count where author.user.id == owner_id.
    Returns (additions, deletions, commits_by_user)
    """
    query_count('recursive_loc')
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            edges {
                                node {
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
    }'''
    additions = deletions = commits_by_user = 0
    cursor = None
    while True:
        variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
        # Use direct POST with session (avoid throwing early so we can persist cache on failure)
        resp = SESSION.post(GQL_URL, json={'query': query, 'variables': variables}, timeout=30)
        if resp.status_code == 200:
            body = resp.json()
            # skip forbidden nodes gracefully
            if body.get('errors'):
                # If this repo's commit history is forbidden, skip it (return zeros)
                forbidden = any("Resource not accessible" in (e.get('message') or '') for e in body['errors'])
                if forbidden:
                    logging.warning("Skipping inaccessible repo %s/%s due to token permissions.", owner, repo_name)
                    return 0, 0, 0
                raise GitHubAPIError(f"GraphQL errors while fetching commits for {owner}/{repo_name}: {body['errors']}")
            repo = body.get('data', {}).get('repository')
            if not repo or repo.get('defaultBranchRef') is None:
                # empty repo or no default branch
                return 0, 0, 0
            history = repo['defaultBranchRef']['target']['history']
            for edge in history.get('edges', []):
                node = edge.get('node', {})
                author = node.get('author', {}).get('user')
                if author and isinstance(author, dict) and author.get('id') == owner_id:
                    commits_by_user += 1
                    additions += int(node.get('additions') or 0)
                    deletions += int(node.get('deletions') or 0)
            page_info = history.get('pageInfo', {})
            if not page_info.get('hasNextPage'):
                break
            cursor = page_info.get('endCursor')
        elif resp.status_code == 403:
            # probably anti-abuse or permission - treat as skip for safety
            logging.warning("403 while fetching commits for %s/%s — skipping repo to avoid crash.", owner, repo_name)
            return 0, 0, 0
        else:
            raise GitHubAPIError(f"Failed to fetch commits for {owner}/{repo_name}: HTTP {resp.status_code} {resp.text[:200]}")
    return additions, deletions, commits_by_user


# ---------------------------
# Cache (JSON) & aggregation
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
            "commit_count": int(self.commit_count),
            "additions": int(self.additions),
            "deletions": int(self.deletions),
            "my_commits": int(self.my_commits),
            "updated_at": float(self.updated_at),
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
            updated_at=float(d.get("updated_at", time.time()))
        )


def load_cache(username: str) -> Dict[str, RepoCacheItem]:
    ensure_cache_dir()
    path = cache_filename_for_user(username)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return {k: RepoCacheItem.from_dict(v) for k, v in raw.items()}
    except Exception as e:
        logging.warning("Failed to read cache (%s): %s — starting fresh.", path, e)
        return {}


def save_cache(username: str, cache: Dict[str, RepoCacheItem]):
    ensure_cache_dir()
    path = cache_filename_for_user(username)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({k: v.to_dict() for k, v in cache.items()}, fh, indent=2)
    except Exception as e:
        logging.exception("Failed to write cache file %s: %s", path, e)


def build_loc_from_edges(edges: List[dict], owner_id: str, username: str, force_refresh: bool = False) -> Tuple[int, int, int, bool]:
    """
    For each repo edge: check cache; if commit_count changed or forced, run recursive_loc_for_repo.
    Returns (total_additions, total_deletions, net, cached_flag)
    """
    cache = load_cache(username)
    new_cache: Dict[str, RepoCacheItem] = {}
    total_add = total_del = 0
    changed = False

    for edge in edges:
        node = edge.get('node', {})
        name = node.get('nameWithOwner')
        if not name:
            continue
        repo_hash = sha256_hex(name)
        # commit total (may be missing)
        commit_total = 0
        try:
            commit_total = int(node.get('defaultBranchRef', {}).get('target', {}).get('history', {}).get('totalCount', 0) or 0)
        except Exception:
            commit_total = 0

        cached_item = cache.get(repo_hash)
        # re-use cached item if present and commit count matches, unless forced
        if cached_item and not force_refresh and cached_item.commit_count == commit_total:
            new_cache[repo_hash] = cached_item
        else:
            # compute fresh
            owner, repo_name = name.split('/', 1)
            try:
                adds, dels, my_commits = recursive_loc_for_repo(owner, repo_name, owner_id)
            except Exception as e:
                logging.exception("Failed to compute LOC for %s: %s. Using cached/zero fallback.", name, e)
                if cached_item:
                    new_cache[repo_hash] = cached_item
                    adds, dels, my_commits = cached_item.additions, cached_item.deletions, cached_item.my_commits
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

    # save new cache and return
    try:
        save_cache(username, new_cache)
    except Exception:
        logging.exception("Failed to persist updated cache.")

    net = total_add - total_del
    return total_add, total_del, net, not changed


# ---------------------------
# SVG helpers (preserve IDs from original)
# ---------------------------

def find_and_replace(root: etree._ElementTree, element_id: str, new_text: Any):
    elem = root.find(f".//*[@id='{element_id}']")
    if elem is not None:
        elem.text = str(new_text)


def justify_format(root: etree._ElementTree, element_id: str, new_text: Any, length: int = 0):
    if isinstance(new_text, int):
        new_text = f"{new_text:,}"
    new_text = str(new_text)
    find_and_replace(root, element_id, new_text)
    just_len = max(0, length - len(new_text))
    if just_len <= 2:
        dot_map = {0: '', 1: ' ', 2: '. '}
        dot_string = dot_map[just_len]
    else:
        dot_string = ' ' + ('.' * just_len) + ' '
    find_and_replace(root, f"{element_id}_dots", dot_string)


def svg_overwrite(filename: str, age_data: str, commit_data: int, star_data: int, repo_data: int, contrib_data: int, follower_data: int, loc_data_tuple: Tuple[int,int,int]):
    """
    Update values in an SVG (must contain elements with IDs used above).
    loc_data_tuple: (additions, deletions, net)
    """
    try:
        tree = etree.parse(filename)
        root = tree.getroot()
        find_and_replace(root, 'age_data', age_data)
        justify_format(root, 'commit_data', commit_data, 22)
        justify_format(root, 'star_data', star_data, 14)
        justify_format(root, 'repo_data', repo_data, 6)
        justify_format(root, 'contrib_data', contrib_data)
        justify_format(root, 'follower_data', follower_data, 10)
        justify_format(root, 'loc_data', loc_data_tuple[2], 9)
        justify_format(root, 'loc_add', loc_data_tuple[0])
        justify_format(root, 'loc_del', loc_data_tuple[1], 7)
        tree.write(filename, encoding='utf-8', xml_declaration=True)
        logging.info("Wrote SVG: %s", filename)
    except FileNotFoundError:
        logging.warning("SVG file not found: %s — skipping.", filename)
    except Exception:
        logging.exception("Failed to update SVG %s", filename)


# ---------------------------
# Misc helpers
# ---------------------------

def daily_readme(birthday: datetime) -> str:
    diff = relativedelta.relativedelta(datetime.today(), birthday)
    parts = []
    if diff.years:
        parts.append(f"{diff.years} year{'s' if diff.years != 1 else ''}")
    if diff.months:
        parts.append(f"{diff.months} month{'s' if diff.months != 1 else ''}")
    if diff.days or not parts:
        parts.append(f"{diff.days} day{'s' if diff.days != 1 else ''}")
    tail = " 🎂" if (diff.months == 0 and diff.days == 0) else ""
    return ", ".join(parts) + tail


def commit_counter_from_cache(username: str) -> int:
    cache = load_cache(username)
    return sum(item.my_commits for item in cache.values())


# ---------------------------
# Main
# ---------------------------

def main():
    print("Calculation times:")
    start = time.perf_counter()

    # 1. user id & createdAt
    (user_data, user_time) = perf_counter(user_getter, USER_NAME)
    OWNER_ID, acc_date = user_data
    print(f"   account data: {user_time:>10.4f} s")

    # 2. age
    (age_data, age_time) = perf_counter(daily_readme, datetime(2005, 11, 20))
    print(f"   age calculation: {age_time:>8.4f} s")

    # 3. repo edges and LOC (owner+collab+org)
    loc_edges_start = time.perf_counter()
    edges = graph_repos_list(USER_NAME, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], per_page=60)
    # Build LOC using cache
    total_loc, loc_del, net_loc, was_cached = build_loc_from_edges(edges, OWNER_ID, USER_NAME, force_refresh=False)
    loc_time = time.perf_counter() - loc_edges_start
    print(f"   LOC (cached): {loc_time:>12.4f} s" if was_cached else f"   LOC (no cache): {loc_time:>12.4f} s")

    # 4. commit data (from cache)
    commit_data, commit_time = perf_counter(commit_counter_from_cache, USER_NAME)
    print(f"   commit count (cached): {commit_time:>7.4f} s")

    # 5. stars, repos, contribs (owner-only quick counts)
    owner_edges = graph_repos_list(USER_NAME, ['OWNER'], per_page=100)
    star_data = stars_counter(owner_edges)
    repo_data = len(owner_edges)
    contrib_edges = graph_repos_list(USER_NAME, ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'], per_page=100)
    contrib_data = len(contrib_edges)

    # 6. followers
    follower_data, follower_time = perf_counter(follower_getter, USER_NAME)
    print(f"   follower getter: {follower_time:>10.4f} s")

    # Format totals for svg and printing
    # convert ints to formatted string earlier as original did for total_loc
    formatted_loc = [total_loc, loc_del, net_loc]
    for i in range(len(formatted_loc)):
        if isinstance(formatted_loc[i], int):
            formatted_loc[i] = "{:,}".format(formatted_loc[i])

    # 7. overwrite svgs
    for svg in SVG_FILES:
        svg_overwrite(svg, age_data, commit_data, star_data, repo_data, contrib_data, follower_data, (total_loc, loc_del, net_loc))

    # 8. timing summary & query counts
    total_time = time.perf_counter() - start
    print(f"Total function time: {total_time:>10.4f} s")
    print("Total GitHub GraphQL API calls:", sum(QUERY_COUNT.values()))
    for funct_name, count in QUERY_COUNT.items():
        print(f"   {funct_name + ':':<28} {count:>6}")

if __name__ == "__main__":
    main()
