"""
Build script: packages main.py into a standalone, distributable
installer with PyInstaller, so it can run on a machine with no Python
installed.

This only builds for whichever OS it's run ON (PyInstaller doesn't
cross-compile) -- run build_windows.bat on Windows to get the Windows
output, and build_mac.command on a Mac to get the macOS output.

Output:
    Windows -> dist/GoogleAIOverviewScraperSetup.exe
               A normal click-through installer (built with Inno Setup):
               it installs the app under Program Files, adds a Start
               Menu shortcut, and registers a proper uninstaller in
               "Add or remove programs". Requires Inno Setup to be
               installed on THIS build machine once -- see the
               [ERROR] message below for the link if it's missing.
               (Only the build machine needs Inno Setup; the people you
               hand GoogleAIOverviewScraperSetup.exe to don't.)
    macOS   -> dist/GoogleAIOverviewScraper.dmg
               A disk image: double-click it, then drag the app onto
               the Applications shortcut inside, same as any normal
               Mac app you'd download.
"""

import glob
import os
import platform
import shutil
import subprocess
import sys

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
APP_NAME = "GoogleAIOverviewScraper"
IS_MAC = platform.system() == "Darwin"


def have_module(name):
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def run_pyinstaller():
    if not have_module("PyInstaller"):
        print("Installing PyInstaller...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)

    separator = os.pathsep  # ';' on Windows, ':' on macOS/Linux

    args = [
        "pyinstaller",
        "main.py",
        "--name", APP_NAME,
        # --onedir on both platforms now: macOS needs it to produce a
        # proper .app bundle for the .dmg, and Windows needs it too --
        # the Inno Setup installer below packages this folder, and
        # --onedir also starts faster than --onefile (which has to
        # silently unpack itself to a temp folder on every launch).
        "--onedir",
        "--windowed",
        "--noconfirm",
        "--add-data", f"css-selectors.json{separator}.",
        "--add-data", f"stealthify.js{separator}.",
        "--hidden-import", "nicegui",
        "--collect-all", "nicegui",
        "--collect-all", "markdownify",
        "--collect-all", "selenium",
        "--collect-all", "webview",
    ]

    if IS_MAC:
        # pywebview's macOS backend (webview.platforms.cocoa) bridges to
        # Cocoa/WebKit through PyObjC, which does its importing at
        # runtime (objc.loadBundle and friends) rather than through
        # normal Python imports PyInstaller's static analysis can see.
        # Without these explicit hints, the frozen app can end up with
        # no working native-window backend at all even when pywebview
        # itself is installed -- ui.run(native=True) then fails
        # immediately with "Native mode is not supported in this
        # configuration", which looks exactly like the app opening and
        # instantly closing, since no window ever actually appears.
        args += [
            "--hidden-import", "webview.platforms.cocoa",
            "--hidden-import", "objc",
            "--hidden-import", "Foundation",
            "--hidden-import", "AppKit",
            "--hidden-import", "WebKit",
            "--hidden-import", "Quartz",
            "--collect-all", "objc",
        ]

    assets_dir = os.path.join(BASE_DIR, "assets")
    if os.path.isdir(assets_dir):
        args += ["--add-data", f"assets{separator}assets"]

    # Optional icon: icon.ico for Windows, icon.icns for macOS. If
    # neither exists, PyInstaller just uses its default icon.
    icon_file = "assets/icon.icns" if IS_MAC else "assets/icon.ico"
    icon_path = os.path.join(BASE_DIR, icon_file)
    if os.path.isfile(icon_path):
        args += ["--icon", icon_path]

    print("Running:", " ".join(args))
    subprocess.run(args, check=True, cwd=BASE_DIR)


def build_dmg():
    """Wrap dist/<APP_NAME>.app into dist/<APP_NAME>.dmg using hdiutil,
    which ships with every Mac -- no extra tools to install. Includes
    an "Applications" shortcut alongside the app so opening the .dmg
    looks and works like any normal downloaded Mac app installer."""
    dist_dir = os.path.join(BASE_DIR, "dist")
    app_path = os.path.join(dist_dir, APP_NAME + ".app")
    if not os.path.isdir(app_path):
        print(f"[ERROR] Expected app bundle not found at {app_path}")
        sys.exit(1)

    staging_dir = os.path.join(dist_dir, "dmg_staging")
    if os.path.isdir(staging_dir):
        shutil.rmtree(staging_dir)
    os.makedirs(staging_dir)

    shutil.copytree(app_path, os.path.join(staging_dir, APP_NAME + ".app"))
    os.symlink("/Applications", os.path.join(staging_dir, "Applications"))

    dmg_path = os.path.join(dist_dir, APP_NAME + ".dmg")
    if os.path.exists(dmg_path):
        os.remove(dmg_path)

    print("Creating .dmg ...")
    subprocess.run(
        [
            "hdiutil", "create",
            "-volname", APP_NAME,
            "-srcfolder", staging_dir,
            # bzip2 compression -- noticeably smaller than the default
            # UDZO (zlib), at the cost of a somewhat slower build.
            "-ov", "-format", "UDBZ",
            dmg_path,
        ],
        check=True,
    )
    shutil.rmtree(staging_dir)
    return dmg_path


def find_iscc():
    """Locate Inno Setup's command-line compiler (ISCC.exe). Checks PATH
    first, then searches the usual install locations -- both the
    classic per-machine "Program Files\\Inno Setup 6" and the newer
    per-user default of "%LOCALAPPDATA%\\Programs\\Inno Setup 6" (recent
    installer versions default to the latter, and the version number in
    the folder name can vary, hence the glob)."""
    which_result = shutil.which("ISCC") or shutil.which("ISCC.exe")
    if which_result and os.path.isfile(which_result):
        return which_result

    search_roots = [
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
    ]
    for root in search_roots:
        if not root or not os.path.isdir(root):
            continue
        for match in glob.glob(os.path.join(root, "Inno Setup*", "ISCC.exe")):
            if os.path.isfile(match):
                return match
    return None


def build_windows_installer():
    """Compile installer.iss with Inno Setup into dist/<APP_NAME>Setup.exe.
    Inno Setup itself has to already be on this build machine -- it's a
    normal, free installer you run once by hand (auto-downloading and
    silently installing it turned out to be unreliable: antivirus tools
    routinely quarantine/corrupt a just-downloaded, unsigned installer
    before it can run, as happened here)."""
    iscc = find_iscc()
    if not iscc:
        print("[ERROR] Couldn't find Inno Setup's ISCC.exe on this machine.")
        print("If you haven't installed Inno Setup yet:")
        print("1. Download it from: https://jrsoftware.org/isdl.php")
        print('   (the "Inno Setup X.X.X" link near the top -- not the QuickStart Pack)')
        print("2. Run the downloaded installer, accepting all the defaults.")
        print("3. Run build_windows.bat again.")
        print()
        print("Already installed it and still seeing this? It's in a location")
        print("this script doesn't know to check -- find ISCC.exe yourself")
        print("(Windows Explorer search) and tell Claude its full path.")
        sys.exit(1)

    iss_path = os.path.join(BASE_DIR, "installer.iss")
    print("Compiling installer with Inno Setup ...")
    subprocess.run([iscc, iss_path], check=True, cwd=BASE_DIR)
    return os.path.join(BASE_DIR, "dist", APP_NAME + "Setup.exe")


def main():
    run_pyinstaller()

    if IS_MAC:
        dmg_path = build_dmg()
        print(f"\nDone. Installer is at: {dmg_path}")
        print("Double-click it, then drag the app onto the Applications shortcut inside.")
    else:
        setup_path = build_windows_installer()
        print(f"\nDone. Installer is at: {setup_path}")
        print("Double-click it and follow the setup wizard -- it installs the app,")
        print("adds a Start Menu shortcut, and registers a clean uninstaller.")


if __name__ == "__main__":
    main()
