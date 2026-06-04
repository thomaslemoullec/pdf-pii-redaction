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


# anonymize_v2 — preservation-first rewrite. v1 raised fidelity issues where the model
# ADDED or "improved" content (e.g. drawing a signature where the original had none) or
# re-typeset the page. v2 frames the task as a pixel-faithful copy with ONLY PII swapped,
# explicitly forbids adding/removing any element, makes signature handling conditional,
# and tells it to keep replacements the same length so the layout never reflows.
_ANONYMIZE_V2 = """You are given a scanned document image. Produce a PIXEL-FAITHFUL copy \
of it in which ONLY the personally identifiable information has been replaced — \
everything else must look identical to the original.

REPLACE every piece of personally identifiable information with a DIFFERENT, realistic, \
synthetic value of the SAME type, format and length (same country conventions), in the \
SAME position. Catch EVERY occurrence — including salutations ("Dear …"), headers, \
footers, address blocks, and any printed name beneath a signature. Keep each replacement \
roughly the SAME length as the original so nothing reflows.

PRESERVE EVERYTHING ELSE EXACTLY — this is the priority:
- Do NOT add anything that is not already on the page: no new signature, stamp, seal, \
logo, watermark, handwriting, line, box, field or text. If a space is blank, leave it blank.
- Do NOT remove or move any non-personal element.
- Keep every number, code, amount, reference, account/document ID, non-personal date, \
label, heading, table value, line, rule, logo and stamp EXACTLY as in the original.
- Keep the same layout, fonts, sizes, weights, alignment, spacing and colours, and the \
original's scanned look (paper tone, slight skew, print vs. handwriting). Do NOT clean up, \
straighten, re-typeset, sharpen or otherwise "improve" the document.

SIGNATURES: ONLY where the original ALREADY contains a handwritten signature, replace it \
with a different synthetic scribble of similar size, style and ink. Do NOT add a signature \
where there is none, and do NOT convert a signature to typed text or typed text to a signature.

The result must be the same document, visually indistinguishable except that it carries no \
real personal data. Output only the edited image."""


# anonymize_v3 — fixes a v2 regression found in a 100-doc run: v2's "keep every …
# account/document ID, non-personal date … EXACTLY" told the model to PRESERVE account
# numbers and dates, which collide with the PII it must replace (IBANs, customer IDs,
# dates of birth) — so structured PII leaked. v3 restores an explicit "replace these"
# list (incl. IDs/IBANs/DOB) AND scopes the "keep unchanged" rule to genuinely
# NON-personal numbers/dates, while retaining v2's no-add / no-re-typeset preservation.
_ANONYMIZE_V3 = """You are given a scanned document image. Produce a faithful copy of it \
in which EVERY piece of personal data has been replaced with realistic synthetic data, and \
everything else is left identical.

REPLACE every occurrence of personal data with a DIFFERENT, realistic, synthetic value of \
the SAME type, format and length, in the SAME position. This MUST include, wherever they \
appear (body text, salutations like "Dear …", headers, footers, address blocks, tables, and \
the printed name under a signature):
- personal names
- dates of birth
- ID / passport / national-ID / customer / membership numbers that identify a person
- IBANs, bank-account and card numbers
- postal addresses
- phone numbers and email addresses
Miss none of these — a SINGLE surviving real value is a failure. Keep each replacement about \
the same length so the layout does not reflow.

KEEP everything that is NOT personal data exactly as in the original — do not alter, add, \
remove, move, re-typeset, straighten, sharpen or "improve" anything else:
- keep NON-personal numbers unchanged: amounts, balances, totals, quantities, rates, \
percentages, transaction and line-item values, document/form codes, and any date that is \
NOT a person's date of birth;
- keep all labels, headings, table structure, lines, logos, stamps, fonts, sizes, spacing, \
colours and the original scanned look (paper tone, slight skew, print vs. handwriting);
- do NOT add anything that is not already on the page: no new signature, stamp, seal, logo, \
line, box or text — if a space is blank, leave it blank;
- ONLY where a handwritten signature ALREADY exists, replace it with a different synthetic \
scribble of similar size and style; never add one, and never convert a signature to typed \
text or typed text to a signature.

The result must be the same document, visually indistinguishable except that it contains no \
real personal data. Output only the edited image."""


_ANONYMIZE_PROMPTS: dict[str, str] = {
    "anonymize_v1": _ANONYMIZE_V1,
    "anonymize_v2": _ANONYMIZE_V2,
    "anonymize_v3": _ANONYMIZE_V3,
}


def pii_prompt(version: str = "pii_v1") -> str:
    """Return the PII-scanner prompt by version, or raise ``KeyError``."""
    return _PII_PROMPTS[version]


def anonymize_prompt(version: str = "anonymize_v1") -> str:
    """Return the synthetic-anonymisation prompt by version, or raise ``KeyError``."""
    return _ANONYMIZE_PROMPTS[version]
