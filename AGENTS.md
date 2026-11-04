# Candidate maintenance rules

The current owner-authorized scope is build and software checks only. Do not start or retry neural inference, model-weight downloads or audio capture/playback locally or in CI without a new explicit instruction. Python/runtime software dependency preparation is permitted for packaging. This governs maintenance checks; it does not remove the application's normal user-initiated model setup.

Preserve user recordings, transcripts, notes and installed model settings. Keep model weights, credentials and personal data out of source archives. Preserve a verified recoverable app/runtime backup before replacing an installed version. Do not silently deploy experimental workers.

Mark candidate receipts `model_validation=not_run` and `semantic_acceptance=not_evaluated`. Keep build previews distinct from accepted releases. Do not claim 100% readiness, runtime correctness or universal hardware support from unit tests, imports or packaging alone. Public publication requires a separate explicit instruction.
