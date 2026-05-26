"""Left panel — skills grouped by SkillType, with type-toggle and search filtering."""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap, QPolygon,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QMenu, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from ..models import Skill, SkillType
from ..skill_settings import (
    BINARY_STATES, STATE_NAME_ONLY, STATE_OFF, STATE_ON, STATE_PLUGIN_OFF,
    STATE_USER_INVOCABLE_ONLY,
)

# Temporary diagnostic logger for the §7.66 drag-drop feature. Routes
# through the app's normal logging pipeline (Help → Open Log Folder),
# and also prints to stderr so `python main.py` from a terminal shows
# the same events live. Remove once the drag-drop is verified working.
_dd_log = logging.getLogger("claude_skills_manager.dragdrop")

# Custom item-data role for the per-item disambiguation suffix. Stored on
# each QTreeWidgetItem so refresh_state() (triggered by Enable/Disable
# toggles) can re-style without re-running the full collision pass.
_DISAMBIG_ROLE = Qt.UserRole + 1


# `:!active` keeps the highlight visible when the tree loses focus
# (e.g. the user clicks into the editor) — otherwise Windows would dim
# the selection to near-invisible grey.
_SKILL_LIST_STYLE = """
QTreeWidget::item {
    padding: 3px 4px;
}
QTreeWidget::item:selected {
    background: #2d6cdf;
    color: white;
    font-weight: bold;
}
QTreeWidget::item:selected:!active {
    background: #c7defc;
    color: #1a1a1a;
    font-weight: bold;
}
QTreeWidget::item:hover:!selected {
    background: #f0f4fb;
}
"""

# Per-type icon palette: distinct hue AND distinct shape, so the type is
# distinguishable for users with color-vision differences (color alone would
# leave Project/Plugin too close). Drawn at 32×32 logical px with DPR=2 so the
# icons render crisply on HiDPI displays.
_TYPE_PAINT: dict[SkillType, tuple[str, str]] = {
    SkillType.GLOBAL:  ("#5350a3", "circle"),    # indigo disc
    SkillType.PROJECT: ("#2e8b57", "square"),    # green rounded square
    SkillType.PLUGIN:  ("#d97706", "diamond"),   # amber diamond
}

# QIcon objects can't exist before QApplication; these caches are populated
# on first call (which only happens once SkillListPanel is instantiated).
# Three icon variants encode the three "control levels" we need to surface
# after §7.63 broke the original shape-distinguishes-which-off assumption:
#
#   * full color       → STATE_ON / NAME_ONLY / USER_INVOCABLE_ONLY
#                        (visible or partially visible in Claude Code)
#   * faded type color → STATE_OFF (user-toggled disabled — flip via this
#                        app's right-click → Enable)
#   * solid grey       → STATE_PLUGIN_OFF (inherited disabled — the
#                        parent plugin is off; manage via /plugin in
#                        Claude Code)
#
# OFF uses opacity reduction (the row is "the same kind of thing as ON,
# just turned off by you"); PLUGIN_OFF uses a hue swap to grey (the row
# is a "different kind of thing — gated by a layer this app doesn't own").
# Shape is preserved across all three so the type-distinguishing channel
# (circle/square/diamond → Global/Project/Plugin) stays intact for users
# with color-vision differences. See §7.64.
_type_icon_cache: dict[SkillType, QIcon] = {}
_type_icon_faded_cache: dict[SkillType, QIcon] = {}
_type_icon_plugin_off_cache: dict[SkillType, QIcon] = {}

# Opacity used for the OFF tier. 0.35 reads as muted at typical tree-row
# sizes (16x16 logical) while still letting the type color show through;
# tuned by eye against the indigo / green / amber triplet so no single
# hue disappears at the dimmed level.
_FADED_OPACITY = 0.35

# Solid grey used for the PLUGIN_OFF tier. The eye picks up a hue change
# faster than an opacity change at small sizes, so painting in grey
# (rather than the type's amber/etc.) is the load-bearing distinction
# from the OFF tier. Chosen to sit between the OFF text grey (#9a9a9a)
# and the PLUGIN_OFF text grey (#c0c0c0) so the icon reads as the
# row's darkest element — still readable, but unmistakably grey.
_PLUGIN_OFF_COLOR = "#a8a8a8"


def _icon_for(skill_type: SkillType) -> QIcon:
    """Return (and lazily build + cache) the full-color badge icon for a
    skill type — used for ON / OFF / NAME_ONLY / USER_INVOCABLE_ONLY rows."""
    cached = _type_icon_cache.get(skill_type)
    if cached is not None:
        return cached
    icon = _paint_type_icon(skill_type, 1.0)
    _type_icon_cache[skill_type] = icon
    return icon


def _icon_for_faded(skill_type: SkillType) -> QIcon:
    """Return (and lazily build + cache) a dimmed version of the type
    icon, used for ``STATE_OFF`` rows — skills the user has toggled off
    through this app. Shape is preserved so the type-distinguishing
    channel survives the dimming."""
    cached = _type_icon_faded_cache.get(skill_type)
    if cached is not None:
        return cached
    icon = _paint_type_icon(skill_type, _FADED_OPACITY)
    _type_icon_faded_cache[skill_type] = icon
    return icon


def _icon_for_plugin_off(skill_type: SkillType) -> QIcon:
    """Return (and lazily build + cache) a grey-painted version of the
    type icon, used for ``STATE_PLUGIN_OFF`` rows — skills whose parent
    plugin is currently disabled. The hue swap (type color → grey) at
    full opacity signals 'this is gated by a layer above this app's
    control'; manage via /plugin. Distinct from :func:`_icon_for_faded`
    so two diamonds sitting next to each other visually announce
    *which* kind of disabled they are — the OFF one stays amber but
    dim, the PLUGIN_OFF one goes grey."""
    cached = _type_icon_plugin_off_cache.get(skill_type)
    if cached is not None:
        return cached
    icon = _paint_type_icon(skill_type, opacity=1.0, color_override=_PLUGIN_OFF_COLOR)
    _type_icon_plugin_off_cache[skill_type] = icon
    return icon


def _paint_type_icon(
    skill_type: SkillType,
    opacity: float,
    *,
    color_override: str | None = None,
) -> QIcon:
    """Paint the badge for a skill type. Three variants share this code
    path — full color (ON), faded type color (OFF), and solid grey
    (PLUGIN_OFF) — so there is no second copy of the shape geometry to
    drift out of sync. ``color_override`` swaps the brush color while
    keeping the shape, used by the PLUGIN_OFF variant to render in grey
    instead of the type's hue (§7.64).

    DPR ordering matters (see §7.24): we paint at physical coords on a
    plain 32x32 pixmap, THEN tag DPR=2.0 as metadata. Setting DPR before
    painting flips the painter into logical (16x16) coords, which clips
    most of the shape — the long-standing "quarter-shape" rendering bug
    that masqueraded as deliberate design."""
    color_hex, shape = _TYPE_PAINT[skill_type]
    if color_override is not None:
        color_hex = color_override
    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setOpacity(opacity)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color_hex))
    if shape == "circle":
        painter.drawEllipse(4, 4, 24, 24)
    elif shape == "square":
        painter.drawRoundedRect(4, 4, 24, 24, 6, 6)
    elif shape == "diamond":
        painter.drawPolygon(QPolygon([
            QPoint(16, 2), QPoint(30, 16), QPoint(16, 30), QPoint(2, 16),
        ]))
    painter.end()
    pix.setDevicePixelRatio(2.0)
    return QIcon(pix)


