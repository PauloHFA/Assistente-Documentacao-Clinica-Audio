"""Reversible name pseudonymization and #interno parent-summary filtering."""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from carevoice.structure.schemas import (
    ABCContent,
    Claim,
    FreeContent,
    Measure,
    SOAPContent,
    StructuredDraft,
)

_INTERNAL_LINE_PATTERN = re.compile(r"(?i)#interno\b")


@dataclass(frozen=True)
class PseudonymMapping:
    """Bijective placeholder mapping for a single transcript."""

    replacements: tuple[tuple[str, str], ...]

    def restore_text(self, value: str) -> str:
        restored = value
        for original, placeholder in self.replacements:
            restored = restored.replace(placeholder, original)
        return restored


@dataclass(frozen=True)
class PseudonymizedTranscript:
    text: str
    mapping: PseudonymMapping


def pseudonymize_transcript(
    transcript: str,
    *,
    patient_name: str,
    patient_alias: str | None = None,
    guardian_names: Sequence[str] = (),
) -> PseudonymizedTranscript:
    """Replace patient and guardian names with stable placeholders before LLM calls."""
    candidates: list[tuple[str, str]] = []
    seen_originals: set[str] = set()

    def register(raw_name: str | None, placeholder: str) -> None:
        if raw_name is None:
            return
        cleaned = " ".join(raw_name.split())
        if len(cleaned) < 2:
            return
        folded = _fold(cleaned)
        if folded not in seen_originals:
            seen_originals.add(folded)
            candidates.append((cleaned, placeholder))
        parts = [part for part in cleaned.split(" ") if len(part) >= 3]
        if len(parts) > 1:
            first_folded = _fold(parts[0])
            if first_folded not in seen_originals:
                seen_originals.add(first_folded)
                candidates.append((parts[0], placeholder))

    register(patient_name, "[PACIENTE]")
    register(patient_alias, "[PACIENTE]")
    for index, guardian in enumerate(guardian_names, start=1):
        register(guardian, f"[RESPONSAVEL_{index}]")

    # Sort longer names first to avoid partial replacement before full name.
    candidates.sort(key=lambda item: len(item[0]), reverse=True)

    masked = transcript
    applied: list[tuple[str, str]] = []
    for original, placeholder in candidates:
        pattern = _accent_insensitive_word_pattern(original)
        new_masked, count = pattern.subn(placeholder, masked)
        if count > 0:
            masked = new_masked
            applied.append((original, placeholder))

    return PseudonymizedTranscript(
        text=masked,
        mapping=PseudonymMapping(replacements=tuple(applied)),
    )


def restore_draft_pseudonyms(draft: StructuredDraft, mapping: PseudonymMapping) -> StructuredDraft:
    """Restore placeholders inside claims, evidence quotes, and measures."""
    if not mapping.replacements:
        return draft
    restore = mapping.restore_text

    def restore_claim(claim: Claim) -> Claim:
        return Claim(
            text=restore(claim.text),
            evidence=tuple(
                item.model_copy(update={"quote": restore(item.quote)}) for item in claim.evidence
            ),
        )

    def restore_measure(measure: Measure) -> Measure:
        return Measure(
            value=measure.value,
            unit=measure.unit,
            is_approximate=measure.is_approximate,
            original_expression=restore(measure.original_expression),
            goal_id=measure.goal_id,
            evidence=tuple(
                item.model_copy(update={"quote": restore(item.quote)}) for item in measure.evidence
            ),
        )

    content = draft.content
    new_content: SOAPContent | ABCContent | FreeContent
    match content:
        case SOAPContent():
            new_content = SOAPContent(
                subjective=tuple(restore_claim(c) for c in content.subjective),
                objective=tuple(restore_claim(c) for c in content.objective),
                assessment=tuple(restore_claim(c) for c in content.assessment),
                plan=tuple(restore_claim(c) for c in content.plan),
                parent_summary=tuple(restore_claim(c) for c in content.parent_summary),
                measures=tuple(restore_measure(m) for m in content.measures),
            )
        case ABCContent():
            new_episodes = tuple(
                ep.model_copy(
                    update={
                        "antecedent": restore_claim(ep.antecedent),
                        "behavior": restore_claim(ep.behavior),
                        "consequence": restore_claim(ep.consequence),
                        "hypothesized_function": (
                            restore_claim(ep.hypothesized_function)
                            if ep.hypothesized_function is not None
                            else None
                        ),
                    }
                )
                for ep in content.episodes
            )
            new_content = ABCContent(
                episodes=new_episodes,
                parent_summary=tuple(restore_claim(c) for c in content.parent_summary),
                measures=tuple(restore_measure(m) for m in content.measures),
            )
        case FreeContent():
            new_sections = tuple(
                sec.model_copy(
                    update={
                        "title": restore(sec.title),
                        "claims": tuple(restore_claim(c) for c in sec.claims),
                    }
                )
                for sec in content.sections
            )
            new_content = FreeContent(
                sections=new_sections,
                parent_summary=tuple(restore_claim(c) for c in content.parent_summary),
                measures=tuple(restore_measure(m) for m in content.measures),
            )

    return draft.model_copy(update={"content": new_content})


def filter_internal_parent_summary(draft: StructuredDraft) -> StructuredDraft:
    """Drop parent_summary claims marked or sourced from #interno notes (RF15)."""

    def keep_claim(claim: Claim) -> bool:
        if _INTERNAL_LINE_PATTERN.search(claim.text):
            return False
        return all(not _INTERNAL_LINE_PATTERN.search(item.quote) for item in claim.evidence)

    content = draft.content
    filtered_summary = tuple(claim for claim in content.parent_summary if keep_claim(claim))
    if filtered_summary == content.parent_summary:
        return draft
    return draft.model_copy(
        update={"content": content.model_copy(update={"parent_summary": filtered_summary})}
    )


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _accent_insensitive_word_pattern(term: str) -> re.Pattern[str]:
    char_map: dict[str, str] = {
        "a": "[aáàâãä]",
        "e": "[eéèêë]",
        "i": "[iíìîï]",
        "o": "[oóòôõö]",
        "u": "[uúùûü]",
        "c": "[cç]",
        "n": "[nñ]",
    }
    pieces: list[str] = []
    for char in _fold(term):
        if char.isspace():
            pieces.append(r"\s+")
        elif char in char_map:
            pieces.append(char_map[char])
        else:
            pieces.append(re.escape(char))
    body = "".join(pieces)
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)
