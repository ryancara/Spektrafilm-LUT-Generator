# Spektrafilm LUT Generator patch v2

This version keeps CLF as the recommended format and adds a CUBE shaper-range diagnostic.

The previous shaper CUBE wrote a 1D shaper over scene-linear 0..100. In Resolve this appears to be interpreted too bright / washed out. This build changes the default CUBE shaper range to 0..1 via `--cube-shaper-max 1.0`, while still allowing the old test with `--cube-shaper-max 100.0`.

Test:

```bash
python spektrafilm_state_to_lut.py --state gui_state.json --format cube --cube-mode shaper --cube-shaper-max 1 --size small --input-mode aces-ap0 --output-mode lut-default -o "Generated LUTs/test_shaper_max1.cube"

python spektrafilm_state_to_lut.py --state gui_state.json --format cube --cube-mode shaper --cube-shaper-max 100 --size small --input-mode aces-ap0 --output-mode lut-default -o "Generated LUTs/test_shaper_max100_old.cube"
```

If max1 is much closer to CLF, the issue was Resolve not respecting the intended 0..100 shaper input range in the CUBE path.
