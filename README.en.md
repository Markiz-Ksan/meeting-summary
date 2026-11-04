<p align="center"><img src="docs/assets/icon.png" width="96" alt="Meeting Summary"></p>

# Meeting Summary

Local transcription and a short conversation summary in a floating window.

[Download](https://github.com/Markiz-Ksan/meeting-summary/releases) · [Setup](docs/SETUP.md) · [Русский](README.md)

- A transparent, scrollable summary of the latest five minutes.
- System audio, microphone, or both.
- Full WAV recordings and timestamped transcripts.
- Matching audio/text names, grouped by date.
- No Bionic, LM Studio or cloud API required.

**Mac:** Apple Silicon, macOS 14.4+, 16 GB RAM recommended. The 8 GB profile is experimental.

**Linux:** Ubuntu 24.04 / Debian 12–13, x86_64 with AVX2, PulseAudio or PipeWire-Pulse. 16 GB RAM recommended; X11 is the primary desktop target.

Python and dependencies are bundled. First setup downloads models: about 6.6 GB on Mac or 6.8 GB on Linux. Processing then works offline.

**Beta:** build 18 model inference, long meetings and physical 8 GB Macs have not been tested. Transcripts and summaries may contain errors.

[Build](packaging/README.md) · [Privacy](PRIVACY.md) · [Changelog](CHANGELOG.md)

Original code uses [CC0](LICENSE). Dependencies retain [their licenses](THIRD_PARTY_NOTICES.md).
