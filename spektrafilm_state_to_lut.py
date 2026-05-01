#!/usr/bin/env python3
"""
Spektrafilm State to LUT Generator

Reads a Spektrafilm gui_state.json file and generates a LUT.

CLF is generated natively using the Spektrafilm sampling engine. CUBE export now
uses the CLF as the reference, then bakes that CLF to a Resolve CUBE using
OpenColorIO/ociobakelut.

Place this file beside spektrafilm_mklut.py, then run with the same Python
environment that can import Spektrafilm.

Example:
    python spektrafilm_state_to_lut.py \
      --state gui_state.json \
      --format cube \
      --size small \
      -o "Generated LUTs/test_from_state.cube"
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    import spektrafilm_mklut as core
except Exception as exc:
    raise SystemExit(
        "Could not import spektrafilm_mklut.py.\n"
        "Put spektrafilm_state_to_lut.py beside spektrafilm_mklut.py, "
        "then run it from that folder.\n\n"
        f"Import error: {exc}"
    )


STATE_TO_PARAM_MAP = [
    # input_image
    # Colourspace fields are handled later by apply_colourspace_policy so the
    # report does not claim ProPhoto/sRGB were applied when ACES AP0 override is active.
    ("input_image.filter_uv", "camera.filter_uv"),
    ("input_image.filter_ir", "camera.filter_ir"),

    # preflashing
    ("preflashing.exposure", "enlarger.preflash_exposure"),
    ("preflashing.y_filter_shift", "enlarger.preflash_y_filter_shift"),
    ("preflashing.m_filter_shift", "enlarger.preflash_m_filter_shift"),
    ("preflashing.just_preflash", "enlarger.just_preflash"),

    # couplers
    ("couplers.active", "film_render.dir_couplers.active"),
    ("couplers.dir_couplers_amount", "film_render.dir_couplers.amount"),
    ("couplers.dir_couplers_ratio", "film_render.dir_couplers.ratio_rgb"),
    ("couplers.dir_couplers_diffusion_um", "film_render.dir_couplers.diffusion_size_um"),
    ("couplers.diffusion_interlayer", "film_render.dir_couplers.diffusion_interlayer"),
    ("couplers.high_exposure_shift", "film_render.dir_couplers.high_exposure_shift"),

    # special
    ("special.film_gamma_factor", "film_render.density_curve_gamma"),
    ("special.print_gamma_factor", "print_render.density_curve_gamma"),
    ("special.print_density_min_factor", "print_render.base_density_scale"),

    # simulation
    ("simulation.film_format_mm", "camera.film_format_mm"),
    ("simulation.exposure_compensation_ev", "camera.exposure_compensation_ev"),
    # auto_exposure is intentionally forced off for LUT generation.
    ("simulation.auto_exposure_method", "camera.auto_exposure_method"),
    ("simulation.print_illuminant", "enlarger.illuminant"),
    ("simulation.print_exposure", "enlarger.print_exposure"),
    ("simulation.print_exposure_compensation", "enlarger.print_exposure_compensation"),
    ("simulation.print_y_filter_shift", "enlarger.y_filter_shift"),
    ("simulation.print_m_filter_shift", "enlarger.m_filter_shift"),
    ("simulation.scan_film", "io.scan_film"),
]

# These state fields are meaningful, but are deliberately overridden by LUT policy
# or LUT-safety policy unless the user explicitly opts into following the state.
OVERRIDDEN_BY_POLICY_STATE_PATHS = [
    "input_image.input_color_space",
    "input_image.apply_cctf_decoding",
    "input_image.spectral_upsampling_method",
    "simulation.output_color_space",
    "simulation.saving_cctf_encoding",
    "simulation.saving_color_space",
    "simulation.auto_exposure",
    "simulation.camera_lens_blur_um",
    "simulation.scan_lens_blur",
    "simulation.scan_unsharp_mask",
]

# These are deliberately not applied to LUT generation. They depend on pixels,
# spatial position, randomness, blur kernels, UI display behaviour, or raw loading.
IGNORED_STATE_PATHS = [
    "load_raw.white_balance",
    "load_raw.temperature",
    "load_raw.tint",
    "grain.active",
    "grain.sublayers_active",
    "grain.particle_area_um2",
    "grain.particle_scale",
    "grain.particle_scale_layers",
    "grain.density_min",
    "grain.uniformity",
    "grain.blur",
    "grain.blur_dye_clouds_um",
    "grain.micro_structure",
    "halation.active",
    "halation.scattering_strength",
    "halation.scattering_size_um",
    "halation.halation_strength",
    "halation.halation_size_um",
    "glare.active",
    "glare.percent",
    "glare.roughness",
    "glare.blur",
    "glare.compensation_removal_factor",
    "glare.compensation_removal_density",
    "glare.compensation_removal_transition",
    "input_image.preview_resize_factor",
    "input_image.upscale_factor",
    "input_image.crop",
    "input_image.crop_center",
    "input_image.crop_size",
    "simulation.compute_full_image",
    "display.use_display_transform",
    "display.gray_18_canvas",
    "display.white_padding",
]

# These exist in gui_state.json but need a confirmed Spektrafilm param path.
KNOWN_UNMAPPED_STATE_PATHS = [
    "special.film_channel_swap",
    "special.print_channel_swap",
]

INPUT_PRESETS = {
    "aces-ap0": ("ACES2065-1", "hanatos2025"),
    "rec2020": ("ITU-R BT.2020", "hanatos2025"),
    "srgb": ("sRGB", "mallett2019"),
    "prophoto": ("ProPhoto RGB", "hanatos2025"),
}


def get_nested(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    obj: Any = data
    for part in dotted.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return default
        obj = obj[part]
    return obj


def has_nested(data: dict[str, Any], dotted: str) -> bool:
    sentinel = object()
    return get_nested(data, dotted, sentinel) is not sentinel


def normalise_tuple_value(value: Any) -> Any:
    # Spektrafilm params commonly use tuples for RGB triples. JSON gives lists.
    if isinstance(value, list):
        return tuple(value)
    return value


def enum_index_by_value(items: list[Any], value: Any, kind: str) -> int:
    for i, item in enumerate(items):
        if getattr(item, "value", None) == value:
            return i
    allowed = ", ".join(str(getattr(item, "value", getattr(item, "name", item))) for item in items)
    raise SystemExit(f"Unknown {kind} in gui_state.json: {value!r}\nAvailable {kind}s: {allowed}")


def enum_index_for_paper(items: list[Any], value: Any) -> int:
    # The existing core script represents the UI 'none' option with a NoPaper
    # object whose .value is a valid fallback paper. Match 'none' explicitly.
    if value in (None, "none", "None"):
        for i, item in enumerate(items):
            if item.__class__.__name__ == "NoPaper" or getattr(item, "name", None) == "none":
                return i
    return enum_index_by_value(items, value, "print paper")


def safe_filename(text: str) -> str:
    text = text.replace(" ", "_").replace("/", "-")
    return "".join(ch for ch in text if ch.isalnum() or ch in "._-+")


def apply_lut_safety(params: Any, report: dict[str, Any]) -> None:
    forced = []

    def force(path: str, value: Any) -> None:
        if core.path_exists(params, path):
            old = copy.deepcopy(get_value_by_path(params, path))
            core.set_by_path(params, path, value)
            forced.append({"path": path, "old": old, "new": value})

    force("debug.deactivate_spatial_effects", True)
    force("debug.deactivate_stochastic_effects", True)
    force("camera.auto_exposure", False)
    force("camera.lens_blur_um", 0.0)
    force("io.crop", False)
    force("io.full_image", True)
    force("io.preview_resize_factor", 1.0)
    force("io.upscale_factor", 1.0)
    force("enlarger.lens_blur", 0.0)
    force("scanner.lens_blur", 0.0)

    if not core.spektrafilm_legacy:
        force("film_render.grain.active", False)
        force("film_render.halation.active", False)
        force("print_render.glare.active", False)
        force("scanner.unsharp_mask", (0.0, 0.0))
    else:
        force("negative.grain.active", False)
        force("negative.halation.active", False)
        force("print_paper.glare.active", False)

    report["forced_lut_safety_settings"] = forced


def get_value_by_path(root: Any, path: str) -> Any:
    obj = root
    for part in path.split("."):
        obj = core._get_child(obj, part)
    return obj


def apply_state_mapping(params: Any, state: dict[str, Any], report: dict[str, Any]) -> None:
    applied = []
    missing_param_paths = []

    for state_path, param_path in STATE_TO_PARAM_MAP:
        if not has_nested(state, state_path):
            continue
        value = normalise_tuple_value(get_nested(state, state_path))
        if not core.path_exists(params, param_path):
            missing_param_paths.append({"state": state_path, "param": param_path, "value": value})
            continue
        try:
            core.set_by_path(params, param_path, value)
            applied.append({"state": state_path, "param": param_path, "value": value})
        except Exception as exc:
            missing_param_paths.append({"state": state_path, "param": param_path, "value": value, "error": str(exc)})

    report["applied_state_settings"] = applied
    report["missing_param_paths"] = missing_param_paths


def collect_ignored_and_unmapped(state: dict[str, Any], report: dict[str, Any]) -> None:
    ignored = []
    for path in IGNORED_STATE_PATHS:
        if has_nested(state, path):
            ignored.append({"state": path, "value": get_nested(state, path)})

    unmapped = []
    for path in KNOWN_UNMAPPED_STATE_PATHS:
        if has_nested(state, path):
            unmapped.append({"state": path, "value": get_nested(state, path)})

    overridden = []
    for path in OVERRIDDEN_BY_POLICY_STATE_PATHS:
        if has_nested(state, path):
            overridden.append({"state": path, "value": get_nested(state, path)})

    report["ignored_non_lut_settings"] = ignored
    report["known_unmapped_settings"] = unmapped
    report["overridden_by_lut_policy"] = overridden


def apply_colourspace_policy(params: Any, opts: Any, state: dict[str, Any], report: dict[str, Any]) -> None:
    """Choose the LUT input/output colour handling.

    Default policy is deliberately conservative:
    - input: ACES AP0/ACES2065-1 linear, matching the existing mklut behaviour.
    - output: ACES AP0/ACES2065-1 linear, so the LUT is scene-linear friendly.

    Use --input-mode state and/or --output-mode state when you explicitly want
    the LUT to follow the saved Spektrafilm GUI state colourspace fields.
    """
    if opts.input_mode == "state":
        state_input = get_nested(state, "input_image.input_color_space", "ProPhoto RGB")
        state_method = get_nested(state, "input_image.spectral_upsampling_method", "hanatos2025")
        params.io.input_color_space = state_input
        params.settings.rgb_to_raw_method = state_method
        report["input_policy"] = f"state: {state_input}, rgb_to_raw_method={state_method}"
    else:
        input_space, method = INPUT_PRESETS[opts.input_mode]
        params.io.input_color_space = input_space
        params.settings.rgb_to_raw_method = method
        report["input_policy"] = f"override: {opts.input_mode} -> {input_space}, rgb_to_raw_method={method}"

    if opts.output_mode == "state":
        output_space = get_nested(state, "simulation.output_color_space", "sRGB")
        output_cctf = bool(get_nested(state, "simulation.saving_cctf_encoding", True))
        params.io.output_color_space = output_space
        params.io.output_cctf_encoding = output_cctf
        report["output_policy"] = f"state: {output_space}, output_cctf_encoding={output_cctf}"
    else:
        params.io.output_color_space = "ACES2065-1"
        params.io.output_cctf_encoding = False
        report["output_policy"] = "lut-default: ACES2065-1 linear, output_cctf_encoding=False"


def make_opts(args: argparse.Namespace, state: dict[str, Any]) -> SimpleNamespace:
    film_value = get_nested(state, "simulation.film_stock")
    paper_value = get_nested(state, "simulation.print_paper")
    if not film_value:
        raise SystemExit("gui_state.json is missing simulation.film_stock")
    if paper_value is None:
        raise SystemExit("gui_state.json is missing simulation.print_paper")

    film_index = enum_index_by_value(core.film_stocks, film_value, "film stock")
    paper_index = enum_index_for_paper(core.print_papers, paper_value)

    output = Path(args.output).expanduser() if args.output else None
    if output is None:
        outdir = Path(args.outdir).expanduser()
        ext = "clfz" if args.format == "clf" and args.compressed else args.format
        output = outdir / f"{safe_filename(film_value)}__{safe_filename(str(paper_value))}__from_state.{ext}"

    input_colorspace_for_title = args.input_mode if args.input_mode != "state" else "state"
    if input_colorspace_for_title == "prophoto":
        # Existing core title/comment system only knows these three names.
        # Keep it readable rather than breaking older writer logic.
        input_colorspace_for_title = "state"

    return SimpleNamespace(
        output=str(output),
        outdir=str(output.parent),
        size=args.size,
        format=args.format,
        input_colorspace=input_colorspace_for_title if input_colorspace_for_title in {"aces-ap0", "rec2020", "srgb"} else "aces-ap0",
        input_mode=args.input_mode,
        output_mode=args.output_mode,
        compressed=args.compressed,
        film=film_index,
        paper=paper_index,
        camera_expcomp=float(get_nested(state, "simulation.exposure_compensation_ev", 0.0)),
        print_exposure=float(get_nested(state, "simulation.print_exposure", 1.0)),
        input_gain=args.input_gain,
        y_shift=float(get_nested(state, "simulation.print_y_filter_shift", 0.0)),
        m_shift=float(get_nested(state, "simulation.print_m_filter_shift", 0.0)),
        auto_ym_shifts=args.auto_ym_shifts,
        film_gamma=float(get_nested(state, "special.film_gamma_factor", 1.0)),
        print_gamma=float(get_nested(state, "special.print_gamma_factor", 1.0)),
        dir_couplers_amount=float(get_nested(state, "couplers.dir_couplers_amount", 1.0)),
        output_black_offset=args.output_black_offset,
        control_values={},
        schema=None,
        controls_json=None,
        server=False,
    )


class StateLUTCreator(core.LUTCreator):
    def __init__(self, opts: Any, state: dict[str, Any], report: dict[str, Any]):
        self.state = state
        self.report = report
        super().__init__(opts)

    def get_params(self, opts: Any) -> Any:
        params = core.photo_params(
            core.film_stocks[opts.film].value,
            core.print_papers[opts.paper].value,
        )

        apply_state_mapping(params, self.state, self.report)
        apply_lut_safety(params, self.report)
        apply_colourspace_policy(params, opts, self.state, self.report)

        if not core.spektrafilm_legacy and isinstance(core.print_papers[opts.paper], core.NoPaper):
            params.io.scan_film = True
        elif core.spektrafilm_legacy and isinstance(core.print_papers[opts.paper], core.NoPaper):
            params.io.compute_negative = True

        # Optional generator-only controls. These are not part of Spektrafilm's gui_state.
        if opts.auto_ym_shifts:
            # Reuse the parent implementation's auto-shift logic by temporarily
            # passing through its expected scalar options after our state has been applied.
            # Simpler version: generate with state y/m shifts as-is for now.
            self.report.setdefault("warnings", []).append(
                "--auto-ym-shifts is accepted for compatibility, but this first state-based script does not yet run the neutral-grey optimisation."
            )

        return params


def _format_report_value(value: Any) -> str:
    if isinstance(value, tuple):
        value = list(value)
    return json.dumps(value, default=str)


def _print_report_section(title: str, rows: list[dict[str, Any]], mode: str) -> None:
    if not rows:
        return
    print(f"\n{title}")
    print("-" * len(title))
    for row in rows:
        if mode == "applied":
            print(f"  {row.get('state')} -> {row.get('param')} = {_format_report_value(row.get('value'))}")
        elif mode == "forced":
            print(f"  {row.get('path')}: {_format_report_value(row.get('old'))} -> {_format_report_value(row.get('new'))}")
        elif mode == "state_value":
            print(f"  {row.get('state')} = {_format_report_value(row.get('value'))}")
        elif mode == "missing":
            suffix = f" ({row.get('error')})" if row.get("error") else ""
            print(f"  {row.get('state')} -> {row.get('param')} = {_format_report_value(row.get('value'))}{suffix}")


def print_report(report: dict[str, Any], detail: bool = False) -> None:
    print("\nSpektrafilm state-to-LUT report")
    print("=" * 34)
    print(f"Film:  {report['film_stock']}")
    print(f"Paper: {report['print_paper']}")
    print(f"Input policy:  {report.get('input_policy')}")
    print(f"Output policy: {report.get('output_policy')}")
    print(f"Applied settings: {len(report.get('applied_state_settings', []))}")
    print(f"Forced LUT-safety settings: {len(report.get('forced_lut_safety_settings', []))}")
    print(f"Overridden by LUT policy: {len(report.get('overridden_by_lut_policy', []))}")
    print(f"Ignored non-LUT settings: {len(report.get('ignored_non_lut_settings', []))}")
    print(f"Known unmapped settings: {len(report.get('known_unmapped_settings', []))}")
    if report.get("missing_param_paths"):
        print(f"Missing param paths: {len(report['missing_param_paths'])}")
    if report.get("warnings"):
        print("Warnings:")
        for warning in report["warnings"]:
            print(f"  - {warning}")

    if detail:
        _print_report_section("Applied state settings", report.get("applied_state_settings", []), "applied")
        _print_report_section("Forced LUT-safety settings", report.get("forced_lut_safety_settings", []), "forced")
        _print_report_section("Overridden by LUT policy", report.get("overridden_by_lut_policy", []), "state_value")
        _print_report_section("Ignored non-LUT settings", report.get("ignored_non_lut_settings", []), "state_value")
        _print_report_section("Known unmapped settings", report.get("known_unmapped_settings", []), "state_value")
        _print_report_section("Missing parameter paths", report.get("missing_param_paths", []), "missing")

def get_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generate a CLF LUT from a Spektrafilm gui_state.json file, or bake a CUBE via OCIO.")
    p.add_argument("--state", required=True, help="Path to Spektrafilm gui_state.json")
    p.add_argument("-o", "--output", help="Output LUT path. Defaults to --outdir/name_from_state.ext")
    p.add_argument("-O", "--outdir", default="Generated LUTs", help="Output folder when --output is not supplied")
    p.add_argument("--format", choices=["cube", "clf"], default="clf", help="CLF is generated natively. CUBE is generated by first making CLF, then baking with OCIO ociobakelut.")
    p.add_argument("-s", "--size", choices=["small", "medium", "large", "huge"], default="medium")
    p.add_argument("--cube-size", type=int, default=64, help="3D cube size for OCIO-baked CUBE output. Testing showed 64 works better than 36 for shadow neutrality.")
    p.add_argument("--ocio-bakelut", default="ociobakelut", help="Path/name of the OpenColorIO ociobakelut executable used for CUBE export.")
    p.add_argument("--keep-intermediate-clf", action="store_true", help="Keep the temporary/reference CLF used when baking CUBE with OCIO.")
    p.add_argument("-z", "--compressed", action="store_true", help="Write .clfz when --format clf")
    p.add_argument(
        "--input-mode",
        choices=["aces-ap0", "rec2020", "srgb", "prophoto", "state"],
        default="aces-ap0",
        help="LUT input colourspace policy. Default keeps the existing ACES AP0 linear LUT workflow.",
    )
    p.add_argument(
        "--output-mode",
        choices=["lut-default", "state"],
        default="lut-default",
        help="LUT output colourspace policy. Default outputs ACES2065-1 linear.",
    )
    p.add_argument("--input-gain", type=float, default=0.0, help="Generator-only gain baked into CUBE or written as CDL in CLF")
    p.add_argument("--output-black-offset", type=float, default=0.0, help="Generator-only black offset")
    p.add_argument("--auto-ym-shifts", action="store_true", help="Compatibility flag; not yet implemented in the state mapper")
    p.add_argument("--dry-run-report", action="store_true", help="Load and map the state, then print the report without writing a LUT")
    p.add_argument("--report-detail", action="store_true", help="Print the detailed applied/forced/ignored/unmapped mapping report")
    p.add_argument("--report-json", help="Optional path to write a detailed JSON report")
    return p.parse_args()




def bake_cube_with_ocio(clf_path: Path, cube_path: Path, args: argparse.Namespace, report: dict[str, Any]) -> None:
    """Bake a generated CLF to a Resolve-compatible CUBE using OCIO."""
    ocio_exe = args.ocio_bakelut
    if shutil.which(ocio_exe) is None and not Path(ocio_exe).expanduser().exists():
        raise SystemExit(
            "CUBE export now requires OpenColorIO's ociobakelut executable.\n"
            "Install OpenColorIO, or choose CLF output instead.\n\n"
            "macOS/Homebrew: brew install opencolorio\n"
            "Then check: ociobakelut --help"
        )

    cmd = [
        str(Path(ocio_exe).expanduser()) if Path(ocio_exe).expanduser().exists() else ocio_exe,
        "--lut", str(clf_path),
        "--format", "resolve_cube",
        "--cubesize", str(args.cube_size),
        str(cube_path),
    ]
    report["cube_bake"] = {
        "method": "ocio_from_clf",
        "ocio_bakelut": cmd[0],
        "cube_size": args.cube_size,
        "intermediate_clf": str(clf_path),
        "command": cmd,
    }
    proc = subprocess.run(cmd, text=True, capture_output=True)
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        raise SystemExit(f"ociobakelut failed with exit code {proc.returncode}")


def generate_native_clf(opts: Any, creator: StateLUTCreator) -> None:
    old_format = opts.format
    try:
        opts.format = "clf"
        creator(opts)
    finally:
        opts.format = old_format


def main() -> None:
    args = get_args()
    if args.compressed and args.format != "clf":
        raise SystemExit("--compressed only applies to --format clf")

    state_path = Path(args.state).expanduser()
    state = json.loads(state_path.read_text(encoding="utf-8"))

    opts = make_opts(args, state)
    Path(opts.output).expanduser().parent.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "state_path": str(state_path),
        "output": opts.output,
        "format": opts.format,
        "size": opts.size,
        "cube_export_method": "ocio_from_clf" if opts.format == "cube" else "native_clf",
        "cube_size": args.cube_size if opts.format == "cube" else None,
        "film_stock": get_nested(state, "simulation.film_stock"),
        "print_paper": get_nested(state, "simulation.print_paper"),
    }
    collect_ignored_and_unmapped(state, report)

    creator = StateLUTCreator(opts, state, report)

    if args.dry_run_report:
        # Force param construction/mapping so the report is meaningful.
        creator.get_params(opts)
    else:
        final_output = Path(opts.output).expanduser()
        if opts.format == "cube":
            # CUBE is now derived from the native/reference CLF via OCIO.
            # The old hand-written CUBE writer is intentionally bypassed.
            tmpdir = None
            if args.keep_intermediate_clf:
                clf_path = final_output.with_suffix(".intermediate.clf")
            else:
                tmpdir = tempfile.TemporaryDirectory(prefix="spektrafilm_cube_bake_")
                clf_path = Path(tmpdir.name) / (final_output.stem + ".clf")
            clf_opts = copy.copy(opts)
            clf_opts.output = str(clf_path)
            clf_opts.format = "clf"
            clf_creator = StateLUTCreator(clf_opts, state, report)
            generate_native_clf(clf_opts, clf_creator)
            bake_cube_with_ocio(clf_path, final_output, args, report)
            if tmpdir is not None:
                tmpdir.cleanup()
        else:
            generate_native_clf(opts, creator)
        report["created"] = str(final_output)

    if args.report_json:
        Path(args.report_json).expanduser().write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print_report(report, detail=args.report_detail)
    if not args.dry_run_report:
        print(f"\nDone: {Path(opts.output).expanduser()}")


if __name__ == "__main__":
    main()
