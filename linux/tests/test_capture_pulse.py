"""Linux CI: genuine system/microphone capture using synthetic virtual sinks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import wave
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

@unittest.skipUnless(sys.platform=='linux' and os.environ.get('MEETING_TEST_PULSE')=='1', 'explicit synthetic PulseAudio CI only')
class PulseIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import numpy as np
        cls.np=np
        cls.root=tempfile.TemporaryDirectory(prefix='meeting-pulse-')
        cls.modules=[]
        for name in ['meeting_system','meeting_mic']:
            cls.modules.append(subprocess.check_output(['pactl','load-module','module-null-sink',f'sink_name={name}'],text=True).strip())
        cls.modules.append(subprocess.check_output(['pactl','load-module','module-remap-source','master=meeting_mic.monitor','source_name=meeting_test_mic'],text=True).strip())
        subprocess.run(['pactl','set-default-sink','meeting_system'],check=True)
        subprocess.run(['pactl','set-default-source','meeting_test_mic'],check=True)
        cls.paths=[]
        for frequency in [440,880]:
            path=Path(cls.root.name)/f'{frequency}.wav'
            audio=(np.sin(2*np.pi*frequency*np.arange(48000*8)/48000)*12000).astype('<i2')
            with wave.open(str(path),'wb') as saved:
                saved.setnchannels(1);saved.setsampwidth(2);saved.setframerate(48000);saved.writeframes(audio.tobytes())
            cls.paths.append(path)

    @classmethod
    def tearDownClass(cls):
        for module in reversed(cls.modules):subprocess.run(['pactl','unload-module',module],check=True)
        cls.root.cleanup()

    def test_all_capture_modes_contain_only_requested_sources(self):
        from meeting_summary.capture import PulseCapture
        report={}
        for mode in ['system','microphone','both']:
            with self.subTest(mode=mode):
                blocks=[];errors=[]
                capture=PulseCapture(mode,blocks.append,errors.append)
                players=[]
                try:
                    capture.start()
                    time.sleep(.5)
                    players=[subprocess.Popen(['paplay',f'--device={sink}',str(path)]) for sink,path in zip(['meeting_system','meeting_mic'],self.paths)]
                    time.sleep(4)
                finally:
                    capture.stop()
                    for player in players:
                        if player.poll() is None:player.terminate()
                        player.wait()
                self.assertFalse(errors,errors)
                audio=self.np.frombuffer(b''.join(blocks),dtype='<i2').astype(float)
                self.assertGreater(len(audio),48000)
                fft=abs(self.np.fft.rfft(audio))
                bins=self.np.fft.rfftfreq(len(audio),1/48000)
                magnitude=lambda f:float(fft[abs(bins-f)<2].max())
                system,mic=magnitude(440),magnitude(880)
                self.assertGreater(max(system,mic),1e6)
                if mode=='system':self.assertGreater(system,max(1,mic)*10)
                if mode=='microphone':self.assertGreater(mic,max(1,system)*10)
                if mode=='both':self.assertGreater(min(system,mic),max(system,mic)*.25)
                report[mode]=dict(frames=len(audio),system_tone=system,microphone_tone=mic)
        destination=os.environ.get('MEETING_CAPTURE_REPORT')
        if destination:Path(destination).write_text(json.dumps(report,indent=2)+'\n')

if __name__=='__main__':unittest.main()
