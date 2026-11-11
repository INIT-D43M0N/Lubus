"""Internationalization support for Yaku."""
import gettext
import locale
import os
from pathlib import Path

# Translation domain
DOMAIN = "yaku"

# Where to find .mo files
LOCALE_DIRS = [
    Path(__file__).parent.parent / "po",  # Development: yaku/../po
    Path("/usr/share/locale"),            # System install
    Path("/usr/local/share/locale"),      # Local install
]

# Current language
_current_language = None
_translator = None


def set_language(lang_code):
    """Set the application language.

    Args:
        lang_code: Language code like 'en_US', 'pt_BR', 'ru_RU', or None for system default.
    """
    global _current_language, _translator

    if lang_code:
        os.environ["LANG"] = lang_code
        os.environ["LC_ALL"] = lang_code
        _current_language = lang_code
    else:
        _current_language = None

    # Clear any cached translator
    _translator = None

    # Re-initialize gettext
    _init_gettext()


def _init_gettext():
    """Initialize gettext with the current language."""
    global _translator

    # Reset translator
    _translator = None

    # Try to find and load translation
    for locale_dir in LOCALE_DIRS:
        try:
            if _current_language:
                # Explicit language
                mo_path = locale_dir / _current_language / "LC_MESSAGES" / f"{DOMAIN}.mo"
                if mo_path.exists():
                    _translator = gettext.translation(
                        DOMAIN, localedir=str(locale_dir), languages=[_current_language]
                    )
                    break
            else:
                # System default
                _translator = gettext.translation(DOMAIN, localedir=str(locale_dir))
                break
        except (OSError, FileNotFoundError, ValueError):
            continue

    if _translator:
        try:
            _translator.install()
        except Exception:
            # If install fails, fall back to null translator
            _translator = None

    if not _translator:
        # Fallback to null translator (no translations)
        gettext.install(DOMAIN)


def get_language():
    """Get the current language code."""
    try:
        default_locale = locale.getdefaultlocale()
        if default_locale and default_locale[0]:
            return _current_language or default_locale[0] or "en_US"
    except (ValueError, TypeError):
        pass
    return _current_language or "en_US"


def _(message):
    """Translation function (alias for gettext.gettext)."""
    if _translator:
        return _translator.gettext(message)
    return message


def ngettext(singular, plural, n):
    """Plural-aware translation."""
    if _translator:
        return _translator.ngettext(singular, plural, n)
    return singular if n == 1 else plural


# Initialize on import
_init_gettext()

# Export for convenience
gettext.install(DOMAIN)