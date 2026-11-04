> Build 18 is a build-only test candidate. Neural inference, model quality and audio acceptance have not been run for this candidate. A successful software build is not full application acceptance.

# Meeting Summary — 0.7.0 beta

Local speech recognition, a full audio/transcript pair, and a short floating summary of the latest five minutes. System audio, microphone, or both can be selected. No meeting content is uploaded to a cloud API.

This is a beta candidate. A build or source snapshot does not prove successful clean-machine installation, 8 GB operation, long-meeting stability, or semantic accuracy. The Linux port remains unvalidated until genuine CPU inference and saved-package CI checks pass.

Apple Silicon macOS 14.4+ is the primary platform. The portable bundle includes Python and dependencies, and downloads pinned, SHA-256 verified models during first setup. Qwen3.5-9B 4-bit is the main profile on 16 GB+ systems; the 4B low-memory profile is experimental. Physical 8 GB Macs have not been validated. Normal Gatekeeper distribution requires Developer ID Application signing and Apple notarization; ad-hoc and Apple Development signing do not establish that trust. See [packaging](packaging/README.md).

The first Linux target is Ubuntu 24.04 / Debian 12–13 x86_64 with AVX2 and PulseAudio or PipeWire-Pulse. DEB and AppImage bundle Python, Qt, ONNX/llama.cpp CPU backends, and audio tools. Pinned Parakeet v3 INT8 and Qwen3.5-9B Q4_K_M downloads total about 6.84 GB. X11 is the primary desktop target; Wayland overlay behavior needs compositor-specific validation. See [Linux documentation](linux/README.md).

Full WAV and TXT files share a timestamp, neutral topic and unique session ID in `Documents/Сводка встреч/YYYY-MM-DD`. Silence and final audio tails are retained; a slow summary does not shorten the transcript. If ASR fails, audio remains and the transcript is marked incomplete. Summary evidence checks reduce some errors but do not prove semantic equivalence.

Windows is deferred. See [Russian documentation](README.md), [privacy](PRIVACY.md), and [third-party licenses](THIRD_PARTY_NOTICES.md).

The project's original code, documentation and graphics use [CC0](LICENSE). Dependencies and models retain their licenses. The synthetic WAV is authorized for this private package; public CC0 redistribution rights are not asserted.

Clean-source packaging uses a pinned standalone Python runtime and checked wheel digests; see [runtime-factory.md](packaging/runtime-factory.md). Preview files remain in private draft [Releases](https://github.com/Markiz-Ksan/meeting-summary/releases). Developer ID/notarization and final hardware/model acceptance remain open.
