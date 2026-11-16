# Third-party notices

Autonom is MIT licensed (see `LICENSE`). It includes data derived from the
following third-party work.

## simslim

- Project: simslim v0.14.0, https://github.com/MobAI-App/simslim
- License: MIT, copyright Interlap
- Used in: `scripts/autonom_lib/ios_slim_catalog.py` (the catalog of iOS
  Simulator launchd services by category and feature, their descriptions, and
  the list of services that must never be disabled). The slimming mechanism in
  `scripts/autonom_lib/ios_slim.py` and `scripts/autonom_lib/simulator_slim.py`
  follows simslim's approach and is written anew for Autonom.

```text
MIT License

Copyright (c) Interlap

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
