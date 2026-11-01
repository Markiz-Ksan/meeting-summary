"""Keep recent text while reconciling overlapping ASR windows; no disk storage."""
import re
from difflib import SequenceMatcher


def sentences(text):
    return [s.strip() for s in re.split(r'(?<=[.!?;])\s+|\n+', text) if s.strip()]


def normalized(text):
    return re.sub(r'[^\w\s]', '', text.lower().replace('ё', 'е')).strip()


def same_utterance(old, new):
    if old == new:
        return True
    # Extend an unfinished last phrase without retaining both versions. Do not
    # equate a short repeated word with an entire unrelated utterance.
    if min(len(old.split()), len(new.split())) >= 2 and (old.startswith(new + ' ') or new.startswith(old + ' ')):
        return True
    return SequenceMatcher(None, old, new, autojunk=False).ratio() >= 0.82


class RecentTranscript:
    def __init__(self, seconds=300, max_entries=300):
        self.seconds = seconds
        self.max_entries = max_entries
        self.entries = []

    def update(self, text, timestamp, audio_seconds):
        self.entries = [e for e in self.entries if timestamp-e[0] <= self.seconds]
        new = sentences(text)
        if not new:
            return self.text
        window_start = timestamp-audio_seconds
        # Only reconcile text that could belong to this audio window.
        cut = next((i for i, e in enumerate(self.entries) if e[0] >= window_start), len(self.entries))
        older, recent = self.entries[:cut], self.entries[cut:]
        old_keys, new_keys = [normalized(e[1]) for e in recent], [normalized(s) for s in new]
        n, m = len(recent), len(new)
        scores = [[0]*(m+1) for _ in range(n+1)]
        matches = [[False]*m for _ in range(n)]
        for i in range(n-1, -1, -1):
            for j in range(m-1, -1, -1):
                matches[i][j] = same_utterance(old_keys[i], new_keys[j])
                scores[i][j] = max(scores[i+1][j], scores[i][j+1], (1+scores[i+1][j+1]) if matches[i][j] else 0)
        pairs = []
        i = j = 0
        while i < n and j < m:
            if matches[i][j] and scores[i][j] == 1+scores[i+1][j+1]:
                pairs.append((i, j)); i += 1; j += 1
            elif scores[i+1][j] >= scores[i][j+1]:
                i += 1
            else:
                j += 1
        # An ASR decoder can omit sentences, change punctuation, or return only
        # a short fragment. Absence in a later decode is NOT proof that a saved
        # sentence was never spoken. Reconcile matched utterances, retaining
        # every unmatched passage instead of replacing the entire window.
        merged = list(older)
        old_cursor = new_cursor = 0

        def append_recovered(items, stamp):
            for value in items:
                key = normalized(value)
                # Shifted audio windows often start halfway through a sentence.
                # Compare only the recent tail: the same phrase later in a
                # meeting must still be recorded as a new occurrence.
                if recent and any(normalized(sentence) != key and normalized(sentence).endswith(key) for _, sentence in merged[-len(recent):]):
                    continue
                merged.append((stamp, value))

        for old_index, new_index in pairs:
            merged.extend(recent[old_cursor:old_index])
            append_recovered(new[new_cursor:new_index], recent[old_index][0])
            old_time, old_sentence = recent[old_index]
            replacement = new[new_index]
            # Preserve the complete version if the new window clips its prefix
            # or suffix; otherwise accept an ASR correction at the same point.
            if normalized(old_sentence).endswith(normalized(replacement)) or normalized(old_sentence).startswith(normalized(replacement) + ' '):
                replacement = old_sentence
            merged.append((old_time, replacement))
            old_cursor, new_cursor = old_index + 1, new_index + 1
        merged.extend(recent[old_cursor:])
        append_recovered(new[new_cursor:], timestamp)
        self.entries = merged
        if self.max_entries is not None:
            self.entries = self.entries[-self.max_entries:]
        return self.text

    @property
    def text(self):
        return ' '.join(s for _, s in self.entries)
