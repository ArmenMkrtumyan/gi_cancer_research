"""Analyse a source link that has no connector.

The registry accepts any URL a researcher pastes, so there is no way to enumerate in
advance which archives will turn up. Rather than fall back to "unrecognised link",
this module reads the page and assesses it against our schema at request time.

How it works: Gemini is given search + URL-fetch tools so it reads the real page, and
a response schema that forces the answer into the same report shape the connector path
produces. The model cannot return prose — only a filled-in report.

Everything here is best effort. A model reading a web page can misread it, so results
are tagged `analysed_by: "analysis"` and shown to users as researched-not-verified,
with the URLs the model actually cited. Reports about sources we *do* have a connector
for never come from here (see compat.build_report).
"""

import os
import random
import re
import time
from typing import List, Literal

from pydantic import BaseModel, Field

import compat

DEFAULT_MODEL = "gemini-2.5-flash-lite"

# Below this, an answer is treated as "the page could not really be read" and the next
# tool combination is tried. A genuine reading of a dataset page runs to thousands of
# characters; a couple of hundred means the model had nothing to work with.
MIN_FINDINGS_CHARS = 400

# Verdicts the analyser may return. SUPPORTED is deliberately absent: it means "a
# connector exists and download is enabled", which is a fact about our codebase, not
# about the source. Only compat.CONNECTOR_REPORTS can award it.
AnalysisVerdict = Literal["partial", "needs_review", "unsupported", "unknown"]

FillLevel = Literal["full", "partial", "none"]

Confidence = Literal["high", "medium", "low"]


class AnalysisError(RuntimeError):
    """Raised when the source could not be analysed (no key, quota, bad response)."""


class TableFit(BaseModel):
    """How one schema table would fare if this dataset were loaded."""

    table: str = Field(description="Table key. Must be one of the keys listed in the prompt.")
    fill: FillLevel = Field(
        description="'full' if the dataset supplies this table's data as completely as a "
                    "TCGA project would; 'partial' if only some columns or some rows would "
                    "land; 'none' if the dataset has nothing for it.")
    note: str = Field(
        description="One short sentence explaining the level, naming the specific fields "
                    "in the dataset where possible. Empty string if fill is 'none' and "
                    "there is nothing useful to add.")


class SourceAnalysis(BaseModel):
    """The model's assessment of a dataset link."""

    dataset_name: str = Field(description="The dataset's own name, or 'unknown'.")
    publisher: str = Field(description="Who hosts or publishes it (e.g. Kaggle, Zenodo), or 'unknown'.")
    what_it_is: str = Field(description="One plain sentence: what this dataset actually contains.")
    verdict: AnalysisVerdict = Field(
        description="'partial' = fits the schema well, just needs a connector built. "
                    "'needs_review' = recognisable but with a real mismatch to resolve. "
                    "'unsupported' = cannot be automated (login, paywall, approval required). "
                    "'unknown' = the page could not be read well enough to judge.")
    headline: str = Field(
        description="One sentence a user reads first. State the single most important fact, "
                    "including bad news. Do not hedge or pad.")
    access: str = Field(description="How someone obtains the files: open download, account required, etc.")
    n_patients: str = Field(description="Number of patients/subjects, or 'unknown'.")
    n_records: str = Field(description="Number of rows/samples/images, or 'unknown'.")
    has_images: bool = Field(description="True only if it contains medical images (slides, CT, MR).")
    file_formats: List[str] = Field(description="File formats offered, e.g. ['csv','svs']. Empty if unclear.")
    tables: List[TableFit] = Field(description="One entry per schema table key given in the prompt.")
    warnings: List[str] = Field(
        description="Concrete problems for someone loading this: licence limits, missing "
                    "identifiers, no survival data, synthetic data, etc. Empty list if none.")
    next_steps: List[str] = Field(
        description="What a person should actually do next. Empty list if nothing to do.")
    confidence: Confidence = Field(
        description="'high' only if the notes clearly describe this dataset's actual "
                    "contents. 'medium' if the main points are established but details "
                    "are missing. 'low' if the notes are thin, contradictory, or may "
                    "describe a different dataset.")
    confidence_reason: str = Field(
        description="One short sentence saying what limited your confidence, or what "
                    "made it high. Name the specific gap, not a generic caveat.")


