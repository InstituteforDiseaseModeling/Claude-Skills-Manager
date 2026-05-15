"""Middle panel — file tree rooted at the currently selected skill folder.

QFileSystemModel gives us free file watching and lazy population: rooting it
at a small subtree (the skill folder) means we never load more than what the
user expands."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QDir, QModelIndex, Qt, Signal  # QModelIndex used by _on_clicked type hint
from PySide6.QtWidgets import QFileSystemModel, QTreeView, QVBoxLayout, QWidget


class FileTreePanel(QWidget):
    file_activated = Signal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.model = QFileSystemModel()
        self.model.setReadOnly(False)
        self.model.setFilter(QDir.AllEntries | QDir.NoDotAndDotDot)

        self.tree = QTreeView()
        self.tree.setHeaderHidden(False)
        self.tree.setAnimated(False)
        self.tree.setIndentation(16)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        self.tree.clicked.connect(self._on_clicked)
        self.tree.doubleClicked.connect(self._on_clicked)
        # Defer setModel until a skill is selected — an unmodelled tree
        # paints empty, which is what we want before the user picks a skill
        # (otherwise QFileSystemModel would show the drive list).

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tree)

    # ------------------------------------------------------------- public API
    def show_directory(self, path: Path) -> None:
        if not path.is_dir():
            self.clear()
            return
        # Attach the model on first use, then hide the metadata columns.
        if self.tree.model() is not self.model:
            self.tree.setModel(self.model)
            for col in (1, 2, 3):
                self.tree.setColumnHidden(col, True)
        root_str = str(path)
        self.model.setRootPath(root_str)
        self.tree.setRootIndex(self.model.index(root_str))
        self.tree.expandToDepth(0)

    def clear(self) -> None:
        self.tree.setModel(None)

    def select_path(self, path: Path) -> None:
        """Programmatically move the tree's current/selected row to ``path``
        without firing ``file_activated``.

        Used by ``MainWindow`` to restore the highlight when the editor
        rejects an ``open_file`` (the user cancelled an unsaved-changes
        prompt, etc.). ``QTreeView.clicked`` is mouse-only — programmatic
        selection changes don't fire it — so this is recursion-safe.
        """
        if self.tree.model() is not self.model:
            return
        idx = self.model.index(str(path))
        if not idx.isValid():
            return
        self.tree.setCurrentIndex(idx)
        self.tree.scrollTo(idx)

    # --------------------------------------------------------------- internal
    def _on_clicked(self, index: QModelIndex) -> None:
        path = Path(self.model.filePath(index))
        if path.is_file():
            self.file_activated.emit(path)