# Toolbar State filter exposes two checkboxes; classification follows §7.14:
# middle states (name-only, user-invocable-only) still load in Claude Code,
# so they count as Enabled. The synthesized plugin-off counts as Disabled.
STATE_GROUP_ENABLED  = "enabled"
STATE_GROUP_DISABLED = "disabled"


def _state_group(state: str) -> str:
    """Map a skill's effective state to its toolbar-filter bucket."""
    if state in (STATE_OFF, STATE_PLUGIN_OFF):
        return STATE_GROUP_DISABLED
    return STATE_GROUP_ENABLED


def _skill_type_for_header_text(text: str) -> SkillType | None:
    """Recover a :class:`SkillType` from a header's display text.

    Header labels look like ``"Global  (5)"`` — the leading token is
    the canonical type name. Returns ``None`` if no SkillType matches,
    which means the row isn't actually a recognised group header
    (defensive — used only as a fallback when the header's
    ``Qt.UserRole`` data tag has been lost). Existing call sites set
    that tag at header construction time, so this fallback should
    rarely trigger; keeping it cheap (single split + linear scan over
    three enum values) means we can afford to call it without worry."""
    if not text:
        return None
    leading = text.split()[0]
    for st in SkillType:
        if st.value == leading:
            return st
    return None


def _search_haystack(skill: Skill) -> str:
    """Lowercased substring-match target for the search box (§7.30).

    * Plugin rows: ``"<name> <plugin>"`` — typing the plugin name in
      the search box surfaces every skill under that plugin, even when
      the skill name itself doesn't contain the typed text (e.g.
      typing ``discord`` matches ``configure``, ``access``).
    * Project rows: ``"<name> <project-folder>"`` — same idea for
      monorepo / multi-project setups.
    * Global rows: just ``"<name>"`` — the group header already says
      "Global"; there's no additional axis to search against.

    Built on the always-on context label from §7.29 so the rule
    ``"what the user sees on the row is also what matches the search"``
    holds: every visible token is searchable, and no hidden tokens
    can match."""
    ctx = _context_label(skill)
    if ctx:
        return f"{skill.name} {ctx}".lower()
    return skill.name.lower()


def _context_label(skill: Skill) -> str:
    """Always-on trailing context for plugin & project rows (§7.29).

    Returns the plugin name for Plugin skills, the project folder name
    (the directory containing ``.claude/``) for Project skills, and
    ``""`` for Global skills (which need no extra context — the group
    header already says "Global").

    Distinct from the on-collision disambiguation in
    ``_build_disambiguation_map``: that suffix only appears when two
    rows would otherwise render identically; this one is *always* on
    so the user can see at a glance which plugin a skill belongs to or
    which project root it came from."""
    if skill.type == SkillType.PLUGIN and skill.plugin_id:
        return skill.plugin_id.partition("@")[0]
    if skill.type == SkillType.PROJECT:
        try:
            return skill.path.parents[2].name
        except IndexError:
            return ""
    return ""


def _apply_state_style(
    item: QTreeWidgetItem, skill: Skill, *, disambiguation: str = "",
) -> None:
    """Apply the per-item visual styling based on a skill's effective state.

    Default ("on") leaves the row alone. Disabled (binary "off" or inherited
    "plugin-off") dims and italicizes. Non-binary states ("name-only",
    "user-invocable-only") get a small bracketed suffix and a softer color
    so the user knows the override is non-default and read-only.

    Final label layout for a row is
    ``<name> · <context>   [<disambig>]   [<state>]``, where each segment
    is optional:

    * ``<context>`` (always-on plugin/project name; see
      ``_context_label``) appears on every Plugin and Project row.
    * ``[<disambig>]`` (on-collision-only; see
      ``_build_disambiguation_map``) appears only when two visible rows
      would otherwise render identically *and* the contexts don't
      already disambiguate them.
    * ``[<state>]`` is the state suffix below (``name-only`` /
      ``user-only``)."""
    state = skill.state
    context = _context_label(skill)
    context_suffix = f"  ·  {context}" if context else ""
    label = skill.name + context_suffix + disambiguation
    # Always reset to a fresh font/color/icon so re-rendering the row
    # clears any styling left over from a previous state. The icon reset
    # matters specifically for transitions out of PLUGIN_OFF (a faded
    # icon must revert to full-color) — if a future iteration adds a
    # "Disable plugin" affordance that flips PLUGIN_OFF → ON live, the
    # icon swap is already wired in.
    plain_font = QFont()
    item.setFont(0, plain_font)
    item.setData(0, Qt.ForegroundRole, None)
    item.setIcon(0, _icon_for(skill.type))

    if state == STATE_ON:
        item.setText(0, label)
        return
    if state == STATE_OFF:
        # User-toggled disabled: faded type icon + italic + medium grey.
        # The user wrote skillOverrides[name] = "off" via this app (or
        # /skills); flipping it back is a single click.
        item.setText(0, label)
        item.setIcon(0, _icon_for_faded(skill.type))
        italic = QFont()
        italic.setItalic(True)
        item.setFont(0, italic)
        item.setForeground(0, QColor("#9a9a9a"))
        return
    if state == STATE_PLUGIN_OFF:
        # Inherited disabled (plugin layer): icon painted in grey
        # (instead of the type's amber/etc.) + italic + lighter grey
        # text. After §7.63 plugin skills can appear in either STATE_OFF
        # or STATE_PLUGIN_OFF; the icon SHAPE alone no longer
        # disambiguates them, so the hue swap to grey carries the
        # distinction — the eye picks up hue change much faster than
        # opacity change at row-icon sizes. The tooltip enrichment in
        # _rebuild names the disabled plugin on hover.
        item.setText(0, label)
        item.setIcon(0, _icon_for_plugin_off(skill.type))
        italic = QFont()
        italic.setItalic(True)
        item.setFont(0, italic)
        item.setForeground(0, QColor("#c0c0c0"))
        return
    if state == STATE_NAME_ONLY:
        item.setText(0, f"{label}   [name-only]")
        item.setForeground(0, QColor("#777777"))
        return
    if state == STATE_USER_INVOCABLE_ONLY:
        item.setText(0, f"{label}   [user-only]")
        item.setForeground(0, QColor("#777777"))
        return
    item.setText(0, label)


