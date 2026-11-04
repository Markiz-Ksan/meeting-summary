# Third-party components

This Linux distribution dynamically bundles the following unmodified libraries. The application remains a directory installation: the user can replace the Qt shared libraries; the AppImage can be extracted with `--appimage-extract` and its directory run with `AppRun`. No static Qt linkage is used.

- PySide6 Essentials / Shiboken6 / Qt: LGPL-3.0 (and component licenses). License and copyright files from installed wheels are retained through PyInstaller metadata and bundled files. Corresponding source: https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.8.3-src/ and https://download.qt.io/archive/qt/6.8/6.8.3/single/ . https://doc.qt.io/qtforpython-6/licenses.html
- sherpa-onnx: Apache-2.0. Source https://github.com/k2-fsa/sherpa-onnx/tree/v2.0.1 . ONNX Runtime and other dependencies retain their upstream license files.
- llama-cpp-python and llama.cpp: MIT. Source https://github.com/abetlen/llama-cpp-python/tree/v0.3.36 ; the vendored llama.cpp commit is part of that source distribution. Official 0.3.36 source SHA-256: `832db0699007f1be95a7e41ef12e88926b02ba836461e36a36372db2760c1a2e`.
- Python: PSF license. NumPy: BSD-3-Clause. Jinja2/MarkupSafe: BSD-3-Clause. certifi: MPL-2.0. diskcache: Apache-2.0. typing_extensions: PSF-2.0. Installed distribution metadata and license files are included by the build.
- FFmpeg, PulseAudio client tools, and their dynamic dependencies: Ubuntu binary packages, unmodified. Exact package versions, copyright notices and source provenance are retained in `_internal/native/licenses`. FFmpeg's effective license depends on the Ubuntu build configuration, including GPL-enabled libraries; consult its copied copyright and `ffmpeg -L` output.
- PyInstaller: GPL with application bundling exception. https://pyinstaller.org/en/stable/license.html
- AppImage build tool: MIT, https://github.com/AppImage/appimagetool . Its binary is not installed in the application; only its runtime is included in the generated AppImage.

Model weights are downloaded separately rather than redistributed inside the package:

- NVIDIA Parakeet TDT 0.6B v3: CC-BY-4.0. Attribution: NVIDIA, 2025; original https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3 . ONNX INT8 conversion by the sherpa-onnx project, revision `2bda32ec70b097a55adaa07d9a7173915b43cc78`, https://huggingface.co/csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8 . The conversion changes model serialization and quantization. https://creativecommons.org/licenses/by/4.0/
- Qwen3.5-9B: Apache-2.0, Qwen/Alibaba Cloud. Original https://huggingface.co/Qwen/Qwen3.5-9B . GGUF Q4_K_M conversion by bartowski, revision `182be2fd6c7bc44887d88a91cb03ff009cc9f549`, linked upstream license in model card: https://huggingface.co/bartowski/Qwen_Qwen3.5-9B-GGUF .

Checksums and exact filenames are in `meeting_summary/models.json`. No model download endpoint receives transcripts or audio.