def _inline_refs(schema):
    """Resolve `$ref`/`$defs` into a self-contained schema.

    Pydantic factors nested models (TableFit) out into `$defs` and points at them with
    `$ref`. The response-schema validator does not follow those, and silently ignores
    the whole schema when it sees one — the model then answers in prose and parsing
    fails. Inlining keeps the models readable while sending a flat schema.
    """
    defs = schema.pop("$defs", {})

    def resolve(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if ref and ref.startswith("#/$defs/"):
                target = defs.get(ref.rsplit("/", 1)[-1], {})
                siblings = {k: v for k, v in node.items() if k != "$ref"}
                return {**resolve(dict(target)), **siblings}  # siblings override the target
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return resolve(schema)


def _schema_context():
    """The table list the model must map onto, built from the report contract itself.

    Derived from compat.TABLES rather than docs/schema.dbml so the analyser can only
    describe tables the report can render, and so the two can never drift.
    """
    lines = [f"- {key}: {label} — {description}" for key, label, description in compat.TABLES]
    return "\n".join(lines)


# Two prompts because the two capabilities cannot be combined in one call: on the free
# tier, asking for a JSON response schema alongside the search tool returns
# "Tool use with a response mime type: 'application/json' is unsupported". So call one
# reads the web and answers in prose, and call two turns that prose into the report.
RESEARCH_PROMPT = """Research the dataset published at this exact URL and report what \
it actually contains.

URL: {url}

Read that page. If it is thin, you may consult its own documentation or the paper it \
cites — but only sources that are clearly about THIS dataset.

CRITICAL — do not confuse this dataset with a similarly named one. Many public \
repositories host several datasets with near-identical titles. Every figure you report \
must come from this URL or from a source that explicitly identifies itself as this \
dataset. If a search result looks similar but you cannot confirm it is the same \
dataset, ignore it and say the figure is unknown. A missing number is fine; a number \
from the wrong dataset is not.

Report, as plain notes, and after each factual claim name where it came from \
(for example "(from the dataset page)" or "(from the linked paper)"):
- The dataset's name and who publishes it.
- What one row or one file represents (a patient? a lab measurement? an image?).
- Whether there is a per-patient identifier linking rows to individual people.
- Which clinical fields exist: stage, treatment, survival time, vital status, biomarkers.
- Whether it contains real medical images (whole-slide, CT, MR) and in what format.
- How many patients and how many rows/files.
- How someone obtains the files, and any licence or account requirement.
- Whether the data is real patient data, or synthetic/simulated/teaching data.

Begin with one line: "PAGE READ: yes" if you retrieved the page's actual contents, or \
"PAGE READ: no" if you could not and are working from search results or prior \
knowledge.

State plainly when the page does not say something. Do not fill gaps with what \
datasets of this kind usually contain."""

STRUCTURE_PROMPT = """Assess whether this dataset can be loaded into an existing \
research database for gastrointestinal cancer.

The database was built around TCGA and is patient-centric. Its chain is:
patient -> diagnosis/treatment/follow-up -> tissue sample -> slide -> stored image file.

These are the only tables you may report on:
{schema}

Here are researched notes about the dataset. Base your assessment only on these — do \
not add facts of your own:

--- BEGIN NOTES ---
{findings}
--- END NOTES ---

For EVERY table key listed above, say whether loading this dataset would fill it fully, \
partly, or not at all, and why. Return exactly one entry per table key.

Rules:
- If the notes do not establish something, the value is 'unknown' and the fill level is \
your honest reading of the notes. Do not invent contents.
- If the notes show the page could not be read, set verdict to 'unknown' and say so in \
the headline.
- A dataset of aggregate statistics, or one row per record with no patient identifier, \
cannot fill the patient chain — say so plainly.
- Flag synthetic, simulated or teaching data prominently in warnings. Such datasets look \
like real cohorts but cannot support research conclusions.
- Never claim this platform can download the dataset automatically. No connector exists \
for it; that is the point of the assessment.
- Set confidence honestly. If the notes begin "PAGE READ: no", or the figures in them \
are unsourced or look like they could describe a different dataset, confidence is 'low' \
and the headline must say the assessment is provisional.

URL assessed: {url}"""


def _client():
    """Build a Gemini client, or explain what is missing."""
    if not os.environ.get("GEMINI_API_KEY"):
        raise AnalysisError("GEMINI_API_KEY is not set, so links without a connector "
                            "cannot be analysed.")
    try:
        from google import genai
    except ImportError as exc:
        raise AnalysisError(f"The Gemini SDK is not installed: {exc}") from exc
    return genai.Client()


RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY = 4.0  # seconds; doubles each attempt
RETRY_MAX_DELAY = 45.0
MIN_CALL_GAP = 4.0  # minimum seconds between calls, to stay under per-minute limits

# Free-tier capacity is heavily contended: a large share of calls come back 503
# "experiencing high demand", and bursts trip a per-minute 429. Both are transient and
# retry cleanly. Without this, transient drops surface to the user as a confident
# report saying the dataset fills no tables at all.
_TRANSIENT = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "high demand", "overloaded")

