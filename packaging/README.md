# Portable Apple Silicon build

The recipient needs neither Python nor pip. The ZIP contains a native arm64 app,
CPython3.12.13, its native dependencies and reviewed Python worker modules. Models
are fetched automatically on first launch from immutable public HuggingFace URLs.
No recordings, transcripts, account credentials or meeting text are sent to a
server. Asset setup makes GET requests only for pinned model weights.

macOS14.4 or later is required. Every bundled Mach-O arm64 slice is audited for
its deployment target and external library dependencies. The installed MLX26.0
wheel is replaced only in the portable copy with the official same-version
macOS14.0 core/Metal wheels, pinned in runtime-wheels.lock.json.

The automatic profile is Qwen3.5-4B4bit below16GB unified memory, Qwen3.5-9B4bit
at16GB or above. Both include Parakeet0.6B4bit. Physical8GB hardware, macOS14.4
hardware and the entire public model download/inference path have not yet been
validated. There is no measured speed or memory guarantee for8GB machines.
The normal public download is approximately6.6GB (9B) or3.7GB (4B), plus runtime.
Initial setup needs about13GB free disk space for downloads and staging.

An existing exact deployed9B model set is recognized using
models.installed.lock.json and reused through hard links, preserving the host's
weights and settings. A clean Mac uses the distinct public pinned weights in
models.lock.json. Their semantic equivalence to the deployed weights is not
claimed. Tokenizer/processor adaptations are bundled explicitly. Asset data
lives under ~/Library/Application Support/MeetingOverlay/assets/<manifest>-<profile>;
workers stage under portable/<worker-id>-<profile>/src. Neither canonical src nor
canonical runtime/models is overwritten. Interrupted downloads resume with Range,
are retried and must pass exact length and SHA-256 checks before becoming usable.

Build from this project on Apple Silicon with a reviewed worker staging folder:

```sh
runtime/.venv/bin/python -B scripts/package_macos.py \
  --worker-source /absolute/path/to/reviewed-workers \
  --output .build.noindex/meeting-overlay-arm64-candidate.zip
```

The default worker source is the installed runtime, because project summary_writer
contains unaccepted experiments. All top-level *.py modules in the explicitly
selected staging directory are included. Swift input is snapshotted before compile.
No .app remains inside the project; temporary builds are removed after success or
failure. The resulting ZIP has an adjacent JSON receipt with SHA-256, native
libraries and isolated fresh-HOME dependency/worker smoke results. Smoke tests do
not load models, capture or play audio, or inspect meetings.

For an offline9B candidate, add --models pointing at the exact deployed
runtime/models directory. This uses models.installed.lock.json. The light4B profile
still requires its public download unless its weights are supplied separately.
A full offline ZIP exceeds GitHub's2GB per-release-asset limit.

Ad-hoc signing is the default candidate mode. Normal Gatekeeper distribution
requires a Developer ID Application certificate and notarization. Apple Development
is a different identity and does not establish that distribution trust. The
available read-only identity check found only Apple Development. Once an existing
Developer ID identity and notarytool profile are provided, the same script accepts
--sign <identity> --notary-profile <profile>. It signs nested Mach-O binaries,
enables the hardened runtime, submits for notarization, staples and validates the
bundle, and recreates the ZIP. No private keys or passwords are written into it.

Worker integration uses AppPaths.root/python/workerScript/workerEnvironment and
RuntimeBootstrap.prepare(progress:completion:). Capture must remain disabled until
bootstrap completes. RuntimeBootstrap.cancel() stops an in-progress download on
quit. Runtime setup emits a clear4B light-profile label before downloads start.

## Manual public-model validation in GitHub Actions

`.github/workflows/macos-models.yml` calls `packaging/ci_models.py`. It has only a
manual dispatch trigger and read-only repository permission. The default is the
4B profile on `macos-15`; a native arm64 check rejects Intel/emulation. Current
GitHub documentation identifies this standard runner as M1 with7GB RAM, so its
result cannot establish physical8GB app acceptance. The9B profile fails below
16GiB and requires an explicitly configured larger or self-hosted ARM runner.
Metal availability is checked before model downloads; a hosted VM without Metal
must fail with evidence instead of reporting inference success. Upstream MLX uses
self-hosted macOS runners for its Metal tests.

The workflow builds a fresh Python3.12.13 venv with the pinned macOS14 MLX wheels
and runtime/requirements.lock, downloads public pinned assets into an empty root,
and processes only tests/meeting-sample.wav (synthetic17.281s, SHA-256 pinned in
helper). It checks timestamped ASR, a nonempty validated summary, neutral title,
unchanged asset hashes/mtime on the second startup and a second inference with
Python network connects/DNS denied. Workers run sequentially; this does not prove
simultaneous ASR+summary memory behavior. JSON reports record actual OS/RAM and
exact worker hashes. WAV, TXT, models, caches and screenshots are never uploaded.

Each selected profile has a60-minute timeout. At the documented standard macOS
rate of$0.062/min, the timeout cap is$3.72/profile before included usage, up to
$7.44 for both. Larger runners have different rates. Runtime is not measured;
10–25minutes/profile is an estimate dependent on network and runner capability.

Sources checked4November2026:
https://docs.github.com/en/actions/reference/runners/github-hosted-runners
https://docs.github.com/en/billing/reference/actions-runner-pricing
https://github.com/ml-explore/mlx/blob/main/.github/workflows/build_and_test.yml
