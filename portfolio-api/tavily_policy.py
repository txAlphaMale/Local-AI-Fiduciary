# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Jason Bentley
"""tavily_policy.py -- what the app may KEEP from a Tavily search.

Tavily's Search API Agreement (read by Cowork, RESULT-RV-RESEARCH.md item 4) bars storing the API output or derivatives, copying, storing,
archiving or caching the content of results, and building a database of it; which agreement governs a given key is unclear. The app takes the
stricter reading: the content of a result is used in memory to answer the request in front of it and is never written to disk or into a stored
record. What may be kept: the link, the title, the site and the date (references to a page, not its content) and the app's own notes.
Pure functions.
"""
import re
import urllib.parse


def domain_of(url):
    try:
        host = urllib.parse.urlparse(url or "").netloc.lower().split("@")[-1].split(":")[0]
    except Exception:
        return None
    return (host[4:] if host.startswith("www.") else host) or None


def refs(articles):
    """Articles reduced to references: title, url, site, date. No content, no snippet, no score."""
    out = []
    for a in articles or []:
        if not isinstance(a, dict):
            continue
        url = a.get("url") if isinstance(a.get("url"), str) and a.get("url").startswith(("http://", "https://")) else None
        out.append({"title": str(a.get("title") or "")[:200], "url": url, "domain": a.get("domain") or domain_of(url),
                    "published_date": a.get("published_date") or a.get("date")})
    return out


def is_full(articles):
    """True when any article still carries content (used by the one-time clean-up of entries stored before this rule)."""
    return any(isinstance(a, dict) and (a.get("content") or a.get("snippet") or a.get("score") is not None) for a in (articles or []))


def clean_stored_articles(v):
    return refs(v) if isinstance(v, list) and is_full(v) else v


_HEADLINE = re.compile(r"^Title: (.*)$", re.M)


def headlines_text(articles, limit=1500):
    """A reference-only text for the stored copy of the news blob: one line per article, no content."""
    lines = []
    for r in refs(articles):
        lines.append(f"- {r['title']}" + (f" ({r['domain']}" + (f", {r['published_date']}" if r.get("published_date") else "") + ")" if r.get("domain") else ""))
    return ("\n".join(lines))[:limit]


def clean_stored_news_text(v):
    """Entries stored before this rule hold article content in 'Title/Content' blocks: keep only the title lines."""
    if not isinstance(v, str) or "Content:" not in v:
        return v
    titles = _HEADLINE.findall(v)
    return "\n".join(f"- {t.strip()}" for t in titles)[:1500]


def sources_for_report(name, args, result):
    """What the Strategist's stored 'sources' keep from a web_search call: the links, not the results' text."""
    if name != "web_search" or not isinstance(result, dict):
        return result
    return {"results": refs(result.get("results")), "note": "links only: the content of search results is not stored"}
