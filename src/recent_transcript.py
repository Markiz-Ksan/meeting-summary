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
    old_words, new_words = set(old.split()), set(new.split())
    # Similar wording can carry the opposite fact. Preserve distinct close
    # utterances rather than treating a changed amount/negation as a typo.
    if bool(old_words & {'не', 'нет', 'not', 'never'}) != bool(new_words & {'не', 'нет', 'not', 'never'}):
        return False
    numbers = lambda value: set(re.findall(r'\d+(?:[.,]\d+)?', value))
    a, b = numbers(old), numbers(new)
    if a and b and a != b:
        return False
    times = {'сегодня', 'завтра', 'вчера', 'понедельник', 'понедельника', 'вторник', 'вторника', 'среду', 'среды', 'четверг', 'четверга', 'пятницу', 'пятницы', 'субботу', 'субботы', 'воскресенье', 'воскресенья'}
    a, b = old_words & times, new_words & times
    if a and b and a != b:
        return False
    return SequenceMatcher(None, old, new, autojunk=False).ratio() >= 0.82


def partial_utterance(short, long):
    """A clipped decoder prefix/suffix, only used with correlated audio times.

    Morphological endings may change at a clipped boundary. Require a whole
    ordered run of at least three words; never erase a changed number/negation.
    """
    left, right = short.split(), long.split()
    if len(left) < 3 or len(left) >= len(right):
        return False
    def run(candidate):
        for a, b in zip(left, candidate):
            if a == b:
                continue
            if a.isdigit() or b.isdigit() or a in {'не', 'нет'} or b in {'не', 'нет'}:
                return False
            common = 0
            for x, y in zip(a, b):
                if x != y:
                    break
                common += 1
            if common < 4 or common / max(len(a), len(b)) < .65:
                return False
        return True
    return run(right[:len(left)]) or run(right[-len(left):])


class RecentTranscript:
    def __init__(self, seconds=300, max_entries=300):
        self.seconds = seconds
        self.max_entries = max_entries
        self.entries = []

    def update(self, text, timestamp, audio_seconds, segments=None):
        self.entries = [e for e in self.entries if timestamp-e[0] <= self.seconds]
        timed = segments is not None
        pairs = [(stamp, value.strip()) for stamp, value in segments if value.strip()] if timed else []
        new = [value for _, value in pairs] if timed else sentences(text)
        new_stamps = [stamp for stamp, _ in pairs]
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
                matches[i][j] = (not timed or abs(recent[i][0]-new_stamps[j]) <= 2) and (same_utterance(old_keys[i], new_keys[j]) or (timed and abs(recent[i][0]-new_stamps[j]) <= .8 and (partial_utterance(old_keys[i], new_keys[j]) or partial_utterance(new_keys[j], old_keys[i]))))
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

        def append_recovered(items, stamp, new_index):
            for offset, value in enumerate(items):
                item_stamp = new_stamps[new_index+offset] if timed else stamp
                key = normalized(value)
                # Shifted audio windows often start halfway through a sentence.
                # Compare only the recent tail: the same phrase later in a
                # meeting must still be recorded as a new occurrence.
                tail = [e for e in merged if abs(e[0]-item_stamp) <= 3] if timed else (merged[-len(recent):] if recent else [])
                if any(normalized(sentence) != key and normalized(sentence).endswith(key) for _, sentence in tail):
                    continue
                merged.append((item_stamp, value))

        for old_index, new_index in pairs:
            merged.extend(recent[old_cursor:old_index])
            append_recovered(new[new_cursor:new_index], recent[old_index][0], new_cursor)
            old_time, old_sentence = recent[old_index]
            replacement = new[new_index]
            # Preserve the complete version if the new window clips its prefix
            # or suffix; otherwise accept an ASR correction at the same point.
            if normalized(old_sentence).endswith(normalized(replacement)) or normalized(old_sentence).startswith(normalized(replacement) + ' ') or (timed and partial_utterance(normalized(replacement), normalized(old_sentence))):
                replacement = old_sentence
            merged.append((old_time, replacement))
            old_cursor, new_cursor = old_index + 1, new_index + 1
        merged.extend(recent[old_cursor:])
        append_recovered(new[new_cursor:], timestamp, new_cursor)
        if timed:
            merged.sort(key=lambda entry: entry[0])
        self.entries = merged
        if self.max_entries is not None:
            self.entries = self.entries[-self.max_entries:]
        return self.text

    @property
    def text(self):
        return ' '.join(s for _, s in self.entries)
