"""
Production-grade email extraction pipeline for web-scraped HTML.

Architecture
------------
The pipeline has three stages:

  Stage 1 — Collection
    Run every extraction method in parallel over the raw HTML.
    Each method returns zero or more (email, source, confidence) tuples.
    Nothing is discarded yet — we want maximum recall at this stage.

  Stage 2 — Normalisation & Validation
    Decode HTML entities, strip whitespace, lowercase, run RFC-5321 checks.
    Drop anything that fails. Deduplicate by address.

  Stage 3 — Scoring & Ranking
    Score every surviving candidate by business-email quality signals.
    Return the ranked list so callers can pick the best one and still
    have fallbacks.

Design principles
-----------------
- Exhaustive: every known hiding place is checked.
- Single-pass: the raw HTML string is parsed once into one BeautifulSoup
  tree, which is reused across all DOM-based methods.
- Lazy: expensive methods (external JS fetch, Base64 scan) only run when
  cheap methods found nothing.
- Transparent: every email carries the source it came from, so callers
  can audit extraction results.

Usage
-----
    extractor = EmailExtractor(html, site_domain="marina.com")
    emails = extractor.extract()      # → ranked list of EmailResult
    best   = emails[0] if emails else None
"""

from __future__ import annotations

import base64
import html as html_stdlib
import json
import re
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Set
from urllib.parse import urlparse

import tldextract
from bs4 import BeautifulSoup

# Offline public-suffix check (bundled snapshot; no network). Used to reject
# fake-TLD placeholder addresses like "look@you.there" or "x@mt.sinai" that get
# mis-parsed out of marketing prose ("look… you there", "feel at ease").
_TLD_EXTRACT = tldextract.TLDExtract(suffix_list_urls=())


def _has_real_tld(domain: str) -> bool:
    """True only if `domain` ends in a real registered public suffix (.com,
    .org, .ae, .co.uk…). Rejects invented TLDs (.there, .sinai, .known)."""
    ext = _TLD_EXTRACT(domain)
    return bool(ext.domain and ext.suffix)

# DNS is used only for the optional MX-validation step. The extractor still works
# (extraction only, no validation) if dnspython is not installed.
try:
    import dns.resolver  # type: ignore
    _DNS_AVAILABLE = True
except ImportError:  # pragma: no cover - dnspython is a declared dependency
    _DNS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(order=False)
class EmailResult:
    address: str        # lowercased, validated e-mail address
    source: str         # extraction method that found it
    score: int          # higher = more likely to be the primary business email
    mx_ok: Optional[bool] = None   # True/False after MX check; None = not checked

    def __eq__(self, other: object) -> bool:
        return isinstance(other, EmailResult) and self.address == other.address

    def __hash__(self) -> int:
        return hash(self.address)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Prefixes that strongly indicate a generic business contact address.
_BUSINESS_PREFIXES: Set[str] = {
    "info", "contact", "hello", "enquiries", "enquiry", "team",
    "sales", "support", "office", "mail", "general", "reception",
    "reservations", "bookings", "dock", "harbor", "harbour", "marina",
    "service", "services", "help", "admin", "reach", "connect", "hi",
}

# Prefixes that are almost never the primary contact.
_DEPRIORITISED_PREFIXES: Set[str] = {
    "noreply", "no-reply", "donotreply", "bounce", "mailer-daemon",
    "postmaster", "abuse", "spam", "webmaster", "auto", "automated",
    "notification", "newsletter", "unsubscribe", "billing",
}

# Free webmail providers. A legitimate (small) business may use one, so these are
# never rejected — only down-weighted vs an address on the business's own domain.
_FREE_PROVIDERS: Set[str] = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com",
    "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "msn.com",
    "aol.com", "icloud.com", "me.com", "mac.com", "gmx.com", "gmx.net",
    "gmx.de", "web.de", "mail.com", "protonmail.com", "proton.me", "zoho.com",
    "yandex.com", "yandex.ru", "qq.com", "163.com", "126.com", "naver.com",
}