def _build_disambiguation_map(visible: list[Skill]) -> dict[Path, str]:
    """For each skill that collides on display name with another in the
    *visible* list **within the same group header (type)**, compute a
    "minimum disambiguator" suffix.

    Disambiguation is **scoped per group** (Global / Plugin / Project) on
    purpose: the tree already separates the three with their own headers,
    so e.g. ``explain-code`` appearing once under Global and once under
    Project is *visually* unambiguous already — adding ``[zhaoweidu]`` /
    ``[pyCOMPS_zdu]`` suffixes would be redundant noise. Suffixes are
    only earned when two siblings under the same header would otherwise
    render identically.

    The suffix is the shortest piece of context that distinguishes the
    colliding peers within their group:

    * Two plugin skills sharing a plugin but installed under different
      marketplaces (e.g. ``skill-creator`` in ``claude-plugins-official``
      vs ``.staging``) → ``[<marketplace>]``.
    * Plugin skills with the same name in different plugins of the same
      marketplace (e.g. ``configure`` in ``discord``/``imessage``/
      ``telegram``) → ``[<plugin>]``.
    * Plugin skills differing on both axes → ``[<plugin>@<marketplace>]``.
    * Global/Project skills colliding by name within their own header
      (e.g. two project roots under a monorepo with same-named skills)
      → ``[<scope-folder>]``, where ``<scope-folder>`` is the directory
      containing ``.claude/``.

    Skills without an in-group name collision get no suffix (returned
    dict omits them) — the common case stays visually quiet."""
    # Bucket by (type, name) so collisions are scoped to the group the
    # skill appears under in the tree. ``SkillType`` is an enum and is
    # hashable, so it's a natural tuple-key partner with the name string.
    by_group_name: dict[tuple, list[Skill]] = {}
    for s in visible:
        by_group_name.setdefault((s.type, s.name), []).append(s)

    out: dict[Path, str] = {}
    for group in by_group_name.values():
        if len(group) < 2:
            continue
        # If the always-on context labels (§7.29) already give every
        # peer in the group a distinct trailing string, the bracketed
        # disambiguator would be redundant — skip it. We only need
        # ``[<disambig>]`` when contexts collide too (e.g., the same
        # plugin name installed under two different marketplaces — both
        # rows render with the same plugin context, so a marketplace
        # suffix is the only remaining differentiator).
        contexts = [_context_label(s) for s in group]
        if len(set(contexts)) == len(group):
            continue
        for skill in group:
            suffix = _disambiguator_for(skill, group)
            if suffix:
                out[skill.path] = f"   [{suffix}]"
    return out


def _disambiguator_for(skill: Skill, group: list[Skill]) -> str:
    """Pick the minimum disambiguator label for ``skill`` against its
    name-collision peers. See ``_build_disambiguation_map`` for the rules.

    Marketplace value is derived from the on-disk *folder name* rather
    than the manifest's canonical ``name`` — because two installs of the
    same marketplace (e.g. ``claude-plugins-official`` and
    ``claude-plugins-official.staging``) share the same manifest name but
    occupy different folders. Folder names are unique by definition (they
    are sibling directories), so they always distinguish two installs;
    manifest names don't. The plugin part of ``plugin_id`` is still the
    canonical name, since that's what the user typed into ``/plugin``."""
    if skill.plugin_id is not None:
        plugin_name, _, _ = skill.plugin_id.partition("@")
        marketplace = _marketplace_folder_for(skill) or skill.plugin_id.partition("@")[2]

        def plugin_of(s: Skill) -> str:
            return s.plugin_id.partition("@")[0] if s.plugin_id else ""

        def market_of(s: Skill) -> str:
            return _marketplace_folder_for(s) or (
                s.plugin_id.partition("@")[2] if s.plugin_id else "")

        plugins_differ = len({plugin_of(s) for s in group}) > 1
        markets_differ = len({market_of(s) for s in group}) > 1
        if plugins_differ and not markets_differ:
            return plugin_name
        if markets_differ and not plugins_differ:
            return marketplace
        # Both differ (or peers lack plugin_id) — full coordinate.
        return f"{plugin_name}@{marketplace}"

    # Global / Project: the canonical layout is <scope>/.claude/skills/<name>/.
    # parents[2] = <scope>; its `.name` is the human-meaningful folder
    # ("pyCOMPS_zdu" rather than the full absolute path).
    try:
        return skill.path.parents[2].name
    except IndexError:
        return ""


def _marketplace_folder_for(skill: Skill) -> str:
    """Return the on-disk marketplace folder name for a plugin skill, or
    ``""`` if the path doesn't match the expected
    ``.../plugins/marketplaces/<X>/...`` shape.

    Walks path components rather than indexing ``parents[N]`` because the
    depth differs across the three supported layouts (A: plugin under
    ``plugins/<p>``, B: plugin as direct child of marketplace, C: shared
    skills/ directly under marketplace). The ``marketplaces`` segment is
    invariant — find it, return the next component."""
    parts = skill.path.parts
    for i, part in enumerate(parts):
        if part == "marketplaces" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


