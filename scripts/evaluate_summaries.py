"""Small offline text evaluation. It never opens audio devices or grades semantics.

Development is the default. Holdout requires an explicit frozen writer manifest.
Every run is immutable and includes a review worksheet; output != acceptance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / 'tests/fixtures/acceptance-v1'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_cases(split, freeze=None):
    manifest = json.loads((CORPUS / 'manifest.json').read_text())
    for name, expected in manifest['files'].items():
        if sha(CORPUS / name) != expected:
            raise ValueError('Corpus changed after annotation: ' + name)
    if split == 'holdout':
        if freeze is None:
            raise ValueError('Holdout requires --freeze pointing to a frozen-writer manifest')
        frozen = json.loads(freeze.read_text())
        if frozen.get('writer_sha256') != sha(ROOT / 'src/summary_writer.py'):
            raise ValueError('Writer does not match the frozen version')
        if frozen.get('corpus_manifest_sha256') != sha(CORPUS / 'manifest.json'):
            raise ValueError('Corpus does not match the frozen version')
        if frozen.get('development_review_status') != 'passed':
            raise ValueError('Development review has not passed')
    return json.loads((CORPUS / (split + '.json')).read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split', choices=['development', 'holdout'], default='development')
    parser.add_argument('--freeze', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    cases = load_cases(args.split, args.freeze)
    if args.limit is not None:
        if args.limit <= 0 or args.split == 'holdout':
            parser.error('--limit must be positive and is development-only')
        cases = cases[:args.limit]
    # Validate holdout protection before importing MLX or loading weights.
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    sys.path.insert(0, str(ROOT / 'src'))
    import summary_writer
    from mlx_lm import load
    from mlx_lm.sample_utils import make_sampler
    run = args.output / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    run.mkdir(parents=True, exist_ok=False)
    metadata = dict(split=args.split, writer_sha256=sha(ROOT / 'src/summary_writer.py'),
                    corpus_manifest_sha256=sha(CORPUS / 'manifest.json'),
                    model='existing Qwen3.5-9B MLX 4bit', reasoning=False,
                    audio_capture=False, audio_playback=False, semantic_acceptance='unreviewed')
    (run / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
    model, tokenizer = load(str(Path.home() / 'Library/Application Support/MeetingOverlay/runtime/models/summary-9b'))
    sampler = make_sampler(temp=0)
    worksheet = []
    for case in cases:
        started = time.monotonic()
        row = dict(id=case['id'], summary='', error=None)
        try:
            row['summary'] = summary_writer.write_summary(model, tokenizer, case['source'], sampler)
        except Exception as error:
            row['error'] = str(error)
        row['seconds'] = round(time.monotonic() - started, 3)
        points = [p[2:] for p in row['summary'].splitlines() if p.startswith('• ')]
        row['structure_pass'] = bool(points) and 1 <= len(points) <= 4 and all(len(p.split()) <= 40 for p in points) and sum(len(p.split()) for p in points) <= 80
        row['expected_empty'] = case['expect_empty']
        # Review each fact separately; never infer semantic truth from nonempty output.
        review = dict(case, output=row,
                      critical_review=[dict(fact=f, preserved=None) for f in case['critical_facts']],
                      important_review=[dict(fact=f, preserved=None) for f in case['important_facts']],
                      forbidden_review=[dict(inference=f, present=None) for f in case['forbidden_inferences']],
                      semantic_duplicates=None, unsupported_claims=[], reviewer=None, status='unreviewed')
        worksheet.append(review)
        with (run / 'results.jsonl').open('a') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        (run / 'review.json').write_text(json.dumps(worksheet, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(row, ensure_ascii=False), flush=True)
    metadata.update(cases=len(worksheet), completed=True)
    (run / 'metadata.json').write_text(json.dumps(metadata, indent=2) + '\n')
    print(str(run), flush=True)


if __name__ == '__main__':
    main()
