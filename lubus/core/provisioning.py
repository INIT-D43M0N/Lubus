"""User/system provisioning helpers for Lubus direct installs.

The generators are intentionally best-effort and adapter-friendly:

* Windows drives receive ``unattend.xml`` (OOBE user, hostname, timezone).
* Linux drives receive a cloud-init NoCloud seed plus a machine-readable
  ``lubus-install.json``.  Distributions that understand cloud-init will
  apply the account on first boot; other adapters can consume the JSON.

None of these helpers touch the host system: they only write inside the
mounted target filesystem.
"""
import json
import os
import subprocess
from xml.sax.saxutils import escape


def _cfg_get(config, key, default=""):
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def build_unattend_xml(config) -> str:
    """Build a Windows unattend/autounattend response file.

    Supported options in ``config``:
      skip_oobe (default True), bypass_win11 (default False),
      remove_defender (default False), username/password/full_name/hostname,
      timezone/language/keyboard.
    """
    from .windows_debloat import defender_first_logon_commands

    username = escape(str(_cfg_get(config, "username", "lubus")))
    password = escape(str(_cfg_get(config, "password", "")))
    full_name = escape(str(_cfg_get(config, "full_name", "") or username))
    hostname = escape(str(_cfg_get(config, "hostname", "lubus-pc")))
    timezone = escape(str(_cfg_get(config, "timezone", "UTC")))
    language = escape(str(_cfg_get(config, "language", "en-US")))
    keyboard = escape(str(_cfg_get(config, "keyboard", "0409:00000409")))
    bypass = bool(_cfg_get(config, "bypass_win11", False))
    skip_oobe = bool(_cfg_get(config, "skip_oobe", True))
    remove_defender = bool(_cfg_get(config, "remove_defender", False))
    accept_eula = bool(_cfg_get(config, "accept_eula", True))

    # --- Windows 11 setup requirement bypass (windowsPE) -----------------
    bypass_block = ""
    if bypass:
        checks = (
            "BypassTPMCheck", "BypassSecureBootCheck", "BypassRAMCheck",
            "BypassCPUCheck", "BypassStorageCheck",
        )
        commands = "\n".join(
            f"""        <RunSynchronousCommand wcm:action="add">
          <Order>{i}</Order>
          <Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v {check} /t REG_DWORD /d 1 /f</Path>
        </RunSynchronousCommand>"""
            for i, check in enumerate(checks, 1)
        )
        bypass_block = f"""
      <RunSynchronous>
{commands}
      </RunSynchronous>"""

    # --- first-logon commands (telemetry / NRO / Defender) ---------------
    first_commands = []
    if skip_oobe:
        first_commands.extend([
            'reg add "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\DataCollection" /v AllowTelemetry /t REG_DWORD /d 0 /f',
            'reg add "HKLM\\SOFTWARE\\Policies\\Microsoft\\Windows\\OOBE" /v DisablePrivacyExperience /t REG_DWORD /d 1 /f',
            'reg add "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\OOBE" /v DisablePrivacyExperience /t REG_DWORD /d 1 /f',
        ])
    if bypass:
        first_commands.append(
            'reg add "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\OOBE" /v BypassNRO /t REG_DWORD /d 1 /f'
        )
    if remove_defender:
        first_commands.extend(defender_first_logon_commands())

    first_logon_block = ""
    if first_commands:
        entries = "\n".join(
            f"""        <SynchronousCommand wcm:action="add">
          <Order>{i}</Order>
          <CommandLine>{escape(cmd)}</CommandLine>
        </SynchronousCommand>"""
            for i, cmd in enumerate(first_commands, 1)
        )
        first_logon_block = f"""
      <FirstLogonCommands>
{entries}
      </FirstLogonCommands>"""

    oobe_block = ""
    accounts_block = ""
    if skip_oobe:
        oobe_block = """
      <OOBE>
        <HideEULAPage>true</HideEULAPage>
        <HideLocalAccountScreen>true</HideLocalAccountScreen>
        <HideOEMRegistrationScreen>true</HideOEMRegistrationScreen>
        <HideOnlineAccountScreens>true</HideOnlineAccountScreens>
        <HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE>
        <HidePrivacyExperience>true</HidePrivacyExperience>
        <ProtectYourPC>3</ProtectYourPC>
      </OOBE>"""
        accounts_block = f"""
      <UserAccounts>
        <LocalAccounts>
          <LocalAccount wcm:action="add">
            <Name>{username}</Name>
            <DisplayName>{full_name}</DisplayName>
            <Group>Administrators</Group>
            <Password>
              <Value>{password}</Value>
              <PlainText>true</PlainText>
            </Password>
          </LocalAccount>
        </LocalAccounts>
      </UserAccounts>"""

    oobe_system = ""
    if skip_oobe or first_logon_block:
        oobe_system = f"""
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-Shell-Setup"
               processorArchitecture="amd64"
               publicKeyToken="31bf3856ad364e35"
               language="neutral" versionScope="nonSxS"
               xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">{oobe_block}{accounts_block}{first_logon_block}
    </component>
  </settings>"""

    return f"""<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup"
               processorArchitecture="amd64"
               publicKeyToken="31bf3856ad364e35"
               language="neutral" versionScope="nonSxS"
               xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <ComputerName>{hostname}</ComputerName>
      <TimeZone>{timezone}</TimeZone>
    </component>
    <component name="Microsoft-Windows-International-Core"
               processorArchitecture="amd64"
               publicKeyToken="31bf3856ad364e35"
               language="neutral" versionScope="nonSxS"
               xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <InputLocale>{keyboard}</InputLocale>
      <SystemLocale>{language}</SystemLocale>
      <UILanguage>{language}</UILanguage>
      <UserLocale>{language}</UserLocale>
    </component>
  </settings>{oobe_system}
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-Setup"
               processorArchitecture="amd64"
               publicKeyToken="31bf3856ad364e35"
               language="neutral" versionScope="nonSxS"
               xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State"
               xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <UserData>
        <AcceptEula>{str(accept_eula).lower()}</AcceptEula>
        <Organization>Lubus</Organization>
      </UserData>{bypass_block}
    </component>
  </settings>
</unattend>
"""


