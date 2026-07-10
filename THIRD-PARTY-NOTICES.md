# Third-party notices

Heard itself is licensed under the Apache License 2.0 (see [`LICENSE`](./LICENSE)).

It builds on models and software created by others. This file records what they
are, who made them, and under what terms - which several of those licenses
require us to do, and all of them deserve.

---

## Machine-learning models

### Speech recognition - NVIDIA Parakeet TDT 0.6B v3

- **Model:** `nvidia/parakeet-tdt-0.6b-v3`
- **Copyright:** NVIDIA Corporation
- **License:** [Creative Commons Attribution 4.0 International (CC-BY-4.0)](https://creativecommons.org/licenses/by/4.0/)
- **Source:** <https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3>

Heard runs this model entirely on your own machine. The weights are **not
redistributed inside the Heard application**; they are downloaded on first use
from the Hugging Face Hub, via the MLX conversion published by the
`mlx-community` organization (<https://huggingface.co/mlx-community/parakeet-tdt-0.6b-v3>).
The weights are used **unmodified**.

CC-BY-4.0 permits commercial use and redistribution, and requires attribution.
This notice is that attribution.

### Text to speech (offline voice) - Kokoro

- **Model:** Kokoro-82M, **License:** Apache-2.0
- **Runtime:** `kokoro-onnx` by thewh1teagle, **License:** MIT
- **Source:** <https://github.com/thewh1teagle/kokoro-onnx>

---

## Voice activity detection - Silero VAD

- **Copyright (c) 2024 Silero Team**
- **License:** MIT
- **Source:** <https://github.com/snakers4/silero-vad>

Heard's Power build **redistributes** the `silero_vad.onnx` model file. The MIT
license text follows, as MIT requires:

```
MIT License

Copyright (c) 2024 Silero Team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## Bundled software

The packaged application embeds a Python runtime and its dependencies. The
principal components:

| Component | License |
|---|---|
| CPython | PSF-2.0 |
| MLX (`mlx`, `mlx-metal`) | MIT |
| `parakeet-mlx` | Apache-2.0 |
| ONNX Runtime | MIT |
| NumPy | BSD-3-Clause |
| `certifi` | MPL-2.0 |
| `tqdm` | MPL-2.0 AND MIT |

Everything else is MIT, BSD, Apache-2.0, ISC, or PSF-2.0.

`certifi` and `tqdm` are covered in part by the Mozilla Public License 2.0. MPL-2.0
is a per-file copyleft: we do not modify either package, and their unmodified
sources are available from the projects above and from the Python Package Index.

A complete, machine-generated inventory of every bundled distribution and its
version is produced at build time. To reproduce it from an installed copy:

```sh
ls /Applications/Heard.app/Contents/Resources/lib/python3.13/*.dist-info
```

---

If you believe an attribution here is incomplete or incorrect, please open an
issue - we would rather fix it than be right.
