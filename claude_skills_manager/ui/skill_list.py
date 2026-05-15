"""Left panel — skills grouped by SkillType, with type-toggle and search filtering."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QIcon, QPainter, QPixmap, QPolygon,
)
from PySide6.QtWidgets import (
    QApplication, QMenu, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..models import Skill, SkillType
from ..skill_settings import (
    BINARY_STATES, STATE_NAME_ONLY, STATE_OFF, STATE_ON, STATE_PLUGIN_OFF,
    STATE_USER_INVOCABLE_ONLY,
)

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

# QIcon objects can't exist before QApplication; this cache is populated on
# first call (which only happens once SkillListPanel is instantiated). The
# parallel "faded" cache holds the same shapes painted at reduced opacity,
# used for PLUGIN_OFF rows so the row is visually distinct from a
# user-toggled OFF row without needing a "[plugin off]" text suffix. Type
# (shape) is preserved; the dimming reads as "inherited-disabled, manage
# elsewhere."
_type_icon_cache: dict[SkillType, QIcon] = {}
_type_icon_faded_cache: dict[SkillType, QIcon] = {}

# Opacity used by _icon_for_faded. 0.35 is enough to read as muted at
# typical tree-row sizes (16x16 logical) while still letting the type
# color show through; tuned by eye against the indigo / green / amber
# triplet so no single hue disappears at the dimmed level.
_FADED_OPACITY = 0.35


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
    """Return (and lazily build + cache) a dimmed version of the type icon,
    used for PLUGIN_OFF rows. Same shape, reduced opacity — preserves the
    type-distinguishing channel while signalling 'disabled at a level above
    your control here.'"""
    cached = _type_icon_faded_cache.get(skill_type)
    if cached is not None:
        return cached
    icon = _paint_type_icon(skill_type, _FADED_OPACITY)
    _type_icon_faded_cache[skill_type] = icon
    return icon


def _paint_type_icon(skill_type: SkillType, opacity: float) -> QIcon:
    """Paint the badge for a skill type at the given opacity. Factored out
    so the full-color and faded variants share one code path — there is no
    second copy of the shape geometry to drift out of sync.

    DPR ordering matters (see §7.24): we paint at physical coords on a
    plain 32x32 pixmap, THEN tag DPR=2.0 as metadata. Setting DPR before
    painting flips the painter into logical (16x16) coords, which clips
    most of the shape — the long-standing "quarter-shape" rendering bug
    that masqueraded as deliberate design."""
    color_hex, shape = _TYPE_PAINT[skill_type]
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
    if state in (STATE_OFF, STATE_PLUGIN_OFF):
        # Both "off" states share visual treatment: faded type icon +
        # italic + grey text. The icon SHAPE still distinguishes the
        # underlying type (circle/square/diamond → Global/Project/Plugin),
        # which by construction also tells you which kind of "off" you're
        # looking at — Global/Project skills are only ever OFF (binary,
        # user-toggleable), Plugin skills are only ever PLUGIN_OFF
        # (inherited, manage via /plugin). The tooltip enrichment in
        # _rebuild surfaces the latter explanation on hover.
        item.setText(0, label)
        item.setIcon(0, _icon_for_faded(skill.type))
        italic = QFont()
        italic.setItalic(True)
        item.setFont(0, italic)
        item.setForeground(0, QColor("#9a9a9a"))
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


class SkillListPanel(QWidget):
    skill_selected = Signal(object)  # emits Skill
    # Emitted when user picks Enable/Disable from the right-click menu.
    # MainWindow handles the actual settings write and panel refresh.
    state_change_requested = Signal(object, str)  # (Skill, "on" | "off")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setStyleSheet(_SKILL_LIST_STYLE)
        self.tree.itemSelectionChanged.connect(self._on_selection)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)

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
                header.setFlags(Qt.ItemIsEnabled)  # not selectable
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

    def _on_context_menu(self, pos: QPoint) -> None:
        """Right-click → copy/open actions for the skill under the cursor.

        Operates on `itemAt(pos)` rather than the currently selected item, so
        users can copy a path from a skill they're not actively viewing
        without triggering a full skill-switch (which would refresh the file
        tree and editor preview)."""
        item = self.tree.itemAt(pos)
        if item is None:
            return
        skill = item.data(0, Qt.UserRole)
        if not isinstance(skill, Skill):
            return  # group header — nothing to copy

        menu = QMenu(self)

        # Enable / Disable — meaningful only for Global/Project skills with a
        # binary state. Plugin skills inherit their plugin's state and
        # non-binary states (name-only, user-invocable-only) are read-only
        # per the design (the user must edit settings.local.json by hand to
        # change those, since we'd otherwise collapse the nuance).
        act_enable  = menu.addAction("Enable")
        act_disable = menu.addAction("Disable")
        is_plugin  = skill.plugin_id is not None
        is_binary  = skill.state in BINARY_STATES
        if is_plugin:
            tip = "Plugin skills can't be toggled individually — manage via /plugin"
            act_enable.setEnabled(False);  act_enable.setToolTip(tip)
            act_disable.setEnabled(False); act_disable.setToolTip(tip)
        elif not is_binary:
            tip = (f"State is '{skill.state}' — edit settings.local.json to change "
                   f"(toggling here would collapse the nuance)")
            act_enable.setEnabled(False);  act_enable.setToolTip(tip)
            act_disable.setEnabled(False); act_disable.setToolTip(tip)
        else:
            # Disable the no-op direction so the menu reflects current state.
            if skill.state == STATE_ON:
                act_enable.setEnabled(False)
            else:  # STATE_OFF
                act_disable.setEnabled(False)
        menu.addSeparator()

        act_path     = menu.addAction("Copy Path")
        act_skill_md = menu.addAction("Copy SKILL.md Path")
        act_name     = menu.addAction("Copy Name")
        act_url      = menu.addAction("Copy as file:// URL")
        menu.addSeparator()
        act_open     = menu.addAction("Open Folder in Explorer")

        # Identity comparison rather than string match — robust to future
        # label edits or localization.
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen is None:
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
