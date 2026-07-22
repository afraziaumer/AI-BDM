"""Decision-maker source #2: Schema.org structured data (JSON-LD).

A site's own `<script type="application/ld+json">` blocks are metadata the
business PUBLISHED specifically for machines (search engines, this crawler)
to read — the most direct, least-guessy source available, more reliable
than pattern-matching prose on a Team page. Organization/LocalBusiness/
Corporation schemas often list founders/employees/contact points directly.

Runs during the crawl (phase1_pipeline.crawl_site's per-page loop) on the
RAW soup, before cleaning strips `<script>` tags — same ordering constraint
as canonical/OG tag capture in clean_page_for_llm.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup

logger = logging.getLogger("ai_bdm.schema_org_extractor")

_PERSON_TYPES = {"person", "employee"}
_ORG_TYPES = {"organization", "corporation", "localbusiness"}


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _schema_type(node: Dict[str, Any]) -> str:
    t = node.get("@type", "")
    if isinstance(t, list):
        t = t[0] if t else ""
    return str(t).lower()


def _extract_contact_point(node: Dict[str, Any]) -> Dict[str, Optional[str]]:
    return {
        "phone": node.get("telephone"),
        "email": node.get("email"),
        "contact_type": node.get("contactType"),
    }


def _extract_person(node: Dict[str, Any], page_url: str) -> Dict[str, Any]:
    name = node.get("name")
    role = node.get("jobTitle")
    same_as = _as_list(node.get("sameAs"))
    linkedin = next((u for u in same_as if isinstance(u, str) and "linkedin.com" in u.lower()), None)
    image = node.get("image")
    if isinstance(image, dict):
        image = image.get("url")
    return {
        "name": name,
        "role": role,
        "department": None,
        "email": node.get("email"),
        "phone": node.get("telephone"),
        "linkedin": linkedin,
        "photo": image if isinstance(image, str) else None,
        "source": "schema",
        "source_url": page_url,
        "confidence": 0.95,  # structured, machine-readable, self-published — high trust
    }


def _walk_nodes(data: Any, out: List[Dict[str, Any]]) -> None:
    """Flatten @graph / nested arrays into a flat list of dict nodes."""
    if isinstance(data, dict):
        if "@graph" in data:
            _walk_nodes(data["@graph"], out)
        else:
            out.append(data)
    elif isinstance(data, list):
        for item in data:
            _walk_nodes(item, out)


def extract_schema_org(html_or_soup, page_url: str) -> Dict[str, Any]:
    """Parse every JSON-LD block on a page. Returns
    {"people": [...], "organization": {...} | None, "contact_points": [...]}.
    Never raises — malformed/absent JSON-LD just means empty results.

    Accepts either raw HTML or an already-parsed BeautifulSoup (the crawler
    already has one in memory at the point this needs to run; standalone
    callers/tests can pass a string).
    """
    people: List[Dict[str, Any]] = []
    organization: Optional[Dict[str, Any]] = None
    contact_points: List[Dict[str, Any]] = []

    try:
        soup = (
            html_or_soup if isinstance(html_or_soup, BeautifulSoup)
            else BeautifulSoup(html_or_soup, "lxml")
        )
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text()
            if not raw or not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            nodes: List[Dict[str, Any]] = []
            _walk_nodes(data, nodes)

            for node in nodes:
                if not isinstance(node, dict):
                    continue
                node_type = _schema_type(node)

                if node_type in _PERSON_TYPES and node.get("name"):
                    people.append(_extract_person(node, page_url))

                if node_type in _ORG_TYPES:
                    if organization is None:
                        organization = {
                            "name": node.get("name"),
                            "type": node_type,
                            "description": node.get("description"),
                            "url": node.get("url"),
                            "logo": (
                                node.get("logo", {}).get("url")
                                if isinstance(node.get("logo"), dict)
                                else node.get("logo")
                            ),
                            "address": node.get("address"),
                            "source_url": page_url,
                        }
                    for founder in _as_list(node.get("founder")):
                        if isinstance(founder, dict) and founder.get("name"):
                            person = _extract_person(founder, page_url)
                            person["role"] = person["role"] or "Founder"
                            people.append(person)
                    for employee in _as_list(node.get("employee")):
                        if isinstance(employee, dict) and employee.get("name"):
                            people.append(_extract_person(employee, page_url))
                    for cp in _as_list(node.get("contactPoint")):
                        if isinstance(cp, dict):
                            contact_points.append(_extract_contact_point(cp))
    except Exception as exc:  # noqa: BLE001 - best-effort, never breaks the crawl
        logger.warning("Schema.org extraction failed for %s: %s", page_url, exc)

    return {"people": people, "organization": organization, "contact_points": contact_points}
