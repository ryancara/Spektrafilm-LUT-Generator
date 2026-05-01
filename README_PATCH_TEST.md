# Patched Spektrafilm LUT Generator test build

This is a minimal diagnostic patch for the CUBE issue.

## What changed

- CLF remains the recommended output.
- The GUI now defaults to `clf`, not `cube`.
- CUBE has a new `--cube-mode` option:
  - `shaper`: the existing PQ shaper CUBE path.
  - `simple`: a plain 3D CUBE over 0..1 linear RGB, with no 1D shaper.

## Why

The existing CUBE path depends on a 1D shaper with `LUT_1D_INPUT_RANGE 0.0 100.0`. If Resolve interprets that shaper range differently from the CLF half-domain shaper, normal scene-linear values can be indexed too high into the shaper, producing a washed-out result.

## Test commands

Recommended working CLF:

```bash
python spektrafilm_state_to_lut.py \
  --state gui_state.json \
  --format clf \
  --size small \
  --input-mode aces-ap0 \
  --output-mode lut-default \
  -o "Generated LUTs/test_from_state.clf"
```

Existing CUBE shaper path:

```bash
python spektrafilm_state_to_lut.py \
  --state gui_state.json \
  --format cube \
  --cube-mode shaper \
  --size small \
  --input-mode aces-ap0 \
  --output-mode lut-default \
  -o "Generated LUTs/test_from_state_shaper.cube"
```

New simple CUBE diagnostic path:

```bash
python spektrafilm_state_to_lut.py \
  --state gui_state.json \
  --format cube \
  --cube-mode simple \
  --size small \
  --input-mode aces-ap0 \
  --output-mode lut-default \
  -o "Generated LUTs/test_from_state_simple.cube"
```

If the simple CUBE looks much closer while the shaper CUBE is washed out, the bug is almost certainly in host interpretation of the CUBE shaper/range, not in the Spektrafilm parameter mapping.
