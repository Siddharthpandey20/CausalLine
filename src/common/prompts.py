"""
Rendering sources into a prompt, and taking one back out again.

Three separate things need to agree on exactly one text format:

    the pipeline          renders sources into a prompt
    counterfactual check  re-issues that prompt with one source removed
    selective replay      re-issues it with one source's content *replaced*
                          by a recomputed value

The pipeline used to own the format privately (`GeminiPipeline._source_block`).
That was fine while nothing read prompts back. It is not fine now: a
counterfactual check that fails to remove the source it meant to remove
reports "output unchanged, therefore no influence", which is an unsafe
preservation produced by a formatting bug and indistinguishable from a real
result. So the format lives here, with the parser, and the two are tested
against each other rather than trusted to match.

The format:

    [S3] (web, https://example.test/1)
    ...content, any number of lines...

    [S4] (database, task/date_samples)
    ...content...

Blocks are separated by a blank line, and a header is a line that starts at
column zero and matches `[S<digits>] (...)`. Boundaries are decided by headers
alone, never by blank lines, so a source whose content contains a blank line
still parses as one block. `render_sources()` parses its own output before
returning it, so content that *would* break the parser is caught while the
prompt is being built -- not during a replay days later, where the symptom is
a wrong influence verdict rather than an exception.

WHY SURGERY TAKES THE BLOCK AS WELL AS THE PROMPT
-------------------------------------------------
A prompt is `prefix + block + suffix`, and some of ours put instructions after
the sources ("Decide the approach in at most three sentences"). Given only the
prompt there is no way to tell where the last source's content ends and the
trailing instruction begins -- both are just text after the last header.
Guessing "the last block ends at the next blank line" truncates any source
whose content contains one, and a redaction that removes *part* of a source
leaves the rest in the prompt while reporting that the source was removed.
That is a false clean produced by a parser, which is the worst kind: the
counterfactual runs, the answer comes back unchanged for the obvious reason,
and the verdict looks like evidence.

So the two-argument form is the load-bearing one: the pipeline stores the
rendered block alongside the prompt in the content store, and surgery is done
inside the block, then spliced back into the prompt as an exact substring
replacement. No boundary is ever inferred.
"""

import re

# A header line. Anchored at the start of a line, which is why content that
# happens to mention "[S3]" mid-sentence does not create a block boundary.
HEADER = re.compile(r"^\[(S\d+)\] \(([^\n]*)\)$", re.MULTILINE)

BLOCK_SEPARATOR = "\n\n"


class PromptFormatError(RuntimeError):
    """The rendered block does not parse back to what went in.

    Means some source's content contains something that looks like a block
    header. Raised at render time on purpose: the alternative is a silently
    mis-parsed prompt during counterfactual replay.
    """


class SourceNotInPrompt(KeyError):
    """Asked to redact or replace a source the prompt does not contain.

    Never silent. A no-op redaction produces an identical prompt, an identical
    answer, and the verdict "this source had no influence" -- the exact false
    clean that docs/03 issue #4 names as the dangerous direction of error.
    """


def render_source(source_id: str, kind: str, where: str, content: str) -> str:
    """One labelled block.

    The id is visible to the agent deliberately: self-report asks which inputs
    it used and it can only answer in ids if it saw them.
    """
    return f"[{source_id}] ({kind}, {where})\n{defuse(content)}"


# The same shape as HEADER, matched against one line rather than a whole block.
_HEADER_LINE = re.compile(r"\[S\d+\] \([^\n]*\)$")


def defuse(content: str) -> str:
    """Stop a source's own text from being read as a block header.

    WHY THIS EXISTS -- MEASURED ON A LIVE MODEL, 11-09-2026.
    The Researcher is told to answer "using only the numbered sources given to
    you", and those sources are labelled `[S6] (web, ...)`. A real model did the
    natural thing and quoted the labels back, at the start of a line, inside its
    answer:

        [S6] (web, https://example.invalid/gen001-d0)
        [S9] (database, task/day_first)

    That answer became a source for the Coder, and rendering the Coder's context
    re-parsed the quoted labels as real block boundaries: five sources in, eight
    blocks out. `render_sources` caught it and raised, which is correct and safe
    -- but it happened *inside a recovery replay*, so the recovery died and the
    test produced no CausalLine row at all.

    `ScriptedClient` cannot do this: its answers come from a fixed vocabulary
    with no brackets in it. So this is a failure mode only a real model
    produces, and it is exactly the kind the real-LLM mode exists to find.

    THE FIX IS ONE SPACE, AND IT IS A NO-OP ON EVERY EXISTING RUN.
    A header is anchored at column zero (see HEADER), so indenting a
    header-looking line by one space makes it content again. A source whose text
    contains no such line is returned unchanged -- which is every source in every
    measurement this project has recorded.

    Escaping rather than rejecting is the right call: the content is not
    malformed, it is a good answer that happens to quote its inputs, and the
    alternative on the replay path is losing the recovery entirely.
    """
    if not content or "[" not in content:
        return content
    return "\n".join(
        " " + line if _HEADER_LINE.match(line) else line
        for line in content.split("\n")
    )