_last_call_at = 0.0  # module-level pacing clock, shared by every call in this process


# A 429 can mean two very different things. A per-minute burst clears in seconds and is
# worth retrying; an exhausted daily quota will not clear until midnight Pacific, so
# retrying just makes the user wait ~100s for the same failure.
_EXHAUSTED = ("PerDay", "RequestsPerDay", "free_tier_requests")


def _is_exhausted(exc):
    text = str(exc)
    return "429" in text and any(marker in text for marker in _EXHAUSTED)


def _is_transient(exc):
    if _is_exhausted(exc):
        return False
    return any(marker in str(exc) for marker in _TRANSIENT)


def _server_retry_delay(exc):
    """The server's own retry hint, when it sends one.

    429 responses carry `retryDelay: '7s'`. Honouring it beats guessing: back off too
    little and the retry is refused again, too much and the user waits for nothing.
    """
    match = re.search(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", str(exc))
    return float(match.group(1)) if match else None


def _pace():
    """Sleep just enough that consecutive calls stay under the per-minute limit.

    Retries are what trip the limit: several attempts fired back to back look like a
    burst. Spacing every call — not just retries — keeps a normal two-call analysis
    comfortably inside the window.
    """
    global _last_call_at
    wait = MIN_CALL_GAP - (time.monotonic() - _last_call_at)
    if wait > 0:
        time.sleep(wait)
    _last_call_at = time.monotonic()


def _generate(client, **kwargs):
    """Call generate_content, pacing calls and retrying transient capacity errors.

    Raises:
        The final exception if every attempt fails, so the caller can report why.
    """
    delay = RETRY_BASE_DELAY
    for attempt in range(RETRY_ATTEMPTS):
        _pace()
        try:
            return client.models.generate_content(**kwargs)
        except Exception as exc:
            if not _is_transient(exc) or attempt == RETRY_ATTEMPTS - 1:
                raise
            # Jitter so parallel callers don't retry in lockstep and re-burst.
            wait = _server_retry_delay(exc) or delay
            time.sleep(min(wait, RETRY_MAX_DELAY) + random.uniform(0, 1.5))
            delay = min(delay * 2, RETRY_MAX_DELAY)


def _citations(response):
    """Pull the pages the model actually consulted, so the user can check its work."""
    seen, out = set(), []
    for candidate in getattr(response, "candidates", None) or []:
        metadata = getattr(candidate, "grounding_metadata", None)
        for chunk in getattr(metadata, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            url = getattr(web, "uri", None)
            if url and url not in seen:
                seen.add(url)
                out.append({"url": url, "title": getattr(web, "title", None)})
    return out


def _research(client, model, source_url):
    """Call one: read the web. Grounded, so the answer is prose, not JSON.

    Returns:
        (findings_text, citations). Raises AnalysisError if the call fails or comes
        back empty — structuring nothing would just produce a confident empty report.
    """
    from google.genai import types

    search = types.Tool(google_search=types.GoogleSearch())
    url_context = types.Tool(url_context=types.UrlContext())
    # url_context FIRST, alone. It fetches the exact URL, whereas google_search looks up
    # the dataset by name and can land on a different one — that is what produced two
    # different row counts for the same Kaggle link. Search is a fallback for pages
    # url_context cannot reach, not the primary source.
    # (Both tools together is strongest in principle, but flash-lite returns an empty
    # body when given both — no error, finish_reason STOP — so it cannot go first.)
    attempts = ([url_context], [url_context, search], [search])

    last_error = None
    thin = None
    for tools in attempts:
        try:
            response = _generate(
                client,
                model=model,
                contents=RESEARCH_PROMPT.format(url=source_url),
                config=types.GenerateContentConfig(
                    # Temperature 0: two users checking the same link should not get
                    # different stories about it. Sampling variance shows up as
                    # judgement flip-flopping, which reads as the tool being unreliable.
                    temperature=0,
                    tools=tools,
                ),
            )
        except Exception as exc:
            last_error = exc
            continue

        findings = (response.text or "").strip()
        if len(findings) >= MIN_FINDINGS_CHARS:
            return findings, _citations(response)
        if findings and thin is None:
            # Keep the best thin answer in case every richer attempt fails; a short
            # honest "this page could not be read" still beats raising.
            thin = (findings, _citations(response))

    if thin is not None:
        return thin
    if last_error is not None:
        raise AnalysisError(f"Could not read the source: {last_error}")
    raise AnalysisError("The source could not be read — the page returned nothing usable.")


def _structure(client, model, source_url, findings):
    """Call two: turn the findings into the report. Schema-constrained, so no tools.

    Returns:
        A validated SourceAnalysis. Raises AnalysisError if the answer will not parse.
    """
    from google.genai import types

    try:
        response = _generate(
            client,
            model=model,
            contents=STRUCTURE_PROMPT.format(
                schema=_schema_context(), findings=findings, url=source_url),
            config=types.GenerateContentConfig(
                temperature=0,  # see _research: repeatability matters more than variety here
                response_mime_type="application/json",
                response_schema=_inline_refs(SourceAnalysis.model_json_schema()),
            ),
        )
    except Exception as exc:
        raise AnalysisError(f"Could not build the report: {exc}") from exc

    try:
        return SourceAnalysis.model_validate_json(response.text or "")
    except Exception as exc:
        raise AnalysisError(f"The report came back unusable: {exc}") from exc


def _evidence_check(source_url, findings, citations):
    """Check the model actually worked from the URL we asked about.

    Two independent signals, because grounding can quietly answer about a similarly
    named dataset — the failure that produced two different row counts for one link:

    * the model's own "PAGE READ:" declaration at the top of its notes, and
    * whether the requested URL's path shows up among the pages it cited.

    Args:
        source_url: The URL the user asked about.
        findings: The research call's notes.
        citations: Pages the model grounded on.

    Returns:
        (confidence_ceiling, warnings). The ceiling is None when nothing looked wrong.
    """
    warnings = []
    ceiling = None

    if re.search(r"PAGE READ:\s*no", findings, re.IGNORECASE):
        ceiling = "low"
        warnings.append(
            "The page itself could not be retrieved — this assessment is based on search "
            "results and may describe a different dataset with a similar name.")

    # Compare on the path's last segment: citation URLs are often redirect wrappers, so
    # the full URL rarely matches even when the right page was read.
    slug = [part for part in re.split(r"[/?#]", source_url) if part][-1:]
    if citations and slug:
        cited = " ".join(c["url"] for c in citations)
        if slug[0] not in cited and not any(slug[0] in (c.get("title") or "") for c in citations):
            ceiling = "low"
            warnings.append(
                "None of the pages consulted match the link you gave, so these findings "
                "may be about a different dataset. Check the cited sources below.")

    return ceiling, warnings


def _to_fills(analysis):
    """Map the model's table verdicts onto the report's fill dict.

    Unknown table keys are dropped and missing ones default to NONE, so a malformed or
    partial answer degrades to "stays empty" rather than corrupting the report.
    """
    fills = compat.empty_fills()
    for entry in analysis.tables:
        key = (entry.table or "").strip()
        if key in fills:
            fills[key] = (entry.fill, entry.note.strip() or None)
    return fills


def analyse_source(source_url, source_type="other", model=None):
    """Read a source link and report how it would map onto the schema.

    Args:
        source_url: The link to assess.
        source_type: Detected type, carried through onto the report.
        model: Gemini model id; defaults to $GEMINI_MODEL.

    Returns:
        A report dict in the same shape as compat.build_report's other paths, with
        `analysed_by` set to compat.BY_ANALYSIS and a `citations` list.

    Raises:
        AnalysisError: if the analysis could not be run or the answer was unusable.
    """
    client = _client()
    model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL

    try:
        findings, citations = _research(client, model, source_url)
        analysis = _structure(client, model, source_url, findings)
    except AnalysisError as exc:
        # Turn the raw quota error into something a user can act on, since this is the
        # failure they will hit most often on the free tier.
        if _is_exhausted(exc):
            raise AnalysisError(
                f"The daily {model} request quota is used up. It resets at midnight "
                "Pacific time. Set GEMINI_MODEL to a model with spare quota, or use a "
                "key from a different Google Cloud project.") from exc
        raise

    # The model rates its own confidence; the evidence check can only lower it, never
    # raise it. A model that read the wrong page is often confident about it, so an
    # external signal has to be able to overrule the self-assessment.
    ceiling, evidence_warnings = _evidence_check(source_url, findings, citations)
    confidence = "low" if ceiling == "low" else analysis.confidence

    label = analysis.publisher.strip()
    report = {
        "source_url": source_url,
        "source_type": source_type,
        "source_label": label if label and label.lower() != "unknown" else "Analysed source",
        "analysed_by": compat.BY_ANALYSIS,
        "connector": None,
        "downloadable": False,  # no connector exists; only compat can enable download
        "verdict": analysis.verdict,
        "headline": analysis.headline.strip(),
        "accession": analysis.dataset_name.strip() or None,
        "confidence": confidence,
        "confidence_reason": analysis.confidence_reason.strip(),
        "probe": {
            "title": analysis.dataset_name.strip(),
            "summary": analysis.what_it_is.strip(),
            "access": analysis.access.strip(),
            "n_patients": analysis.n_patients.strip(),
            "n_records": analysis.n_records.strip(),
            "has_images": analysis.has_images,
            "file_formats": analysis.file_formats,
        },
        "probe_error": None,
        # Evidence warnings lead: "this may be the wrong dataset" outranks anything the
        # model has to say about the dataset's own limitations.
        "warnings": evidence_warnings + [w.strip() for w in analysis.warnings if w.strip()],
        "next_steps": [s.strip() for s in analysis.next_steps if s.strip()],
        "citations": citations,
        "model": model,
    }
    return compat.finalise(report, _to_fills(analysis))
