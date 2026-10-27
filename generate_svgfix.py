#!/usr/bin/env python3
"""
Generates dark_mode.svg and light_mode.svg for a GitHub profile README.
Arch Linux logo on the left, neofetch-style details on the right.

Usage:
    export 
    python generate_svg.py             # fetch stats (cached) + render
    python generate_svg.py --refresh   # ignore the LOC cache and recount everything

Edit details.txt (same folder) to change the content.
If no token is set (or the API fails), the [Stats] values in details.txt are used.

Token: classic PAT with scopes `repo`, `read:user`, `read:org`
       (drop `repo` if you only want public repos counted).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "details.txt"
CACHE = HERE / ".stats_cache.json"

NBSP = "\u00a0"   # non-breaking space: survives renderers that collapse whitespace

# ---- layout ----------------------------------------------------------------
WIDTH = 985
LOGO_X = 25
TEXT_X = 390
LINE_H = 20
FIRST_Y = 30
LINE_CHARS = 60          # width of a details row, in characters
COL1, COL2 = 32, 25      # stats two-column layout (COL1 + 3 + COL2 = 60)

ARCH_LOGO = r"""
                  -`
                 .o+`
                `ooo/
               `+oooo:
              `+oooooo:
              -+oooooo+:
            `/:-:++oooo+:
           `/++++/+++++++:
          `/++++++++++++++:
         `/+++ooooooooooooo/`
        ./ooosssso++osssssso+`
       .oossssso-````/ossssss+`
      -osssssso.      :ssssssso.
     :osssssss/        osssso+++.
    /ossssssss/        +ssssooo/-
  `/ossssso+/:-        -:/+osssso+-
 `+sso+:-`                 `.-/+oso:
`++:.                           `-/+/
.`                                 `/
""".strip("\n").split("\n")

THEMES = {
    "dark":  dict(bg="#161b22", text="#c9d1d9", key="#ffa657", value="#a5d6ff",
                  add="#3fb950", dele="#f85149", cc="#616e7f"),
    "light": dict(bg="#f6f8fa", text="#24292f", key="#953800", value="#0a3069",
                  add="#1a7f37", dele="#cf222e", cc="#c2cfde"),
}


# ---- github api ------------------------------------------------------------
API = "https://api.github.com/graphql"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def gql(token, query, variables=None, retries=4):
    """POST a GraphQL query. Retries on transient 5xx / network errors."""
    body = json.dumps({"query": query, "variables": variables or {}}).encode()
    headers = {
        "Authorization": f"bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "profile-readme-svg-generator",
    }
    last_err = None
    for attempt in range(retries):
        req = urllib.request.Request(API, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise RuntimeError("GitHub rejected the token (401). Check GITHUB_TOKEN.") from e
            if e.code == 403:
                raise RuntimeError(f"GitHub returned 403 (rate limit or missing scope): "
                                   f"{e.read().decode(errors='replace')[:300]}") from e
            last_err = e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
        else:
            if data.get("errors"):
                raise RuntimeError(f"GraphQL error: {data['errors']}")
            return data["data"]
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GitHub API failed after {retries} attempts: {last_err}")


PROFILE_Q = """
query {
  viewer {
    id
    login
    createdAt
    followers { totalCount }
    repositoriesContributedTo(first: 1,
        contributionTypes: [COMMIT, ISSUE, PULL_REQUEST, REPOSITORY]) { totalCount }
  }
}"""

REPOS_Q = """
query($id: ID!, $cursor: String) {
  viewer {
    repositories(first: 100, after: $cursor,
                 ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        stargazerCount
        owner { login }
        defaultBranchRef {
          target { ... on Commit { history(author: {id: $id}) { totalCount } } }
        }
      }
    }
  }
}"""

HISTORY_Q = """
query($owner: String!, $name: String!, $id: ID!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef {
      target {
        ... on Commit {
          history(first: 100, after: $cursor, author: {id: $id}) {
            pageInfo { hasNextPage endCursor }
            nodes { additions deletions }
          }
        }
      }
    }
  }
}"""


def fetch_commit_total(token, created_at):
    """Sum commit contributions for every calendar year since the account was created."""
    start_year = int(created_at[:4])
    now = datetime.now(timezone.utc)
    aliases = []
    for year in range(start_year, now.year + 1):
        frm = f"{year}-01-01T00:00:00Z"
        to = f"{year}-12-31T23:59:59Z" if year < now.year else now.strftime("%Y-%m-%dT%H:%M:%SZ")
        aliases.append(
            f'y{year}: contributionsCollection(from: "{frm}", to: "{to}") '
            f"{{ totalCommitContributions }}"
        )
    data = gql(token, "query { viewer { " + " ".join(aliases) + " } }")
    return sum(v["totalCommitContributions"] for v in data["viewer"].values())


def fetch_repos(token, uid):
    """All repos the user owns / collaborates on / is an org member of."""
    repos, cursor = [], None
    while True:
        page = gql(token, REPOS_Q, {"id": uid, "cursor": cursor})["viewer"]["repositories"]
        for n in page["nodes"]:
            ref = n.get("defaultBranchRef")
            commits = ((ref or {}).get("target") or {}).get("history", {}).get("totalCount", 0)
            repos.append({
                "name": n["nameWithOwner"],
                "owner": n["owner"]["login"],
                "stars": n["stargazerCount"],
                "commits": commits,          # commits authored by me on default branch
            })
        if not page["pageInfo"]["hasNextPage"]:
            return repos
        cursor = page["pageInfo"]["endCursor"]


def repo_loc(token, uid, full_name):
    """(additions, deletions) of my commits on the repo's default branch."""
    owner, name = full_name.split("/", 1)
    add = dele = 0
    cursor = None
    while True:
        data = gql(token, HISTORY_Q, {"owner": owner, "name": name, "id": uid, "cursor": cursor})
        ref = data["repository"]["defaultBranchRef"]
        if not ref or not ref.get("target"):
            return add, dele
        hist = ref["target"]["history"]
        for node in hist["nodes"]:
            add += node["additions"]
            dele += node["deletions"]
        if not hist["pageInfo"]["hasNextPage"]:
            return add, dele
        cursor = hist["pageInfo"]["endCursor"]


