"""Versioned prompt templates.

Every prompt carries an explicit version (``pii_v1``, ``anonymize_v1``…) and is
exposed by version, so a production run can log the exact ``prompt_version`` it used
and a regression can be traced to a specific template revision.
"""

from __future__ import annotations

# pii_v1 — the page PII scanner. Reports the TYPES and locations of personal data
# on a page so the anonymiser knows what to remove — and is explicitly told NEVER to
# return the values, so the PII report can't itself become a leak (PII-minimal).
_PII_V1 = """You are a privacy reviewer screening a scanned document page. Report the \
PERSONALLY IDENTIFIABLE INFORMATION that is ACTUALLY VISIBLE AND READABLE on THIS page.

Report ONLY data you can actually see on the page. Do NOT enumerate the categories \
below, do NOT guess, do NOT invent entries, and do NOT assume a blank or template \
page contains anything. If the page is blank, near-blank, or has no readable \
personal data, return an EMPTY list.

For each PII occurrence you actually see, report ONLY:
- "type", one of exactly: name, date_of_birth, id_number, iban_account, address, \
phone, email, signature, other
- optionally "box": the bounding box [x0, y0, x1, y1] around it.

CRITICAL: never output the actual value of any PII (no names, numbers, dates, \
addresses) — only the type and location. The report must not itself contain \
personal data. Multilingual (EN/DE/FR). Return strictly the JSON schema; no prose."""


_PII_PROMPTS: dict[str, str] = {"pii_v1": _PII_V1}

# anonymize_v1 — the synthetic-anonymisation instruction for the image model ("Nano
# Banana"). Regenerates the page with every PII value swapped for a DIFFERENT realistic
# synthetic of the same type/format, preserving everything else — so the output is a
# faithful document carrying no real personal data.
_ANONYMIZE_V1 = """Edit this scanned document image. Replace EVERY piece of \
personally identifiable information with a DIFFERENT, realistic, synthetic value of \
the SAME type and format (same country conventions, same length), in the SAME field \
position. This includes names, dates of birth, ID/passport/document numbers, IBANs \
and account numbers, addresses, phone numbers, emails, and signatures — and you MUST \
catch EVERY occurrence, including ones in salutations ("Dear …"), headers, footers, \
and printed names under signatures. Replace a signature with a different synthetic \
signature scribble.

Change ONLY personal-data values. Do NOT alter any non-personal content: keep every \
number, code, amount, reference, table value, document ID, date that is not a \
birth/personal date, label, heading, line, logo and stamp EXACTLY as in the original. \
Do not move a value into a different field. The result must look like a genuine \
document of the same kind but contain no real personal data. Output only the edited \
image."""


_ANONYMIZE_PROMPTS: dict[str, str] = {"anonymize_v1": _ANONYMIZE_V1}


def pii_prompt(version: str = "pii_v1") -> str:
    """Return the PII-scanner prompt by version, or raise ``KeyError``."""
    return _PII_PROMPTS[version]


def anonymize_prompt(version: str = "anonymize_v1") -> str:
    """Return the synthetic-anonymisation prompt by version, or raise ``KeyError``."""
    return _ANONYMIZE_PROMPTS[version]
