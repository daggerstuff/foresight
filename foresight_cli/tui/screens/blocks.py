"""Context blocks screen — view and manage context blocks."""

from __future__ import annotations

import json
import logging

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

logger = logging.getLogger(__name__)

from foresight.server import ContextBlockAction, init_db, manage_context_blocks

BLOCK_LABELS = [
    "guidance",
    "pending_items",
    "project_context",
    "user_preferences",
    "session_patterns",
    "core_directives",
    "tool_guidelines",
    "self_improvement",
]

# Label color mapping for visual distinction
LABEL_COLORS: dict[str, str] = {
    "guidance": "#5eead4",
    "pending_items": "#fbbf24",
    "project_context": "#6c9fff",
    "user_preferences": "#c084fc",
    "session_patterns": "#4ade80",
    "core_directives": "#f87171",
    "tool_guidelines": "#6c9fff",
    "self_improvement": "#c084fc",
}


class BlockItem(ListItem):
    """A context block in the list."""

    def __init__(self, label: str, content: str = "", **kwargs) -> None:
        preview = (content or "(empty)")[:80]
        color = LABEL_COLORS.get(label, "#6c9fff")
        display_text = f"[{color}]●[/] [bold]{label}[/bold]  [dim]{preview}[/dim]"
        super().__init__(Label(display_text), **kwargs)
        self.block_label = label


class BlocksScreen(Screen):
    """View and manage context blocks."""

    def compose(self) -> ComposeResult:
        yield Label("[bold]Context Blocks[/bold]", classes="section-title")
        yield Horizontal(
            ListView(id="block-list", classes="memory-list"),
            Vertical(
                Static("[bold]Block Details[/bold]", id="block-detail-title"),
                Static("Select a block to view", id="block-detail", classes="detail-panel"),
                Label("\n[bold]Edit Block[/bold]"),
                Input(placeholder="Enter new content...", id="block-content-input"),
                Horizontal(
                    Button("Update", variant="primary", id="btn-update"),
                    Button("Reset", id="btn-reset"),
                    Button("Clear", id="btn-clear-block"),
                    Button("Refresh", id="btn-refresh-blocks"),
                ),
                id="block-detail-column",
            ),
        )

    def on_mount(self) -> None:
        self.refresh_data()

    @work(exclusive=True, thread=True)
    def refresh_data(self) -> None:
        """Load blocks from Foresight in background thread."""
        try:
            init_db()
            result = manage_context_blocks(options=ContextBlockAction(action="list"))
            blocks = []
            if isinstance(result, str):
                try:
                    payload = json.loads(result)
                    blocks = payload.get("blocks", []) if isinstance(payload, dict) else []
                except Exception:
                    blocks = []
            elif isinstance(result, dict):
                blocks = result.get("blocks", [])

            self.app.call_from_thread(self._render_blocks, blocks)
        except Exception as e:
            self.app.call_from_thread(self._render_error, str(e))

    def _render_blocks(self, blocks: list) -> None:
        """Render context block list on UI thread."""
        try:
            list_view = self.query_one("#block-list", ListView)
            list_view.clear()
            if blocks:
                for b in blocks:
                    label = b.get("label", "?")
                    content = b.get("content", "")
                    list_view.append(BlockItem(label, content))
            else:
                for label in BLOCK_LABELS:
                    list_view.append(BlockItem(label))
        except Exception as exc:
            logger.debug("Failed to render block list in TUI: %s", exc)

    def _render_error(self, message: str) -> None:
        """Render error on UI thread."""
        try:
            list_view = self.query_one("#block-list", ListView)
            list_view.clear()
            list_view.append(ListItem(Static(f"[red]Error: {message}[/red]")))
        except Exception as exc:
            logger.debug("Failed to render error in TUI: %s", exc)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Show block details on selection."""
        item = event.item
        if isinstance(item, BlockItem):
            detail = self.query_one("#block-detail", Static)
            color = LABEL_COLORS.get(item.block_label, "#6c9fff")
            try:
                result = manage_context_blocks(options=ContextBlockAction(action="get", label=item.block_label))
                if isinstance(result, str):
                    payload = json.loads(result)
                    content = payload.get("content", "(empty)") if isinstance(payload, dict) else result
                elif isinstance(result, dict):
                    content = result.get("content", "(empty)")
                else:
                    content = str(result)
                detail.update(f"[{color} bold]{item.block_label}[/]\n\n{content}")
            except Exception as e:
                detail.update(f"[{color} bold]{item.block_label}[/]\n\n[red]Error: {e}[/red]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button actions."""
        list_view = self.query_one("#block-list", ListView)
        selected = list_view.highlighted_child
        if not isinstance(selected, BlockItem):
            return

        label = selected.block_label
        detail = self.query_one("#block-detail", Static)
        color = LABEL_COLORS.get(label, "#6c9fff")

        if event.button.id == "btn-update":
            input_widget = self.query_one("#block-content-input", Input)
            content = input_widget.value.strip()
            if content:
                try:
                    init_db()
                    manage_context_blocks(options=ContextBlockAction(action="update", label=label, content=content))
                    detail.update(f"[{color} bold]{label}[/]\n\n{content}")
                    input_widget.value = ""
                    self.refresh_data()
                except Exception as e:
                    detail.update(f"[red]Error updating block: {e}[/red]")

        elif event.button.id == "btn-reset":
            try:
                init_db()
                manage_context_blocks(options=ContextBlockAction(action="reset", label=label))
                detail.update(f"[{color} bold]{label}[/]\n\n(Reset to default)")
                self.refresh_data()
            except Exception as e:
                detail.update(f"[red]Error resetting block: {e}[/red]")

        elif event.button.id == "btn-clear-block":
            try:
                init_db()
                manage_context_blocks(options=ContextBlockAction(action="clear", label=label))
                detail.update(f"[{color} bold]{label}[/]\n\n(Cleared)")
                self.refresh_data()
            except Exception as e:
                detail.update(f"[red]Error clearing block: {e}[/red]")

        elif event.button.id == "btn-refresh-blocks":
            self.refresh_data()
