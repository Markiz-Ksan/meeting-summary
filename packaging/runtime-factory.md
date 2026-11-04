# Clean-source Apple Silicon runtime

`scripts/prepare_macos_runtime.py` prepares the runtime layout consumed by
`scripts/package_macos.py`. It reads repository locks and official download
sources. It does not read an installed MeetingOverlay runtime, models, recordings
or settings, and does not create or install an application.

The launcher needs Python3.12 or newer and the Apple command-line tools on native
arm64 macOS14.4 or newer. Output must be a new path. Existing paths are refused;
a failed build retains its incomplete output and failure receipt for diagnosis.
All downloads are restricted to their declared HTTPS sources and checked for exact
size and SHA-256. Certificate and hostname verification remain enabled. A launcher
with an empty CA store can load the standard macOS `/private/etc/ssl/cert.pem`;
no system trust settings are changed.

## Prepare from the checked-in lock

From the clean checkout on an official ARM macOS runner:

```sh
python3 scripts/prepare_macos_runtime.py \
  --plan packaging/runtime-factory.lock.json \
  --output "$RUNNER_TEMP/meeting-runtime-source"
```

The checked-in plan freezes every selected filename, source URL, size and SHA-256. The
57 package versions come from `runtime/requirements.lock`. MLX and MLX-Metal use
the exact macOS14 wheel entries in `packaging/runtime-wheels.lock.json`; remaining
wheels are selected deterministically for CPython3.12, arm64/universal2 or pure
Python, and a deployment target no newer than14.4. No source distributions are
built. Installation uses only the verified local wheels, `--no-index`, exact
version/hash requirements and an isolated venv; `pip check` must pass.

Normal builds use this lock without resolving PyPI metadata; it is also the
factory's default when `--plan` is omitted. Its SHA-256 is
`183af55f9766ed10c3ec9fa49f1bd9843dd95dad5c7e38fc36e748333eb78ebf`.
The same plan passed the clean-temp factory and import checks.

For explicit dependency maintenance only, `--plan-output NEW.json` resolves
current PyPI metadata for the version pins without downloading packages. Review
the resulting URLs, sizes, hashes and licenses before replacing the checked-in
lock. A future added or yanked wheel cannot change normal builds. App archives
are not claimed byte-identical across SDKs, signing runs or timestamps.

The result contains:

- `python/`: standalone CPython3.12.13 and its upstream installation files.
- `.venv/lib/python3.12/site-packages/`: the locked runtime dependencies.
- `runtime-factory-receipt.json`: source plan, native audit, import smoke and
  retained license/notice paths.

The factory reuses the packager's native relocation and audit functions. Every
Mach-O must contain arm64, use bundled/system libraries and declare macOS14.4 or
older. External symlinks are refused. Ad-hoc signatures are regenerated only for
the new runtime binaries after relocation. The isolated import smoke denies
network access and imports MLX, MLX-LM, Parakeet, NumPy, SciPy, SoundFile,
tokenizers and sentencepiece without loading models or opening audio devices.

## Hand the prepared runtime to the existing packager

For a separately approved frozen build18 checkout, the next command is:

```sh
python3 scripts/package_macos.py \
  --runtime-source "$RUNNER_TEMP/meeting-runtime-source" \
  --worker-source "$PWD/src" \
  --version 0.7.0 --build 18 \
  --output "$RUNNER_TEMP/MeetingSummary-0.7.0-beta-macos-arm64.zip"
```

Use the explicit reviewed worker source. The packager's default installed-worker
path is intentionally not suitable for a clean checkout.

The manual-only `.github/workflows/macos-package.yml` uses the same factory and
explicit paths on `macos-15`, rejects Intel and systems older than14.4, and checks
the saved archive, five worker hashes, signatures, native dependencies and imports.
Its artifact contains the ZIP, `SHA256SUMS` and sanitized software receipts.
It does not download models, run inference, capture or play audio. Receipts mark
`model_validation=not_run` and `semantic_acceptance=not_evaluated`. The workflow
does not upload a release or install an app. Its ad-hoc signature is not Developer
ID signing or notarization; hosted build execution remains a separate gate.

## Fixed upstream interpreter and notices

The official [20260718 Python-build-standalone release](https://github.com/astral-sh/python-build-standalone/releases/tag/20260718)
provides `cpython-3.12.13+20260718-aarch64-apple-darwin-install_only_stripped.tar.gz`:
25,000,146 bytes; SHA-256
`9a1e9e06175c10efd8378b904b07fa21bd791ab3345d7cdffeb4a76c9ff55903`.

The factory keeps archive and wheel license files. Install-only archives omit
full-distribution metadata, so it also carries the official consolidated
[component notices from the same release](https://github.com/astral-sh/python-build-standalone/blob/20260718/python-licenses.rst)
into `python/share/licenses/python-build-standalone/python-licenses.rst`:
105,875 bytes; SHA-256
`e43fb936c6655d7996dba480d7ebdea492d6040ec388eb8ed9d1000f72de8cab`.
License text is unchanged. Native library load commands and ad-hoc signatures are
updated as recorded in the receipt. The tested plan downloads175,572,946 bytes
including the interpreter, notices and57 wheels.
