"""PDF rendering for approved clinical session versions and periodic reports."""

import html
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from threading import Lock

import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

from carevoice.structure.schemas import (
    ABCContent,
    ABCEpisode,
    Claim,
    FreeContent,
    Measure,
    SOAPContent,
    StructuredDraft,
)

_FONT_REGULAR = "CareVoiceSans"
_FONT_BOLD = "CareVoiceSansBold"
_FONTS_REGISTERED = False
_FONT_REGISTRATION_LOCK = Lock()
_UNIT_LABELS = {
    "percentage": "%",
    "seconds": "s",
    "minutes": "min",
    "count": "ocorrências",
    "scale": "pontos",
}


@dataclass(frozen=True)
class PDFIdentity:
    patient_name: str
    professional_name: str
    profession: str
    council_registry: str


class PDFRenderer:
    """Render Unicode-capable PDFs; every dynamic value is HTML-escaped."""

    def render(
        self,
        draft: StructuredDraft,
        identity: PDFIdentity,
        *,
        is_rectification: bool = False,
        rectification_reason: str | None = None,
    ) -> bytes:
        self._register_fonts()
        buffer = BytesIO()
        document = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=48,
            leftMargin=48,
            topMargin=54,
            bottomMargin=56,
            title="Evolução clínica",
            author="CareVoice AI",
        )
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "CareVoiceTitle",
            parent=styles["Title"],
            fontName=_FONT_BOLD,
            fontSize=17,
            leading=21,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#183B56"),
            spaceAfter=12,
        )
        heading_style = ParagraphStyle(
            "CareVoiceHeading",
            parent=styles["Heading2"],
            fontName=_FONT_BOLD,
            fontSize=12,
            leading=16,
            textColor=colors.HexColor("#183B56"),
            keepWithNext=True,
        )
        body_style = ParagraphStyle(
            "CareVoiceBody",
            parent=styles["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=10,
            leading=14,
            wordWrap="LTR",
            splitLongWords=True,
            spaceAfter=5,
        )

        story: list[Flowable] = [
            Paragraph("Evolução clínica", title_style),
        ]
        if is_rectification:
            stamp = f"Retificação da evolução de {draft.session_date:%d/%m/%Y}"
            if rectification_reason:
                stamp = f"{stamp} — Motivo: {html.escape(rectification_reason)}"
            story.append(Paragraph(f"<b>{stamp}</b>", body_style))
        story.extend(
            [
                Paragraph(f"<b>Paciente:</b> {html.escape(identity.patient_name)}", body_style),
                Paragraph(f"<b>Data da sessão:</b> {draft.session_date:%d/%m/%Y}", body_style),
                Paragraph(
                    f"<b>Profissional:</b> {html.escape(identity.professional_name)}",
                    body_style,
                ),
                Paragraph(
                    f"<b>Profissão:</b> {html.escape(identity.profession)}",
                    body_style,
                ),
                Paragraph(
                    f"<b>Registro:</b> {html.escape(identity.council_registry)}",
                    body_style,
                ),
                Spacer(1, 10),
            ]
        )
        story.extend(_content_story(draft, heading_style, body_style))
        story.extend(
            [
                Spacer(1, 16),
                Paragraph(
                    "Documento elaborado com apoio de IA e revisado pelo profissional responsável.",
                    body_style,
                ),
            ]
        )
        document.build(
            story,
            onFirstPage=_draw_page_number,
            onLaterPages=_draw_page_number,
        )
        return buffer.getvalue()

    def render_periodic_report(
        self,
        identity: PDFIdentity,
        *,
        start_date: date,
        end_date: date,
        sessions: Sequence[tuple[int, StructuredDraft]],
        goal_summaries: Sequence[str] = (),
    ) -> bytes:
        """Render a periodic consolidated clinical progress PDF (RF23)."""
        self._register_fonts()
        buffer = BytesIO()
        document = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=48,
            leftMargin=48,
            topMargin=54,
            bottomMargin=56,
            title="Relatório periódico clínico",
            author="CareVoice AI",
        )
        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(
            "CareVoiceReportTitle",
            parent=styles["Title"],
            fontName=_FONT_BOLD,
            fontSize=16,
            leading=20,
            alignment=TA_CENTER,
            textColor=colors.HexColor("#183B56"),
            spaceAfter=12,
        )
        heading_style = ParagraphStyle(
            "CareVoiceReportHeading",
            parent=styles["Heading2"],
            fontName=_FONT_BOLD,
            fontSize=12,
            leading=16,
            textColor=colors.HexColor("#183B56"),
            keepWithNext=True,
        )
        body_style = ParagraphStyle(
            "CareVoiceReportBody",
            parent=styles["BodyText"],
            fontName=_FONT_REGULAR,
            fontSize=10,
            leading=14,
            wordWrap="LTR",
            splitLongWords=True,
            spaceAfter=5,
        )

        story: list[Flowable] = [
            Paragraph("Relatório clínico periódico", title_style),
            Paragraph(f"<b>Paciente:</b> {html.escape(identity.patient_name)}", body_style),
            Paragraph(
                f"<b>Período:</b> {start_date:%d/%m/%Y} a {end_date:%d/%m/%Y}",
                body_style,
            ),
            Paragraph(
                f"<b>Profissional:</b> {html.escape(identity.professional_name)} "
                f"({html.escape(identity.profession)} — {html.escape(identity.council_registry)})",
                body_style,
            ),
            Paragraph(f"<b>Sessões aprovadas no período:</b> {len(sessions)}", body_style),
            Spacer(1, 8),
        ]
        if goal_summaries:
            story.append(Paragraph("Evolução de metas", heading_style))
            for summary in goal_summaries:
                story.append(Paragraph(html.escape(summary), body_style))
            story.append(Spacer(1, 8))

        for version_number, draft in sessions:
            rect_tag = " (Retificada)" if version_number > 1 else ""
            story.append(
                Paragraph(
                    f"Sessão {draft.session_date:%d/%m/%Y} — v{version_number}{rect_tag}",
                    heading_style,
                )
            )
            story.extend(_content_story(draft, heading_style, body_style))
            story.append(Spacer(1, 8))

        story.append(
            Paragraph(
                "Documento elaborado com apoio de IA e revisado pelo profissional responsável.",
                body_style,
            )
        )
        document.build(
            story,
            onFirstPage=_draw_page_number,
            onLaterPages=_draw_page_number,
        )
        return buffer.getvalue()

    @staticmethod
    def _register_fonts() -> None:
        global _FONTS_REGISTERED
        if _FONTS_REGISTERED:
            return
        with _FONT_REGISTRATION_LOCK:
            if _FONTS_REGISTERED:
                return
            font_directory = Path(reportlab.__file__).parent / "fonts"
            regular_path = font_directory / "Vera.ttf"
            bold_path = font_directory / "VeraBd.ttf"
            if not regular_path.is_file() or not bold_path.is_file():
                raise RuntimeError("ReportLab Unicode font files are unavailable")
            pdfmetrics.registerFont(TTFont(_FONT_REGULAR, str(regular_path)))
            pdfmetrics.registerFont(TTFont(_FONT_BOLD, str(bold_path)))
            _FONTS_REGISTERED = True


