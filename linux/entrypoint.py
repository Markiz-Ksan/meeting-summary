#!/usr/bin/env python3
"""Frozen executable entrypoint; headless verification is explicitly opt-in."""
import argparse
import json
from pathlib import Path
import sys

def main():
    parser = argparse.ArgumentParser(prog='meeting-summary')
    parser.add_argument('--version', action='store_true')
    parser.add_argument('--download-models', action='store_true')
    parser.add_argument('--gui-smoke', action='store_true')
    parser.add_argument('--inference-test', type=Path, metavar='SYNTHETIC_WAV')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    from meeting_summary import __version__
    if args.version:
        print(__version__)
        return 0
    if args.download_models:
        from meeting_summary.models import ensure_models
        ensure_models(progress=lambda name, done, total: print(f'{name}: {done}/{total}', flush=True))
        return 0
    if args.gui_smoke:
        from meeting_summary.verification import gui_smoke
        report = gui_smoke()
    elif args.inference_test:
        from meeting_summary.verification import inference_test
        report = inference_test(args.inference_test,report_path=args.report)
    else:
        from meeting_summary.gui import run
        return run()
    value = json.dumps(report, ensure_ascii=False, indent=2)
    print(value, flush=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(value+'\n', encoding='utf-8')
    return 0

if __name__ == '__main__':
    # PyInstaller workers must enter the multiprocessing bootstrap before CLI parsing.
    import multiprocessing
    multiprocessing.freeze_support()
    raise SystemExit(main())
