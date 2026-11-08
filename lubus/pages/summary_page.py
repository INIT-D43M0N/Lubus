"""Summary/Confirmation page (Step 3 of the wizard).

Layout: ``data/ui/summary_page.ui`` — read-only Adw.ActionRows under two
Adw.PreferencesGroups plus the verify Adw.SwitchRow, with the destructive
warning carried by an Adw.Banner (HIG: banners, not red labels).
"""
import os

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw

from lubus.utils.i18n import _
from lubus.core.images import (
    FileSystem, ImageType, PartitionScheme, TargetSystem,
)
from lubus.pages.base import UiPage


class SummaryPage(UiPage):
    """Page showing a summary of all settings before flashing."""

    ui_name = "summary_page.ui"
    root_id = "root"

    def __init__(self, on_verify_toggled=None, **kwargs):
        super().__init__()
        self.on_verify_toggled = on_verify_toggled
        self.on_windows_options_changed = None
        self._settings: dict = {}

        # Widgets
        self.warning_banner = self.get("warning_banner")
        self.drive_row = self.get("drive_row")
        self.image_row = self.get("image_row")
        self.install_mode_row = self.get("install_mode_row")
        self.user_row = self.get("user_row")
        self.partition_row = self.get("partition_row")
        self.windows_group = self.get("windows_group")
        self.win_skip_oobe_row = self.get("win_skip_oobe_row")
        self.win_bypass_row = self.get("win_bypass_row")
        self.win_remove_defender_row = self.get("win_remove_defender_row")
        self.win_username_row = self.get("win_username_row")
        self.win_password_row = self.get("win_password_row")
        self.win_fullname_row = self.get("win_fullname_row")
        self._updating_windows = False
        self.os_row = self.get("os_row")
        self.edition_row = self.get("edition_row")
        self.scheme_row = self.get("scheme_row")
        self.target_row = self.get("target_row")
        self.fs_row = self.get("fs_row")
        self.label_row = self.get("label_row")
        self.cluster_row = self.get("cluster_row")
        self.write_mode_row = self.get("mode_row")
        self.verify_row = self.get("verify_row")

        self.verify_row.connect("notify::active", self._on_verify_toggled)
        self.win_skip_oobe_row.connect("notify::active", self._on_windows_option_changed)
        self.win_bypass_row.connect("notify::active", self._on_windows_option_changed)
        self.win_remove_defender_row.connect("notify::active", self._on_windows_option_changed)
        self.win_username_row.connect("changed", self._on_windows_option_changed)
        self.win_password_row.connect("changed", self._on_windows_option_changed)
        self.win_fullname_row.connect("changed", self._on_windows_option_changed)

    # ---------------------------------------------------------------- public

    def update_summary(self, settings: dict):
        """Refresh every row from the current settings dict."""
        self._settings = settings

        drives = settings.get("drives") or []
        if not drives and settings.get("drive") is not None:
            drives = [settings["drive"]]
        if drives:
            self.drive_row.set_subtitle(
                "\n".join(
                    f"• {d.device} · {d.model} · {d.size_human}"
                    for d in drives
                )
            )

        install = settings.get("install_config")
        direct = bool(install and install.is_direct)
        if direct:
            self.install_mode_row.set_subtitle(_("Direct install (portable)"))
            self.user_row.set_visible(True)
            self.user_row.set_subtitle(
                f"{install.username}@{install.hostname}"
            )
            self.partition_row.set_visible(True)
            windows = install.filesystem == "ntfs"
            if install.is_custom:
                if windows:
                    boot = "UEFI" if install.boot_mode == "uefi" else "BIOS"
                    self.partition_row.set_subtitle(
                        _("Custom · Windows {root} GiB · ESP {esp} MiB · {boot}").format(
                            root=install.root_gib,
                            esp=install.esp_mib,
                            boot=boot,
                        )
                    )
                else:
                    self.partition_row.set_subtitle(
                        _("Custom · Root {root} GiB · Swap {swap} GiB · {fs}").format(
                            root=install.root_gib,
                            swap=install.swap_gib,
                            fs=install.filesystem,
                        )
                    )
            else:
                self.partition_row.set_subtitle(_("Automatic"))
        else:
            self.install_mode_row.set_subtitle(_("Live USB"))
            self.user_row.set_visible(False)
            self.partition_row.set_visible(False)

        info = settings.get("image_info")
        windows = bool(info is not None and info.image_type == ImageType.WINDOWS_INSTALLER)
        self.windows_group.set_visible(windows)
        if windows:
            opts = settings.get("windows_options") or {}
            install = settings.get("install_config")
            self._updating_windows = True
            try:
                self.win_skip_oobe_row.set_active(opts.get("skip_oobe", True))
                self.win_bypass_row.set_active(
                    opts.get("bypass_win11",
                             getattr(install, "bypass_win11", False))
                )
                self.win_remove_defender_row.set_active(
                    opts.get("remove_defender", False)
                )
                self.win_username_row.set_text(
                    opts.get("username") or getattr(install, "username", "lubus")
                )
                self.win_password_row.set_text(
                    opts.get("password") or getattr(install, "password", "")
                )
                self.win_fullname_row.set_text(
                    opts.get("full_name") or getattr(install, "full_name", "")
                )
            finally:
                self._updating_windows = False

        image_path = settings.get("image_path")
        if image_path:
            self.image_row.set_subtitle(os.path.basename(image_path))

        info = settings.get("image_info")
        os_types = [
            _("Windows Installer"),
            _("Linux (ISOhybrid / dd mode)"),
            _("Linux Installer (file copy)"),
            _("Other / Unknown"),
        ]
        os_idx = 0 if info is None else _os_type_index(info)
        if 0 <= os_idx < len(os_types):
            self.os_row.set_subtitle(os_types[os_idx])

        scheme = settings.get("scheme", PartitionScheme.GPT)
        self.scheme_row.set_subtitle(
            _("GPT (UEFI)") if scheme == PartitionScheme.GPT else _("MBR (BIOS/CSM)")
        )

        target = settings.get("target", TargetSystem.UEFI)
        self.target_row.set_subtitle(
            _("UEFI (non-CSM)") if target == TargetSystem.UEFI else _("BIOS (CSM/Legacy)")
        )

        fs = settings.get("filesystem", FileSystem.FAT32)
        self.fs_row.set_subtitle(fs.name if fs else "—")

        self.label_row.set_subtitle(settings.get("volume_label") or "LUBUS")

        mode = settings.get("write_mode", 1)
        self.write_mode_row.set_subtitle(
            _("DD Image Mode (block copy)") if mode == 0
            else _("ISO Image Mode (file extraction)")
        )

        if os_idx == 0 and settings.get("edition_index"):
            self.edition_row.set_subtitle(f"Index {settings['edition_index']}")
            self.edition_row.set_visible(True)
        else:
            self.edition_row.set_visible(False)

    def get_windows_options(self) -> dict:
        """Return the Windows setup/debloat options."""
        return {
            "skip_oobe": bool(self.win_skip_oobe_row.get_active()),
            "bypass_win11": bool(self.win_bypass_row.get_active()),
            "remove_defender": bool(self.win_remove_defender_row.get_active()),
            "username": self.win_username_row.get_text().strip() or "lubus",
            "password": self.win_password_row.get_text(),
            "full_name": self.win_fullname_row.get_text().strip(),
        }

    def get_verify_enabled(self) -> bool:
        return self.verify_row.get_active()

    def set_verify_enabled(self, enabled: bool):
        self.verify_row.set_active(enabled)

    def set_verify_supported(self, supported: bool):
        """Raw verification is only meaningful for direct DD images."""
        self.verify_row.set_sensitive(supported)
        if supported:
            self.verify_row.set_subtitle(
                _("Compare the drive with the source image")
            )
        else:
            self.verify_row.set_subtitle(
                _("Available for direct DD images only")
            )

    # ------------------------------------------------------------- internals

    def _on_verify_toggled(self, row, _pspec):
        if self.on_verify_toggled:
            self.on_verify_toggled(row.get_active())

    def _on_windows_option_changed(self, *_args):
        if self._updating_windows:
            return
        if self.on_windows_options_changed:
            self.on_windows_options_changed(self.get_windows_options())


def _os_type_index(info) -> int:
    """Map an ImageInfo onto the summary's image-type list."""
    from lubus.core.images import ImageType

    mapping = {
        ImageType.WINDOWS_INSTALLER: 0,
        ImageType.LINUX_ISHYBRID: 1,
        ImageType.LINUX_INSTALLER: 2,
    }
    return mapping.get(info.image_type, 3)
