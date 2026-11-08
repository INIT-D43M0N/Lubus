# Contributing to Lubus

Thank you for your interest in contributing to Lubus! This document provides guidelines for contributing.

## Code of Conduct

Please be respectful and inclusive in all interactions. We follow the [Contributor Covenant](https://www.contributor-covenant.org/).

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/yourusername/lubus.git`
3. Create a branch: `git checkout -b feature/my-feature`
4. Make your changes
5. Run tests: `pytest tests/`
6. Run linting: `ruff check lubus/`
7. Submit a pull request

## Development Setup

### Dependencies

```bash
# Ubuntu/Debian
sudo apt install python3-gobject python3-pytest python3-ruff gir1.2-gtk-4.0 gir1.2-adw-1 libgirepository1.0-dev

# Fedora
sudo dnf install python3-gobject python3-pytest python3-ruff gobject-introspection-devel gtk4-devel libadwaita-devel

# Arch
sudo pacman -S python-gobject python-pytest python-ruff gobject-introspection gtk4 libadwaita
```

### Install in Development Mode

```bash
pip install -e ".[dev]"
```

### Run Tests

```bash
pytest tests/ -v
```

### Run Linting

```bash
ruff check lubus/
ruff format lubus/
```

### Type Checking

```bash
mypy lubus/
```

## Code Style

- Follow PEP 8 (enforced by ruff)
- Use type hints for all public functions
- Write docstrings for all public classes and functions
- Maximum line length: 100 characters
- Use double quotes for strings

## Architecture Guidelines

- Keep GTK/UI code separate from core logic
- Core modules (`lubus/core/`) should have no GTK dependencies
- Use async/threading for long-running operations
- All disk operations go through UDisks2 D-Bus
- Handle errors gracefully with user-friendly messages

## Testing

- Write unit tests for core logic
- Mock external dependencies (UDisks, subprocesses)
- Test edge cases (cancelled operations, permission errors, etc.)

## Pull Request Process

1. Ensure all tests pass
2. Update documentation if needed
3. Add changelog entry in `CHANGELOG.md`
4. Request review from maintainers

## Reporting Issues

- Use the GitHub issue tracker
- Provide steps to reproduce
- Include system information (distro, GTK version, etc.)
- Attach logs if relevant

## Translations

Translations are managed in the `po/` directory. To add a new language:

1. Copy `po/lubus.pot` to `po/<lang_code>.po`
2. Translate the strings
3. Add the language to `meson.build`

## Security

- Never run as root; use UDisks2 + polkit for privileged operations
- Validate all user inputs
- Sanitize file paths
- Use subprocess with explicit argv lists (no shell=True)

## Questions?

Feel free to open a discussion on GitHub or contact the maintainers.