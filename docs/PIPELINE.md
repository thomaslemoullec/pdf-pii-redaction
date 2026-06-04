# How it works — from launch to reviewed output

The full walkthrough of what happens to your documents, every Gemini/DLP call, and **why**
each step is built the way it is. The shape is **detect → don't trust → verify → let a
human decide**. (For the README summary, see the project [README](../README.md).)

## Stage 0 — You configure the job

From the launch form (or the CLI) you set the source `gs://` prefix, the output prefix, an
optional **free-text description** of the documents, and a **review policy** (all docs, or
only flagged).

> **Why free text, not sampling?** A diverse corpus's PII can't be inferred reliably by
> peeking at a few pages. Letting the operator say what they expect is more accurate, and
> it becomes the scope for the certified DLP detectors.

## Stage 1 — Plan the PII scope · *Gemini (Flash)*

One constrained Gemini call maps the description to the controlled PII vocabulary
(`name`, `iban_account`, `address`, …). Output is filtered against that vocabulary, so a
hallucinated type is dropped; a blank description ⇒ *scan for everything*.

> **Why Flash here, Pro everywhere else?** This is lightweight and text-only — getting it
> slightly wrong only widens/narrows the scan scope, it **can't cause a leak** (the vision
> scan is the real detector). So it runs on the cheaper tier; every accuracy-critical step
> stays on Pro.

## Stage 2 — Scan each page · *Gemini vision ∪ Cloud DLP*

An ensemble unions two detectors per page: **Gemini vision** (types + location, never
values) and **Cloud DLP** (certified infoTypes — checksum-validated IBANs etc., scoped to
the planned types, `include_quote=false`).

> **Why two detectors?** They fail differently — vision catches PII with no regex
> (handwriting, signatures, names in salutations); DLP catches structured PII with
> certified precision. The union maximises recall. **Types/locations, never values** —
> because the scan report is stored, and storing values would itself be a leak.

## Stage 3 — Read the source once · *Gemini vision (Pro)*

One call transcribes the source page and extracts its real PII as `{type, value}`. These
values are used **only in-memory**, to later check each one actually disappeared.

> **Why once?** The source doesn't change across retries, so extracting it every attempt
> would be wasted work.

## Stage 4 — Synthesise → check → retry · *the redaction agent*

This loop is a small **agent** (`redaction_agent.py`): it calls each signal *and the
decision* as an explicit, swappable tool — `SynthesizeTool`, `MetricsTool`, `DlpLeakTool`,
`JudgeTool` (the LLM-as-judge subagent), a deterministic `RedactionPolicy`, and a
`FeedbackTool`. For each page, bounded by `max_attempts`:

1. **Synthesise** · *Nano Banana Pro* — regenerate the page, every real value → a realistic
   fake of the same type/format, layout untouched. On a retry the previous correction is
   fed back in.
2. **Certified leak check (value-carryover)** · *Cloud DLP*, **on by default** — DLP reads
   the real values on the source and synthetic pages; any source value that still appears
   is a hard, certified leak. It compares *values*, so synthetic fakes never trip it.
3. **Judge** — output transcribed (Pro), deterministic **metrics** computed, and the
   **LLM judge** (Pro) returns `{leaked, all_pii_removed, layout_preserved, rationale}`.
4. **Decide** — `pass` returns; otherwise a targeted correction is built and it retries.
   The **best-scoring** attempt is kept.

> **Why three independent leak signals?** Defense in depth. The **metrics** compare the
> Gemini-extracted values (covers vision-only PII DLP can't OCR). The **DLP carryover**
> compares certified values (an independent extractor — a real value must slip past *two*
> detectors to be missed). The **LLM** adds a semantic rationale. The **decision is
> deterministic** — it gates leaks, so it must be auditable, not probabilistic.
> **Why value-carryover, not a type-presence re-scan?** Synthesis intentionally writes fake
> PII; "is there an IBAN here?" would flag the fake on every page. Comparing actual values
> means a fake never trips it, while a real survivor (even reformatted) is caught.
> **Why bounded retries?** Each attempt costs an image-gen + Pro vision calls; a stubborn
> page goes to a human instead of looping.

## What you see

Live progress, a run report (counts + estimated cost), and a **worst-scoring-first** review
queue: original vs synthetic side-by-side, detected PII, score, verdict, and the judge's
rationale. Validate or reject; validated docs move to `…/validated/<doc>/`.

## Every model / DLP call at a glance

| Step | When | Service |
|---|---|---|
| Plan PII scope | per job | Gemini Flash |
| Scan (vision) | per page | Gemini Pro |
| Scan (DLP) | per page | Cloud DLP |
| Read source | per page | Gemini Pro |
| Synthesise | per attempt | Nano Banana Pro |
| DLP carryover (source + each output) | per page / per attempt | Cloud DLP |
| Transcribe output + LLM judge | per attempt | Gemini Pro |
