# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

## Project Overview

**slate** is a video image classifier: a Python CLI tool that adds short descriptions to generically named raw video files from a camera.

<!-- Expand on why this project exists / additional context -->

## Setup

<!-- How to install dependencies / bootstrap a dev environment -->

## Commands

<!-- Build, run, test, lint commands -->

- Build:
- Run:
- Test:
- Lint: `uv run ruff check .` (add `--fix` to auto-fix), `uv run ruff format .` to reformat (`--check` to verify without changing anything)

## Architecture

<!-- High-level structure: key modules, data flow, important design decisions -->

## Conventions

<!-- Coding style, naming, patterns specific to this repo -->

- Formatting and style are enforced by `ruff` (config in `pyproject.toml`'s
  `[tool.ruff]`/`[tool.ruff.lint]`), not hand-applied -- run `uv run ruff
  format .` rather than manually matching existing style, and `uv run ruff
  check .` before considering a change done.

## Notes

<!-- Anything else Claude should know: gotchas, external services, etc. -->