def load_cache(refresh):
    if refresh or not CACHE.exists():
        return {}
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def fetch_stats(token, refresh=False):
    """Returns dict with keys: login, repos, contributed, stars, commits,
    followers, loc, loc_add, loc_del (numbers already formatted as strings)."""
    log("fetching profile...")
    viewer = gql(token, PROFILE_Q)["viewer"]
    uid, login = viewer["id"], viewer["login"]

    log("fetching repositories...")
    repos = fetch_repos(token, uid)
    owned = [r for r in repos if r["owner"].lower() == login.lower()]

    log("counting commits...")
    commits = fetch_commit_total(token, viewer["createdAt"])

    log("counting lines of code (cached per repo)...")
    cache = load_cache(refresh)
    total_add = total_del = 0
    for i, r in enumerate(repos, 1):
        if r["commits"] == 0:
            continue
        hit = cache.get(r["name"])
        if hit and hit.get("commits") == r["commits"]:
            a, d = hit["add"], hit["del"]
        else:
            log(f"  [{i}/{len(repos)}] {r['name']}")
            a, d = repo_loc(token, uid, r["name"])
            cache[r["name"]] = {"commits": r["commits"], "add": a, "del": d}
        total_add += a
        total_del += d
    try:
        CACHE.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    except OSError as e:
        log(f"warning: could not write cache: {e}")

    fmt = lambda n: f"{n:,}"
    return {
        "login": login,
        "repos": fmt(len(owned)),
        "contributed": fmt(viewer["repositoriesContributedTo"]["totalCount"]),
        "stars": fmt(sum(r["stars"] for r in owned)),
        "commits": fmt(commits),
        "followers": fmt(viewer["followers"]["totalCount"]),
        "loc": fmt(total_add - total_del),
        "loc_add": fmt(total_add),
        "loc_del": fmt(total_del),
    }