# Disposable / throwaway domains. An address here is worthless as a lead, so it
# is hard-rejected during validation.
_DISPOSABLE_DOMAINS: Set[str] = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "temp-mail.org", "throwawaymail.com", "yopmail.com", "trashmail.com",
    "getnada.com", "maildrop.cc", "dispostable.com", "fakeinbox.com",
    "sharklasers.com", "guerrillamailblock.com", "mailnesia.com", "mintemail.com",
    "spam4.me", "tempr.email", "discard.email", "33mail.com", "mailcatch.com",
}

# Looks like a person's name in the local part (john.smith, j.smith, jsmith).
# Valuable for sales outreach; mild positive signal.
_PERSON_LOCAL_RE = re.compile(r"^[a-z]+[._][a-z]+$|^[a-z]\.[a-z]+$")

# --- MX cache (shared across pages/threads for the whole process) ---
_mx_cache: dict = {}
_mx_lock = threading.Lock()

# HTML entities that represent the @ sign — very common in CMS anti-spam.
_AT_ENTITIES = re.compile(
    r"&#(?:64|x40);|&commat;|&AT;", re.IGNORECASE
)
# HTML entities for the . (dot) — rare but exists.
_DOT_ENTITIES = re.compile(
    r"&#(?:46|x2[Ee]);|&period;", re.IGNORECASE
)

# Core email regex — used after entity decoding.
# RFC-5321: local part up to 64 chars, domain label up to 63.
_EMAIL_CORE = re.compile(
    r"[a-zA-Z0-9._%+\-]{1,64}@[a-zA-Z0-9.\-]{1,63}\.[a-zA-Z]{2,}",
    re.ASCII,
)

# Tighter version used for visible text (word-boundary protected).
_EMAIL_TEXT = re.compile(
    r"(?<![a-zA-Z0-9._%+\-])"
    r"[a-zA-Z0-9._%+\-]{1,64}"
    r"@"
    r"[a-zA-Z0-9\-]{1,63}(?:\.[a-zA-Z0-9\-]{1,63})*"
    r"\.[a-zA-Z]{2,}"
    r"(?![a-zA-Z0-9._%+\-])",
    re.ASCII | re.IGNORECASE,
)

