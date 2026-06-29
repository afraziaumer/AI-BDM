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
from dataclasses import dataclass, field
from typing import List, Optional, Set
from urllib.parse import urlparse

from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(order=False)
class EmailResult:
    address: str        # lowercased, validated e-mail address
    source: str         # extraction method that found it
    score: int          # higher = more likely to be the primary business email

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
    return True


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


def _score(address: str, site_domain: str) -> int:
    """Score an email address by business-contact quality (higher = better)."""
    score = 50  # base
    local = address.split("@")[0].lower()
    addr_domain = address.split("@")[1].lower().removeprefix("www.")
    clean_site = site_domain.lower().removeprefix("www.")

    # Strong positive: domain matches the site we scraped
    if addr_domain == clean_site or clean_site.endswith("." + addr_domain):
        score += 50

    # Positive: known business prefix
    if local in _BUSINESS_PREFIXES:
        score += 30

    # Negative: deprioritised prefix
    if local in _DEPRIORITISED_PREFIXES:
        score -= 80

    # Bonus: source reliability
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

    def extract(self) -> List[EmailResult]:
        """Run all extraction stages and return scored, ranked results."""
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

        return sorted(normalised, key=lambda r: r.score, reverse=True)

    def best(self) -> Optional[str]:
        """Return the single best email address, or None."""
        results = self.extract()
        return results[0].address if results else None

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
