from __future__ import annotations

from rich.console import Console

# Centralized so every phase reports status the same way -- green/checkmark
# for success, yellow/warning-triangle for recoverable problems (skipped,
# missing files, collisions), red/cross for hard failures, so a scroll-back
# through a long batch reads at a glance rather than requiring line-by-line
# reading.
console = Console()
error_console = Console(stderr=True)


def ok(message: str) -> None:
    console.print(f"[bold green]✅ {message}[/bold green]")


def warn(message: str) -> None:
    console.print(f"[bold yellow]⚠️  {message}[/bold yellow]")


def error(message: str) -> None:
    console.print(f"[bold red]❌ {message}[/bold red]")


def fatal(message: str) -> None:
    error_console.print(f"[bold red]❌ {message}[/bold red]")


def skip(message: str) -> None:
    console.print(f"[dim]⏭️  {message}[/dim]")


def info(message: str) -> None:
    console.print(f"[cyan]ℹ️  {message}[/cyan]")


def renamed(message: str) -> None:
    console.print(f"[green]🔄 {message}[/green]")