def render_sources(items: list[tuple[str, str, str, str]]) -> str:
    """items: [(source_id, kind, where, content), ...] -> one prompt block.

    Round-trips through the parser before returning. See PromptFormatError.

    The round-trip compares against the **defused** content, because that is
    what actually goes into the prompt and therefore what a later redaction has
    to find and remove. Comparing against the raw content would fail for every
    source `defuse()` had to touch, which is the bug it exists to fix.
    """
    block = BLOCK_SEPARATOR.join(
        render_source(sid, kind, where, content) for sid, kind, where, content in items
    )
    parsed = parse_sources(block)
    expected = [(sid, defuse(content)) for sid, _, _, content in items]
    got = [(sid, content) for sid, _, content in parsed]
    if got != expected:
        mismatched = [
            sid for (sid, _), (other, _) in zip(expected, got) if sid != other
        ] or [sid for sid, _ in expected]
        raise PromptFormatError(
            "a rendered source block does not parse back to its inputs, so a "
            "later redaction would remove the wrong text. Likely a source "
            f"whose content contains a line looking like '[S1] (...)'. "
            f"Check {mismatched[:3]}."
        )
    return block


def parse_sources(block: str) -> list[tuple[str, str, str]]:
    """A rendered block -> [(source_id, header_detail, content), ...]

    Content is everything between one header and the next, with the separating
    blank line removed.
    """
    matches = list(HEADER.finditer(block))
    out: list[tuple[str, str, str]] = []
    for index, match in enumerate(matches):
        start = match.end() + 1  # skip the newline after the header
        end = matches[index + 1].start() if index + 1 < len(matches) else len(block)
        content = block[start:end]
        if content.endswith(BLOCK_SEPARATOR):
            content = content[: -len(BLOCK_SEPARATOR)]
        out.append((match.group(1), match.group(2), content.rstrip("\n")))
    return out


def sources_in(text: str) -> list[str]:
    """Source ids that appear as block headers, in order of appearance.

    Runs on a whole prompt, not just the block, so it answers "which sources
    were actually rendered into this prompt" -- which is the set a
    counterfactual is allowed to redact from.
    """
    seen: list[str] = []
    for match in HEADER.finditer(text):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


def _span(block: str, source_id: str) -> tuple[int, int]:
    """Character span of one source inside a rendered block, separator included.

    Takes a **block**, not a whole prompt. The last source runs to the end of a
    block, which is only unambiguous because a block contains nothing but
    sources -- see the module docstring on why inferring that boundary from a
    prompt is unsafe.
    """
    matches = list(HEADER.finditer(block))
    for index, match in enumerate(matches):
        if match.group(1) != source_id:
            continue
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(block)
        return start, end
    raise SourceNotInPrompt(
        f"{source_id} does not appear in this source block. "
        f"Present: {sources_in(block)}"
    )


def redact_source(block: str, source_id: str) -> str:
    """Remove one source from a rendered block.

    This is the counterfactual: the same request, minus one input. Everything
    else -- task statement, system instruction, the other sources, their order
    -- stays byte-identical, which is what makes a difference in the answer
    attributable to the removal rather than to rewording.
    """
    start, end = _span(block, source_id)
    out = block[:start] + block[end:]
    if out == block:
        raise SourceNotInPrompt(
            f"redacting {source_id} changed nothing. Refusing to run a "
            "counterfactual that did not actually remove anything."
        )
    return _tidy(out)


def replace_source(block: str, source_id: str, content: str) -> str:
    """Swap one source's content for a recomputed value, keeping its id and slot.

    Selective replay needs this: a downstream event being recomputed must see
    the *new* output of the upstream event it depended on, not the logged one,
    and it must see it under the same label in the same position so that
    nothing else about the request has moved.
    """
    start, end = _span(block, source_id)
    header = next(m for m in HEADER.finditer(block) if m.group(1) == source_id).group(0)
    tail = block[end:]
    separator = BLOCK_SEPARATOR if tail else ""
    return _tidy(block[:start] + f"{header}\n{content}" + separator + tail)


# --- splicing a modified block back into the prompt it came from --------------


def splice_block(prompt: str, block: str, new_block: str) -> str:
    """Replace `block` inside `prompt` with `new_block`, exactly once.

    Refuses anything ambiguous. If the stored block is not a substring of the
    stored prompt, the two came from different events or one of them was
    rewritten, and continuing would run a counterfactual against a request that
    is not the one being explained.
    """
    occurrences = prompt.count(block)
    if occurrences == 0:
        raise SourceNotInPrompt(
            "the stored source block does not appear in the stored prompt, so "
            "the two do not belong to the same event. Refusing to guess where "
            "the sources are."
        )
    if occurrences > 1:
        raise SourceNotInPrompt(
            f"the source block appears {occurrences} times in the prompt; "
            "which one to operate on is undefined."
        )
    return prompt.replace(block, new_block, 1)


def redact_in_prompt(prompt: str, block: str, source_id: str) -> str:
    """The counterfactual request: this prompt, without this one source."""
    return splice_block(prompt, block, redact_source(block, source_id))


def replace_in_prompt(prompt: str, block: str, source_id: str, content: str) -> str:
    """The replay request: this prompt, with one source's content recomputed."""
    return splice_block(prompt, block, replace_source(block, source_id, content))


def _tidy(text: str) -> str:
    """Collapse the run of blank lines a removal can leave behind.

    Cosmetic for a human, load-bearing for the method: three blank lines where
    there were two is a difference in the prompt, and a prompt difference is
    something a counterfactual is not allowed to introduce beyond the one
    source it removed.
    """
    return re.sub(r"\n{3,}", BLOCK_SEPARATOR, text)