def write_windows_provisioning(win_mount: str, config):
    """Write autounattend.xml / unattend.xml and metadata.

    Both the root and Windows\\Panther locations are populated: the root copy
    is used by setup/PE, while Panther is read by the deployed OS on first
    boot.
    """
    xml = build_unattend_xml(config)
    targets = [
        ("autounattend.xml", os.path.join(win_mount, "autounattend.xml")),
        ("unattend.xml", os.path.join(win_mount, "unattend.xml")),
        ("autounattend.xml",
         os.path.join(win_mount, "Windows", "Panther", "autounattend.xml")),
        ("unattend.xml",
         os.path.join(win_mount, "Windows", "Panther", "unattend.xml")),
    ]
    for _name, path in targets:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(xml)
        except OSError:
            continue
    try:
        _write_json(os.path.join(win_mount, "LubusInstall.json"), config)
    except OSError:
        pass


def _hash_password(password: str) -> str | None:
    """Hash a password for cloud-init without storing it in plain text."""
    if not password:
        return None
    try:
        result = subprocess.run(
            ["openssl", "passwd", "-6", password],
            capture_output=True, check=False, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.decode("utf-8").strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def build_cloud_init_user_data(config) -> str:
    """Build a cloud-init user-data document with the requested account."""
    username = str(_cfg_get(config, "username", "lubus"))
    password = str(_cfg_get(config, "password", ""))
    full_name = str(_cfg_get(config, "full_name", "") or username)
    hostname = str(_cfg_get(config, "hostname", "lubus-pc"))
    timezone = str(_cfg_get(config, "timezone", "UTC"))
    password_hash = _hash_password(password)

    account = (
        f"    passwd: {password_hash}\n"
        if password_hash else ""
    )
    chpasswd = (
        "" if password_hash else
        f"chpasswd:\n  list: |\n    {username}:{password}\n  expire: false\n"
    )

    # YAML-safe enough for the simple values validated by InstallConfig.
    return f"""#cloud-config
hostname: {hostname}
preserve_hostname: false
timezone: {timezone}
users:
  - name: {username}
    gecos: {full_name}
    groups: [sudo, wheel]
    shell: /bin/bash
    lock_passwd: false
{account}{chpasswd}ssh_pwauth: true
"""


def _write_json(path: str, config):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "username": _cfg_get(config, "username", "lubus"),
                "full_name": _cfg_get(config, "full_name", ""),
                "hostname": _cfg_get(config, "hostname", "lubus-pc"),
                "timezone": _cfg_get(config, "timezone", "UTC"),
                "partition_mode": _cfg_get(config, "partition_mode", "auto"),
                "filesystem": _cfg_get(config, "filesystem", "ext4"),
            },
            f,
            indent=2,
        )



def write_installer_autounattend(mount_root: str, config):
    """Write ``autounattend.xml`` at the root of a Windows installer USB."""
    xml = build_unattend_xml(config)
    try:
        with open(os.path.join(mount_root, "autounattend.xml"), "w",
                  encoding="utf-8") as f:
            f.write(xml)
    except OSError:
        pass

def write_linux_provisioning(root_mount: str, config):
    """Write cloud-init seed and metadata into a mounted Linux root."""
    seed = os.path.join(root_mount, "var", "lib", "cloud", "seed", "nocloud-net")
    try:
        os.makedirs(seed, exist_ok=True)
        with open(os.path.join(seed, "user-data"), "w", encoding="utf-8") as f:
            f.write(build_cloud_init_user_data(config))
        with open(os.path.join(seed, "meta-data"), "w", encoding="utf-8") as f:
            f.write(
                "instance-id: lubus-direct\n"
                f"local-hostname: {_cfg_get(config, 'hostname', 'lubus-pc')}\n"
            )
    except OSError:
        pass

    # Best-effort static files for non-cloud-init distributions.
    try:
        with open(os.path.join(root_mount, "etc", "hostname"), "w",
                  encoding="utf-8") as f:
            f.write(str(_cfg_get(config, "hostname", "lubus-pc")) + "\n")
    except OSError:
        pass
    try:
        with open(os.path.join(root_mount, "etc", "timezone"), "w",
                  encoding="utf-8") as f:
            f.write(str(_cfg_get(config, "timezone", "UTC")) + "\n")
    except OSError:
        pass
    try:
        _write_json(os.path.join(root_mount, "lubus-install.json"), config)
    except OSError:
        pass
