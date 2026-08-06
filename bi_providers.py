"""Pluggable Business Intelligence Provider Architecture.

    BusinessIntelligenceManager
            |
    Provider Interface (BusinessIntelligenceProvider)
            |
    WebsiteIntelligenceProvider   (free — already-crawled site data)
    GoogleXRayProvider            (Serper site:/X-Ray search — linkedin_discovery.py
                                    + public_search_decision_makers.py)
    ApifyProvider                 (placeholder — no API call, no key required)
    ... future: Apollo / Proxycurl / People Data Labs / ZoomInfo / Clearbit

The rest of AI-BDM talks ONLY to BusinessIntelligenceManager's discover_*
methods — never to linkedin_discovery.py / public_search_decision_makers.py
/ social_discovery.py directly for orchestration. Those modules still do
the real discovery work; this file standardizes HOW they're invoked,
composed, and prioritized, so adding a new provider (Apollo, Proxycurl,
...) is one new BusinessIntelligenceProvider subclass registered in
PROVIDER_REGISTRY — no other file changes.

Every provider method returns the SAME standardized shape (see
make_result): {"provider", "confidence", "found", "company", "socials",
"decision_makers", "contacts", "metadata", "timestamp"} — regardless of
which provider or which capability produced it, so callers never special-
case a specific provider's response format.

Configuration (env, read once at import time — see the block below):
    BUSINESS_INTELLIGENCE_PROVIDER   which external provider is active
                                      ("google" today; "apify" once ready)
    GOOGLE_XRAY_ENABLED              default true
    APIFY_ENABLED                    default false
    APIFY_API_KEY                    default "" (unused until Apify is real)
    BI_CONFIDENCE_THRESHOLD          fallback-to-next-provider floor
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from time_utils import utc_now_iso
from typing import Any, Dict, List, Optional

import aiohttp

import linkedin_discovery
import linkedin_enrichment
import public_search_decision_makers
import social_discovery
import organization

logger = logging.getLogger("ai_bdm.bi_providers")


# === Configuration ===========================================================
def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


BUSINESS_INTELLIGENCE_PROVIDER = os.getenv("BUSINESS_INTELLIGENCE_PROVIDER", "google").strip().lower()
GOOGLE_XRAY_ENABLED = _env_bool("GOOGLE_XRAY_ENABLED", True)
APIFY_ENABLED = _env_bool("APIFY_ENABLED", False)
APIFY_API_KEY = os.getenv("APIFY_API_KEY", "")

# A provider's result below this confidence (or with found=False) triggers
# a fallback to the next provider in priority order.
BI_CONFIDENCE_THRESHOLD = float(os.getenv("BI_CONFIDENCE_THRESHOLD", "0.6"))


# === Standardized provider response =========================================
def make_result(
    provider: str, *, confidence: float = 0.0,
    company: Optional[Dict[str, Any]] = None,
    socials: Optional[Dict[str, Any]] = None,
    decision_makers: Optional[List[Dict[str, Any]]] = None,
    contacts: Optional[List[Dict[str, Any]]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    found: Optional[bool] = None,
) -> Dict[str, Any]:
    """The ONE response shape every provider method returns. Callers read
    whichever of company/socials/decision_makers/contacts/metadata is
    relevant to the capability they asked for; the rest stay at their
    empty defaults so no caller has to guess a field is absent vs. None."""
    company = company or {}
    socials = socials or {}
    decision_makers = decision_makers or []
    contacts = contacts or []
    metadata = metadata or {}
    if found is None:
        found = bool(company or socials or decision_makers or contacts or metadata)
    return {
        "provider": provider,
        "confidence": confidence,
        "found": found,
        "company": company,
        "socials": socials,
        "decision_makers": decision_makers,
        "contacts": contacts,
        "metadata": metadata,
        "timestamp": utc_now_iso(),
    }


# === Provider interface ======================================================
class BusinessIntelligenceProvider(ABC):
    """Every provider must expose exactly this surface. The Manager (and
    the rest of AI-BDM) only ever calls these ten methods — never anything
    provider-specific — so swapping or adding a provider never requires
    touching a call site elsewhere.

    Each method's signature is deliberately uniform across very different
    providers: (session, *, name, domain, **ctx). `ctx` is a grab-bag for
    whatever a given provider actually needs (external_links, city,
    country, industry, crawl_decision_makers, schema_organization, store,
    known_socials, required_fields, ...) — a provider ignores whatever it
    doesn't use via **ctx rather than the interface growing a parameter
    per provider's needs.
    """

    name: str = "base"

    @abstractmethod
    async def discover_company(self, session: aiohttp.ClientSession, *, name: str, domain: str, **ctx: Any) -> Dict[str, Any]: ...

    @abstractmethod
    async def discover_social_profiles(self, session: aiohttp.ClientSession, *, name: str, domain: str, **ctx: Any) -> Dict[str, Any]: ...

    @abstractmethod
    async def discover_linkedin_company(self, session: aiohttp.ClientSession, *, name: str, domain: str, **ctx: Any) -> Dict[str, Any]: ...

    @abstractmethod
    async def discover_decision_makers(self, session: aiohttp.ClientSession, *, name: str, domain: str, **ctx: Any) -> Dict[str, Any]: ...

    @abstractmethod
    async def discover_contacts(self, session: aiohttp.ClientSession, *, name: str, domain: str, **ctx: Any) -> Dict[str, Any]: ...

    @abstractmethod
    async def discover_company_metadata(self, session: aiohttp.ClientSession, *, name: str, domain: str, **ctx: Any) -> Dict[str, Any]: ...

    @abstractmethod
    async def discover_locations(self, session: aiohttp.ClientSession, *, name: str, domain: str, **ctx: Any) -> Dict[str, Any]: ...

    @abstractmethod
    async def discover_executives(self, session: aiohttp.ClientSession, *, name: str, domain: str, **ctx: Any) -> Dict[str, Any]: ...

    @abstractmethod
    async def discover_departments(self, session: aiohttp.ClientSession, *, name: str, domain: str, **ctx: Any) -> Dict[str, Any]: ...

    @abstractmethod
    async def validate_company(self, session: aiohttp.ClientSession, *, name: str, domain: str, **ctx: Any) -> Dict[str, Any]: ...


# === Tier 1 — Website Intelligence (free, always runs first) ===============
class WebsiteIntelligenceProvider(BusinessIntelligenceProvider):
    """Always tried first, and free: every method here reads data ALREADY
    extracted during the crawl (decision_maker_extractor.py via
    ctx['crawl_decision_makers'], schema_org_extractor.py via
    ctx['schema_organization'], social_discovery.py run fresh over
    ctx['external_links']) rather than making a new network call. The
    crawl already did this work once, for free, while fetching pages."""

    name = "website"

    async def discover_company(self, session, *, name, domain, **ctx):
        schema_org = ctx.get("schema_organization") or {}
        company = {k: v for k, v in {
            "name": schema_org.get("name") or name or None,
            "description": schema_org.get("description"),
            "url": schema_org.get("url") or (f"https://{domain}" if domain else None),
        }.items() if v}
        return make_result(self.name, confidence=1.0 if schema_org else 0.5,
                            company=company, found=bool(company))

    async def discover_social_profiles(self, session, *, name, domain, **ctx):
        external_links = ctx.get("external_links") or []
        socials = social_discovery.extract_social_links(external_links)
        found_count = sum(1 for p, v in socials.items() if p != "other" and v)
        return make_result(self.name, confidence=1.0 if found_count else 0.0,
                            socials=socials, found=found_count > 0)

    async def discover_linkedin_company(self, session, *, name, domain, **ctx):
        schema_org = ctx.get("schema_organization") or {}
        same_as = schema_org.get("sameAs") or []
        linkedin_url = next(
            (u for u in same_as if isinstance(u, str) and "linkedin.com" in u.lower()), None,
        )
        if not linkedin_url:
            external_links = ctx.get("external_links") or []
            linkedin_url = social_discovery.extract_social_links(external_links).get("linkedin")
        company = {"url": linkedin_url} if linkedin_url else {}
        return make_result(self.name, confidence=1.0 if linkedin_url else 0.0,
                            company=company, found=bool(linkedin_url))

    async def discover_decision_makers(self, session, *, name, domain, **ctx):
        people = ctx.get("crawl_decision_makers") or []
        named = [p for p in people if p.get("name")]
        return make_result(self.name, confidence=1.0 if named else 0.0,
                            decision_makers=people, found=bool(named))

    async def discover_contacts(self, session, *, name, domain, **ctx):
        contacts = ctx.get("contacts") or []
        return make_result(self.name, confidence=1.0 if contacts else 0.0,
                            contacts=contacts, found=bool(contacts))

    async def discover_company_metadata(self, session, *, name, domain, **ctx):
        schema_org = ctx.get("schema_organization") or {}
        metadata = {k: v for k, v in schema_org.items()
                    if k not in {"name", "description", "url", "sameAs"} and v}
        return make_result(self.name, confidence=1.0 if metadata else 0.0,
                            metadata=metadata, found=bool(metadata))

    async def discover_locations(self, session, *, name, domain, **ctx):
        address = ctx.get("address")
        metadata = {"address": address} if address else {}
        return make_result(self.name, confidence=1.0 if address else 0.0,
                            metadata=metadata, found=bool(address))

    async def discover_executives(self, session, *, name, domain, **ctx):
        people = ctx.get("crawl_decision_makers") or []
        execs = [p for p in people if p.get("department") == "Executive" and p.get("name")]
        return make_result(self.name, confidence=1.0 if execs else 0.0,
                            decision_makers=execs, found=bool(execs))

    async def discover_departments(self, session, *, name, domain, **ctx):
        people = ctx.get("crawl_decision_makers") or []
        org = organization.build_organization(people)
        return make_result(self.name, confidence=1.0 if org.get("total_people") else 0.0,
                            metadata=org, found=bool(org.get("total_people")))

    async def validate_company(self, session, *, name, domain, **ctx):
        schema_org = ctx.get("schema_organization") or {}
        valid = bool(schema_org) or bool(ctx.get("crawl_decision_makers"))
        return make_result(self.name, confidence=1.0 if valid else 0.3,
                            company={"validated": valid}, found=valid)


# === Tier 2 — Google X-Ray (Serper site:-search based discovery) ===========
class GoogleXRayProvider(BusinessIntelligenceProvider):
    """Serper-search-based ('X-Ray') discovery — linkedin_discovery.py
    (LinkedIn company page, decision-maker /in/ profiles, Facebook/
    Instagram/X fallback via the same shared search engine) +
    public_search_decision_makers.py (site:domain search for named
    people). Costs real Serper API calls; only reached when the free
    Website tier didn't already satisfy the request. Never scrapes any
    social platform directly — only reads Serper's indexed titles/
    snippets (see linkedin_discovery.py's module docstring).
    """

    name = "google_xray"

    async def discover_company(self, session, *, name, domain, **ctx):
        return await self.discover_linkedin_company(session, name=name, domain=domain, **ctx)

    async def discover_social_profiles(self, session, *, name, domain, **ctx):
        known = dict(ctx.get("known_socials") or {})
        if not known.get("linkedin"):
            li_result = await self.discover_linkedin_company(session, name=name, domain=domain, **ctx)
            if li_result.get("found"):
                known["linkedin"] = li_result["company"].get("url")

        _FALLBACK_PLATFORMS = (
            ("facebook", "facebook.com"), ("instagram", "instagram.com"), ("x", "x.com"),
        )
        for platform, host in _FALLBACK_PLATFORMS:
            if known.get(platform):
                continue
            queries = [f'site:{host} "{name}"'] if name else []
            queries.append(f"site:{host} {domain}")
            match = await linkedin_discovery.discover_profile_via_search(
                session, queries, name or "", lambda link, h=host: h in link,
            )
            if match:
                known[platform] = match.url
                logger.info("[google_xray] %s profile found for %s: %s (confidence=%.2f)",
                            platform, domain, match.url, match.confidence)

        found_count = sum(1 for p, v in known.items() if p != "other" and v)
        return make_result(self.name, confidence=1.0 if found_count else 0.0,
                            socials=known, found=found_count > 0)

    async def discover_linkedin_company(self, session, *, name, domain, **ctx):
        city = ctx.get("city", "")
        country = ctx.get("country", "")
        industry = ctx.get("industry", "")
        store = ctx.get("store")
        match = await linkedin_discovery.discover_company_linkedin(
            session, name or "", domain, city, country, industry,
        )
        if not match:
            return make_result(self.name, confidence=0.0, found=False)
        li_data = await linkedin_enrichment.enrich_linkedin_company(match, domain)
        if store is not None:
            store.write_linkedin_company_now(domain, li_data)
        logger.info("[google_xray] LinkedIn company page found for %s: %s (confidence=%.2f)",
                    domain, match.url, match.confidence)
        return make_result(self.name, confidence=match.confidence, company=li_data, found=True)

    async def discover_decision_makers(self, session, *, name, domain, **ctx):
        city = ctx.get("city", "")
        country = ctx.get("country", "")
        industry = ctx.get("industry", "")
        store = ctx.get("store")

        people: List[Dict[str, Any]] = []
        fallback_people = await public_search_decision_makers.discover_via_public_search(session, domain)
        people.extend(fallback_people)

        cached_candidates = store.read_linkedin_candidates(domain) if store is not None else None
        if linkedin_discovery.is_cache_fresh(cached_candidates):
            xray_people = cached_candidates.get("profiles") or []
            logger.info("[google_xray] LinkedIn candidates cache HIT for %s (%d profile(s)).",
                        domain, len(xray_people))
        else:
            xray_people = await linkedin_discovery.discover_decision_makers(
                session, name or "", domain, city, country, industry,
            )
            if store is not None:
                linkedin_discovery.save_linkedin_candidates(
                    store, domain, (ctx.get("known_socials") or {}).get("linkedin"), xray_people,
                )
        people.extend(xray_people)

        named = [p for p in people if p.get("name")]
        avg_conf = (sum(p.get("confidence", 0.5) for p in named) / len(named)) if named else 0.0
        return make_result(self.name, confidence=avg_conf, decision_makers=people, found=bool(named))

    async def discover_contacts(self, session, *, name, domain, **ctx):
        # No standalone contact-search implementation exists for this
        # provider today — an honest empty result, not a fabricated one.
        return make_result(self.name, confidence=0.0, found=False)

    async def discover_company_metadata(self, session, *, name, domain, **ctx):
        return make_result(self.name, confidence=0.0, found=False)

    async def discover_locations(self, session, *, name, domain, **ctx):
        return make_result(self.name, confidence=0.0, found=False)

    async def discover_executives(self, session, *, name, domain, **ctx):
        result = await self.discover_decision_makers(session, name=name, domain=domain, **ctx)
        execs = [p for p in result["decision_makers"] if p.get("department") == "Executive" and p.get("name")]
        return make_result(self.name, confidence=result["confidence"] if execs else 0.0,
                            decision_makers=execs, found=bool(execs))

    async def discover_departments(self, session, *, name, domain, **ctx):
        result = await self.discover_decision_makers(session, name=name, domain=domain, **ctx)
        org = organization.build_organization(result["decision_makers"])
        return make_result(self.name, confidence=result["confidence"] if org.get("total_people") else 0.0,
                            metadata=org, found=bool(org.get("total_people")))

    async def validate_company(self, session, *, name, domain, **ctx):
        result = await self.discover_linkedin_company(session, name=name, domain=domain, **ctx)
        return make_result(self.name, confidence=result["confidence"],
                            company={"validated": result["found"]}, found=result["found"])


# === Tier 3 (future) — Apify placeholder ====================================
class ApifyProvider(BusinessIntelligenceProvider):
    """Placeholder only. Every method returns an honest empty standardized
    response; NO Apify API call is made and NO API key is required, so
    this provider can sit in the priority chain today (behind
    APIFY_ENABLED) without affecting the pipeline. Real Apify actor calls
    get implemented inside these same methods later — no other file in
    AI-BDM changes when that happens; APIFY_API_KEY is already threaded
    through to here, just unused until then.
    """

    name = "apify"

    async def _not_implemented(self) -> Dict[str, Any]:
        return make_result(self.name, confidence=0.0, found=False)

    async def discover_company(self, session, *, name, domain, **ctx):
        return await self._not_implemented()

    async def discover_social_profiles(self, session, *, name, domain, **ctx):
        return await self._not_implemented()

    async def discover_linkedin_company(self, session, *, name, domain, **ctx):
        return await self._not_implemented()

    async def discover_decision_makers(self, session, *, name, domain, **ctx):
        return await self._not_implemented()

    async def discover_contacts(self, session, *, name, domain, **ctx):
        return await self._not_implemented()

    async def discover_company_metadata(self, session, *, name, domain, **ctx):
        return await self._not_implemented()

    async def discover_locations(self, session, *, name, domain, **ctx):
        return await self._not_implemented()

    async def discover_executives(self, session, *, name, domain, **ctx):
        return await self._not_implemented()

    async def discover_departments(self, session, *, name, domain, **ctx):
        return await self._not_implemented()

    async def validate_company(self, session, *, name, domain, **ctx):
        return await self._not_implemented()


# === Registry + Manager ======================================================
# Add a future provider by writing one BusinessIntelligenceProvider subclass
# above and registering it here — nothing else in AI-BDM needs to change.
PROVIDER_REGISTRY: Dict[str, type] = {
    "google": GoogleXRayProvider,
    "apify": ApifyProvider,
}
_ENABLED_FLAGS: Dict[str, bool] = {
    "google": GOOGLE_XRAY_ENABLED,
    "apify": APIFY_ENABLED,
}

# Capabilities whose result is a set of named fields that can be filled
# in PART by one provider and the REST by the next (e.g. Website finds a
# linkedin link but not facebook/instagram/x) — tracked via `required_fields`
# in ctx. Capabilities not listed here use simple first-good-result-wins
# fallback (e.g. decision_makers: Website's own extraction is used whole if
# it found anyone at all, matching today's behavior exactly).
_MERGEABLE_CAPABILITIES = {
    "discover_social_profiles": "socials",
    "discover_company_metadata": "metadata",
    "discover_locations": "metadata",
}


class BusinessIntelligenceManager:
    """Builds the provider chain from config and executes each capability
    (discover_*) against providers in priority order: Website Intelligence
    ALWAYS first (it's free), then whichever single external enrichment
    provider is selected via BUSINESS_INTELLIGENCE_PROVIDER (only if its
    own *_ENABLED flag is set). Falls back to the next provider only when
    confidence is below `confidence_threshold` or (for mergeable
    capabilities) required fields are still missing after merging what's
    been found so far.

    Adding a provider: write one BusinessIntelligenceProvider subclass,
    register it in PROVIDER_REGISTRY. Switching the active provider:
    change BUSINESS_INTELLIGENCE_PROVIDER + its *_ENABLED flag. Neither
    requires touching this class or any call site elsewhere in AI-BDM.
    """

    def __init__(self, confidence_threshold: float = BI_CONFIDENCE_THRESHOLD):
        self.confidence_threshold = confidence_threshold
        self.providers: List[BusinessIntelligenceProvider] = [WebsiteIntelligenceProvider()]
        provider_cls = PROVIDER_REGISTRY.get(BUSINESS_INTELLIGENCE_PROVIDER)
        if provider_cls is not None and _ENABLED_FLAGS.get(BUSINESS_INTELLIGENCE_PROVIDER, False):
            self.providers.append(provider_cls())
        elif BUSINESS_INTELLIGENCE_PROVIDER not in PROVIDER_REGISTRY:
            logger.warning(
                "BUSINESS_INTELLIGENCE_PROVIDER=%r is not a registered provider "
                "(known: %s) — running Website-only.",
                BUSINESS_INTELLIGENCE_PROVIDER, sorted(PROVIDER_REGISTRY),
            )
        logger.info("BusinessIntelligenceManager providers: %s",
                    [p.name for p in self.providers])

    async def _run(
        self, capability: str, session: aiohttp.ClientSession, *,
        name: str, domain: str, cache_hit: bool = False,
        required_fields: Optional[List[str]] = None, **ctx: Any,
    ) -> Dict[str, Any]:
        if cache_hit:
            logger.info("[BI:%s] %s — cache HIT, skipping provider chain.", capability, domain)
            return make_result("cache", confidence=1.0, found=True)

        merge_field = _MERGEABLE_CAPABILITIES.get(capability)
        merged = make_result("none", confidence=0.0, found=False)
        providers_used: List[str] = []

        for provider in self.providers:
            method = getattr(provider, capability)
            start = time.monotonic()
            try:
                result = await method(session, name=name, domain=domain, **ctx)
            except Exception as exc:  # noqa: BLE001 - one provider's failure must not break the chain
                logger.warning("[BI:%s] provider=%s failed for %s: %s",
                               capability, provider.name, domain, exc)
                continue
            elapsed_ms = (time.monotonic() - start) * 1000
            fields_returned = [k for k in ("company", "socials", "decision_makers",
                                            "contacts", "metadata") if result.get(k)]
            logger.info(
                "[BI:%s] provider=%s domain=%s confidence=%.2f found=%s "
                "fields=%s elapsed_ms=%.0f cache_hit=False",
                capability, provider.name, domain, result.get("confidence", 0.0),
                result.get("found"), fields_returned, elapsed_ms,
            )
            providers_used.append(provider.name)

            if merge_field:
                target = merged[merge_field]
                for k, v in (result.get(merge_field) or {}).items():
                    if v and not target.get(k):
                        target[k] = v
                merged["confidence"] = max(merged["confidence"], result.get("confidence", 0.0))
                merged["found"] = merged["found"] or result.get("found", False)
                merged["provider"] = "+".join(providers_used)
                still_missing = [f for f in (required_fields or []) if not target.get(f)]
                if not still_missing:
                    return merged
                logger.info("[BI:%s] %s still missing %s after %s — trying next provider.",
                            capability, domain, still_missing, provider.name)
                continue

            merged = result
            if result.get("found") and result.get("confidence", 0.0) >= self.confidence_threshold:
                return result
            logger.info("[BI:%s] %s below threshold/not found from %s — falling back to next provider.",
                        capability, domain, provider.name)

        return merged

    async def discover_company(self, session, **kw): return await self._run("discover_company", session, **kw)
    async def discover_social_profiles(self, session, **kw): return await self._run("discover_social_profiles", session, **kw)
    async def discover_linkedin_company(self, session, **kw): return await self._run("discover_linkedin_company", session, **kw)
    async def discover_decision_makers(self, session, **kw): return await self._run("discover_decision_makers", session, **kw)
    async def discover_contacts(self, session, **kw): return await self._run("discover_contacts", session, **kw)
    async def discover_company_metadata(self, session, **kw): return await self._run("discover_company_metadata", session, **kw)
    async def discover_locations(self, session, **kw): return await self._run("discover_locations", session, **kw)
    async def discover_executives(self, session, **kw): return await self._run("discover_executives", session, **kw)
    async def discover_departments(self, session, **kw): return await self._run("discover_departments", session, **kw)
    async def validate_company(self, session, **kw): return await self._run("validate_company", session, **kw)


# Module-level singleton — built once from config at import time, reused
# across every domain in a run (providers themselves are stateless; any
# per-call state lives in ctx, not on the provider instances).
_manager: Optional[BusinessIntelligenceManager] = None


def get_manager() -> BusinessIntelligenceManager:
    global _manager
    if _manager is None:
        _manager = BusinessIntelligenceManager()
    return _manager
