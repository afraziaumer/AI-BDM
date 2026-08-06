"""Step 8 — business_intelligence.json: a consolidated summary that
REFERENCES the other per-domain files (social_profiles.json,
linkedin_company.json, decision_makers.json, organization.json) rather than
duplicating their content — a quick-glance index for later AI modules
("does this business have a known CTO?", "how many social channels do they
run?") without needing to open and parse four separate files for a yes/no.

Pure function — takes what phase1_pipeline.py already assembled from the
other modules and summarizes it. No new discovery, no new network calls.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def build_business_intelligence_summary(
    domain: str,
    social_profiles: Optional[Dict[str, Any]],
    linkedin_company: Optional[Dict[str, Any]],
    decision_makers: Optional[List[Dict[str, Any]]],
    organization: Optional[Dict[str, Any]],
    github_profile: Optional[Dict[str, Any]] = None,
    youtube_profile: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    social_profiles = social_profiles or {}
    decision_makers = decision_makers or []
    organization = organization or {}

    active_platforms = sorted(
        p for p in social_profiles
        if p != "other" and social_profiles.get(p)
    )
    executives = [
        p for p in decision_makers
        if p.get("department") == "Executive" and p.get("name")
    ]
    tech_leaders = [
        p for p in decision_makers
        if p.get("department") == "Technology" and p.get("name")
    ]
    sources_used = sorted({p.get("source") for p in decision_makers if p.get("source")})

    return {
        "domain": domain,
        "social": {
            "platforms_found": active_platforms,
            "platform_count": len(active_platforms),
            "linkedin_url": social_profiles.get("linkedin"),
        },
        "linkedin_company": {
            "found": bool(linkedin_company),
            "url": (linkedin_company or {}).get("url"),
            "requires_provider": (linkedin_company or {}).get("requires_provider", True),
        } if linkedin_company is not None else {"found": False},
        "github": {"found": bool(github_profile), "url": (github_profile or {}).get("url")},
        "youtube": {"found": bool(youtube_profile), "url": (youtube_profile or {}).get("url")},
        "decision_makers": {
            "total_found": sum(1 for p in decision_makers if p.get("name")),
            "sources_used": sources_used,
            "has_executive_contact": bool(executives),
            "has_technology_contact": bool(tech_leaders),
            "executive_names": [p["name"] for p in executives],
        },
        "organization": {
            "departments_identified": list((organization.get("department_counts") or {}).keys()),
            "total_people": organization.get("total_people", 0),
        },
    }
