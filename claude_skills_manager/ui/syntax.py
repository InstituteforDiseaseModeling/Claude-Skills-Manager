"""Lightweight per-extension syntax highlighters.

Production code might delegate to Pygments, but a hand-rolled set of
QSyntaxHighlighters keeps the dependency footprint small and renders
fast on big files."""
from __future__ import annotations

from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextDocument


def _fmt(color: str, *, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Bold)
    if italic:
        f.setFontItalic(True)
    return f


class _RuleHighlighter(QSyntaxHighlighter):
    """Apply a fixed list of (regex, format) rules per block."""

    rules: list[tuple[QRegularExpression, QTextCharFormat]] = []

    def highlightBlock(self, text: str) -> None:  # noqa: N802 — Qt naming
        for pattern, fmt in self.rules:
            it = pattern.globalMatch(text)
            while it.hasNext():
                match = it.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), fmt)


class PythonHighlighter(_RuleHighlighter):
    KEYWORDS = (
        "and as assert async await break class continue def del elif else except "
        "False finally for from global if import in is lambda None nonlocal not "
        "or pass raise return True try while with yield match case"
    ).split()

    def __init__(self, document: QTextDocument) -> None:
        super().__init__(document)
        kw_fmt = _fmt("#0033B3", bold=True)
        self.rules = [(QRegularExpression(rf"\b{kw}\b"), kw_fmt) for kw in self.KEYWORDS]
        self.rules += [
            (QRegularExpression(r"\bdef\s+(\w+)"),    _fmt("#7A3E9D", bold=True)),
            (QRegularExpression(r"\bclass\s+(\w+)"),  _fmt("#7A3E9D", bold=True)),
            (QRegularExpression(r"@\w+"),             _fmt("#9E880D")),
            (QRegularExpression(r"\b\d+(\.\d+)?\b"),  _fmt("#1750EB")),
            # strings — basic, single-line
            (QRegularExpression(r'"[^"\\]*(\\.[^"\\]*)*"'), _fmt("#067D17")),
            (QRegularExpression(r"'[^'\\]*(\\.[^'\\]*)*'"), _fmt("#067D17")),
            # comments last so they win
            (QRegularExpression(r"#.*"),              _fmt("#8C8C8C", italic=True)),
        ]


class JsonHighlighter(_RuleHighlighter):
    def __init__(self, document: QTextDocument) -> None:
        super().__init__(document)
        self.rules = [
            (QRegularExpression(r'"(?:[^"\\]|\\.)*"\s*(?=:)'), _fmt("#871094", bold=True)),
            (QRegularExpression(r'"(?:[^"\\]|\\.)*"'),        _fmt("#067D17")),
            (QRegularExpression(r"\b(true|false|null)\b"),    _fmt("#0033B3", bold=True)),
            (QRegularExpression(r"-?\b\d+(\.\d+)?\b"),        _fmt("#1750EB")),
        ]


class MarkdownHighlighter(_RuleHighlighter):
    def __init__(self, document: QTextDocument) -> None:
        super().__init__(document)
        self.rules = [
            (QRegularExpression(r"^#{1,6} .*$"),     _fmt("#0033B3", bold=True)),
            (QRegularExpression(r"\*\*[^*]+\*\*"),   _fmt("#000000", bold=True)),
            (QRegularExpression(r"(?<!\*)\*[^*\n]+\*(?!\*)"), _fmt("#000000", italic=True)),
            (QRegularExpression(r"`[^`]+`"),         _fmt("#067D17")),
            (QRegularExpression(r"^\s*>.*"),         _fmt("#8C8C8C", italic=True)),
            (QRegularExpression(r"^\s*[-*+]\s+"),    _fmt("#7A3E9D", bold=True)),
            (QRegularExpression(r"\[[^\]]+\]\([^)]+\)"), _fmt("#1750EB")),
        ]


_EXT_MAP: dict[str, type[QSyntaxHighlighter]] = {
    "py": PythonHighlighter,
    "json": JsonHighlighter,
    "md": MarkdownHighlighter,
    "markdown": MarkdownHighlighter,
}


def highlighter_for_extension(extension: str, document: QTextDocument) -> QSyntaxHighlighter | None:
    cls = _EXT_MAP.get(extension.lower().lstrip("."))
    return cls(document) if cls else None
