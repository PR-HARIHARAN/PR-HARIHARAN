#!/usr/bin/env python3
"""
today.py — Optimized GitHub Summary Generator
Author: @yourname
"""

import os
import time
import json
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# =============================
# CONFIG & LOGGING
# =============================
load_dotenv()

USER_NAME = os.getenv("USER_NAME")
TOKEN = os.getenv("ACCESS_TOKEN")
SVG_PATH = Path("output.svg")
CACHE_FILE = Path(".cache.json")
GRAPHQL_URL = "https://api.github.com/graphql"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

HEADERS = {"Authorization": f"Bearer {TOKEN}"}
MAX_WORKERS = 6
REQUEST_TIMEOUT = 30

# =============================
# UTILS
# =============================

def graphql_request(query: str, variables: dict = None):
    """Perform a GitHub GraphQL request with retry and error handling."""
    for attempt in range(3):
        resp = requests.post(
            GRAPHQL_URL,
            headers=HEADERS,
            json={"query": query, "variables": variables or {}},
            timeout=REQUEST_TIMEOUT,
        )
        data = resp.json()

        if "errors" in data:
            errors = data["errors"]
            skip_errors = [
                e for e in errors if "Resource not accessible" in e.get("message", "")
            ]
            if skip_errors:
                logging.warning("⚠️ Skipping some inaccessible repos.")
                return data.get("data", {})
            if attempt < 2:
                logging.warning(f"Retrying due to GraphQL error: {errors}")
                time.sleep(2)
                continue
            raise Exception(f"GraphQL failed: {errors}")

        return data.get("data", {})

    raise Exception("GraphQL request failed after retries.")

# =============================
# CORE QUERIES
# =============================

def fetch_user_id(user: str):
    query = """
    query($login: String!) {
      user(login: $login) { id name createdAt }
    }
    """
    data = graphql_request(query, {"login": user})
    return data["user"]

def fetch_repo_edges(user: str, affiliation: str = "OWNER"):
    query = """
    query($login: String!, $affiliations: [RepositoryAffiliation]) {
      user(login: $login) {
        repositories(first: 100, ownerAffiliations: $affiliations, privacy: PUBLIC) {
          totalCount
          edges {
            node {
              name
              stargazerCount
              forkCount
              defaultBranchRef {
                name
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
        }
      }
    }
    """
    return graphql_request(query, {"login": user, "affiliations": [affiliation]})

def get_commit_count(edges):
    """Sum commit counts from repo edges."""
    total = 0
    for edge in edges:
        try:
            total += edge["node"]["defaultBranchRef"]["target"]["history"]["totalCount"]
        except Exception:
            continue
    return total

# =============================
# PARALLEL FETCHING
# =============================

def fetch_all_repos(user):
    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_repo_edges, user, aff): aff
            for aff in ["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"]
        }
        for future in as_completed(futures):
            aff = futures[future]
            try:
                res = future.result()
                results[aff] = res
            except Exception as e:
                logging.error(f"Failed for {aff}: {e}")
    return results

# =============================
# CACHE HANDLING
# =============================

def load_cache():
    if CACHE_FILE.exists():
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_cache(data):
    with open(CACHE_FILE, "w") as f:
        json.dump(data, f, indent=2)

# =============================
# STATS AGGREGATION
# =============================

def calculate_stats(user_data, repo_data):
    total_repos = 0
    total_commits = 0
    total_stars = 0

    for aff, res in repo_data.items():
        edges = res["user"]["repositories"]["edges"]
        total_repos += len(edges)
        total_commits += get_commit_count(edges)
        total_stars += sum(e["node"]["stargazerCount"] for e in edges)

    return {
        "user": user_data["name"],
        "joined": user_data["createdAt"],
        "repos": total_repos,
        "commits": total_commits,
        "stars": total_stars,
    }

# =============================
# SVG GENERATION
# =============================

def generate_svg(stats):
    """Simple minimal SVG card."""
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="400" height="180">
  <style>
    .title {{ font: bold 18px sans-serif; fill: #2f80ed; }}
    .stat {{ font: 14px monospace; fill: #333; }}
  </style>
  <rect width="100%" height="100%" fill="#f9f9f9" stroke="#e1e4e8" />
  <text x="20" y="40" class="title">{stats['user']}'s GitHub Summary</text>
  <text x="20" y="80" class="stat">Repositories: {stats['repos']}</text>
  <text x="20" y="105" class="stat">Total Commits: {stats['commits']}</text>
  <text x="20" y="130" class="stat">Stars: {stats['stars']}</text>
  <text x="20" y="155" class="stat">Joined: {stats['joined'][:10]}</text>
</svg>
"""
    SVG_PATH.write_text(svg)
    logging.info(f"✅ SVG updated at {SVG_PATH.absolute()}")

# =============================
# MAIN EXECUTION
# =============================

def main():
    start = time.time()
    logging.info(f"Starting summary for {USER_NAME}")

    if not TOKEN:
        logging.error("❌ Missing GITHUB_TOKEN in .env")
        return

    user_data = fetch_user_id(USER_NAME)
    logging.info("Fetched user id successfully.")

    repo_data = fetch_all_repos(USER_NAME)
    stats = calculate_stats(user_data, repo_data)
    logging.info(f"Stats calculated: {stats}")

    save_cache(stats)
    generate_svg(stats)

    elapsed = round(time.time() - start, 2)
    logging.info(f"✅ Done in {elapsed}s")

if __name__ == "__main__":
    main()
