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
    Returns the full response dict (data + errors).
    """
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
        # ✅ FIX: Guard against None edges (API can return null edges for deleted/inaccessible repos)
        if not edge:
            continue
        node = edge.get('node') or {}
        if not node:
            continue
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
    elem = root.find(f".//*[@id='{element_id}']")
    if elem is not None:
        elem.text = str(new_text).ljust(length)
