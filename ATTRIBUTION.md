# Attribution

This project builds on work from the ART raw processor external LUT tools and Spektrafilm.

## ART external LUT helper

`spektrafilm_mklut.py` is derived from ART's external LUT helper for Spektrafilm:

- ART repository: https://github.com/artraweditor/ART
- Source directory: https://github.com/artraweditor/ART/tree/master/tools/extlut

ART is released under the GNU General Public License version 3. This repository includes a copy of the GPLv3 licence in `LICENSE`.

Project-specific changes include:

- Reading Spektrafilm `gui_state.json` through `spektrafilm_state_to_lut.py`
- Mapping saved GUI settings to LUT-compatible Spektrafilm parameters
- Forcing LUT-safe options for spatial/stochastic effects
- Using ACES2065-1 / ACES AP0 linear in and out as the default LUT workflow
- Generating native CLF as the reference output
- Baking CUBE output from the generated CLF using OpenColorIO `ociobakelut`
- Adding a small Tkinter GUI wrapper

## Spektrafilm

This tool requires a separate Spektrafilm install. It does not bundle Spektrafilm.

- Spektrafilm: https://github.com/andreavolpato/spektrafilm
