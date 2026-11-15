"""Where your sites are (and aren't) cited, computed from captured exchanges at read time."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher

from audit import terms


def clean_domains(items) -> list[str]:
    out = []
    for d in items or []:
        d = re.sub(r"^https?://", "", str(d).strip().lower()).split("/")[0]
        d = d[4:] if d.startswith("www.") else d
        if d and "." in d and d not in out:
            out.append(d)
    return out


def matches(domain: str, tracked: str) -> bool:
    return domain == tracked or domain.endswith("." + tracked)


def group_similar(rows, threshold=0.85):
    """Cluster exchanges whose prompts are the same or nearly the same."""
    groups: list[dict] = []
    for r in rows:
        norm = r["prompt_norm"] or ""
        if not norm:
            continue
        for g in groups:
            sm = SequenceMatcher(None, g["key"], norm)
            if g["key"] == norm or (sm.real_quick_ratio() >= threshold and sm.quick_ratio() >= threshold
                                    and sm.ratio() >= threshold):
                g["ids"].append(r["id"])
                g["apps"].add(r["app"])
                break
        else:
            groups.append({"key": norm, "prompt": r["prompt"], "ids": [r["id"]], "apps": {r["app"]}, "ts": r["ts"]})
    return groups


def _first_rank(sources, tracked):
    for i, s in enumerate(sources, 1):
        if matches(s["domain"], tracked):
            return i, s["url"]
    return None, None


def compute(exchanges: list[dict], mine: list[str], competitors: list[str]) -> dict:
    """exchanges: dicts with id, ts, app, prompt, prompt_norm, sources (list), search_queries (list)."""
    tracked = [(d, "mine") for d in mine] + [(d, "competitor") for d in competitors if d not in mine]
    apps = sorted({e["app"] for e in exchanges})
    per_app_total = Counter(e["app"] for e in exchanges)
    per_app_with_sources = Counter(e["app"] for e in exchanges if e["sources"])

    # Scoreboard: for each tracked domain and app, share of queries citing it and average first rank.
    board = []
    for dom, kind in tracked:
        cells = {}
        for app in apps:
            ranks = [r for e in exchanges if e["app"] == app for r in [_first_rank(e["sources"], dom)[0]] if r]
            n = per_app_total[app]
            cells[app] = {"cited": len(ranks), "total": n, "with_sources": per_app_with_sources[app],
                          "share": round(len(ranks) / n, 3) if n else None,
                          "avg_rank": round(sum(ranks) / len(ranks), 1) if ranks else None}
        total_cited = sum(c["cited"] for c in cells.values())
        board.append({"domain": dom, "kind": kind, "cells": cells, "cited": total_cited,
                      "share": round(total_cited / len(exchanges), 3) if exchanges else None})

    # Missed queries: none of the user's domains cited.
    missed, competitor_wins, missed_terms, missed_searches = [], Counter(), Counter(), Counter()
    my_hits: dict[int, list] = {}
    for e in exchanges:
        hits = [(d, *_first_rank(e["sources"], d)) for d in mine]
        hits = [h for h in hits if h[1]]
        if hits:
            my_hits[e["id"]] = hits
            continue
        if not mine:
            continue
        cited = Counter(s["domain"] for s in e["sources"])
        comp = [d for d in competitors if any(matches(c, d) for c in cited)]
        competitor_wins.update(comp)
        for q in e["search_queries"]:
            if not q.startswith("fetch:"):
                missed_searches[q.lower()] += 1
                missed_terms.update(terms(q))
        missed.append({
            "id": e["id"], "ts": e["ts"], "app": e["app"], "prompt": e["prompt"],
            "search_queries": e["search_queries"], "no_sources": not e["sources"],
            "cited": [{"domain": d, "count": c, "competitor": any(matches(d, x) for x in competitors)}
                      for d, c in cited.most_common(12)],
            "top_urls": [s["url"] for s in e["sources"][:5]],
        })

    # Same question, different apps: cited in some apps but not others.
    gaps = []
    by_id = {e["id"]: e for e in exchanges}
    for g in group_similar(sorted(exchanges, key=lambda e: -e["ts"])):
        if len(g["apps"]) < 2 or not mine:
            continue
        per_app = defaultdict(lambda: {"cited": False, "url": None, "rank": None, "ids": [], "searches": []})
        for i in g["ids"]:
            e = by_id[i]
            cell = per_app[e["app"]]
            cell["ids"].append(i)
            cell["searches"] += [q for q in e["search_queries"] if q not in cell["searches"]]
            for d, rank, url in my_hits.get(i, []):
                if not cell["cited"] or rank < cell["rank"]:
                    cell.update(cited=True, url=url, rank=rank)
        states = {a: c["cited"] for a, c in per_app.items()}
        if any(states.values()) and not all(states.values()):
            my_url = next(c["url"] for c in per_app.values() if c["cited"])
            missed_by = [a for a, v in states.items() if not v]
            gaps.append({"prompt": g["prompt"], "apps": dict(per_app), "my_url": my_url,
                         "missed_searches": [q for a in missed_by for q in per_app[a]["searches"]],
                         "cited_by": sorted(a for a, v in states.items() if v),
                         "missed_by": sorted(a for a, v in states.items() if not v)})

    return {
        "apps": apps, "mine": mine, "competitors": competitors, "board": board,
        "missed": missed[:200], "missed_count": len(missed), "total": len(exchanges),
        "gaps": gaps, "competitor_wins": competitor_wins.most_common(20),
        "missed_searches": missed_searches.most_common(30), "missed_terms": missed_terms.most_common(40),
    }
