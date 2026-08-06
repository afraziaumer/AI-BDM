"""Step 7 — for each detected decision maker, infer which business
problems they're likely responsible for, so a later query like "who should
I contact about their CRM?" can point at the right person instead of just
"the CEO" for everything.

Rule-based, not ML — a department/title maps to a small, curated set of
business-problem categories with a per-mapping confidence reflecting how
DIRECT that ownership typically is (a CTO owning "Cybersecurity" is a much
more direct, confident inference than a CFO owning "Analytics"). The
final per-person confidence is the MINIMUM of the person's own extraction
confidence and the mapping's confidence — being unsure WHO someone is
should discount how confident we are about what they own just as much as
an indirect mapping should, same "both signals must agree" principle used
in linkedin_discovery.py's title-confidence check.
"""

from __future__ import annotations

from typing import Any, Dict, List

# department -> {problem_category: mapping_confidence}. Only the
# problem-category vocabulary from the spec; ordered roughly by how
# directly each department owns each category.
_DEPARTMENT_PROBLEM_MAP: Dict[str, Dict[str, float]] = {
    "Executive": {
        "Digital Transformation": 0.85, "Cybersecurity": 0.55,
        "Cloud Infrastructure": 0.5, "AI": 0.55,
    },
    "Technology": {
        "CRM": 0.75, "ERP": 0.7, "IoT": 0.85, "Cybersecurity": 0.92,
        "Cloud Infrastructure": 0.92, "Website Redesign": 0.75,
        "Digital Transformation": 0.9, "Analytics": 0.75, "AI": 0.85,
        "Automation": 0.8, "Mobile App": 0.75,
    },
    "Operations": {
        "CRM": 0.65, "ERP": 0.85, "IoT": 0.75, "Automation": 0.85,
        "Cloud Infrastructure": 0.5,
    },
    "Sales": {
        "CRM": 0.92, "Analytics": 0.6, "Automation": 0.55,
    },
    "Marketing": {
        "Marketing Automation": 0.92, "Website Redesign": 0.8,
        "Customer Experience": 0.75, "Analytics": 0.7, "Mobile App": 0.55,
    },
    "Finance": {
        "ERP": 0.7, "Automation": 0.6, "Analytics": 0.55,
    },
    "Customer Success": {
        "Customer Experience": 0.92, "CRM": 0.8, "Automation": 0.5,
    },
    "Support": {
        "Customer Experience": 0.85, "CRM": 0.6, "Automation": 0.55,
    },
    "Human Resources": {
        "Automation": 0.5,
    },
}

MIN_OWNERSHIP_CONFIDENCE = 0.5  # below this, don't list the category at all
MAX_CATEGORIES_PER_PERSON = 5


def infer_likely_ownership(person: Dict[str, Any]) -> Dict[str, Any]:
    """Returns the SAME person dict with `likely_owner_of` (list of
    {category, confidence}, highest first) added. Empty list for a person
    with no recognized department — no ownership guessed without a basis."""
    department = person.get("department")
    person_confidence = float(person.get("confidence") or 0.0)
    mapping = _DEPARTMENT_PROBLEM_MAP.get(department, {})

    scored = []
    for category, mapping_confidence in mapping.items():
        combined = min(person_confidence, mapping_confidence)
        if combined >= MIN_OWNERSHIP_CONFIDENCE:
            scored.append({"category": category, "confidence": round(combined, 3)})
    scored.sort(key=lambda c: c["confidence"], reverse=True)

    return {**person, "likely_owner_of": scored[:MAX_CATEGORIES_PER_PERSON]}


def annotate_buying_committee(people: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Apply infer_likely_ownership to every person in the list."""
    return [infer_likely_ownership(p) for p in people]
