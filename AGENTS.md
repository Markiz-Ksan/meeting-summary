# Maintenance

- Current scope: builds and software checks. Model downloads, inference, audio capture and playback require a new explicit instruction.
- Preserve recordings, transcripts, installed models and settings. Keep credentials and personal data out of Git.
- Keep one verified recovery backup before replacing the installed app. Do not deploy experimental workers silently.
- Keep model/audio validation marked not_run when skipped. Unit tests do not prove model quality or hardware support.
- Mac builds use ad-hoc signatures and SHA-256. Developer ID and notarization are not required. Do not change macOS security settings.
- Keep README and release notes short: features, setup, requirements and essential limitations. Omit approval history, author-decision wording and internal CI reports.
- Keep the repository and releases private until explicitly instructed otherwise.
