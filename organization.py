"""Step 6 — infer a business's organizational structure from its detected
decision makers.

Pure function over already-extracted people (from decision_maker_extractor.py
/ schema_org_extractor.py / public_search_decision_makers.py, merged by
phase1_pipeline.py's business-intelligence assembly) — no new discovery, no
new network calls, just grouping by the SAME `department` field
decision_maker_extractor.infer_department already computed per person.

Consumed by future AI modules (per the spec: "help later AI modules
understand who likely owns different business problems") and by
buying_committee.py, which maps department -> likely problem ownership.
"""

from __future__ import annotations

from typing import Any, Dict, List

DEPARTMENTS = (
    "Executive", "Technology", "Operations", "Sales", "Marketing",
    "Finance", "Customer Success", "Support", "Human Resources",
)


def build_organization(people: List[Dict[str, Any]]) -> Dict[str, Any]:
    """{"departments": {name: [person, ...]}, "department_counts": {...},
    "unclassified": [...]}. A person missing a `department` value (name-only
    contact-page entries, or a role our keyword table doesn't recognize)
    goes into "unclassified" rather than being dropped or force-guessed
    into a bucket."""
    departments: Dict[str, List[Dict[str, Any]]] = {d: [] for d in DEPARTMENTS}
    unclassified: List[Dict[str, Any]] = []

    for person in people:
        dept = person.get("department")
        if dept in departments:
            departments[dept].append(person)
        else:
            unclassified.append(person)

    department_counts = {d: len(v) for d, v in departments.items() if v}
    if unclassified:
        department_counts["Unclassified"] = len(unclassified)

    return {
        "departments": {d: v for d, v in departments.items() if v},
        "department_counts": department_counts,
        "unclassified": unclassified,
        "has_executive_contact": bool(departments["Executive"]),
        "has_technology_contact": bool(departments["Technology"]),
        "total_people": len(people),
    }