def apply_live_stats(sections, live):
    """Overwrite values in the [Stats] section (creating it if missing)."""
    live = {k: v for k, v in live.items() if k != "login"}
    for title, items in sections:
        if title.lower() == "stats":
            merged = dict(i for i in items if i)
            merged.update(live)
            items[:] = list(merged.items())
            return
    sections.append(("Stats", list(live.items())))


# ---- config parsing --------------------------------------------------------
def parse_config(path):
    """Returns (settings dict, ordered list of (section, [(key, value) | None]))."""
    settings, sections, current = {}, [], None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            if name.lower() == "settings":
                current = "settings"
            else:
                current = []
                sections.append((name, current))
            continue
        if current is None:
            continue
        if not line:                      # blank row inside a section
            if current != "settings" and current:
                current.append(None)
            continue
        if "=" not in line:
            continue
        key, value = (s.strip() for s in line.split("=", 1))
        if current == "settings":
            settings[key] = value
        else:
            current.append((key, value))
    return settings, sections


def age_string(born):
    """Years, months, days between `born` and today."""
    today = date.today()
    y, m, d = today.year - born.year, today.month - born.month, today.day - born.day
    if d < 0:
        m -= 1
        # days in the month before today's month
        prev_month = today.month - 1 or 12
        prev_year = today.year if today.month > 1 else today.year - 1
        first_next = date(prev_year + (prev_month == 12), prev_month % 12 + 1, 1)
        d += (first_next - date(prev_year, prev_month, 1)).days
    if m < 0:
        y -= 1
        m += 12
    return f"{y} years, {m} months, {d} days"


def resolve(value):
    if value.startswith("@age:"):
        return age_string(date.fromisoformat(value[5:].strip()))
    return value


# ---- svg building blocks ---------------------------------------------------
def t(text, cls=None):
    text = escape(text)
    return f'<tspan class="{cls}">{text}</tspan>' if cls else f"<tspan>{text}</tspan>"


def key_markup(key):
    """'Languages.Real' -> key 'Languages' + plain '.' + key 'Real'"""
    return t(".").join(t(p, "key") for p in key.split("."))


def row(y, inner):
    return f'<tspan x="{TEXT_X}" y="{y}">{inner}</tspan>'


def header(y, title, first=False):
    label = title if first else f"- {title}"
    dashes = "—" * max(3, LINE_CHARS - len(label))
    return row(y, f"{escape(label)} {dashes}")


def dotted(label, value, width):
    """Returns (dots_string) filling `width` chars for '. label: dots value'."""
    used = 2 + len(label) + 1 + 2 + len(value)      # '. ' + 'label:' + 2 spaces + value
    return "." * max(3, width - used)


def kv_row(y, key, value):
    dots = dotted(key, value, LINE_CHARS)
    return row(y, t(". ", "cc") + key_markup(key) + ":" +
               t(f" {dots} ", "cc") + t(value, "value"))


def stat_pair(label, value, width, prefix=""):
    dots = "." * max(3, width - len(prefix) - len(label) - 1 - 2 - len(value))
    return (t(prefix, "cc") if prefix else "") + t(label, "key") + ":" + \
        t(f" {dots} ", "cc") + t(value, "value")


def stats_rows(y, s):
    g = lambda k: s.get(k, "0")
    # row 1: Repos {Contributed} | Stars
    left_len = 2 + len("Repos:") + 2 + len(g("repos")) + 1 + len(f"{{Contributed: {g('contributed')}}}")
    dots1 = "." * max(3, COL1 - left_len)
    r1 = (t(". ", "cc") + t("Repos", "key") + ":" + t(f" {dots1} ", "cc") +
          t(g("repos"), "value") + " {" + t("Contributed", "key") + ": " +
          t(g("contributed"), "value") + "} | " + stat_pair("Stars", g("stars"), COL2))
    # row 2: Commits | Followers
    r2 = (stat_pair("Commits", g("commits"), COL1, ". ") + " | " +
          stat_pair("Followers", g("followers"), COL2))
    # row 3: Lines of code
    add, dele = g("loc_add"), g("loc_del")
    tail_len = len(f" ( {add}++, . {dele}-- )")
    label = "Lines of Code on GitHub"
    dots3 = "." * max(3, LINE_CHARS - (2 + len(label) + 1 + 2 + len(g("loc")) + tail_len))
    r3 = (t(". ", "cc") + t(label, "key") + ":" + t(f" {dots3} ", "cc") +
          t(g("loc"), "value") + " ( " + t(add, "addColor") + t("++", "addColor") +
          ", " + t(". ") + t(dele, "delColor") + t("--", "delColor") + " )")
    return [row(y, r1), row(y + LINE_H, r2), row(y + 2 * LINE_H, r3)]