def _draw_page_number(canvas: object, document: object) -> None:
    canvas.saveState()  # type: ignore[attr-defined]
    canvas.setFont(_FONT_REGULAR, 8)  # type: ignore[attr-defined]
    canvas.setFillColor(colors.HexColor("#5B6573"))  # type: ignore[attr-defined]
    canvas.drawRightString(547, 28, f"Página {document.page}")  # type: ignore[attr-defined]
    canvas.restoreState()  # type: ignore[attr-defined]


def _content_story(
    draft: StructuredDraft,
    heading_style: ParagraphStyle,
    body_style: ParagraphStyle,
) -> list[Flowable]:
    story: list[Flowable] = [
        Paragraph(f"Template: {draft.template}", heading_style),
    ]
    match draft.content:
        case SOAPContent():
            sections = (
                ("Subjetivo", draft.content.subjective),
                ("Objetivo", draft.content.objective),
                ("Avaliação", draft.content.assessment),
                ("Plano", draft.content.plan),
                ("Resumo para responsáveis", draft.content.parent_summary),
            )
            for title, claims in sections:
                story.extend(_claims_section(title, claims, heading_style, body_style))
            story.extend(_measures_section(draft.content.measures, heading_style, body_style))
        case ABCContent():
            story.append(Paragraph("Episódios ABC", heading_style))
            for index, episode in enumerate(draft.content.episodes, start=1):
                story.extend(_episode_section(index, episode, heading_style, body_style))
            story.extend(
                _claims_section(
                    "Resumo para responsáveis",
                    draft.content.parent_summary,
                    heading_style,
                    body_style,
                )
            )
            story.extend(_measures_section(draft.content.measures, heading_style, body_style))
        case FreeContent():
            for section in draft.content.sections:
                story.extend(
                    _claims_section(section.title, section.claims, heading_style, body_style)
                )
            story.extend(
                _claims_section(
                    "Resumo para responsáveis",
                    draft.content.parent_summary,
                    heading_style,
                    body_style,
                )
            )
            story.extend(_measures_section(draft.content.measures, heading_style, body_style))
    return story


def _claims_section(
    title: str,
    claims: tuple[Claim, ...],
    heading_style: ParagraphStyle,
    body_style: ParagraphStyle,
) -> list[Flowable]:
    story: list[Flowable] = [Paragraph(html.escape(title), heading_style)]
    if not claims:
        story.append(Paragraph("Não informado.", body_style))
    else:
        for claim in claims:
            story.append(Paragraph(html.escape(claim.text), body_style))
    story.append(Spacer(1, 5))
    return story


def _episode_section(
    index: int,
    episode: ABCEpisode,
    heading_style: ParagraphStyle,
    body_style: ParagraphStyle,
) -> list[Flowable]:
    parts: list[Flowable] = [
        Paragraph(f"Episódio {index}", heading_style),
        Paragraph(f"<b>Antecedente:</b> {html.escape(episode.antecedent.text)}", body_style),
        Paragraph(f"<b>Comportamento:</b> {html.escape(episode.behavior.text)}", body_style),
        Paragraph(f"<b>Consequência:</b> {html.escape(episode.consequence.text)}", body_style),
    ]
    if episode.hypothesized_function is not None:
        parts.append(
            Paragraph(
                f"<b>Função hipotetizada:</b> {html.escape(episode.hypothesized_function.text)}",
                body_style,
            )
        )
    return [KeepTogether(parts)]


def _measures_section(
    measures: tuple[Measure, ...],
    heading_style: ParagraphStyle,
    body_style: ParagraphStyle,
) -> list[Flowable]:
    story: list[Flowable] = [Paragraph("Medidas", heading_style)]
    if not measures:
        story.append(Paragraph("Não informado.", body_style))
    else:
        for measure in measures:
            approximate = "~" if measure.is_approximate else ""
            number = _format_decimal(measure.value)
            unit = _UNIT_LABELS[measure.unit]
            expression = html.escape(measure.original_expression)
            story.append(
                Paragraph(
                    f"{approximate}{number} {html.escape(unit)} — expressão original: {expression}",
                    body_style,
                )
            )
    story.append(Spacer(1, 5))
    return story


def _format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f").replace(".", ",")
