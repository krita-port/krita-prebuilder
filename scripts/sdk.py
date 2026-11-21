#!/usr/bin/env python3
"""Build pinned SDK components without modifying the system or shared emsdk."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NotRequired, TypedDict
from urllib.request import urlopen

type CommandArg = str | Path | int


class SourcePackage(TypedDict):
    version: str
    url: str
    sha256: str
    directory: str
    archive: NotRequired[str]


ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / "build"
CACHE_DIR = ROOT / "cache"
EMSCRIPTEN_VERSION = (ROOT / ".emscripten-version").read_text().strip()
PACKAGES: dict[str, SourcePackage] = json.loads((ROOT / "sources.json").read_text())[
    "packages"
]
QT_VERSION = PACKAGES["qtbase"]["version"]
if any(
    entry["version"] != QT_VERSION
    for name, entry in PACKAGES.items()
    if name.startswith("qt")
):
    raise ValueError("Qt module source versions do not match qtbase")
BUILD_ROOT = BUILD_DIR / "jspi"
CXX_EXCEPTION_FLAG = "-fwasm-exceptions"
C_TARGET_FLAGS = "-pthread -fwasm-exceptions"
HOST = BUILD_DIR / "install/host"
TARGET = BUILD_DIR / "install-jspi/target"
COMMON = BUILD_DIR / "install/common"


def emsdk_root() -> Path:
    configured = os.environ.get("EMSDK_ROOT") or os.environ.get("EMSDK")
    if configured:
        root = Path(configured).resolve()
    else:
        compiler = shutil.which("emcc")
        if not compiler:
            raise ValueError(
                "Emscripten not found. Load your configured SDK environment so emcc is on PATH."
            )
        root = Path(compiler).resolve().parents[2]
    if not (root / ".emscripten").is_file():
        raise ValueError(f"Missing emsdk configuration: {root / '.emscripten'}")
    return root


def run(
    args: Sequence[CommandArg],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    print("+", " ".join(map(str, args)), flush=True)
    if env is None:
        env = {
            **os.environ,
            "CCACHE_DISABLE": "1",
            "GIT_CEILING_DIRECTORIES": str(BUILD_DIR / "sources"),
        }
    subprocess.run([str(arg) for arg in args], cwd=cwd, env=env, check=True)


def archive_path(name: str) -> Path:
    return (
        CACHE_DIR
        / "downloads"
        / PACKAGES[name].get("archive", PACKAGES[name]["url"].rsplit("/", 1)[1])
    )


def verify(path: Path, digest: str) -> None:
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError(f"SHA-256 mismatch: {path}")


def fetch(name: str) -> None:
    entry = PACKAGES[name]
    path = archive_path(name)
    if path.exists():
        verify(path, entry["sha256"])
        print(f"Already downloaded and verified: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            with urlopen(entry["url"], timeout=120) as stream:
                while chunk := stream.read(1024 * 1024):
                    output.write(chunk)
            output.close()
            verify(temporary, entry["sha256"])
            temporary.rename(path)
        finally:
            temporary.unlink(missing_ok=True)


def prepare(name: str) -> Path:
    entry = PACKAGES[name]
    archive = archive_path(name)
    verify(archive, entry["sha256"])
    series = ROOT / "patches" / name / "series"
    patches = []
    digest = hashlib.sha256(archive.name.encode() + entry["sha256"].encode())
    if series.exists():
        digest.update(series.read_bytes())
        for line in series.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if Path(line).name != line or not line.endswith(".patch"):
                raise ValueError(f"Invalid patch name: {line}")
            patch = series.parent / line
            if not patch.is_file() or patch.is_symlink() or patch in patches:
                raise ValueError(f"Invalid or duplicate patch: {patch}")
            digest.update(patch.name.encode() + patch.read_bytes())
            patches.append(patch)
        if set(series.parent.glob("*.patch")) != set(patches):
            raise ValueError(f"Unlisted patch in {series.parent}")
    identity = {
        "package": name,
        "source_sha256": entry["sha256"],
        "patch_digest": digest.hexdigest(),
    }
    parent = BUILD_DIR / "sources"
    parent.mkdir(parents=True, exist_ok=True)
    dest = parent / f"{entry['directory']}-{digest.hexdigest()[:12]}"
    if dest.exists():
        manifest = dest / ".sdk-source.json"
        if not manifest.is_file() or json.loads(manifest.read_text()) != identity:
            raise ValueError(f"Existing source lacks matching provenance: {dest}")
        # Never apply patches again or overwrite an existing tree.
        return dest
    with tempfile.TemporaryDirectory(prefix=".source-", dir=parent) as temporary:
        with tarfile.open(archive) as stream:
            stream.extractall(temporary, filter="data")
        tree = Path(temporary) / entry["directory"]
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": temporary}
        for patch in patches:
            run(
                ["git", "apply", "--check", "--whitespace=error-all", patch],
                cwd=tree,
                env=env,
            )
            run(["git", "apply", "--whitespace=error-all", patch], cwd=tree, env=env)
        (tree / ".sdk-source.json").write_text(json.dumps(identity, indent=2) + "\n")
        tree.rename(dest)
    return dest


def cmake_component(
    component: str,
    source: Path,
    prefix: Path,
    options: Sequence[CommandArg],
    wasm: bool,
    jobs: int,
    qt: bool = True,
) -> None:
    # A new patch set gets a new configure directory; failed probes from the
    # previous source must not survive as cached feature results.
    build = BUILD_ROOT / f"{component}-{source.name.rsplit('-', 1)[-1]}"
    command = [ROOT / "scripts/with-emsdk"] if wasm else []
    compiler = (
        TARGET / "bin/qt-cmake"
        if wasm and qt
        else (
            HOST / "bin/qt-cmake"
            if component in ["qttools-host", "qtshadertools-host", "qtdeclarative-host"]
            else "cmake"
        )
    )
    cross_options = []
    if wasm:
        cross_options = [
            f"-DCMAKE_C_FLAGS={C_TARGET_FLAGS}",
            f"-DCMAKE_CXX_FLAGS=-pthread {CXX_EXCEPTION_FLAG}",
            f"-DCMAKE_FIND_ROOT_PATH={TARGET}",
        ]
        if not qt:
            sdk = emsdk_root()
            cross_options += [
                f"-DCMAKE_TOOLCHAIN_FILE={sdk}/upstream/emscripten/cmake/Modules/Platform/Emscripten.cmake",
                f"-DCMAKE_FIND_ROOT_PATH={TARGET}",
            ]
    run(
        [
            *command,
            compiler,
            "-S",
            source,
            "-B",
            build,
            "-G",
            "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_INSTALL_PREFIX={prefix}",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DBUILD_TESTING=OFF",
            "-DBUILD_QCH=OFF",
            "-DBUILD_DOCS=OFF",
            "-DBUILD_TRANSLATIONS=OFF",
            "-DKDE_INSTALL_LIBDIR=lib",
            "-DKF_SKIP_PO_PROCESSING=ON",
            f"-DCMAKE_PREFIX_PATH={TARGET if wasm else HOST};{COMMON}",
            f"-DECM_DIR={COMMON}/share/ECM/cmake",
            *(
                [
                    f"-DQT_HOST_PATH={HOST}",
                    f"-DQT_HOST_PATH_CMAKE_DIR={HOST}/lib/cmake",
                    "-DKF_IGNORE_PLATFORM_CHECK=ON",
                ]
                if wasm
                else []
            ),
            *cross_options,
            *options,
        ]
    )
    run(
        [
            *command,
            "cmake",
            "--build",
            build,
            "--parallel",
            jobs,
            *(["--target", "gsl", "gslcblas"] if component == "gsl" else []),
        ]
    )
    run([*command, "cmake", "--install", build])


def meson_component(
    component: str, source: Path, options: Sequence[CommandArg], jobs: int
) -> None:
    meson = prepare("meson") / "meson.py"
    directory = BUILD_ROOT / f"{component}-{source.name.rsplit('-', 1)[-1]}"
    directory.parent.mkdir(parents=True, exist_ok=True)
    cross = directory.parent / "emscripten-meson.ini"
    sdk = emsdk_root()
    cross.write_text(
        "\n".join(
            [
                "[binaries]",
                f"c = {str(sdk / 'upstream/emscripten/emcc')!r}",
                f"cpp = {str(sdk / 'upstream/emscripten/em++')!r}",
                f"ar = {str(sdk / 'upstream/emscripten/emar')!r}",
                f"ranlib = {str(sdk / 'upstream/emscripten/emranlib')!r}",
                "strip = 'true'",
                "pkg-config = 'pkg-config'",
                "exe_wrapper = ['node']",
                "[host_machine]",
                "system = 'emscripten'",
                "cpu_family = 'wasm32'",
                "cpu = 'wasm32'",
                "endian = 'little'",
                "[properties]",
                "needs_exe_wrapper = true",
                f"pkg_config_libdir = [{str(TARGET / 'lib/pkgconfig')!r}, {str(TARGET / 'share/pkgconfig')!r}]",
                "[built-in options]",
                f"c_args = {C_TARGET_FLAGS.split()!r}",
                f"cpp_args = {['-pthread', CXX_EXCEPTION_FLAG]!r}",
                f"c_link_args = {C_TARGET_FLAGS.split()!r}",
                f"cpp_link_args = {['-pthread', CXX_EXCEPTION_FLAG]!r}",
                "c_thread_count = 0",
                "cpp_thread_count = 0",
                f"pkg_config_path = [{str(TARGET / 'lib/pkgconfig')!r}, {str(TARGET / 'share/pkgconfig')!r}]",
                "",
            ]
        )
    )
    command: list[CommandArg] = [ROOT / "scripts/with-emsdk", sys.executable, meson]
    # Fontconfig embeds these paths as browser filesystem paths. DESTDIR keeps
    # its installation in the SDK while its runtime root is '/'.
    setup: list[CommandArg] = [
        "setup",
        directory,
        source,
        "--cross-file",
        cross,
        "--prefix",
        "/" if component == "fontconfig" else TARGET,
        "--libdir",
        "lib",
        "--default-library",
        "static",
        "--buildtype",
        "release",
        "--wrap-mode",
        "nodownload",
        *options,
    ]
    if (directory / "meson-private/coredata.dat").exists():
        setup.append("--reconfigure")
    run([*command, *setup])
    run([*command, "compile", "-C", directory, "-j", jobs])
    install_env = {**os.environ, "CCACHE_DISABLE": "1"}
    if component == "fontconfig":
        install_env["DESTDIR"] = str(TARGET)
    run([*command, "install", "-C", directory, "--no-rebuild"], env=install_env)
    # Keep static dependency discovery correct both here and after relocation.
    for pc in (TARGET / "lib/pkgconfig").glob("*.pc"):
        text = pc.read_text()
        text = (
            "\n".join(
                "prefix=${pcfiledir}/../.." if line.startswith("prefix=") else line
                for line in text.splitlines()
            )
            + "\n"
        )
        pc.write_text(text)


def build(component: str, jobs: int) -> None:
    options: list[str] | None
    name = (
        "qtbase"
        if component.startswith("qtbase-")
        else (
            "freetype"
            if component == "freetype-bootstrap"
            else component.removesuffix("-host")
        )
    )
    source = prepare(name)
    if name == "qtbase":
        wasm = component.endswith("-wasm")
        directory = BUILD_ROOT / component
        directory.mkdir(parents=True, exist_ok=True)
        command = [ROOT / "scripts/with-emsdk"] if wasm else []
        qt_options: list[CommandArg] = [
            "-static",
            "-release",
            "-nomake",
            "examples",
            "-nomake",
            "tests",
            "-qt-pcre",
            "-qt-doubleconversion",
            "-no-feature-system-doubleconversion",
            "-no-feature-dbus",
            "-no-feature-zstd",
        ]
        if wasm:
            qt_options += ["-feature-wasm-jspi", "-feature-wasm-exceptions"]
            qt_options += [
                "-platform",
                "wasm-emscripten",
                "-qt-host-path",
                HOST,
                "-prefix",
                TARGET,
                "-feature-thread",
                "-system-freetype",
                "-system-harfbuzz",
                "-system-libpng",
                "-system-zlib",
                "-feature-system-freetype",
                "-feature-system-harfbuzz",
                "-feature-system-png",
                "-feature-system-zlib",
                "-no-feature-fontconfig",
                "-feature-printsupport",
                "-no-feature-icu",
                "--",
                f"-DCMAKE_PREFIX_PATH={TARGET}",
                f"-DCMAKE_FIND_ROOT_PATH={TARGET}",
            ]
        else:
            qt_options += [
                "-prefix",
                HOST,
                "-qt-zlib",
                "-gui",
                "-feature-gui",
                "-widgets",
                "-feature-widgets",
                "-no-opengl",
                "-qt-freetype",
                "-qt-harfbuzz",
                "-qt-libpng",
                "-no-feature-system-freetype",
                "-no-feature-system-harfbuzz",
                "-no-feature-system-png",
                "-qt-libjpeg",
                "-no-feature-system-jpeg",
                "-no-feature-libudev",
                "-no-feature-xkbcommon",
                "-no-feature-linuxfb",
                "-no-feature-evdev",
                "-no-feature-cups",
                "-no-feature-fontconfig",
                "-no-feature-xcb",
                "-no-feature-wayland",
                "-no-feature-vulkan",
                "-no-feature-network",
                "-no-feature-sql",
                "-no-feature-testlib",
                "-no-feature-glib",
                "-no-feature-icu",
            ]
        # Qt's external gzip path uses -n, omitting the embedded MIME timestamp.
        if not wasm:
            qt_options.append("--")
        qt_options.append("-DQT_AVOID_CMAKE_ARCHIVING_API=ON")
        run([*command, source / "configure", *qt_options], cwd=directory)
        run([*command, "cmake", "--build", directory, "--parallel", jobs])
        run([*command, "cmake", "--install", directory])
        if wasm:
            fonts = TARGET / "share/krita-wasm-sdk/fonts"
            fonts.mkdir(parents=True, exist_ok=True)
            for filename in ["DejaVuSans.ttf", "DEJAVU-LICENSE"]:
                shutil.copy2(source / "src/3rdparty/wasm" / filename, fonts / filename)
    elif component == "ecm":
        cmake_component(component, source, COMMON, [], False, jobs)
    elif component == "qttools-host":
        cmake_component(
            component,
            source,
            HOST,
            [
                "-DQT_BUILD_EXAMPLES=OFF",
                "-DQT_BUILD_TESTS=OFF",
                "-DFEATURE_assistant=OFF",
                "-DFEATURE_designer=OFF",
                "-DFEATURE_clang=OFF",
                "-DFEATURE_qdoc=OFF",
                "-DFEATURE_qtplugininfo=OFF",
                "-DFEATURE_qtattributionsscanner=OFF",
            ],
            False,
            jobs,
        )
    elif name in ["qtsvg", "qt5compat", "qtshadertools", "qtdeclarative"]:
        wasm = not component.endswith("-host")
        options = ["-DQT_BUILD_EXAMPLES=OFF", "-DQT_BUILD_TESTS=OFF"]
        if component == "qtdeclarative-host":
            # QML generators need native Qml, not native Quick. Hide qsb only
            # from this configure; the target configure uses the real host qsb.
            options += [
                "-DCMAKE_DISABLE_FIND_PACKAGE_Qt6ShaderToolsTools=ON",
                "-DCMAKE_DISABLE_FIND_PACKAGE_Qt6ShaderTools=ON",
            ]
        cmake_component(
            component, source, TARGET if wasm else HOST, options, wasm, jobs
        )
    elif component.startswith("kcoreaddons"):
        cmake_component(
            component,
            source,
            HOST if component.endswith("-host") else TARGET,
            [
                "-DKCOREADDONS_USE_QML=OFF",
                "-DBUILD_PYTHON_BINDINGS=OFF",
                "-DUSE_DBUS=OFF",
                "-DENABLE_INOTIFY=OFF",
                "-DCMAKE_DISABLE_FIND_PACKAGE_UDev=ON",
            ],
            not component.endswith("-host"),
            jobs,
        )
    elif component.startswith("kconfig"):
        cmake_component(
            component,
            source,
            HOST if component.endswith("-host") else TARGET,
            [
                "-DKCONFIG_USE_QML=OFF",
                "-DUSE_DBUS=OFF",
                f"-DKCONFIG_USE_GUI={'OFF' if component.endswith('-host') else 'ON'}",
                *(
                    []
                    if component.endswith("-host")
                    else [f"-DKF6_HOST_TOOLING={HOST}/lib/cmake"]
                ),
            ],
            not component.endswith("-host"),
            jobs,
        )
    elif component in [
        "kwidgetsaddons",
        "kcompletion",
        "kguiaddons",
        "ki18n",
        "kitemviews",
        "kcolorscheme",
        "kcodecs",
    ]:
        cmake_component(
            component,
            source,
            TARGET,
            [
                "-DBUILD_PYTHON_BINDINGS=OFF",
                "-DBUILD_DESIGNERPLUGIN=OFF",
                "-DBUILD_WITH_QML=OFF",
                "-DUSE_DBUS=OFF",
                "-DWITH_X11=OFF",
                "-DWITH_WAYLAND=OFF",
                "-DBUILD_GEO_SCHEME_HANDLER=OFF",
                f"-DKF6_HOST_TOOLING={HOST}/lib/cmake",
            ],
            True,
            jobs,
        )
    elif component == "boost":
        run(
            [
                source / "bootstrap.sh",
                f"--prefix={TARGET}",
                "--with-libraries=system",
                "--without-icu",
            ],
            cwd=source,
        )
        run(
            [
                source / "b2",
                "--with-system",
                f"--prefix={TARGET}",
                f"--build-dir={BUILD_ROOT}/boost",
                f"-j{jobs}",
                "-d0",
                "install",
            ],
            cwd=source,
        )
    elif component in ["fontconfig", "fribidi", "harfbuzz"]:
        options = {
            "fontconfig": [
                "-Ddoc=disabled",
                "-Dnls=disabled",
                "-Dtests=disabled",
                "-Dtools=disabled",
                "-Dcache-build=disabled",
                "-Dxml-backend=expat",
                "-Ddefault-fonts-dirs=/fonts",
                "-Dadditional-fonts-dirs=[]",
                "-Dcache-dir=/tmp/fontconfig-cache",
                "-Dtemplate-dir=/usr/share/fontconfig/conf.avail",
                "-Dbaseconfig-dir=/etc/fonts",
                "-Dconfig-dir=/etc/fonts/conf.d",
                "-Dxml-dir=/usr/share/xml/fontconfig",
            ],
            "fribidi": ["-Ddocs=false", "-Dbin=false", "-Dtests=false"],
            "harfbuzz": [
                "-Dglib=disabled",
                "-Dgobject=disabled",
                "-Dcairo=disabled",
                "-Dicu=disabled",
                "-Dgraphite2=disabled",
                "-Dtests=disabled",
                "-Ddocs=disabled",
                "-Dutilities=disabled",
                "-Dintrospection=disabled",
                "-Dfreetype=enabled",
            ],
        }[component]
        meson_component(component, source, options, jobs)
    else:
        options = {
            "zlib": [
                "-DZLIB_BUILD_SHARED=OFF",
                "-DZLIB_BUILD_STATIC=ON",
                "-DZLIB_BUILD_TESTING=OFF",
            ],
            "libpng": [
                "-DPNG_SHARED=OFF",
                "-DPNG_STATIC=ON",
                "-DPNG_TESTS=OFF",
                "-DPNG_TOOLS=OFF",
            ],
            "expat": [
                "-DEXPAT_SHARED_LIBS=OFF",
                "-DEXPAT_BUILD_TESTS=OFF",
                "-DEXPAT_BUILD_EXAMPLES=OFF",
                "-DEXPAT_BUILD_TOOLS=OFF",
            ],
            "lcms2": [
                "-DLCMS2_BUILD_SHARED=OFF",
                "-DLCMS2_BUILD_STATIC=ON",
                "-DLCMS2_BUILD_TOOLS=OFF",
                "-DLCMS2_BUILD_TESTS=OFF",
            ],
            "eigen": [
                "-DEIGEN_BUILD_BLAS=OFF",
                "-DEIGEN_BUILD_LAPACK=OFF",
                "-DEIGEN_BUILD_TESTING=OFF",
            ],
            "xsimd": ["-DBUILD_TESTS=OFF"],
            "immer": [
                "-Dimmer_BUILD_TESTS=OFF",
                "-Dimmer_BUILD_EXAMPLES=OFF",
                "-Dimmer_BUILD_DOCS=OFF",
                "-Dimmer_BUILD_EXTRAS=OFF",
            ],
            "zug": [
                "-Dzug_BUILD_TESTS=OFF",
                "-Dzug_BUILD_EXAMPLES=OFF",
                "-Dzug_BUILD_DOCS=OFF",
            ],
            "lager": [
                "-Dlager_BUILD_TESTS=OFF",
                "-Dlager_BUILD_FAILURE_TESTS=OFF",
                "-Dlager_BUILD_EXAMPLES=OFF",
                "-Dlager_BUILD_DEBUGGER_EXAMPLES=OFF",
                "-Dlager_BUILD_DOCS=OFF",
                "-Dlager_EMBED_RESOURCES_PATH=OFF",
            ],
            "quazip": [
                "-DQUAZIP_QT_MAJOR_VERSION=6",
                "-DQUAZIP_BZIP2=OFF",
                "-DQUAZIP_ENABLE_TESTS=OFF",
                "-DQUAZIP_INSTALL=ON",
            ],
            "exiv2": [
                "-DEXIV2_BUILD_SAMPLES=OFF",
                "-DEXIV2_ENABLE_NLS=OFF",
                "-DEXIV2_BUILD_EXIV2_COMMAND=OFF",
                "-DEXIV2_ENABLE_INIH=OFF",
                "-DEXIV2_BUILD_UNIT_TESTS=OFF",
                "-DEXIV2_BUILD_FUZZ_TESTS=OFF",
                "-DEXIV2_ENABLE_CURL=OFF",
                "-DEXIV2_ENABLE_WEBREADY=OFF",
                "-DEXIV2_ENABLE_BROTLI=OFF",
            ],
            "freetype-bootstrap": [
                "-DFT_DISABLE_BZIP2=ON",
                "-DFT_DISABLE_BROTLI=ON",
                "-DFT_REQUIRE_PNG=ON",
                "-DFT_DISABLE_HARFBUZZ=ON",
            ],
            "freetype": [
                "-DFT_DISABLE_BZIP2=ON",
                "-DFT_DISABLE_BROTLI=ON",
                "-DFT_REQUIRE_PNG=ON",
                "-DFT_DISABLE_HARFBUZZ=OFF",
                "-DFT_REQUIRE_HARFBUZZ=ON",
                "-DFT_DYNAMIC_HARFBUZZ=OFF",
            ],
            "libunibreak": [],
            "gsl": ["-DNO_AMPL_BINDINGS=ON"],
        }.get(component)
        if options is None:
            raise ValueError(f"Unknown component: {component}")
        cmake_component(
            component, source, TARGET, options, True, jobs, qt=component == "quazip"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["fetch", "prepare", "build"])
    parser.add_argument("component")
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()
    try:
        if args.command == "fetch":
            fetch(args.component)
        elif args.command == "prepare":
            print(prepare(args.component))
        else:
            build(args.component, args.jobs)
            for pc in (TARGET / "lib/pkgconfig").glob("*.pc"):
                lines = pc.read_text().splitlines()
                pc.write_text(
                    "\n".join(
                        "prefix=${pcfiledir}/../.."
                        if line.startswith("prefix=")
                        else re.sub(
                            r"\s+-sPTHREAD_POOL_SIZE=\S+",
                            "",
                            line.replace(str(TARGET), "${prefix}"),
                        )
                        for line in lines
                    )
                    + "\n"
                )
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