# ---- main render -----------------------------------------------------------
def build_details(settings, sections):
    lines, y = [], FIRST_Y
    lines.append(header(y, settings.get("username", "username"), first=True))
    y += LINE_H
    for idx, (title, items) in enumerate(sections):
        if idx > 0:                                   # blank row + header
            y += LINE_H
            lines.append(header(y, "GitHub Stats" if title.lower() == "stats" else title))
            y += LINE_H
        if title.lower() == "stats":
            stats = dict(i for i in items if i)
            lines += stats_rows(y, stats)
            y += 3 * LINE_H
            continue
        while items and items[-1] is None:            # ignore trailing blank lines
            items.pop()
        for item in items:
            if item is None:
                lines.append(row(y, t(". ", "cc")))
            else:
                lines.append(kv_row(y, item[0], resolve(item[1])))
            y += LINE_H
    return lines, y


def build_logo(color, height):
    y0 = max(FIRST_Y, (height - len(ARCH_LOGO) * LINE_H) // 2 + 10)
    out = [f'<text x="{LOGO_X}" y="{y0}" fill="{color}" font-weight="bold" xml:space="preserve">']
    for i, ln in enumerate(ARCH_LOGO):
        out.append(f'<tspan x="{LOGO_X}" y="{y0 + i * LINE_H}">{escape(ln).replace(" ", NBSP)}</tspan>')
    out.append("</text>")
    return "\n".join(out)


def render(theme, settings, details, last_y):
    c = THEMES[theme]
    height = max(last_y + 10, FIRST_Y + len(ARCH_LOGO) * LINE_H + 20)
    body = "\n".join(details)
    return f"""<?xml version='1.0' encoding='UTF-8'?>
<svg xmlns="http://www.w3.org/2000/svg" font-family="ConsolasFallback,Consolas,monospace" width="{WIDTH}px" height="{height}px" font-size="16px">
<style>
@font-face {{
src: local('Consolas'), local('Consolas Bold');
font-family: 'ConsolasFallback';
font-display: swap;
-webkit-size-adjust: 109%;
size-adjust: 109%;
}}
.key {{fill: {c['key']};}}
.value {{fill: {c['value']};}}
.addColor {{fill: {c['add']};}}
.delColor {{fill: {c['dele']};}}
.cc {{fill: {c['cc']};}}
text, tspan {{white-space: pre;}}
</style>
<rect width="{WIDTH}px" height="{height}px" fill="{c['bg']}" rx="15"/>
{build_logo(settings.get('logo_color', '#1793d1'), height)}
<text x="{TEXT_X}" y="{FIRST_Y}" fill="{c['text']}" xml:space="preserve">
{body}
</text>
</svg>
"""


def main():
    settings, sections = parse_config(CONFIG)

    # Optional setting in details.txt [Settings]:  token_env = MY_TOKEN_VAR
    token = os.environ.get(settings.get("token_env", "GITHUB_TOKEN"), "").strip()
    if token:
        try:
            live = fetch_stats(token, refresh="--refresh" in sys.argv)
            apply_live_stats(sections, live)
            settings.setdefault("username", live["login"])
        except RuntimeError as e:
            log(f"warning: {e}\n         falling back to the values in details.txt")
    else:
        log("GITHUB_TOKEN not set - using the [Stats] values from details.txt")

    details, last_y = build_details(settings, sections)
    for theme in ("dark", "light"):
        out = HERE / settings.get(f"output_{theme}", f"{theme}_mode.svg")
        out.write_text(render(theme, settings, details, last_y), encoding="utf-8")
        print(f"wrote {out}")


if __name__ == "__main__":
    main()
