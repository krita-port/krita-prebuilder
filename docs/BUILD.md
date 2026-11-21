# Building and verifying the SDK

The current recipes use Emscripten 7.0.0, Qt 6.10.2 and KDE Frameworks 6.24.0. Both host and Wasm Qt builds use Qt’s external archive API: its `gzip -n` command omits the timestamp from the embedded MIME database. Use Emscripten from your current environment (`EMSDK` or `emcc` on `PATH`). `EMSDK_ROOT` is an optional override. The Emscripten version is stored in `.emscripten-version`. Qt versions are read from `sources.json`; all Qt module versions must agree with its `qtbase` entry. The shared installation is read without activation changes. The host build requires Python 3.12 or newer, CMake, Ninja, a native C/C++ compiler, gperf and pkg-config; the workflow records its environment and installs its build utilities.

Install the Python requirements in an active environment before building:

```sh
python3 -m venv build/python-checks
. build/python-checks/bin/activate
python -m pip install --cache-dir cache/pip -r requirements.txt
```

Downloaded source archives are stored in `cache/downloads/`. Packaged SDK binary archives are written to `cache/prebuilt/`. Extracted sources, build directories, install staging, compiler caches, temporary files and logs stay under `build/`. Both directories are ignored by Git. Package filenames retain the `krita-wasm-sdk` prefix used by consumers. Binary packages include file inventories and component notices. Clone this repository for recipes and patches; `sources.json` records upstream versions, download URLs and input checksums.

## Build the SDK

Run the following commands from the repository root. The host build fetches checksum-locked sources and compiles native tools; the JSPI build compiles the Wasm dependency closure. The host prefix is `build/install/host` and the target prefix is `build/install-jspi/target`. Build commands and compiler output stream directly to the terminal and CI log, including host tools, target dependencies, supplements, catalogs and browser diagnostics.

```sh
scripts/build-jspi-host
scripts/build-jspi
scripts/check-target.py
scripts/warm-cache
scripts/build-dependency-probe
scripts/build-loop-probe
scripts/test-browser-probe.py build/jspi/sdk-smoke --expect SDK_SMOKE_OK
scripts/test-browser-probe.py build/jspi/sdk-deps --expect SDK_DEPS_OK
scripts/test-browser-probe.py build/jspi/event-loop-probe-sdk --expect LOOP_PROBE_OK
scripts/package.py --profile dependencies --check-repeat
```

The existing SDK runner uses a fresh headless Firefox profile and a loopback static server with isolation headers. It does not use a personal browser profile. Wasm JSPI requires `WebAssembly.Suspending` and `WebAssembly.promising` in the tested browser.

## Internationalization supplement

After building the base SDK:

```sh
scripts/i18n.py build
scripts/i18n.py catalogs --catalogs build/i18n/catalogs-a
scripts/i18n.py catalogs --catalogs build/i18n/catalogs-b
scripts/i18n.py compare --catalogs build/i18n/catalogs-a --compare build/i18n/catalogs-b
scripts/build-i18n-probe
(cd build/jspi/sdk-i18n && node gettext.js && node gettext.js C && node gettext.js POSIX)
scripts/test-browser-probe.py build/jspi/sdk-i18n --expect SDK_I18N_OK
scripts/i18n.py package --catalogs build/i18n/catalogs-a --tag local --check-repeat
```

The supplement contains GNU gettext host tools, libintl, KI18n/Transcript and Qt/KDE catalogs. Catalog comparison verifies the two generated catalog trees; it is not an independent full-SDK rebuild.

## QtCore correction supplement

Fresh full builds include the Qt native-event dispatch and WebAssembly collation patches. Queued browser events snapshot their transient target and composed path before dispatch returns; the event-loop probe exercises this through a real Shadow DOM pointer event as well as synchronous dialogs, threads and exceptions. Consumers that need a separate archive bound to an immutable base can generate the existing supplement with:

```sh
python3 scripts/qtcore-correction.py --base-sdk PATH_TO_VERIFIED_BASE --output cache/prebuilt/qtcore-local
```

This copies the base QtCore archive before replacing its event-dispatch and collation objects, and includes the matching private collation header. It verifies the exact base inventory and includes Qt source provenance and licenses. Choose a new output directory rather than overwriting an existing package.

## Browser collation

Qt on WebAssembly uses the current browser’s `Intl.Collator` for locale-aware, case-insensitive, numeric and punctuation-aware sorting. It does not bundle ICU data. The C locale retains Qt’s string comparison behavior. JavaScript collators remain local to the calling worker; Qt collators and sort keys can be copied between threads. Because the browser does not expose precomputed sort keys, `QCollatorSortKey` retains a snapshot of the comparison rules and text and compares through the same backend. It preserves comparison semantics without the precomputation speedup of ICU keys. Locale data and some ordering details follow the browser version.

The dependency probe checks direct comparisons, sort-key lifetimes and copies, C-locale behavior, cross-thread use and a real `QFileSystemModel` directory listing. Unsupported collation warnings fail the probe. Existing CI runs this probe against both the build prefix and the relocated packaged SDK.

## Verification boundary

The `build` job runs C/C++ formatting and Python checks first, builds and uploads the SDK, internationalization supplement and QtCore correction, and prepares a separate runtime-probe artifact before archive, catalog and packaging checks. The relocation probe is compiled with the original target prefix temporarily hidden and the extracted SDK’s compiler cache frozen. Repeat packaging uses separate `build/package-repeat/` outputs and compares them with the uploaded archives.

The `test` job downloads the already-built runtime probes and runs them in Firefox and Node without rebuilding the SDK or installing Emscripten. If only this job fails, use GitHub Actions’ **Re-run failed jobs** to retry it with the existing build artifacts. Probe artifacts are for CI diagnostics and are not published in the Nightly release. The `publish` job waits for both `build` and `test` to pass.

After all checks pass, pushes to `main` and manual runs on `main` create or update the `latest` tag and the `Nightly build` prerelease. It contains the SDK, internationalization and QtCore correction archives, with no separate source archives or checksum assets. Downloads can be verified using the asset digests returned by the GitHub API. PRs and manual runs on other branches do not publish.

These checks do not establish byte-identical independent compilation. The host compiler, complete build image and all generated compiler caches are not yet fully pinned or reproducible.

## C and C++ formatting

Use clang-format 22, which is preinstalled on the Ubuntu 26.04 runner. The repository configuration preserves embedded JavaScript in Emscripten macros and keeps string literals intact.

```sh
scripts/format.sh
scripts/format.sh --check
```

The default applies formatting; `--check` reports formatting errors without modifying files. CI uses `scripts/format.sh --check` before Python setup and dependency builds.

## Python checks

With the Python environment above active:

```sh
ruff format --check scripts
ruff check scripts
mypy
```

CI uses Python 3.14 from `actions/setup-python` and runs these checks directly without creating a virtual environment. Use `build/python-checks/bin/ruff format scripts` to apply formatting locally.
