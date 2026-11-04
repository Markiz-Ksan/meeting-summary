"""Actual ONNX ASR and llama.cpp CPU inference; models load only on explicit start."""
import math
import os
import re
from . import shared


def timed_segments(tokens, timestamps, start, duration):
    if len(tokens) != len(timestamps) or not tokens:
        return None
    segments, pieces = [], []
    began = previous = None
    for token, stamp in zip(tokens, timestamps):
        stamp = float(stamp)
        if not math.isfinite(stamp) or stamp < 0 or stamp > duration + .5:
            raise ValueError('Некорректная разметка распознавания')
        if pieces and stamp-previous >= 1:
            segments.append((start+began, ''.join(pieces).strip()))
            pieces = []
        if not pieces:
            began = min(stamp, duration)
        pieces.append(token.replace('▁', ' '))
        previous = stamp
        if re.search(r'[.!?;]\s*$', token):
            segments.append((start+began, ''.join(pieces).strip()))
            pieces = []
    if pieces:
        segments.append((start+began, ''.join(pieces).strip()))
    return [(stamp, text) for stamp, text in segments if any(c.isalnum() for c in text)]


def chat_prompt(messages):
    parts = []
    for message in messages:
        if message['role'] not in ('system', 'user', 'assistant'):
            raise ValueError('Invalid chat role')
        # Transcript is untrusted data, and may literally contain special chat tokens.
        content = re.sub(r'<\|([^|]+)\|>', r'＜|\1|＞', message['content'])
        parts.append(f"<|im_start|>{message['role']}\n{content}<|im_end|>\n")
    return ''.join(parts) + '<|im_start|>assistant\n<think>\n\n</think>\n\n'


class SpeechEngine:
    def __init__(self, files, threads=None):
        import sherpa_onnx
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(files['encoder.int8.onnx']), decoder=str(files['decoder.int8.onnx']),
            joiner=str(files['joiner.int8.onnx']), tokens=str(files['tokens.txt']),
            num_threads=threads or max(1, min(4, (os.cpu_count() or 4)//2)),
            sample_rate=16000, feature_dim=80, model_type='nemo_transducer',
            decoding_method='greedy_search', provider='cpu', debug=False)

    def transcribe(self, pcm, sample_rate, started):
        import numpy as np
        audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
        if not len(audio) or float(np.sqrt(np.mean(audio**2))) < .0003:
            return '', None
        stream = self.recognizer.create_stream()
        stream.accept_waveform(sample_rate, audio)
        self.recognizer.decode_stream(stream)
        result = stream.result
        return result.text.strip(), timed_segments(result.tokens, result.timestamps, started, len(audio)/sample_rate)


class SummaryEngine:
    def __init__(self, files, threads=None):
        from llama_cpp import Llama
        filename = next(path for name, path in files.items() if name.endswith('.gguf'))
        self.model = Llama(model_path=str(filename), n_ctx=8192, n_batch=256,
                           n_threads=threads or max(1, min(8, (os.cpu_count() or 4)//2)),
                           n_gpu_layers=0, seed=42, verbose=False)

    def generate(self, messages):
        prompt = chat_prompt(messages)
        # Greedy bounded output matches macOS summary policy; no network/server involved.
        result = self.model.create_completion(prompt, max_tokens=700, temperature=0,
                                              stop=['<|im_end|>', '<|endoftext|>'], echo=False)
        return result['choices'][0]['text'].strip()

    def summarize(self, source):
        from summary_writer import write_summary_with
        return write_summary_with(self.generate, source)

    def title(self, source):
        from session_title import generate_title
        return generate_title(self.generate, source)
