# Meson build file for Yaku
project('yaku',
  version: '6.1.0',
  license: 'GPL-3.0-or-later',
  default_options: [
    'warning_level=2',
    'werror=false',
  ]
)

# Dependencies
gnome = import('gnome')
python = import('python').find_installation('python3', required: true)
pkg = import('pkgconfig')

# Check for required dependencies
deps = [
  dependency('gtk4', version: '>=4.10'),
  dependency('libadwaita-1', version: '>=1.3'),
  dependency('gio-2.0', version: '>=2.70'),
  dependency('gio-unix-2.0', version: '>=2.70'),
]

# Check for system tools (Fedora uses different binary names)
tools = {
  'wimlib-imagex': 'wimlib-utils',
  'grub-mkimage': 'grub2-tools',
  'grub2-mkimage': 'grub2-tools',  # Fedora binary name
  'mkfs.vfat': 'dosfstools',
  'mkfs.ntfs': 'ntfs-3g',
  'mkfs.exfat': 'exfatprogs',
  'mkfs.ext4': 'e2fsprogs',
  'mkfs.btrfs': 'btrfs-progs',
  'udisksctl': 'udisks2',
}

found_tools = []
missing_tools = []
foreach tool, pkg_name: tools
  if find_program(tool, required: false).found()
    found_tools += tool
  else
    missing_tools += tool
  endif
endforeach

if missing_tools.length() > 0
  warning('Missing optional tools: ' + ', '.join(missing_tools))
  # Build list of package names for missing tools
  missing_pkgs = []
  foreach t: missing_tools
    missing_pkgs += tools[t]
  endforeach
  warning('Some features may not work. Install: ' + ', '.join(missing_pkgs))
endif

# Python package - check for gi module
py3 = import('python').find_installation('python3', required: true)
py_gi_check = run_command(py3, ['-c', 'import gi; import gi.repository.Gtk; import gi.repository.Adw; print("OK")'], check: true)

# Data files
data_dir = join_paths(get_option('datadir'), 'yaku')

# Desktop file
# Configuration for template files
conf = configuration_data()
conf.set('APP_ID', 'com.github.INIT-D43M0N.yaku')
conf.set('VERSION', meson.project_version())
conf.set('VERSION_DATE', run_command('date', ['-u', '+%Y-%m-%d'], check: true).stdout().strip())
conf.set('PYTHON', py3.path())
conf.set('MODULE', 'yaku.main')

# Desktop file
desktop_file = configure_file(
  input: 'data/com.github.INIT-D43M0N.yaku.desktop.in',
  output: 'com.github.INIT-D43M0N.yaku.desktop',
  configuration: conf,
  install_dir: join_paths(get_option('datadir'), 'applications'),
)

# AppData/Metainfo file
metainfo_file = configure_file(
  input: 'data/com.github.INIT-D43M0N.yaku.metainfo.xml.in',
  output: 'com.github.INIT-D43M0N.yaku.metainfo.xml',
  configuration: conf,
  install_dir: join_paths(get_option('datadir'), 'metainfo'),
)

# Icon (scalable wrapper + native 1024px raster)
icon_file = configure_file(
  input: 'data/icons/hicolor/scalable/apps/yaku.svg',
  output: 'com.github.INIT-D43M0N.yaku.svg',
  copy: true,
  install_dir: join_paths(get_option('datadir'), 'icons', 'hicolor', 'scalable', 'apps'),
)

icon_png = configure_file(
  input: 'data/icons/hicolor/1024x1024/apps/yaku.png',
  output: 'com.github.INIT-D43M0N.yaku.png',
  copy: true,
  install_dir: join_paths(get_option('datadir'), 'icons', 'hicolor', '1024x1024', 'apps'),
)

# Install Python package - main module
python.install_sources(
  [
    'yaku/__init__.py',
    'yaku/main.py',
    'yaku/window.py',
    'yaku/config.py',
  ],
  subdir: 'yaku',
)

# Install submodules
python.install_sources(
  [
    'yaku/utils/__init__.py',
    'yaku/utils/i18n.py',
    'yaku/utils/logging.py',
    'yaku/utils/resources.py',
    'yaku/utils/threading.py',
  ],
  subdir: 'yaku/utils',
)

