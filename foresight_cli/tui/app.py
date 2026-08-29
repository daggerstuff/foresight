"""Foresight Textual TUI — main application."""

from __future__ import annotations

from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import var
from textual.screen import Screen
from textual.theme import Theme
from textual.widgets import Footer, Header, TabbedContent, TabPane

from foresight_cli.utils.config import CliConfig

from .screens.blocks import BlocksScreen
from .screens.dashboard import DashboardScreen
from .screens.memories import MemoriesScreen

# Custom midnight ocean theme — dark variant
FOREIGHT_DARK_THEME = Theme(
    name="foresight-dark",
    primary="#6c9fff",
    secondary="#c084fc",
    accent="#5eead4",
    foreground="#e2e8f0",
    background="#0f1117",
    success="#4ade80",
    warning="#fbbf24",
    error="#f87171",
    surface="#0f1117",
    panel="#1a1f2e",
    dark=True,
    variables={
        "block-cursor-text-style": "none",
        "footer-key-foreground": "#5eead4",
        "input-selection-background": "#6c9fff 35%",
        "input-cursor-foreground": "#e2e8f0",
        "input-cursor-background": "#6c9fff",
    },
)

# Custom midnight ocean theme — light variant
FOREIGHT_LIGHT_THEME = Theme(
    name="foresight-light",
    primary="#3b82f6",
    secondary="#9333ea",
    foreground="#1e293b",
    background="#f8fafc",
    success="#16a34a",
    warning="#d97706",
    error="#dc2626",
    surface="#f8fafc",
    panel="#e2e8f0",
    accent="#0d9488",
    dark=False,
    variables={
        "block-cursor-text-style": "none",
        "footer-key-foreground": "#0d9488",
        "input-selection-background": "#3b82f6 35%",
        "input-cursor-foreground": "#1e293b",
        "input-cursor-background": "#3b82f6",
    },
)


class ForesightTUI(App):
    """Foresight interactive terminal UI."""

    DEFAULT_CSS = """
    Screen {
        background: $surface;
    }

    Header {
        background: $panel;
        color: $accent;
        text-style: bold;
        height: 3;
        border-bottom: heavy $accent 40%;
    }

    Footer {
        background: $panel;
        color: $text-muted;
        border-top: heavy $primary 20%;
    }

    TabbedContent {
        height: 100%;
    }

    TabBar {
        background: $panel;
        height: 3;
        border-bottom: solid $primary 20%;
    }

    Tab {
        padding: 0 2;
        color: $text-muted;
        text-style: bold;
        transition: color 0.2s, background 0.2s;
    }

    Tab:hover {
        color: $accent;
        background: $surface;
    }

    Tab.-active {
        color: $accent;
        background: $surface;
        text-style: bold;
        border-top: thick $accent;
    }

    TabPane {
        padding: 1;
    }

    /* Stats grid */
    .stats-grid {
        layout: grid;
        grid-size: 3;
        grid-gutter: 1;
        height: auto;
        margin: 1 0;
    }

    .stat-card {
        border: round $primary 60%;
        padding: 1 2;
        height: auto;
        background: $panel 30%;
        transition: border-color 0.3s, background 0.3s;
    }

    .stat-card:hover {
        border: round $accent;
        background: $panel 50%;
    }

    .stat-label {
        color: $accent;
        text-style: bold;
    }

    .stat-value {
        color: $text;
        text-style: bold;
        content-align: center middle;
        height: 3;
    }

    /* Memory list */
    .memory-list {
        height: 1fr;
        border: round $primary 30%;
        background: $panel 10%;
    }

    .memory-list > ListItem {
        padding: 0 1;
        transition: background 0.15s;
    }

    .memory-list > ListItem:hover {
        background: $panel 40%;
    }

    .memory-list > ListItem.-highlight {
        background: $accent 15%;
        border-left: thick $accent;
    }

    /* Search box */
    .search-box {
        margin: 0 0 1 0;
    }

    .detail-panel {
        border: round $secondary 50%;
        height: 1fr;
        padding: 1;
        background: $panel 15%;
    }

    Button {
        margin: 0 1;
        min-width: 10;
        transition: background 0.2s, border 0.2s;
    }

    .action-bar {
        height: auto;
        margin: 1 0;
    }

    .section-title {
        color: $accent;
        text-style: bold;
        margin: 0 0 1 0;
    }

    #dashboard-content {
        padding: 1 2;
    }

    #dashboard-content > Static {
        margin: 0 0 0 0;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "Quit", show=True),
        Binding("d", "toggle_theme", "Toggle theme", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("1", "switch_tab('dashboard')", "Dashboard", show=False),
        Binding("2", "switch_tab('memories')", "Memories", show=False),
        Binding("3", "switch_tab('blocks')", "Blocks", show=False),
    ]

    TITLE = "Foresight"
    SUB_TITLE = "Memory Management Terminal"

    user_id: str | None = var(None)
    config: CliConfig | None = var(None)

    def __init__(self, user_id: str | None = None, config: CliConfig | None = None) -> None:
        super().__init__()
        self.user_id = user_id
        self.config = config

    def on_mount(self) -> None:
        self.register_theme(FOREIGHT_DARK_THEME)
        self.register_theme(FOREIGHT_LIGHT_THEME)
        self.theme = "foresight-dark"
        self.refresh_data()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="dashboard"):
            with TabPane("Dashboard", id="dashboard"):
                yield DashboardScreen()
            with TabPane("Memories", id="memories"):
                yield MemoriesScreen()
            with TabPane("Blocks", id="blocks"):
                yield BlocksScreen()
        yield Footer()

    def action_toggle_theme(self) -> None:
        """Toggle between dark and light themes."""
        self.theme = "foresight-light" if self.theme == "foresight-dark" else "foresight-dark"

    def action_refresh(self) -> None:
        """Refresh all screens."""
        for screen in self.screen_stack:
            if hasattr(screen, "refresh_data"):
                screen.refresh_data()

    def refresh_data(self) -> None:
        """Refresh data on all active screens."""
        for child in self.query(Screen):
            if hasattr(child, "refresh_data"):
                child.refresh_data()