class _SkillTreeWidget(QTreeWidget):
    """QTreeWidget subclass with outgoing drag of Project skill rows
    and incoming drop on the Global section header.

    Drag-source eligibility is gated by ``Qt.ItemIsDragEnabled`` on
    each row (set only on Project rows in
    :meth:`SkillListPanel._rebuild`). Qt's ``startDrag`` filters by
    the flag before invoking :meth:`mimeData`, so Global / Plugin
    rows never enter our payload path — defense in depth alongside
    the per-type mutation rule (CLAUDE.md). The Global header
    receives ``Qt.ItemIsDropEnabled`` while Project / Plugin headers
    do not, so Qt's hit-testing rejects drops on the wrong sections
    without needing an explicit position check beyond
    :meth:`_target_skill_type`.

    On a committed drop we emit :attr:`skill_drop_requested` and
    deliberately do NOT chain through ``super().dropEvent`` —
    Qt's default would attempt internal model rearrangement, which
    has no meaning here. The drop is a *signal-triggering gesture*;
    the actual on-disk mutation happens in
    :meth:`MainWindow._on_skill_drop` after the user picks Copy /
    Move / Cancel in a confirmation dialog."""

    SKILL_MIME_TYPE = "application/x-claude-skill"

    # (source: Skill, target_type: SkillType)
    skill_drop_requested = Signal(object, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        # Viewport acceptance is the load-bearing call. For
        # ``QAbstractItemView`` subclasses, drop events are routed
        # to the **viewport widget**, not the view widget — calling
        # ``setAcceptDrops`` only on ``self`` is unreliable across
        # PySide6 versions because Qt's view-level call doesn't
        # always forward to the viewport. Without this line, Qt
        # silently drops every dragMove / drop event before any
        # of our overrides fire, so the cursor never updates and
        # dropEvent never runs — symptoms the user reports as
        # "no behavior at all over the Global header."
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        # DragDrop allows both outgoing drags and incoming drops, with
        # our overrides controlling acceptance. InternalMove would let
        # Qt rearrange items within the tree by default — explicitly
        # not what we want.
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        # §7.66 diagnostic — confirms the subclass is actually being
        # instantiated by SkillListPanel (rather than something
        # silently falling back to plain QTreeWidget).
        _dd_log.info(
            "[dd] _SkillTreeWidget initialized "
            "(dragEnabled=%s, acceptDrops=%s, "
            "viewport.acceptDrops=%s, dragDropMode=%s)",
            self.dragEnabled(),
            self.acceptDrops(),
            self.viewport().acceptDrops(),
            self.dragDropMode())
        # Cache of the source Skill captured at drag start (in
        # ``mimeData``) and consumed at drop time (in ``dropEvent``).
        # The MIME payload carries the source path string for "is
        # this our drag?" identification on the receive side; the
        # live object reference here spares us a tree-walk re-lookup
        # at drop time and is the canonical payload for the upward
        # signal.
        self._drag_source: Skill | None = None

    def mimeTypes(self) -> list[str]:
        # Advertise only our custom format. Qt's default ("application/
        # x-qabstractitemmodeldatalist", used for internal tree
        # rearrange) would let foreign drags claim to match — by
        # narrowing to our format we make the MIME filter on the
        # receive side a one-line check.
        return [self.SKILL_MIME_TYPE]

    def mimeData(self, items: list[QTreeWidgetItem]) -> QMimeData | None:
        _dd_log.info(
            "[dd] mimeData called, items=%d", len(items) if items else 0)
        # Qt's startDrag has already filtered items by
        # ItemIsDragEnabled before calling us, so a Global / Plugin
        # row can't appear here. Defensive isinstance still cheap.
        if not items:
            _dd_log.warning("[dd] mimeData: no items, drag will die")
            return None
        skill = items[0].data(0, Qt.UserRole)
        if not isinstance(skill, Skill):
            _dd_log.warning(
                "[dd] mimeData: items[0].data is not a Skill (%r), "
                "drag will die", type(skill).__name__)
            return None
        self._drag_source = skill
        mime = QMimeData()
        # Bytes payload exists so Qt's MIME hand-shake is well-
        # formed (advertising a format then returning empty data
        # confuses some downstream consumers). The drop side reads
        # the live ``self._drag_source`` object rather than parsing
        # this back into a Skill — encoding/decoding loses the
        # type/state/description fields we already have in hand.
        mime.setData(
            self.SKILL_MIME_TYPE,
            str(skill.path).encode("utf-8"))
        _dd_log.info(
            "[dd] mimeData: returning payload for %s (%s)",
            skill.name, skill.type.value)
        # Dump header Y-ranges so the next log can be cross-
        # referenced against dragMoveEvent vp_pos.y values to see
        # whether the cursor actually reaches the Global header.
        # Wrapped in try/except because diagnostic code MUST NEVER
        # break the feature under observation — the previous
        # version's ``int(header.flags())`` raised on PySide6 6.5+,
        # bubbled out of mimeData, returned nullptr to Qt, and
        # silently killed every drag operation. Passive diagnostics
        # is a discipline.
        try:
            for i in range(self.topLevelItemCount()):
                header = self.topLevelItem(i)
                rect = self.visualItemRect(header)
                data = header.data(0, Qt.UserRole)
                flags = header.flags()
                # data is a plain str after the QVariant round-trip
                # (SkillType is a str-Enum, so PySide6 stores only
                # the underlying string). Repr it directly rather
                # than trying isinstance — the previous "type=?"
                # output was the smoking-gun signal that this
                # round-trip was the bug.
                _dd_log.info(
                    "[dd] header[%d] type_raw=%r, y_range=(%d..%d), "
                    "IsEnabled=%s, IsDropEnabled=%s, "
                    "IsSelectable=%s",
                    i, data,
                    rect.top(), rect.bottom(),
                    bool(flags & Qt.ItemIsEnabled),
                    bool(flags & Qt.ItemIsDropEnabled),
                    bool(flags & Qt.ItemIsSelectable))
        except Exception as exc:
            _dd_log.warning(
                "[dd] header-dump failed (non-fatal): %r", exc)
        return mime

    def _viewport_pos(self, event) -> QPoint:
        """Map a ``QDropEvent.position()`` (widget-relative) to the
        viewport's coordinate system, which is what ``itemAt`` and
        ``visualItemRect`` both expect.

        QDropEvent.position() is documented as "relative to the
        receiving widget" — for ``QTreeWidget`` that's the widget
        itself, not its viewport. A 1-2px frame offset is usually
        invisible, but at item edges (the topmost row, the bottom
        of the last row) the off-by-frame pushes the cursor just
        outside Qt's internal hit-rect and ``itemAt`` returns
        ``None``. Mapping through ``viewport().mapFrom`` makes the
        two coordinate systems consistent."""
        return self.viewport().mapFrom(
            self, event.position().toPoint())

    def _header_at_y(self, y: int) -> QTreeWidgetItem | None:
        """Geometric fallback for ``itemAt`` over a header row.

        PySide6's ``itemAt`` returns ``None`` for items lacking
        ``Qt.ItemIsSelectable`` — including all of our section
        headers, which are deliberately non-selectable so clicks
        don't visually highlight them. We walk the top-level items
        and return whichever ``visualItemRect`` contains ``y``.

        Mirrors :meth:`SkillListPanel._header_at`, which exists for
        the same Qt limitation in the right-click handler — see
        the ``_on_context_menu`` docstring for the historical note
        and the §7.66 DESIGN.md entry for why the drag-drop path
        needs its own copy (the panel-level helper isn't visible
        from inside the inner tree widget without leaking the
        boundary)."""
        for i in range(self.topLevelItemCount()):
            header = self.topLevelItem(i)
            rect = self.visualItemRect(header)
            if rect.height() > 0 and rect.top() <= y <= rect.bottom():
                return header
        return None

    def _target_skill_type(self, pos: QPoint) -> SkillType | None:
        """Return the ``SkillType`` of the section header at ``pos``,
        or None if ``pos`` is over a skill row, an empty area, or a
        non-Global header.

        Two-layered hit-test: ``itemAt`` first (fast path, works
        for skill rows because they're ``ItemIsSelectable``), then
        a geometric :meth:`_header_at_y` fallback (slow path for
        headers, which lack ``ItemIsSelectable`` and thus get
        ``None`` from ``itemAt`` in PySide6). Both paths converge
        on the same ``isinstance(data, SkillType)`` filter so the
        return shape is identical."""
        item = self.itemAt(pos)
        if item is None:
            item = self._header_at_y(pos.y())
            if item is None:
                return None
        # Headers are top-level (parent is None); skill rows always
        # have a parent (their type header).
        if item.parent() is not None:
            return None
        data = item.data(0, Qt.UserRole)
        if isinstance(data, SkillType):
            return data
        return None

    def dragEnterEvent(self, event) -> None:
        formats = event.mimeData().formats()
        _dd_log.info(
            "[dd] dragEnterEvent — formats=%s, hasOurFormat=%s",
            list(formats),
            event.mimeData().hasFormat(self.SKILL_MIME_TYPE))
        # MIME filter only — position-based acceptance happens in
        # dragMoveEvent. Splitting the checks lets the cursor enter
        # the widget (cursor visibly changes to "drag in progress")
        # but only highlight a drop indicator when actually hovering
        # over the Global header.
        if not event.mimeData().hasFormat(self.SKILL_MIME_TYPE):
            event.ignore()
            return
        event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        _dd_log.info("[dd] dragLeaveEvent")
        super().dragLeaveEvent(event)

    def _find_global_header(self) -> QTreeWidgetItem | None:
        """Return the Global section header item, or None if Global
        is currently filtered out of the visible tree.

        The data round-trip through ``QTreeWidgetItem.setData`` /
        ``data`` strips the SkillType enum identity — because
        ``SkillType`` inherits from ``str``, PySide6's QVariant
        bridge stores the underlying string value and returns a
        plain ``str`` on read. So ``isinstance(data, SkillType)``
        returns False after a round-trip even though the value is
        correct. We compare against the string value
        (``SkillType.GLOBAL.value == "Global"``) which matches
        what actually comes back."""
        for i in range(self.topLevelItemCount()):
            header = self.topLevelItem(i)
            data = header.data(0, Qt.UserRole)
            # Two acceptable shapes: the original SkillType enum
            # (in case PySide6 ever preserves it) OR the bare
            # string value the round-trip currently produces.
            if data == SkillType.GLOBAL or data == SkillType.GLOBAL.value:
                return header
        return None

    def _is_over_global_header(self, vp_pos: QPoint) -> bool:
        """True iff ``vp_pos`` (viewport coords) falls within the
        Global header's ``visualItemRect``. Pure geometric check —
        does not rely on Qt's ``itemAt`` (which returns None for
        non-selectable items) or the per-item ``ItemIsDropEnabled``
        flag (which doesn't reach our overrides reliably in PySide6
        6.5+ when combined with non-selectable headers)."""
        header = self._find_global_header()
        if header is None:
            return False
        rect = self.visualItemRect(header)
        if rect.height() <= 0:
            return False
        return rect.top() <= vp_pos.y() <= rect.bottom()

    def dragMoveEvent(self, event) -> None:
        # Position-gated acceptance gives the user the standard
        # accept/forbidden cursor switch as they drag over Global
        # vs. elsewhere. Previous rewrite "always accept" was a
        # reliability workaround when ``_target_skill_type`` was
        # broken by the str-Enum round-trip bug; with
        # ``_is_over_global_header`` working correctly, position
        # gating is safe and gives the right UX feedback.
        if not event.mimeData().hasFormat(self.SKILL_MIME_TYPE):
            event.ignore()
            return
        vp_pos = self._viewport_pos(event)
        over_global = self._is_over_global_header(vp_pos)
        # Diagnostic kept for one round of verification — if Qt
        # stops firing dragMove after our first ignore (the
        # symptom seen back when target=None everywhere), the
        # log will show only one entry. If Qt keeps firing,
        # we'll see the over_global=True transitions.
        self._dd_move_count = getattr(self, "_dd_move_count", 0) + 1
        last = getattr(self, "_dd_last_over_global", "sentinel")
        if over_global != last or self._dd_move_count % 30 == 1:
            _dd_log.info(
                "[dd] dragMoveEvent #%d: vp_pos=%s, over_global=%s",
                self._dd_move_count,
                (vp_pos.x(), vp_pos.y()),
                over_global)
            self._dd_last_over_global = over_global
        if not over_global:
            # Cursor → "forbidden" sign. Tells the user this
            # position isn't a valid drop target. Qt should
            # continue firing dragMove events as long as the
            # cursor stays inside our widget — the previous
            # "ignore breaks the drag" symptom was specific to
            # the broken hit-test always returning None, where
            # the FIRST event was already an ignore over the
            # source skill row, so Qt gave up immediately. With
            # the geometric check working, our first dragMove
            # over the drag-start position still ignores (it's
            # not Global) but subsequent moves over Global will
            # accept and re-engage Qt.
            event.ignore()
            return
        event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        vp_pos = self._viewport_pos(event)
        over_global = self._is_over_global_header(vp_pos)
        _dd_log.info(
            "[dd] dropEvent: vp_pos=%s, over_global=%s, "
            "hasOurFormat=%s, drag_source=%s",
            (vp_pos.x(), vp_pos.y()),
            over_global,
            event.mimeData().hasFormat(self.SKILL_MIME_TYPE),
            self._drag_source.name if self._drag_source else None)
        if not event.mimeData().hasFormat(self.SKILL_MIME_TYPE):
            event.ignore()
            return
        if not over_global:
            # Released outside the Global header — just discard the
            # drag silently. No error dialog, no signal; the user's
            # intent was unclear and the most useful response is "do
            # nothing." Same UX as releasing outside any drop target.
            _dd_log.info(
                "[dd] dropEvent: outside Global header, ignoring")
            event.ignore()
            return
        source = self._drag_source
        # Clear regardless of outcome — a successful drop consumes
        # the source; a failed dropEvent (source went None somehow)
        # shouldn't leave a stale reference for the next drag to
        # accidentally re-emit.
        self._drag_source = None
        if source is None:
            _dd_log.warning(
                "[dd] dropEvent: drag_source was None, ignoring")
            event.ignore()
            return
        # Accept BEFORE emitting so Qt's drag-success bookkeeping
        # finishes synchronously; the slot can then put up a modal
        # QMessageBox without Qt sitting in a half-completed drop.
        event.acceptProposedAction()
        _dd_log.info(
            "[dd] dropEvent: emitting skill_drop_requested for %s",
            source.name)
        self.skill_drop_requested.emit(source, SkillType.GLOBAL)


class SkillListPanel(QWidget):
    skill_selected = Signal(object)  # emits Skill
    # Emitted when user picks Enable/Disable from the right-click menu.
    # MainWindow handles the actual settings write and panel refresh.
    state_change_requested = Signal(object, str)  # (Skill, "on" | "off")
    # Emitted when user picks "Test Skill…" from the right-click menu
    # (§7.34). Same target as the toolbar's Test button, but the menu
    # entry operates on the skill *under the cursor* — not the currently
    # selected one — so the user can open a tester for a different skill
    # without disrupting their current selection.
    test_skill_requested = Signal(object)  # emits Skill
    # Emitted when user picks "Delete Skill…" from the right-click menu.
    # Only emitted for Global/Project skills — the menu entry is hidden
    # entirely for Plugin skills (which the user manages via /plugin).
    # MainWindow performs the double confirmation, then soft-deletes the
    # skill folder to the OS Recycle Bin and triggers a rescan.
    delete_skill_requested = Signal(object)  # emits Skill

    # Emitted when the user picks "New Skill…" from the right-click
    # menu of a Global or Project group header. Payload is the
    # SkillType of the header that was right-clicked, so MainWindow
    # can preselect the corresponding radio in NewSkillDialog. Plugin
    # headers never emit this — plugin skills can't be created from
    # the GUI (per-type mutation rule in CLAUDE.md), so the menu
    # item isn't shown there in the first place.
    new_skill_requested = Signal(object)  # emits SkillType

    # Emitted when the user drag-drops a Project skill onto the Global
    # section header. Payload: (source: Skill, target_type: SkillType).
    # MainWindow shows the Copy / Move / Cancel confirmation dialog and
    # dispatches to ``skill_relocate``. Plugin rows are non-draggable
    # (no Qt.ItemIsDragEnabled flag) and the Global header is the only
    # drop target (sole header with Qt.ItemIsDropEnabled), so the
    # signal's "source.type" will always be PROJECT and "target_type"
    # always GLOBAL in practice — defensive checks in the receiver
    # cover any future widening.
    skill_drop_requested = Signal(object, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.tree = _SkillTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setStyleSheet(_SKILL_LIST_STYLE)
        self.tree.itemSelectionChanged.connect(self._on_selection)
        # Double-click on a skill row → open Test Skill dialog (§7.70),
        # same dispatch as Ctrl+T and the context-menu "Test Skill…"
        # entry. ``itemDoubleClicked`` is a notification signal — it
        # fires AFTER Qt's default handling, so double-clicking a
        # group header still toggles its expand/collapse state; the
        # handler just skips header rows (no Skill payload in
        # UserRole, so the isinstance guard naturally filters them).
        self.tree.itemDoubleClicked.connect(self._on_double_click)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        # Re-emit the inner widget's drop signal as the panel's own —
        # MainWindow connects to the panel (signals up / methods down,
        # §8.3); reaching into ``self.tree`` from MainWindow would
        # leak the internal widget and break the panel boundary.
        self.tree.skill_drop_requested.connect(self.skill_drop_requested)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tree)

        self._all_skills: list[Skill] = []
        self._enabled_types: set[SkillType] = set(SkillType)
        self._enabled_state_groups: set[str] = {
            STATE_GROUP_ENABLED, STATE_GROUP_DISABLED,
        }
        self._filter_text: str = ""

    # ------------------------------------------------------------- public API
    def set_skills(self, skills: list[Skill]) -> None:
        self._all_skills = sorted(skills, key=lambda s: s.name.lower())
        self._rebuild()

    def all_skills(self) -> list[Skill]:
        """Return a shallow copy of the most-recent full skill list.

        "Full" means unfiltered — checkbox filters and the search box
        affect what's *visible* in the tree, not what's stored here.
        Callers (e.g. ``MainWindow._find_skill_by_md_path``) get the
        canonical post-scan view so they can locate a skill that
        might be hidden by the current filters. Shallow copy so the
        caller can't accidentally mutate our internal state by
        re-ordering / mutating the returned list."""
        return list(self._all_skills)

    def set_type_enabled(self, type_: SkillType, enabled: bool) -> None:
        if enabled:
            self._enabled_types.add(type_)
        else:
            self._enabled_types.discard(type_)
        self._rebuild()

    def set_state_group_enabled(self, group: str, enabled: bool) -> None:
        """Toolbar State checkbox handler. ``group`` is one of
        ``STATE_GROUP_ENABLED`` / ``STATE_GROUP_DISABLED``."""
        if enabled:
            self._enabled_state_groups.add(group)
        else:
            self._enabled_state_groups.discard(group)
        self._rebuild()

    def set_filter(self, text: str) -> None:
        """Update the substring filter (case-insensitive).

        Match target depends on skill type (see ``_search_haystack``):

        * Plugin rows match against ``name + plugin name`` — searching
          ``discord`` finds every skill in the discord plugin.
        * Project rows match against ``name + project folder name`` —
          searching ``pyCOMPS`` finds every skill under that project.
        * Global rows match against ``name`` only.

        Mirrors the always-on context labels rendered on each row
        (§7.29) so what the user sees is what they can search for."""
        self._filter_text = text.strip().lower()
        self._rebuild()

    def clear_selection(self) -> None:
        """Drop the tree's current selection without firing ``skill_selected``.

        Used by ``MainWindow`` when the user clears the search box — that
        gesture means "start over" (matching app-start / Refresh) and should
        leave the tree visually unselected. Signals are blocked because
        ``setCurrentItem(None)`` would otherwise fire ``itemSelectionChanged``,
        which is wired to ``_on_selection``; even though that handler
        early-returns on empty selection, blocking is the safer pattern when
        a programmatic reset shouldn't re-enter the selection flow."""
        self.tree.blockSignals(True)
        try:
            self.tree.clearSelection()
            self.tree.setCurrentItem(None)
        finally:
            self.tree.blockSignals(False)

    def select_skill(self, skill: Skill) -> bool:
        """Programmatically move the tree's current/selected row to the item
        matching ``skill.path``, without firing ``skill_selected``. Returns
        True if a matching row was found and selected, False otherwise (e.g.
        the skill is filtered out of the current view).

        Used by ``MainWindow`` to restore the highlight when a skill switch
        is rejected — the user cancels the unsaved-changes prompt and the
        tree must visually re-match whatever the editor is actually showing.
        Mirrors ``FileTreePanel.select_path``; both panels follow the rule
        "user clicks may move the highlight; programmatic restores must not
        re-fire the user-action signal."

        Signals are blocked because ``QTreeWidget.setCurrentItem`` fires the
        same ``itemSelectionChanged`` that the original click did. Without
        the block, the restore would re-enter ``_on_selection``, re-emit
        ``skill_selected``, and the discard prompt would loop indefinitely
        for the same dirty buffer."""
        self.tree.blockSignals(True)
        try:
            for i in range(self.tree.topLevelItemCount()):
                header = self.tree.topLevelItem(i)
                for j in range(header.childCount()):
                    item = header.child(j)
                    stored = item.data(0, Qt.UserRole)
                    if isinstance(stored, Skill) and stored.path == skill.path:
                        self.tree.setCurrentItem(item)
                        self.tree.scrollToItem(item)
                        return True
            return False
        finally:
            self.tree.blockSignals(False)

    def refresh_state(self, skill: Skill) -> None:
        """Re-style the row matching ``skill`` after its state changed.

        We deliberately don't ``_rebuild()`` here — that would clear the
        user's selection, scroll position, and group expansion state for a
        toggle that touches a single row. The stored disambiguation suffix
        (set during ``_rebuild``) is reused so the label keeps any
        ``[plugin]`` / ``[marketplace]`` suffix that was added to resolve a
        name collision."""
        for i in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(i)
            for j in range(header.childCount()):
                item = header.child(j)
                stored = item.data(0, Qt.UserRole)
                if isinstance(stored, Skill) and stored.path == skill.path:
                    suffix = item.data(0, _DISAMBIG_ROLE) or ""
                    _apply_state_style(item, skill, disambiguation=suffix)
                    return

    # ----------------------------------------------------------------- internal
    def _rebuild(self) -> None:
        self.tree.clear()
        bold = QFont()
        bold.setBold(True)

        # Build group headers in a stable, conventional order
        groups: dict[SkillType, QTreeWidgetItem] = {}
        for type_ in (SkillType.GLOBAL, SkillType.PROJECT, SkillType.PLUGIN):
            if type_ in self._enabled_types:
                header = QTreeWidgetItem([type_.value])
                header.setFont(0, bold)
                header.setIcon(0, _icon_for(type_))
                # Headers are not selectable (Qt.ItemIsEnabled only).
                # The Global header additionally gets
                # ``Qt.ItemIsDropEnabled`` so it can serve as the
                # drag-drop target for Project → Global relocation
                # (§7.66). Project / Plugin headers stay drop-disabled
                # so Qt's hit-test rejects drops on them pre-emptively
                # — complementing the explicit dragMoveEvent gate in
                # :class:`_SkillTreeWidget`.
                if type_ == SkillType.GLOBAL:
                    header.setFlags(
                        Qt.ItemIsEnabled | Qt.ItemIsDropEnabled)
                else:
                    header.setFlags(Qt.ItemIsEnabled)
                # Tag the header with its SkillType so the right-click
                # handler can distinguish "header clicked" (data is a
                # SkillType) from "skill row clicked" (data is a Skill)
                # without parsing the display text. ``_on_selection``
                # already does ``isinstance(data, Skill)`` — that gate
                # still rejects header clicks correctly because
                # SkillType is not a Skill. ``_target_skill_type`` in
                # the tree widget also reads this for drop-target
                # acceptance (§7.66).
                header.setData(0, Qt.UserRole, type_)
                groups[type_] = header

        # Pass 1 — collect the filtered set so we can detect name collisions
        # *within what's actually visible*. Filtering down to a single instance
        # via search/state thus suppresses the disambiguation suffix
        # automatically: no collision in view, no clutter.
        visible: list[Skill] = []
        for skill in self._all_skills:
            if skill.type not in groups:
                continue
            if _state_group(skill.state) not in self._enabled_state_groups:
                continue
            if self._filter_text and self._filter_text not in _search_haystack(skill):
                continue
            visible.append(skill)

        disambig = _build_disambiguation_map(visible)

        # Pass 2 — build the items now that we know each row's suffix.
        # Icon assignment is deferred to _apply_state_style so the same
        # call site picks the full-color or faded variant per state.
        for skill in visible:
            suffix = disambig.get(skill.path, "")
            item = QTreeWidgetItem()
            item.setData(0, Qt.UserRole, skill)
            item.setData(0, _DISAMBIG_ROLE, suffix)
            tooltip = f"{skill.type.value} — {skill.path}"
            if skill.state == STATE_PLUGIN_OFF:
                # The "[plugin off]" text used to live on the row itself;
                # since we removed it (faded icon now carries the signal),
                # surface the explanation on hover so users still know
                # WHY the row is dimmed and where to manage it.
                tooltip += "\n(plugin disabled — manage via /plugin)"
            item.setToolTip(0, tooltip)
            # Drag/drop flag tuning (§7.66). Drops only land on the
            # Global section header — strip ``Qt.ItemIsDropEnabled``
            # from every skill row so Qt's hit-test rejects row-level
            # drops pre-emptively (no cursor flicker between
            # "accepted" and our explicit ignore in dragMoveEvent).
            # Drag eligibility: only Project skills can be drag-
            # sources (per-type mutation rule, CLAUDE.md). Strip
            # ``Qt.ItemIsDragEnabled`` for Global / Plugin rows so
            # Qt's startDrag never even passes them to ``mimeData``.
            flags = item.flags() & ~Qt.ItemIsDropEnabled
            if skill.type != SkillType.PROJECT:
                flags &= ~Qt.ItemIsDragEnabled
            item.setFlags(flags)
            _apply_state_style(item, skill, disambiguation=suffix)
            groups[skill.type].addChild(item)

        for type_, header in groups.items():
            header.setText(0, f"{type_.value}  ({header.childCount()})")
            self.tree.addTopLevelItem(header)
            header.setExpanded(True)

    def _on_selection(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            return
        skill = items[0].data(0, Qt.UserRole)
        if isinstance(skill, Skill):
            self.skill_selected.emit(skill)

    def _on_double_click(self, item: QTreeWidgetItem, _column: int) -> None:
        """Double-click on a skill row → emit ``test_skill_requested``
        so MainWindow opens the Test Skill dialog (§7.70).

        Same dispatch as Ctrl+T (§7.69) and the context-menu
        "Test Skill…" entry — three entry points, one downstream
        handler (``MainWindow.open_test_dialog``). Group-header
        double-clicks fall through: the ``isinstance`` guard skips
        them and Qt's default expand/collapse toggling has already
        fired by the time this slot runs.

        Column argument is ignored — the tree is single-column and
        a double-click anywhere in the row should test the same
        skill."""
        if item is None:
            return
        stored = item.data(0, Qt.UserRole)
        if isinstance(stored, Skill):
            self.test_skill_requested.emit(stored)

    def _on_context_menu(self, pos: QPoint) -> None:
        """Right-click → copy/open actions for the skill under the cursor.

        Operates on `itemAt(pos)` rather than the currently selected item, so
        users can copy a path from a skill they're not actively viewing
        without triggering a full skill-switch (which would refresh the file
        tree and editor preview).

        Right-clicks on group headers route to ``_on_header_context_menu``
        instead — currently a single "New Skill…" entry on Global and
        Project headers (not Plugin, which is upstream-only). Different
        menu shape from skill rows, so the dispatch lives in a sibling
        method rather than a multi-mode branch inside this one.

        Header / row detection uses ``item.parent() is None`` because
        ``QTreeWidgetItem.parent()`` returns ``None`` only for top-
        level items — robust against any `Qt.UserRole` round-trip
        quirks. ``itemAt(pos)`` returning None on header positions
        (a known Qt issue with items that have only `Qt.ItemIsEnabled`
        and no `ItemIsSelectable`) is handled by the
        ``_header_at`` fallback — which historically masked this
        feature gap, because before this iteration the early return
        WAS the intended behavior for header clicks."""
        item = self.tree.itemAt(pos)
        if item is None:
            # itemAt() can return None on non-selectable items in
            # PySide6. Try a geometric fallback against the top-
            # level items' visual rects before giving up.
            item = self._header_at(pos)
            if item is None:
                return
        if item.parent() is None:
            # Top-level item → group header. The SkillType is
            # stored on the header at construction; fall back to
            # parsing the label if the data round-trip somehow
            # dropped it (defense in depth — should never happen).
            header_type = item.data(0, Qt.UserRole)
            if not isinstance(header_type, SkillType):
                header_type = _skill_type_for_header_text(item.text(0))
            if header_type is not None:
                self._on_header_context_menu(pos, header_type)
            return
        stored = item.data(0, Qt.UserRole)
        if not isinstance(stored, Skill):
            return  # neither a header nor a skill row — nothing to do
        skill = stored

        menu = QMenu(self)
        # Qt hides QAction tooltips inside menus by default. Enable them
        # so deferred-effect / read-only tooltips on Enable/Disable
        # (especially for plugin-off rows — §7.63 decision 3) are
        # actually discoverable on hover, not just silently set.
        menu.setToolTipsVisible(True)

        # "Test Skill…" sits at the top — it's the most action-oriented
        # entry in the menu (everything else is read-only metadata
        # copying), and putting it first matches the convention that
        # primary actions live above secondary ones. Always enabled
        # because the dialog itself handles every skill type / state
        # gracefully (a disabled skill can still be tested; the
        # response just shows that ``claude`` ignored it).
        #
        # The ``\tCtrl+T`` suffix is a QMenu convention for rendering
        # a shortcut hint in the right gutter without actually
        # registering the key sequence on this transient QAction
        # (§7.69). The real Ctrl+T binding lives on MainWindow's
        # persistent ``_test_skill_action`` and acts on the currently
        # selected skill; using ``setShortcut`` here would create a
        # second QAction with the same chord and Qt would emit
        # "ambiguous shortcut" warnings whenever the menu is open.
        act_test = menu.addAction("Test Skill…\tCtrl+T")
        menu.addSeparator()

        # Enable / Disable — applies only when the row's effective state
        # is toggleable. Three read-only branches:
        #   * plugin-off: parent plugin is disabled; per-skill toggling
        #     only applies to enabled plugins, so route the user to
        #     /plugin (§7.63 decision 3).
        #   * non-binary override (name-only / user-invocable-only):
        #     toggling would collapse the nuance, so the user must edit
        #     settings.local.json by hand.
        # Otherwise the active direction stays enabled and writes
        # through write_override regardless of skill type.
        act_enable  = menu.addAction("Enable")
        act_disable = menu.addAction("Disable")
        if skill.state == STATE_PLUGIN_OFF:
            tip = ("Plugin is currently disabled — enable it via /plugin "
                   "in Claude Code to manage this skill")
            act_enable.setEnabled(False);  act_enable.setToolTip(tip)
            act_disable.setEnabled(False); act_disable.setToolTip(tip)
        elif skill.override_state not in BINARY_STATES:
            tip = (f"Override is '{skill.override_state}' — edit "
                   f"settings.local.json to change (toggling here would "
                   f"collapse the nuance)")
            act_enable.setEnabled(False);  act_enable.setToolTip(tip)
            act_disable.setEnabled(False); act_disable.setToolTip(tip)
        else:
            # Disable the no-op direction so the menu reflects current state.
            if skill.override_state == STATE_ON:
                act_enable.setEnabled(False)
            else:  # STATE_OFF
                act_disable.setEnabled(False)
        menu.addSeparator()

        act_path     = menu.addAction("Copy Path")
        act_skill_md = menu.addAction("Copy SKILL.md Path")
        act_name     = menu.addAction("Copy Name")
        act_url      = menu.addAction("Copy as file:// URL")
        menu.addSeparator()
        # Same ``\t<chord>`` gutter-hint convention as Test Skill…
        # above — the real Ctrl+E binding is on MainWindow's
        # ``_open_folder_action`` (§7.69).
        act_open     = menu.addAction("Open Folder in Explorer\tCtrl+E")

        # Delete Skill — destructive, soft (Recycle Bin), Global/Project
        # only. Hidden entirely for Plugin skills per spec; plugin
        # folders are managed elsewhere (the marketplace install) and a
        # delete here would orphan plugin manifests. The ellipsis ("…")
        # signals to the user that this opens a confirmation flow, not
        # an immediate destructive action — same convention as
        # "Test Skill…" above.
        act_delete = None
        if skill.type != SkillType.PLUGIN:
            menu.addSeparator()
            # Same ``\t<chord>`` gutter-hint convention as Test Skill…
            # and Open Folder above — the real Ctrl+D / Del bindings
            # live on MainWindow's ``_delete_skill_action`` (§7.69).
            # Putting ``setShortcut`` on this transient menu QAction
            # would collide with the persistent one and trigger Qt's
            # "ambiguous shortcut" warning while the menu is open.
            # Both alternates are rendered in the gutter (slash-
            # separated) so the menu surfaces every way to trigger
            # the action; the persistent QAction has both bindings.
            act_delete = menu.addAction("Delete Skill…\tCtrl+D / Del")

        # Identity comparison rather than string match — robust to future
        # label edits or localization.
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return

        if chosen is act_test:
            self.test_skill_requested.emit(skill)
            return
        if chosen is act_enable:
            self.state_change_requested.emit(skill, STATE_ON)
            return
        if chosen is act_disable:
            self.state_change_requested.emit(skill, STATE_OFF)
            return

        clip = QApplication.clipboard()
        if chosen is act_path:
            clip.setText(str(skill.path))
        elif chosen is act_skill_md:
            target = skill.skill_md_path if skill.skill_md_path is not None else skill.path
            clip.setText(str(target))
        elif chosen is act_name:
            clip.setText(skill.name)
        elif chosen is act_url:
            clip.setText(QUrl.fromLocalFile(str(skill.path)).toString())
        elif chosen is act_open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(skill.path)))
        elif act_delete is not None and chosen is act_delete:
            # MainWindow owns the double-confirmation flow and the
            # actual recycle-bin call; this panel only emits the
            # request, matching the panel-emits-signal /
            # MainWindow-handles-effect pattern used by Enable/Disable
            # and Test Skill above.
            self.delete_skill_requested.emit(skill)

    def _header_at(self, pos: QPoint) -> QTreeWidgetItem | None:
        """Geometric fallback for ``self.tree.itemAt(pos)`` when it
        returns ``None`` on a top-level (header) item.

        Walks the top-level items and returns whichever has a
        visible row containing ``pos.y()``. The X coordinate is
        intentionally ignored — header rows span the full tree
        width regardless of horizontal click position, and trusting
        the X check would mis-miss clicks that landed on the empty
        right-hand portion of the row.

        Returns ``None`` if no top-level row contains ``pos`` — the
        click was on empty viewport space below the last group.

        Used because PySide6's ``QTreeWidget.itemAt`` can return
        ``None`` on items that have only ``Qt.ItemIsEnabled`` (no
        ``ItemIsSelectable``), which is exactly the flag shape we
        use for group headers."""
        y = pos.y()
        for i in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(i)
            rect = self.tree.visualItemRect(header)
            if rect.height() > 0 and rect.top() <= y <= rect.bottom():
                return header
        return None

    def _on_header_context_menu(
        self, pos: QPoint, header_type: SkillType,
    ) -> None:
        """Right-click handler for group headers (Global / Project /
        Plugin). Currently shows a single "New Skill…" entry,
        pre-filling the Type radio with the section the user clicked.

        Plugin headers get no menu — plugin skills can't be created
        from the GUI (per-type mutation rule in CLAUDE.md), so the
        affordance is intentionally absent rather than disabled. A
        disabled-with-tooltip entry would invite the question "why
        is this here at all?"; absence is the clearer signal.

        Same panel-emits-signal / MainWindow-handles-effect pattern
        as the skill-row context menu — this method only emits
        ``new_skill_requested``; MainWindow opens the dialog."""
        if header_type == SkillType.PLUGIN:
            return
        menu = QMenu(self)
        act_new = menu.addAction(f"New {header_type.value} Skill…")
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is act_new:
            self.new_skill_requested.emit(header_type)