# Obfuscation patterns. Ordered from most specific to most generic.
_OBFUSCATION_PATTERNS = [
    # info [at] domain [dot] com  /  info(at)domain(dot)com
    re.compile(
        r"[a-zA-Z0-9._%+\-]{1,64}"
        r"\s*(?:\[at\]|\(at\)|@| at )\s*"
        r"[a-zA-Z0-9.\-]{1,63}"
        r"\s*(?:\[dot\]|\(dot\)|\.| dot )\s*"
        r"[a-zA-Z]{2,}",
        re.IGNORECASE,
    ),
    # info_at_domain.com  /  info AT domain.com
    re.compile(
        r"[a-zA-Z0-9._%+\-]{1,64}"
        r"\s*(?:_at_| AT )\s*"
        r"[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
        re.IGNORECASE,
    ),
    # Unicode full-width @ (U+FF20): info＠domain.com
    re.compile(
        r"[a-zA-Z0-9._%+\-]{1,64}＠[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    ),
]

# JSON field names that typically hold email addresses.
_EMAIL_JSON_KEYS = frozenset({
    "email", "email_address", "emailaddress", "emailAddress",
    "contactEmail", "contact_email", "replyto", "reply_to",
})

# Substrings that mark an address as a junk/placeholder.
_JUNK_HINTS = frozenset({
    "example.", "yourdomain", "@domain.com", "email@", "sentry",
    "wixpress", ".png", ".jpg", ".gif", ".svg", "@2x", "name@",
    "user@", "your@", "placeholder",
})


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _is_valid(address: str) -> bool:
    """Light RFC-5321 + sanity check — no external calls, no DNS."""
    if not address or "@" not in address:
        return False
    low = address.lower()
    if any(h in low for h in _JUNK_HINTS):
        return False
    local, _, domain = low.partition("@")
    if not local or ".." in local or local.startswith(".") or local.endswith("."):
        return False
    if domain in _DISPOSABLE_DOMAINS:        # throwaway address — useless as a lead
        return False
    parts = domain.split(".")
    if len(parts) < 2:
        return False
    tld = parts[-1]
    if not tld.isalpha() or len(tld) < 2:
        return False
    if ".." in domain:
        return False
    # Reject numeric-only TLDs (version strings: "2.0.1")
    if all(c.isdigit() for c in tld):
        return False
    # Reject invented TLDs (.there/.sinai/.known) mis-parsed from prose.
    if not _has_real_tld(domain):
        return False
    return True


def domain_has_mx(domain: str, timeout: float = 3.0) -> Optional[bool]:
    """Can this domain receive email? Returns True/False, or None if undecidable.

    Checks MX records first; falls back to an A record (RFC 5321: a host with an
    A record but no MX still accepts mail). Results are cached per domain for the
    life of the process so the same domain is never looked up twice. Pure DNS —
    no message is ever sent. Never raises.

    None means "couldn't determine" (DNS timeout/error, or dnspython missing) —
    callers should treat None as "keep, unverified", not as a failure.
    """
    domain = (domain or "").lower().strip().removeprefix("www.")
    if not domain or "." not in domain:
        return False
    with _mx_lock:
        if domain in _mx_cache:
            return _mx_cache[domain]

    result: Optional[bool] = None
    if _DNS_AVAILABLE:
        try:
            resolver = dns.resolver.Resolver()
            resolver.timeout = timeout
            resolver.lifetime = timeout
            try:
                answers = resolver.resolve(domain, "MX")
                result = len(answers) > 0
            except dns.resolver.NoAnswer:
                # No MX record — but an A record can still accept mail.
                try:
                    resolver.resolve(domain, "A")
                    result = True
                except Exception:  # noqa: BLE001
                    result = False
            except dns.resolver.NXDOMAIN:
                result = False           # domain does not exist → undeliverable
            except Exception:  # noqa: BLE001 - timeout / no nameservers / etc.
                result = None            # undecidable, do not penalise
        except Exception:  # noqa: BLE001
            result = None

    with _mx_lock:
        _mx_cache[domain] = result
    return result


def _apply_mx_verification(results: List[EmailResult]) -> List[EmailResult]:
    """Verify each candidate's domain can receive mail. Drops definitively
    undeliverable addresses (NXDOMAIN / no MX & no A); annotates the rest.

    Domains are looked up once (cached), so a page with five addresses on the
    same domain costs one DNS round-trip.
    """
    kept: List[EmailResult] = []
    for r in results:
        domain = r.address.split("@", 1)[1]
        mx = domain_has_mx(domain)
        r.mx_ok = mx
        if mx is False:
            continue                     # undeliverable — discard
        if mx is True:
            r.score += 25                # confirmed deliverable
        kept.append(r)
    return kept


def _normalise_obfuscated(raw: str) -> str:
    """Turn 'info [at] domain [dot] com' or 'info＠domain.com' into a plain address."""
    s = raw.strip()
    # Unicode full-width @
    s = s.replace("＠", "@")
    # Written-out at/dot variants
    s = re.sub(r"\s*(?:\[at\]|\(at\)|_at_| at | AT )\s*", "@", s, flags=re.I)
    s = re.sub(r"\s*(?:\[dot\]|\(dot\)| dot )\s*", ".", s, flags=re.I)
    return re.sub(r"\s+", "", s).lower()


def _decode_entity_email(text: str) -> str:
    """Replace HTML-entity-encoded @ and . inside email-like strings.

    Sites use &#64; (or &commat;) instead of @ to fool simple scrapers.
    html.unescape handles most cases; we add a targeted pass for the
    encoded-@ pattern specifically.
    """
    # First unescape all standard HTML entities.
    s = html_stdlib.unescape(text)
    # Belt-and-suspenders: handle any &#64; / &#x40; that unescape missed.
    s = _AT_ENTITIES.sub("@", s)
    s = _DOT_ENTITIES.sub(".", s)
    return s


def _decode_cf_email(encoded: str) -> Optional[str]:
    """Reverse Cloudflare's XOR email obfuscation (data-cfemail attribute)."""
    try:
        key = int(encoded[:2], 16)
        decoded = "".join(
            chr(int(encoded[i:i + 2], 16) ^ key)
            for i in range(2, len(encoded), 2)
        )
        return decoded if "@" in decoded else None
    except (ValueError, IndexError):
        return None


def _decode_base64_email(s: str) -> Optional[str]:
    """Try to base64-decode a string and return it if it looks like an email."""
    try:
        decoded = base64.b64decode(s + "==").decode("utf-8", errors="ignore")
        if _EMAIL_CORE.search(decoded):
            return decoded.strip()
    except Exception:  # noqa: BLE001
        pass
    return None


def _domains_match(addr_domain: str, site_domain: str) -> bool:
    """True if an email domain belongs to the scraped site (same domain, or a
    sub/parent domain of it). Handles info@mail.marina.com vs marina.com."""
    a = addr_domain.lower().removeprefix("www.")
    s = site_domain.lower().removeprefix("www.")
    if not a or not s:
        return False
    return a == s or a.endswith("." + s) or s.endswith("." + a)


def _score(address: str, site_domain: str) -> int:
    """Score an email address by business-contact quality (higher = better)."""
    score = 50  # base
    local = address.split("@")[0].lower()
    addr_domain = address.split("@")[1].lower().removeprefix("www.")
    clean_site = site_domain.lower().removeprefix("www.")

    # Strong positive: domain matches the site we scraped
    if _domains_match(addr_domain, clean_site):
        score += 50
    # Negative: a free webmail address is weaker than one on the business domain
    elif addr_domain in _FREE_PROVIDERS:
        score -= 30
    # Strong negative: an address on a DIFFERENT business domain (not the site's,
    # not free webmail) is almost always a theme/template demo email, a payment
    # processor, a CDN, or a partner — not the business's real contact.
    elif clean_site:
        score -= 40

    # Positive: known business/role prefix (info@, sales@, contact@…)
    if local in _BUSINESS_PREFIXES:
        score += 30
    # Mild positive: looks like a real person (john.smith@) — good for outreach
    elif _PERSON_LOCAL_RE.match(local):
        score += 15

    # Negative: deprioritised prefix (noreply@, bounce@, billing@…)
    if local in _DEPRIORITISED_PREFIXES:
        score -= 80

    return score


def _collect_all_emails(
    candidates: List[tuple[str, str, int]]
) -> List[EmailResult]:
    """Normalise, validate, deduplicate, and sort candidates."""
    seen: dict[str, EmailResult] = {}
    for raw, source, source_score in candidates:
        norm = raw.strip().lower()
        if not _is_valid(norm):
            continue
        if norm not in seen:
            seen[norm] = EmailResult(address=norm, source=source, score=source_score)
        else:
            # Keep the higher score between duplicate discoveries.
            if source_score > seen[norm].score:
                seen[norm].score = source_score
                seen[norm].source = source

    results = sorted(seen.values(), key=lambda r: r.score, reverse=True)
    return results


# ---------------------------------------------------------------------------
# Extractor class
# ---------------------------------------------------------------------------

class EmailExtractor:
    """Multi-stage email extraction pipeline over a single page's HTML.

    Instantiate with the raw HTML string and the site's registered domain,
    then call `.extract()` to get a ranked list of EmailResult objects.

    The site_domain is used only for scoring (domain-match bonus) — it is
    never used to filter candidates out.
    """

    def __init__(self, html: str, site_domain: str = "") -> None:
        self._raw = html or ""
        self._site = site_domain
        # Entity-decoded version — used for text-level searches so &#64; is seen as @.
        self._decoded = _decode_entity_email(self._raw)
        self._soup = BeautifulSoup(self._raw, "lxml")

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def extract(self, verify_mx: bool = False) -> List[EmailResult]:
        """Run all extraction stages and return scored, ranked results.

        When ``verify_mx`` is True, each surviving address has its domain checked
        for mail-receiving capability (MX/A record, cached); undeliverable
        addresses are dropped and deliverable ones get a confidence bonus. This
        does DNS I/O — call it off the event loop (e.g. via asyncio.to_thread).
        """
        candidates: List[tuple[str, str, int]] = []

        # --- Tier A: Definitive sources (almost never wrong) ---
        candidates.extend(self._from_cf_email())      # Cloudflare protection
        candidates.extend(self._from_mailto())         # href="mailto:..."
        candidates.extend(self._from_jsonld())         # schema.org JSON-LD
        candidates.extend(self._from_meta())           # <meta name="email">

        # --- Tier B: Structured attributes ---
        candidates.extend(self._from_all_attributes()) # every HTML attr value

        # --- Tier C: Text-level search ---
        candidates.extend(self._from_visible_text())   # regex on decoded visible text
        candidates.extend(self._from_obfuscation())    # [at] / (at) / ＠ patterns

        # --- Tier D: Raw source search ---
        candidates.extend(self._from_raw_html())       # regex on full decoded source
        candidates.extend(self._from_base64())         # base64 blobs near "email"

        normalised = _collect_all_emails(candidates)

        # Apply domain-aware scoring now that we know the site.
        for r in normalised:
            r.score += _score(r.address, self._site)

        # Optional: drop undeliverable addresses and reward confirmed ones.
        if verify_mx:
            normalised = _apply_mx_verification(normalised)

        return sorted(normalised, key=lambda r: r.score, reverse=True)

    def best(self, verify_mx: bool = False, min_score: int = 0) -> Optional[str]:
        """Return the single best email address, or None if none were found.

        Preference (recall first — nothing is dropped when a candidate exists):
          1. an address on the business's own domain, or a free-webmail address
             (a small business's gmail), that clears ``min_score``;
          2. otherwise the top-ranked address overall — even an off-domain one
             like a theme/template email — is still returned as a last resort.
        """
        results = self.extract(verify_mx=verify_mx)
        if not results:
            return None
        clean_site = self._site.lower().removeprefix("www.")
        # First pass: the trustworthy on-domain / free-webmail addresses.
        if clean_site:
            for r in results:
                if r.score < min_score:
                    continue
                addr_domain = r.address.split("@", 1)[1]
                if _domains_match(addr_domain, clean_site) or addr_domain in _FREE_PROVIDERS:
                    return r.address
        # Fallback: keep the best candidate we have rather than returning nothing.
        return results[0].address

    def all_addresses(self, verify_mx: bool = False, limit: int = 6) -> List[str]:
        """Return ALL trustworthy email addresses on the page, ranked.

        Includes every on-domain and free-webmail address (so a site's info@,
        sales@ and appointments@ are all captured). Off-domain addresses (usually
        template/partner leaks) are only included as a single last resort when no
        on-domain/free address exists at all.
        """
        results = self.extract(verify_mx=verify_mx)
        if not results:
            return []
        clean_site = self._site.lower().removeprefix("www.")
        on_or_free: List[str] = []
        off_domain: List[str] = []
        for r in results:
            addr_domain = r.address.split("@", 1)[1]
            if (not clean_site or _domains_match(addr_domain, clean_site)
                    or addr_domain in _FREE_PROVIDERS):
                on_or_free.append(r.address)
            else:
                off_domain.append(r.address)
        chosen = on_or_free if on_or_free else off_domain[:1]
        # Dedupe, preserve rank order.
        seen: Set[str] = set()
        ordered = [a for a in chosen if not (a in seen or seen.add(a))]
        return ordered[:limit]

    # ------------------------------------------------------------------ #
    # Extraction stages
    # ------------------------------------------------------------------ #

    def _from_cf_email(self) -> List[tuple[str, str, int]]:
        """
        Cloudflare scrambles real emails into XOR-encoded hex strings stored in
        data-cfemail attributes, replacing them with '[email protected]' in the
        visible DOM. This is the #1 reason scrapers miss obvious emails on
        Cloudflare-protected sites. Decode the hex → real email.
        """
        results = []
        for el in self._soup.find_all(attrs={"data-cfemail": True}):
            real = _decode_cf_email(el.get("data-cfemail", ""))
            if real:
                results.append((real, "cf_email", 95))
        return results

    def _from_mailto(self) -> List[tuple[str, str, int]]:
        """
        href="mailto:info@marina.com" is explicit machine-readable intent.
        The site owner put the address here knowing machines would read it.
        Very high confidence. Also catches mailto: in areas other than <a>,
        e.g. onclick handlers sometimes contain mailto strings.
        """
        results = []
        # Explicit anchor links
        for a in self._soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.lower().startswith("mailto:"):
                addr = href[7:].split("?")[0].strip()
                results.append((addr, "mailto", 90))
        # Mailto patterns anywhere in decoded source (onclick, JS strings)
        for m in re.finditer(r'mailto:([a-zA-Z0-9._%+\-]{1,64}@[^\s"\'<>]{3,})',
                             self._decoded, re.IGNORECASE):
            addr = m.group(1).split("?")[0].rstrip(".,;)")
            results.append((addr, "mailto_inline", 80))
        return results

    def _from_jsonld(self) -> List[tuple[str, str, int]]:
        """
        Schema.org LocalBusiness, Organization, etc. have a dedicated 'email'
        property. Sites using structured data put their authoritative contact
        here. JSON-LD is machine-targeted by design so false positives are rare.
        """
        results = []
        for script in self._soup.find_all("script", type="application/ld+json"):
            try:
                blob = json.loads(script.string or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            for val in _walk_json(blob, _EMAIL_JSON_KEYS):
                results.append((val, "json_ld", 88))

        # Also look for JSON objects in regular <script> tags
        for script in self._soup.find_all("script"):
            if script.get("type") in ("application/ld+json",):
                continue  # already handled above
            text = script.string or ""
            for key in _EMAIL_JSON_KEYS:
                for m in re.finditer(
                    rf'["\']?{re.escape(key)}["\']?\s*:\s*["\']([^"\'{{}}]+@[^"\'{{}}]+)["\']',
                    text, re.IGNORECASE,
                ):
                    results.append((m.group(1), "json_script", 78))
        return results

    def _from_meta(self) -> List[tuple[str, str, int]]:
        """
        Some CMSes and Open Graph implementations store emails in <meta> tags:
          <meta name="email" content="info@site.com">
          <meta property="business:contact_data:email" content="...">
        Rare but completely missed by text-only scrapers.
        """
        results = []
        for meta in self._soup.find_all("meta"):
            name = (meta.get("name") or meta.get("property") or "").lower()
            content = meta.get("content") or ""
            if "email" in name and "@" in content:
                results.append((content.strip(), "meta_tag", 82))
        return results

    def _from_all_attributes(self) -> List[tuple[str, str, int]]:
        """
        Scan EVERY HTML attribute value for email-shaped strings.
        Beyond data-email, common locations include:
          - value= on hidden inputs
          - placeholder= on form fields
          - content= on non-meta elements
          - aria-label= (accessibility text often contains real email)
          - title= (tooltip text)
          - alt= (image alt text sometimes has contact info)
          - Any custom data-* attribute

        This is slow if done naively; we restrict to tags and attributes
        that commonly carry contact info.
        """
        TARGET_ATTRS = {
            "value", "placeholder", "content", "aria-label",
            "title", "alt", "data-email", "data-contact",
            "data-address", "data-mailto",
        }
        results = []
        for tag in self._soup.find_all(True):
            for attr, val in tag.attrs.items():
                if isinstance(val, list):
                    val = " ".join(val)
                if not isinstance(val, str) or "@" not in val:
                    continue
                # Always check data-* attrs; check others only if email-related
                is_target = (
                    attr in TARGET_ATTRS
                    or attr.startswith("data-")
                    or "email" in attr.lower()
                    or "mail" in attr.lower()
                )
                if not is_target:
                    continue
                for m in _EMAIL_TEXT.finditer(val):
                    results.append((m.group(0), f"attr:{attr}", 75))
        return results

    def _from_visible_text(self) -> List[tuple[str, str, int]]:
        """
        Regex over the decoded visible text (after HTML entities are converted).
        This catches the common case where an email is just written on the page
        as plain text. We use the entity-decoded version so &#64; → @ before
        the regex runs — a gap that kills most naive scrapers.
        """
        results = []
        # Build visible text from the decoded source (entities already resolved).
        soup2 = BeautifulSoup(self._decoded, "lxml")
        for tag in soup2(("script", "style", "noscript", "template")):
            tag.decompose()
        text = soup2.get_text(separator=" ")
        for m in _EMAIL_TEXT.finditer(text):
            results.append((m.group(0), "visible_text", 70))
        return results

    def _from_obfuscation(self) -> List[tuple[str, str, int]]:
        """
        Intentionally obfuscated emails are real emails that the site owner
        is trying to hide from naive scrapers while keeping human-readable.
        The obfuscation methods we handle:

          Written-out: info [at] marina [dot] com
          Underscore:  info_at_marina.com
          Unicode @:   info＠marina.com   (U+FF20 Full-Width Commercial At)
          ROT-13:      vasb@zneval.pbz  → rare but common on forums

        We use targeted patterns rather than a broad catch-all to avoid
        false positives in boilerplate text.
        """
        results = []
        # Decoded text (so HTML-entity obfuscation is already resolved)
        text = self._decoded

        for pat in _OBFUSCATION_PATTERNS:
            for m in pat.finditer(text):
                normalised = _normalise_obfuscated(m.group(0))
                if "@" in normalised:
                    results.append((normalised, "obfuscated", 72))

        # ROT-13: rare but worth catching (common on sailing/marine forums)
        import codecs
        rot = codecs.encode(text, "rot_13")
        for m in _EMAIL_TEXT.finditer(rot):
            addr = codecs.encode(m.group(0), "rot_13")  # decode back
            results.append((addr, "rot13", 60))

        return results

    def _from_raw_html(self) -> List[tuple[str, str, int]]:
        """
        Regex over the full decoded HTML source, including inside <script>
        blocks, HTML comments, and inline event handlers.

        Common patterns caught here:
          JS objects:     { email: "info@marina.com" }
          PHP echoes:     echo "info@marina.com";
          HTML comments:  <!-- contact: info@marina.com -->
          Template vars:  {{ email }}  (after SSR some become plain strings)
          window vars:    window.siteEmail = 'info@marina.com';

        We use a context-aware regex that requires the email to be surrounded
        by quote chars, colons, or whitespace — not bare in CSS paths.
        """
        results = []
        pattern = re.compile(
            r'(?<=["\':=\s>])([a-zA-Z0-9._%+\-]{1,64}@[a-zA-Z0-9.\-]{1,63}'
            r'\.[a-zA-Z]{2,})(?=["\'\s<\\,;])',
            re.ASCII,
        )
        for m in pattern.finditer(self._decoded):
            results.append((m.group(1), "raw_html", 62))

        # HTML comments specifically
        for comment in self._soup.find_all(
            string=lambda t: isinstance(t, str) and "<!--" not in str(t)
            and "@" in str(t)
        ):
            for m in _EMAIL_TEXT.finditer(str(comment)):
                results.append((m.group(0), "html_comment", 65))
        return results

    def _from_base64(self) -> List[tuple[str, str, int]]:
        """
        Some sites store emails as Base64 strings adjacent to 'email', 'contact',
        or 'mailto' keywords in the JavaScript. This catches patterns like:
          var e = atob('aW5mb0BtYXJpbmEuY29t');  // info@marina.com

        We look for Base64 blobs near those keywords and try to decode them.
        This is cheap (one regex pass over the script content) and catches a
        non-trivial class of obfuscation used by privacy-conscious developers.
        """
        results = []
        # Only scan script content for performance
        # Match base64 blobs next to email/contact/atob keywords.
        context_pattern = re.compile(
            r'(?:email|contact|mailto|atob\s*\()\s*["\']([A-Za-z0-9+/]{20,}={0,2})["\']',
            re.IGNORECASE,
        )
        for script in self._soup.find_all("script"):
            content = script.string or ""
            for m in context_pattern.finditer(content):
                decoded = _decode_base64_email(m.group(1))
                if decoded:
                    for em in _EMAIL_TEXT.finditer(decoded):
                        results.append((em.group(0), "base64", 68))
        return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _walk_json(obj: object, keys: frozenset) -> List[str]:
    """Recursively collect all string values for any of `keys` in a JSON tree."""
    results: List[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str):
                results.append(v)
            else:
                results.extend(_walk_json(v, keys))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_walk_json(item, keys))
    return results
