"""Linux CI: genuine system/microphone capture using synthetic virtual sinks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import wave
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

@unittest.skipUnless(sys.platform == 'linux' and os.environ.get('MEETING_TEST_PULSE') == '1',
                     'explicit synthetic PulseAudio CI only')
class PulseIntegration(unittest.TestCase):
    @classmethod
    def write_report(cls):
        destination = os.environ.get('MEETING_CAPTURE_REPORT')
        if destination:
            Path(destination).write_text(json.dumps(cls.report, indent=2) + '\n')

    @classmethod
    def setUpClass(cls):
        import numpy as np
        cls.np = np
        cls.root = tempfile.TemporaryDirectory(prefix='meeting-pulse-')
        cls.modules = []
        cls.report = dict(format=3, synthetic=True, cases={},
                          ffmpeg_version=subprocess.check_output(['ffmpeg', '-version'], text=True),
                          pulse_version=subprocess.check_output(['pulseaudio', '--version'], text=True))
        cls.write_report()
        for name in ['meeting_system', 'meeting_mic']:
            cls.modules.append(subprocess.check_output(
                ['pactl', 'load-module', 'module-null-sink', f'sink_name={name}'], text=True).strip())
        cls.modules.append(subprocess.check_output(
            ['pactl', 'load-module', 'module-remap-source', 'master=meeting_mic.monitor',
             'source_name=meeting_test_mic'], text=True).strip())
        subprocess.run(['pactl', 'set-default-sink', 'meeting_system'], check=True)
        subprocess.run(['pactl', 'set-default-source', 'meeting_test_mic'], check=True)
        cls.paths = []
        for frequency in [440, 880]:
            path = Path(cls.root.name) / f'{frequency}.wav'
            audio = (np.sin(2*np.pi*frequency*np.arange(48000*8)/48000)*12000).astype('<i2')
            with wave.open(str(path), 'wb') as saved:
                saved.setnchannels(1); saved.setsampwidth(2); saved.setframerate(48000)
                saved.writeframes(audio.tobytes())
            cls.paths.append(path)

    @classmethod
    def tearDownClass(cls):
        for module in reversed(cls.modules):
            subprocess.run(['pactl', 'unload-module', module], check=True)
        cls.root.cleanup()
        cls.write_report()

    def capture_case(self, mode, *, legacy_probe=False):
        from meeting_summary.capture import PulseCapture, capture_command
        blocks, errors, players = [], [], []
        capture = PulseCapture(mode, blocks.append, errors.append, log_level='verbose')
        def legacy_command(*args, **kwargs):
            # Same graph and buffering; change ONE variable to isolate startup:
            # restore stream-info probing on each real Pulse input.
            command = capture_command(*args, **kwargs)
            while '-nofind_stream_info' in command:
                command.remove('-nofind_stream_info')
            return command
        began = time.monotonic()
        try:
            if legacy_probe:
                with patch('meeting_summary.capture.capture_command', side_effect=legacy_command):
                    capture.start()
            else:
                capture.start()
            time.sleep(.5)
            players = [subprocess.Popen(['paplay', f'--device={sink}', str(path)])
                       for sink, path in zip(['meeting_system', 'meeting_mic'], self.paths)]
            # Fixed four-second capture budget, unchanged from the failing run.
            # No extra warm-up or relaxed frame/tone threshold masks startup loss.
            time.sleep(4)
        except BaseException as error:
            errors.append(error)
        finally:
            try:
                capture.stop()
            except BaseException as error:
                errors.append(error)
            for player in players:
                if player.poll() is None:
                    player.terminate()
                try:
                    player.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    player.kill(); player.wait()
        audio = self.np.frombuffer(b''.join(blocks), dtype='<i2').astype(float)
        result = capture.diagnostic_snapshot()
        result.update(frames=len(audio), errors=[repr(error) for error in errors],
                      total_seconds=round(time.monotonic()-began, 6),
                      legacy_stream_probe=legacy_probe)
        if len(audio):
            fft = abs(self.np.fft.rfft(audio))
            bins = self.np.fft.rfftfreq(len(audio), 1/48000)
            magnitude = lambda frequency: float(fft[abs(bins-frequency)<2].max())
            result.update(system_tone=magnitude(440), microphone_tone=magnitude(880))
        key = 'legacy-both' if legacy_probe else mode
        self.report['cases'][key] = result
        self.write_report()  # Save failures BEFORE assertions or later steps can abort.
        print(json.dumps(dict(case=key, **result)), flush=True)
        return result

    def test_all_capture_modes_contain_only_requested_sources(self):
        for mode in ['system', 'microphone', 'both']:
            with self.subTest(mode=mode):
                result = self.capture_case(mode)
                self.assertFalse(result['errors'], result)
                self.assertGreater(result['frames'], 48000, result)
                system, mic = result['system_tone'], result['microphone_tone']
                self.assertGreater(max(system, mic), 1e6, result)
                if mode == 'system': self.assertGreater(system, max(1, mic)*10, result)
                if mode == 'microphone': self.assertGreater(mic, max(1, system)*10, result)
                if mode == 'both': self.assertGreater(min(system, mic), max(system, mic)*.25, result)
        # Diagnostic control is not a success gate: the three production modes
        # above must still pass all original assertions. Its trace establishes
        # whether the old serial stream-info startup delay reproduces on this same runner.
        self.capture_case('both', legacy_probe=True)

if __name__ == '__main__':
    unittest.main()
