# Meson build file for Lubus
project('lubus',
  version: '5.0.0',
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
data_dir = join_paths(get_option('datadir'), 'lubus')

# Desktop file
# Configuration for template files
conf = configuration_data()
conf.set('APP_ID', 'com.github.yourusername.lubus')
conf.set('VERSION', meson.project_version())
conf.set('VERSION_DATE', run_command('date', ['-u', '+%Y-%m-%d'], check: true).stdout().strip())
conf.set('PYTHON', py3.path())
conf.set('MODULE', 'lubus.main')

# Desktop file
desktop_file = configure_file(
  input: 'data/com.github.yourusername.lubus.desktop.in',
  output: 'com.github.yourusername.lubus.desktop',
  configuration: conf,
  install_dir: join_paths(get_option('datadir'), 'applications'),
)

# AppData/Metainfo file
metainfo_file = configure_file(
  input: 'data/com.github.yourusername.lubus.metainfo.xml.in',
  output: 'com.github.yourusername.lubus.metainfo.xml',
  configuration: conf,
  install_dir: join_paths(get_option('datadir'), 'metainfo'),
)

# Icon (scalable wrapper + native 1024px raster)
icon_file = configure_file(
  input: 'data/icons/hicolor/scalable/apps/lubus.svg',
  output: 'com.github.yourusername.lubus.svg',
  copy: true,
  install_dir: join_paths(get_option('datadir'), 'icons', 'hicolor', 'scalable', 'apps'),
)

icon_png = configure_file(
  input: 'data/icons/hicolor/1024x1024/apps/lubus.png',
  output: 'com.github.yourusername.lubus.png',
  copy: true,
  install_dir: join_paths(get_option('datadir'), 'icons', 'hicolor', '1024x1024', 'apps'),
)

# Install Python package - main module
python.install_sources(
  [
    'lubus/__init__.py',
    'lubus/main.py',
    'lubus/window.py',
    'lubus/config.py',
  ],
  subdir: 'lubus',
)

# Install submodules
python.install_sources(
  [
    'lubus/utils/__init__.py',
    'lubus/utils/i18n.py',
    'lubus/utils/logging.py',
    'lubus/utils/resources.py',
    'lubus/utils/threading.py',
  ],
  subdir: 'lubus/utils',
)

python.install_sources(
  [
    'lubus/core/__init__.py',
    'lubus/core/devices.py',
    'lubus/core/images.py',
    'lubus/core/partitions.py',
    'lubus/core/writer.py',
    'lubus/core/windows_togo.py',
    'lubus/core/bcd.py',
    'lubus/core/verify.py',
    'lubus/core/icon_utils.py',
    'lubus/core/provisioning.py',
    'lubus/core/direct_install.py',
    'lubus/core/iso_catalog.py',
    'lubus/core/iso_downloader.py',
    'lubus/core/windows_install.py',
    'lubus/core/windows_boot.py',
    'lubus/core/windows_debloat.py',
    'lubus/core/windows_drivers.py',
    'lubus/core/multi_flash.py',
  ],
  subdir: 'lubus/core',
)

python.install_sources(
  [
    'lubus/udisks/__init__.py',
    'lubus/udisks/client.py',
  ],
  subdir: 'lubus/udisks',
)

python.install_sources(
  [
    'lubus/pages/__init__.py',
    'lubus/pages/base.py',
    'lubus/pages/drive_page.py',
    'lubus/pages/image_page.py',
    'lubus/pages/summary_page.py',
    'lubus/pages/flash_page.py',
    'lubus/pages/disc.py',
    'lubus/pages/hdd.py',
    'lubus/pages/iso_download.py',
    'lubus/pages/drive_dialog.py',
  ],
  subdir: 'lubus/pages',
)

# Install data files
install_data(
  'data/styles.css',
  install_dir: join_paths(get_option('datadir'), 'lubus'),
)

# Windows BCD template documentation (templates can be supplied at runtime).
install_data(
  'data/bcd/README.md',
  install_dir: join_paths(get_option('datadir'), 'lubus', 'bcd'),
)

# Easter-egg artwork: the interactive disc used on Step 2 and Step 4.
install_data(
  files(
    'data/assets/discback.png',
    'data/assets/discfront.png',
  ),
  install_dir: join_paths(get_option('datadir'), 'lubus', 'assets'),
)

# ISO download catalog + schema
install_data(
  files(
    'data/iso_catalog.json',
    'data/iso_catalog.schema.json',
  ),
  install_dir: join_paths(get_option('datadir'), 'lubus'),
)

# Install GTK4/Libadwaita interface definitions (.ui)
install_data(
  files(
    'data/ui/drive_page.ui',
    'data/ui/image_page.ui',
    'data/ui/summary_page.ui',
    'data/ui/flash_page.ui',
    'data/ui/shortcuts.ui',
  ),
  install_dir: join_paths(get_option('datadir'), 'lubus', 'ui'),
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
  input: 'data/lubus-wrapper.in',
  output: 'lubus',
  configuration: {
    'PYTHON': python.path(),
    'MODULE': 'lubus.main',
  },
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)

# Test
test('import_test', python, args: ['-c', 'import lubus; print("OK")'])

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