python.install_sources(
  [
    'yaku/core/__init__.py',
    'yaku/core/devices.py',
    'yaku/core/images.py',
    'yaku/core/partitions.py',
    'yaku/core/gpt.py',
    'yaku/core/chromeos.py',
    'yaku/core/brunch.py',
    'yaku/core/macos.py',
    'yaku/core/bsd.py',
    'yaku/core/writer.py',
    'yaku/core/windows_togo.py',
    'yaku/core/bcd.py',
    'yaku/core/verify.py',
    'yaku/core/icon_utils.py',
    'yaku/core/provisioning.py',
    'yaku/core/direct_install.py',
    'yaku/core/iso_catalog.py',
    'yaku/core/iso_downloader.py',
    'yaku/core/windows_install.py',
    'yaku/core/windows_boot.py',
    'yaku/core/windows_debloat.py',
    'yaku/core/windows_drivers.py',
    'yaku/core/multi_flash.py',
  ],
  subdir: 'yaku/core',
)

python.install_sources(
  [
    'yaku/udisks/__init__.py',
    'yaku/udisks/client.py',
  ],
  subdir: 'yaku/udisks',
)

python.install_sources(
  [
    'yaku/pages/__init__.py',
    'yaku/pages/base.py',
    'yaku/pages/drive_page.py',
    'yaku/pages/image_page.py',
    'yaku/pages/brunch_page.py',
    'yaku/pages/summary_page.py',
    'yaku/pages/flash_page.py',
    'yaku/pages/disc.py',
    'yaku/pages/hdd.py',
    'yaku/pages/iso_download.py',
    'yaku/pages/drive_dialog.py',
  ],
  subdir: 'yaku/pages',
)

# Install data files
install_data(
  'data/styles.css',
  install_dir: join_paths(get_option('datadir'), 'yaku'),
)

# Windows BCD template documentation (templates can be supplied at runtime).
install_data(
  'data/bcd/README.md',
  install_dir: join_paths(get_option('datadir'), 'yaku', 'bcd'),
)

# Easter-egg artwork: the interactive disc used on Step 2 and Step 4.
install_data(
  files(
    'data/assets/discback.png',
    'data/assets/discfront.png',
  ),
  install_dir: join_paths(get_option('datadir'), 'yaku', 'assets'),
)

# ISO download catalog + schema
install_data(
  files(
    'data/iso_catalog.json',
    'data/iso_catalog.schema.json',
  ),
  install_dir: join_paths(get_option('datadir'), 'yaku'),
)

# Install GTK4/Libadwaita interface definitions (.ui)
install_data(
  files(
    'data/ui/drive_page.ui',
    'data/ui/image_page.ui',
    'data/ui/brunch_page.ui',
    'data/ui/summary_page.ui',
    'data/ui/flash_page.ui',
    'data/ui/shortcuts.ui',
  ),
  install_dir: join_paths(get_option('datadir'), 'yaku', 'ui'),
)

# Install data files
install_data(
  desktop_file,
  install_dir: join_paths(get_option('datadir'), 'applications'),
)

install_data(
  metainfo_file,
  install_dir: join_paths(get_option('datadir'), 'metainfo'),
)

install_data(
  icon_file,
  install_dir: join_paths(get_option('datadir'), 'icons', 'hicolor', 'scalable', 'apps'),
)

# Create wrapper script
wrapper = configure_file(
  input: 'data/yaku-wrapper.in',
  output: 'yaku',
  configuration: {
    'PYTHON': python.path(),
    'MODULE': 'yaku.main',
  },
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)

# Test
test('import_test', python, args: ['-c', 'import yaku; print("OK")'])

# Summary - compute values first
tools_found_str = ', '.join(found_tools)
if found_tools.length() == 0
  tools_found_str = 'none'
endif

tools_missing_str = ', '.join(missing_tools)
if missing_tools.length() == 0
  tools_missing_str = 'none'
endif

summary = {
  'Project': meson.project_name(),
  'Version': meson.project_version(),
  'Python': python.version(),
  'GTK4': deps[0].version(),
  'Libadwaita': deps[1].version(),
  'Tools found': tools_found_str,
  'Tools missing': tools_missing_str,
}