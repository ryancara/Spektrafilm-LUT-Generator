# Spektrafilm LUT Generator

A small helper app for turning a saved Spektrafilm GUI state into a LUT.

The intended workflow is:

```text
Spektrafilm GUI
→ save/export gui_state.json
→ Spektrafilm LUT Generator reads that state
→ LUT-safe colour settings are mapped into Spektrafilm
→ spatial/stochastic effects are disabled or ignored
→ generate a CLF, or bake that CLF to a Resolve-compatible CUBE with OpenColorIO
```

This project does **not** bundle Spektrafilm. Users install Spektrafilm separately, then point the GUI to the Python executable that can import Spektrafilm.

## Current status

- **CLF** is the native/reference output.
- **CUBE** export is created by first generating a CLF, then baking it to a 64³ Resolve CUBE using OpenColorIO `ociobakelut`.
- The old hand-written CUBE writer is no longer used in the normal workflow.

Recommended default workflow:

```text
Input policy:  aces-ap0
Output policy: lut-default
CLF sampling quality: Standard - 36³
CUBE output resolution: 64³
```

In this mode, the LUT expects **ACES2065-1 / ACES AP0 linear RGB** as input and outputs **ACES2065-1 / ACES AP0 linear RGB**.

## Requirements

### Required for CLF output

- Python that can import Spektrafilm
- The Python packages required by Spektrafilm
- A Tkinter-capable Python if using the GUI

### Required for CUBE output

- Everything required for CLF output
- OpenColorIO command-line tools, specifically `ociobakelut`

On macOS with Homebrew:

```bash
brew install opencolorio
ociobakelut --help
```

## Quick start: GUI

macOS:

```bash
./launch_mac.command
```

The included macOS launcher activates a conda environment named `Spektrafilm` by default. To use a different conda environment name without editing the file:

```bash
SPEKTRAFILM_CONDA_ENV="your-env-name" ./launch_mac.command
```

Or run manually:

```bash
conda activate Spektrafilm
python spektrafilm_state_to_lut_gui.py
```

Apple’s system Python/Tk may open a blank GUI window on some macOS systems. Running the GUI from the Spektrafilm conda environment is recommended.

In the GUI:

1. Choose your Spektrafilm `gui_state.json`.
2. Choose an output folder.
3. Choose the Python executable that can import Spektrafilm.
   - Example macOS/Miniforge path: `/Users/<name>/miniforge3/envs/Spektrafilm/bin/python`
4. For CUBE export, make sure `ociobakelut` is available or choose its path.
5. Choose `CLF` or `CUBE`.
6. Use `Standard - 36³` for CLF sampling quality unless you need a slower/higher-resolution export.
7. Use `64` for CUBE output resolution.
8. Generate the LUT.

## Quick start: command line

Generate the reference CLF:

```bash
conda activate Spektrafilm
python spektrafilm_state_to_lut.py \
  --state gui_state.json \
  --format clf \
  --size medium \
  --input-mode aces-ap0 \
  --output-mode lut-default \
  -o "Generated LUTs/kodak_gold_200__kodak_2383__AP0-linear__CLF36.clf"
```

Generate a CUBE via OCIO:

```bash
conda activate Spektrafilm
python spektrafilm_state_to_lut.py \
  --state gui_state.json \
  --format cube \
  --size medium \
  --cube-size 64 \
  --input-mode aces-ap0 \
  --output-mode lut-default \
  -o "Generated LUTs/kodak_gold_200__kodak_2383__AP0-linear__CLF36__OCIO-CUBE64.cube"
```

Dry-run report:

```bash
conda activate Spektrafilm
python spektrafilm_state_to_lut.py \
  --state gui_state.json \
  --format clf \
  --dry-run-report \
  --report-detail
```

## Quality settings

The GUI shows explicit sampling labels instead of the old small/medium/large/huge names:

| GUI label | Internal option | Grid |
| --- | --- | --- |
| Standard - 36³ | `--size medium` | 36 × 36 × 36 |
| High - 64³ | `--size large` | 64 × 64 × 64 |
| Extreme - 121³ | `--size huge` | 121 × 121 × 121 |

`16³` is intentionally not shown in the GUI because it is too coarse for normal colour work. It was only useful for early debugging.

For CUBE output, `64³` is recommended. In testing, OCIO-baked 64³ CUBEs matched the reference CLF much better in shadows than 36³ CUBEs.

## What gets ignored for LUT generation?

A 3D LUT cannot represent effects that depend on neighbouring pixels, randomness, image size, or raw-loading context. For LUT safety, the tool disables or ignores settings such as:

- grain
- halation
- print glare
- lens blur
- unsharp mask
- crop/preview/upscale settings
- raw white balance loading settings
- display canvas/padding settings

The report separates:

- applied state settings
- forced LUT-safety settings
- overridden LUT-policy settings
- ignored non-LUT settings
- known unmapped settings

## Why CLF first, then CUBE?

CLF can represent the scene-linear Spektrafilm transform more safely than a hand-written CUBE. Earlier hand-written CUBE experiments were visibly washed out or incorrect. The current CUBE workflow uses the generated CLF as the reference, then asks OpenColorIO to bake a Resolve-compatible CUBE.

## Colour workflow notes

Default policy:

```text
input-mode:  aces-ap0
output-mode: lut-default
```

This means:

```text
ACES2065-1 / ACES AP0 linear RGB in
→ Spektrafilm transform
→ ACES2065-1 / ACES AP0 linear RGB out
```

The saved Spektrafilm GUI state may contain other input/output display settings, such as ProPhoto or sRGB. By default, those are overridden so the LUT has a predictable scene-linear AP0 workflow.

## Project layout

```text
spektrafilm_state_to_lut_gui.py   GUI wrapper
spektrafilm_state_to_lut.py       Reads gui_state.json, maps state settings, generates CLF/CUBE
spektrafilm_mklut.py              Core CLF sampling/writing engine
gui_state.json                    Example Spektrafilm GUI state
launch_mac.command                macOS launcher
launch_linux.sh                   Linux launcher
launch_windows.bat                Windows launcher
ATTRIBUTION.md                    Upstream attribution notes
LICENSE                           GPLv3 licence
```

## Attribution and licence

This project builds on ART's external LUT helper for Spektrafilm. ART is released under the GNU General Public License version 3. See `ATTRIBUTION.md` and `LICENSE`.

This tool also depends on a separate Spektrafilm install, but does not bundle Spektrafilm.
