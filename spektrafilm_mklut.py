#!/usr/bin/env python3
#
# General-purpose Spektrafilm LUT generator.
#
# Generates ASC-CLF LUTs by running the Spektrafilm film simulation pipeline
# (see https://github.com/andreavolpato/spektrafilm).
#
# The old hand-written .cube writer has been removed from the normal workflow.
# CUBE export should be done by generating CLF first, then baking that CLF with
# OpenColorIO ociobakelut.

import os
os.environ['KMP_WARNINGS'] = 'off'

import numpy
import argparse
import gzip
import struct
import math
import sys
import json
import time
import io
import warnings
import copy
from pathlib import Path
try:
    from importlib import metadata as importlib_metadata
except Exception:
    importlib_metadata = None
from scipy.optimize import least_squares
from contextlib import redirect_stdout, redirect_stderr
try:
    from spektrafilm import photo_params, AgXPhoto
    from spektrafilm.model.stocks import FilmStocks, PrintPapers
    spektrafilm_legacy = False
except ImportError:
    from agx_emulsion.model.process import photo_params, AgXPhoto
    from agx_emulsion.model.stocks import FilmStocks, PrintPapers
    spektrafilm_legacy = True


def _enum(cls, *vals):
    res = []
    for v in vals:
        try:
            res.append(cls[v])
        except KeyError:
            pass
    return res

film_stocks = _enum(FilmStocks,
                    'kodak_portra_400',
                    'kodak_ultramax_400',
                    'kodak_gold_200',
                    'kodak_vision3_50d',
                    'fujifilm_pro_400h',
                    'fujifilm_xtra_400',
                    'fujifilm_c200',
                    'kodak_ektar_100',
                    'kodak_portra_160',
                    'kodak_portra_800',
                    'kodak_portra_800_push1',
                    'kodak_portra_800_push2',
                    'kodak_vision3_250d',
                    'kodak_vision3_200t',
                    'kodak_vision3_500t',
                    'kodak_ektachrome_100',
                    'kodak_kodachrome_64',
                    'fujifilm_velvia_100',
                    'fujifilm_provia_100f')
                    

print_papers = _enum(PrintPapers,
                     'kodak_endura_premier',
                     'kodak_ektacolor_edge',
                     'kodak_supra_endura',
                     'kodak_portra_endura',
                     'fujifilm_crystal_archive_typeii',
                     'kodak_2393',
                     'kodak_2383')
class NoPaper:
    def __init__(self):
        self.name = 'none'
        self.value = 'kodak_endura_premier' # arbitrary but valid
print_papers.append(NoPaper())


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SCHEMA_PATH = SCRIPT_DIR / 'control_schema.json'


INTERNAL_ATTR_NAMES = {
    "T", "mT", "ctypes", "data", "flags", "flat", "imag", "real",
    "base", "shape", "strides", "size", "nbytes", "ndim", "itemsize", "device",
}
INTERNAL_PATH_PARTS = INTERNAL_ATTR_NAMES | {
    "c_contiguous", "f_contiguous", "contiguous", "format", "readonly",
    "suboffsets", "aligned", "behaved", "carray", "farray", "fnc", "forc",
    "fortran", "num", "owndata", "writeable", "writebackifcopy", "coords",
    "index",
}
ARRAY_LIKE_TYPE_NAMES = {"ndarray", "Array", "Tensor"}


def _is_array_like(value):
    tname = type(value).__name__
    return (
        tname in ARRAY_LIKE_TYPE_NAMES
        or (hasattr(value, "shape") and hasattr(value, "dtype"))
        or (hasattr(value, "ndim") and hasattr(value, "shape") and hasattr(value, "size"))
    )


def _is_internal_path(path):
    parts = [p for p in path.split(".") if p]
    if any(p in INTERNAL_PATH_PARTS for p in parts):
        return True
    # Spektrafilm stock spectral/density data is large and not a GUI control.
    if len(parts) >= 2 and parts[0] in {"film", "print"} and parts[1] == "data":
        return True
    return False


def _array_summary(value):
    shape = getattr(value, "shape", None)
    dtype = getattr(value, "dtype", None)
    if shape is not None:
        return f"{type(value).__name__}(shape={tuple(shape)}, dtype={dtype})"
    return f"{type(value).__name__}"


def _json_safe_value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        if len(value) <= 12 and all(isinstance(x, (str, int, float, bool, type(None))) for x in value):
            return list(value)
        return f'{type(value).__name__}(len={len(value)})'
    if _is_array_like(value):
        return _array_summary(value)
    return repr(value)


def _walk_params(obj, prefix='', depth=0, max_depth=8, seen=None, include_internals=False, include_arrays=False):
    if seen is None:
        seen = set()
    if depth > max_depth:
        return []
    if prefix and not include_internals and _is_internal_path(prefix):
        return []

    oid = id(obj)
    if oid in seen:
        return []
    if not isinstance(obj, (str, int, float, bool, type(None))):
        seen.add(oid)

    if isinstance(obj, (str, int, float, bool, type(None))):
        return [{
            'path': prefix,
            'type': type(obj).__name__,
            'value': obj,
        }] if prefix else []

    if isinstance(obj, (list, tuple)):
        return [{
            'path': prefix,
            'type': type(obj).__name__,
            'value': _json_safe_value(obj),
        }] if prefix else []

    if _is_array_like(obj):
        if include_arrays and prefix:
            return [{
                'path': prefix,
                'type': type(obj).__name__,
                'value': _array_summary(obj),
            }]
        return []

    if isinstance(obj, dict):
        out = []
        for key, value in obj.items():
            child = f'{prefix}.{key}' if prefix else str(key)
            out.extend(_walk_params(value, child, depth + 1, max_depth, seen, include_internals, include_arrays))
        return out

    attrs = []
    for name in dir(obj):
        if name.startswith('_'):
            continue
        if not include_internals and name in INTERNAL_ATTR_NAMES:
            continue
        try:
            value = getattr(obj, name)
        except Exception:
            continue
        if callable(value):
            continue
        child = f'{prefix}.{name}' if prefix else name
        if not include_internals and _is_internal_path(child):
            continue
        attrs.append((name, value))

    if not attrs:
        if prefix and (include_internals or not _is_internal_path(prefix)):
            return [{
                'path': prefix,
                'type': type(obj).__name__,
                'value': _json_safe_value(obj),
            }]
        return []

    out = []
    for name, value in attrs:
        child = f'{prefix}.{name}' if prefix else name
        out.extend(_walk_params(value, child, depth + 1, max_depth, seen, include_internals, include_arrays))
    return out


def _backend_version():
    if importlib_metadata is None:
        return None
    for package_name in ('spektrafilm', 'agx-emulsion', 'agx_emulsion'):
        try:
            return importlib_metadata.version(package_name)
        except Exception:
            pass
    return None


def _backend_path():
    try:
        import spektrafilm
        return str(Path(spektrafilm.__file__).resolve())
    except Exception:
        try:
            import agx_emulsion
            return str(Path(agx_emulsion.__file__).resolve())
        except Exception:
            return None


def _enum_item_json(index, item):
    return {
        'index': index,
        'name': getattr(item, 'name', str(item)),
        'value': None if isinstance(item, NoPaper) else getattr(item, 'value', None),
    }


def load_schema(path=None):
    schema_path = Path(path).expanduser() if path else DEFAULT_SCHEMA_PATH
    if not schema_path.exists():
        return {'schema_version': 2, 'controls': []}
    with open(schema_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_control_values(path=None):
    if not path:
        return {}
    control_path = Path(path).expanduser()
    if not control_path.exists():
        raise SystemExit(f'Controls JSON not found: {control_path}')
    with open(control_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if isinstance(data, dict) and 'controls' in data and isinstance(data['controls'], dict):
        return data['controls']
    if isinstance(data, dict):
        return data
    raise SystemExit(f'Controls JSON must contain an object: {control_path}')


def coerce_value(value, spec):
    ctype = spec.get('type', 'float')
    if ctype == 'bool':
        if isinstance(value, str):
            return value.strip().lower() in ('1', 'true', 'yes', 'on')
        return bool(value)
    if ctype == 'int':
        return int(value)
    if ctype == 'float':
        return float(value)
    return value


def _get_child(obj, part):
    if isinstance(obj, dict):
        return obj[part]
    if isinstance(obj, (list, tuple)) and part.isdigit():
        return obj[int(part)]
    return getattr(obj, part)


def path_exists(root, path):
    try:
        obj = root
        for part in path.split('.'):
            obj = _get_child(obj, part)
        return True
    except Exception:
        return False


def set_by_path(root, path, value):
    parts = path.split('.')
    obj = root
    for part in parts[:-1]:
        obj = _get_child(obj, part)
    last = parts[-1]
    if isinstance(obj, dict):
        if last not in obj:
            raise AttributeError(path)
        obj[last] = value
    elif isinstance(obj, list) and last.isdigit():
        obj[int(last)] = value
    else:
        if not hasattr(obj, last):
            raise AttributeError(path)
        setattr(obj, last, value)


def first_existing_path(root, paths):
    for path in paths:
        if path and path_exists(root, path):
            return path
    return None


def apply_controls_to_opts(opts):
    opts.control_values = load_control_values(getattr(opts, 'controls_json', None))
    if not opts.control_values:
        return
    schema = load_schema(getattr(opts, 'schema', None))
    for spec in schema.get('controls', []):
        name = spec.get('name')
        if not name or name not in opts.control_values:
            continue
        option_name = spec.get('option', name)
        if not hasattr(opts, option_name):
            continue
        try:
            setattr(opts, option_name, coerce_value(opts.control_values[name], spec))
        except Exception as exc:
            raise SystemExit(f'Invalid value for control {name}: {exc}')


def apply_schema_controls_to_params(params, opts):
    values = getattr(opts, 'control_values', {}) or {}
    if not values:
        return
    schema = load_schema(getattr(opts, 'schema', None))
    warnings_out = []
    for spec in schema.get('controls', []):
        name = spec.get('name')
        if not name or name not in values:
            continue
        if spec.get('special'):
            continue
        path = spec.get('path')
        paths = []
        if path:
            paths.append(path)
        paths.extend(spec.get('fallback_paths', []) or [])
        if not paths:
            continue
        value = coerce_value(values[name], spec)
        target = first_existing_path(params, paths)
        if target is None:
            if not spec.get('optional', False):
                warnings_out.append(f'Control {name}: none of these paths exist: {paths}')
            continue
        try:
            set_by_path(params, target, value)
        except Exception as exc:
            warnings_out.append(f'Control {name}: could not set {target}: {exc}')
    for line in warnings_out:
        sys.stderr.write('warning: ' + line + '\n')


def capabilities_json(schema_path=None, inspect_params=True, include_internals=False):
    cap = {
        'backend': 'agx_emulsion' if spektrafilm_legacy else 'spektrafilm',
        'legacy_backend': bool(spektrafilm_legacy),
        'backend_version': _backend_version(),
        'backend_path': _backend_path(),
        'generator': Path(__file__).name,
        'formats': {'cube': 'via_ocio_in_state_wrapper', 'clf': True, 'clfz': True},
        'default_format': 'clf',
        'input_colorspaces': ['aces-ap0', 'rec2020', 'srgb'],
        'films': [_enum_item_json(i, s) for i, s in enumerate(film_stocks)],
        'papers': [_enum_item_json(i, s) for i, s in enumerate(print_papers)],
        'available_parameter_paths': [],
        'available_parameter_count': 0,
        'parameter_filter': 'filtered_controls_view' if not include_internals else 'include_internals',
        'schema_controls': [],
    }
    try:
        schema = load_schema(schema_path)
        cap['schema_controls'] = [
            {
                'name': c.get('name'),
                'label': c.get('label', c.get('name')),
                'type': c.get('type', 'float'),
                'path': c.get('path'),
                'fallback_paths': c.get('fallback_paths', []),
                'special': bool(c.get('special', False)),
            }
            for c in schema.get('controls', [])
        ]
    except Exception as exc:
        cap['schema_error'] = str(exc)

    if inspect_params and film_stocks and print_papers:
        try:
            params = photo_params(film_stocks[0].value, print_papers[0].value)
            paths = _walk_params(params, max_depth=8, include_internals=include_internals)
            cap['available_parameter_paths'] = paths
            cap['available_parameter_count'] = len(paths)
        except Exception as exc:
            cap['parameter_inspection_error'] = str(exc)
    return cap


def getopts():
    p = argparse.ArgumentParser()
    p.add_argument('-o', '--output')
    p.add_argument('-O', '--outdir', default='.')
    p.add_argument('-s', '--size', choices=['small', 'medium', 'large', 'huge'],
                   default='medium')
    p.add_argument('--format', choices=['clf'], default='clf',
                   help='Output format. Native output is CLF only. Use spektrafilm_state_to_lut.py --format cube to bake a CUBE via OCIO.')
    p.add_argument('--input-colorspace',
                   choices=['aces-ap0', 'rec2020', 'srgb'],
                   default='aces-ap0',
                   help='RGB space expected by the LUT. For scene-linear workflows, use aces-ap0.')
    p.add_argument('--cube-shaper-size', type=int, default=4096,
                   help='Number of samples in the .cube 1D shaper LUT.')
    p.add_argument('--cube-shaper-max', type=float, default=1.0,
                   help='Scene-linear maximum for the .cube 1D shaper input range. Default 1.0 is Resolve-friendly; old experimental behaviour used 100.0.')
    p.add_argument('--cube-mode', choices=['shaper', 'simple'], default='shaper',
                   help='CUBE writing mode. shaper preserves the HDR/scene-linear PQ shaper path; simple writes a plain 3D LUT over 0..1 for compatibility testing.')
    p.add_argument('--list-options', action='store_true',
                   help='Print available film and paper indices and exit.')
    p.add_argument('--capabilities', action='store_true',
                   help='Print backend capabilities, schema controls, and inspectable parameter paths as JSON, then exit.')
    p.add_argument('--capabilities-internals', action='store_true',
                   help='Include noisy internal/numpy paths in --capabilities output.')
    p.add_argument('--schema', default=str(DEFAULT_SCHEMA_PATH),
                   help='Path to control_schema.json for schema-driven controls.')
    p.add_argument('--controls-json', default=None,
                   help='JSON file containing schema control values to apply.')
    p.add_argument('-z', '--compressed', action='store_true',
                   help='Only applies to CLF output.')
    film_avail = f"Film stock to use. Options: " + \
        ", ".join(f'{i} : {s.name}' for (i, s) in enumerate(film_stocks))
    p.add_argument('-f', '--film', type=int, choices=range(len(film_stocks)),
                   help=film_avail, default=0)
    paper_avail = f"Print paper to use. Options: " + \
        ", ".join(f'{i} : {s.name}' for (i, s) in enumerate(print_papers))
    p.add_argument('-p', '--paper', type=int, choices=range(len(print_papers)),
                   default=0, help=paper_avail)
    p.add_argument('-e', '--camera-expcomp', type=float, default=0)
    p.add_argument('-E', '--print-exposure', type=float, default=1)
    p.add_argument('-g', '--input-gain', type=float, default=0)
    p.add_argument('--y-shift', type=float, default=0)
    p.add_argument('--m-shift', type=float, default=0)
    p.add_argument('--film-gamma', type=float, default=1)
    p.add_argument('--print-gamma', type=float, default=1)
    p.add_argument('--dir-couplers-amount', type=float, default=1)
    p.add_argument('--output-black-offset', type=float, default=0)
    p.add_argument('--gamut', choices=['srgb', 'rec2020'], default=None,
                   help='Legacy alias for --input-colorspace; kept for old ART JSON compatibility.')
    p.add_argument('--json', nargs=2)
    p.add_argument('--server', action='store_true')
    p.add_argument('--auto-ym-shifts', action='store_true')
    opts = p.parse_args()

    if opts.capabilities:
        print(json.dumps(capabilities_json(opts.schema, include_internals=opts.capabilities_internals), indent=2))
        sys.exit(0)

    if opts.list_options:
        print('Film stocks:')
        for i, s in enumerate(film_stocks):
            print(f'  {i:2d}: {s.name}')
        print('\nPrint papers:')
        for i, s in enumerate(print_papers):
            print(f'  {i:2d}: {s.name}')
        sys.exit(0)

    if opts.gamut is not None:
        opts.input_colorspace = opts.gamut

    if opts.compressed and opts.format != 'clf':
        raise SystemExit('--compressed only applies to CLF output')

    if opts.json:
        with open(opts.json[0]) as f:
            params = json.load(f)
        update_opts(opts, params, opts.json[1])

    apply_controls_to_opts(opts)

    if not opts.output:
        film = film_stocks[opts.film].name
        paper = print_papers[opts.paper].name
        safe_film = film.replace(' ', '_').replace('/', '-')
        safe_paper = paper.replace(' ', '_').replace('/', '-')
        name = f'{safe_film}@{safe_paper}.clf{"z" if opts.compressed else ""}'
        opts.output = os.path.join(opts.outdir, name)
    return opts


def update_opts(opts, params, output):
    opts.film = params.get("film", opts.film)
    opts.paper = params.get("paper", opts.paper)
    opts.camera_expcomp = params.get("camera_expcomp", opts.camera_expcomp)
    opts.print_exposure = params.get("print_exposure", opts.print_exposure)
    opts.input_gain = params.get("input_gain", opts.input_gain)
    opts.y_shift = params.get("y_shift", opts.y_shift)
    opts.m_shift = params.get("m_shift", opts.m_shift)
    opts.auto_ym_shifts = params.get("auto_ym_shifts", opts.auto_ym_shifts)
    opts.film_gamma = params.get("film_gamma", opts.film_gamma)
    opts.print_gamma = params.get("print_gamma", opts.print_gamma)
    opts.dir_couplers_amount = params.get("dir_couplers_amount",
                                          opts.dir_couplers_amount)
    opts.output_black_offset = params.get(
        "output_black_offset", opts.output_black_offset)
    opts.output = output


def srgb(a, inv):
    if not inv:
        a = numpy.fmax(numpy.fmin(a, 1.0), 0.0)
        return numpy.where(a <= 0.0031308,
                           12.92 * a,
                           1.055 * numpy.power(a, 1.0/2.4)-0.055)
    else:
        return numpy.where(a <= 0.04045, a / 12.92,
                           numpy.power((a + 0.055) / 1.055, 2.4))

def pq(a, inv):
    m1 = 2610.0 / 16384.0
    m2 = 2523.0 / 32.0
    c1 = 107.0 / 128.0
    c2 = 2413.0 / 128.0
    c3 = 2392.0 / 128.0
    scale = 100.0
    if not inv:
        # assume 1.0 is 100 nits, normalise so that 1.0 is 10000 nits
        a /= scale
        # apply the PQ curve
        aa = numpy.power(a, m1)
        res = numpy.power((c1 + c2 * aa)/(1.0 + c3 * aa), m2)
    else:
        p = numpy.power(a, 1.0/m2)
        aa = numpy.fmax(p-c1, 0.0) / (c2 - c3 * p)
        res = numpy.power(aa, 1.0/m1)
        res *= scale
    return res    



class LUTCreator:
    lutsize = {
        'small' : 16,
        'medium' : 36,
        'large' : 64,
        'huge' : 121
        }

    ap0_to_rec709 = """\
        <Matrix inBitDepth="32f" outBitDepth="32f" >
          <!-- ACES AP0 to Linear Rec.709 -->
          <Array dim="3 3">
            2.55128702 -1.11947013 -0.4318176
            -0.27586285  1.36601602 -0.09015301
            -0.01729251 -0.14852912  1.16582168
          </Array>
        </Matrix>   
    """.encode('utf-8')


    rec709_to_ap0 = """\
        <Matrix inBitDepth="32f" outBitDepth="32f" >
          <!-- Linear Rec.709 to ACES AP0-->
          <Array dim="3 3">
            0.43392843 0.3762503  0.18982151
            0.088802   0.81526168 0.09593625
            0.01775005 0.10944762 0.87280228
          </Array>
        </Matrix>   
    """.encode('utf-8')

    ap0_to_rec2020 = """\
        <Matrix inBitDepth="32f" outBitDepth="32f" >
          <!-- ACES AP0 to Rec2020-->
          <Array dim="3 3">
            1.50910172 -0.2589874  -0.2501146
            -0.07757638  1.17706684 -0.09949036
            0.0020526  -0.03114411  1.02909153
          </Array>
        </Matrix>   
    """.encode('utf-8')

    rec2020_to_ap0 = """\
        <Matrix inBitDepth="32f" outBitDepth="32f" >
          <!-- Linear Rec.2020 to ACES AP0-->
          <Array dim="3 3">
            0.67022657 0.15216775 0.17760585
            0.0441723  0.86177705 0.09405057
            0.0        0.02577705 0.97422293
          </Array>
        </Matrix>   
    """.encode('utf-8')

    def get_base_image(self, opts):
        dim = self.lutsize[opts.size]
        sz = complex(0, float(dim))
        table = numpy.mgrid[0.0:1.0:sz, 0.0:1.0:sz, 0.0:1.0:sz].reshape(3,-1).T
        n = int(math.sqrt(dim**3))
        data = table.reshape(-1)

        # CLF and the default CUBE path use a PQ-style shaper domain:
        #   LUT input linear scene value -> PQ shaper -> 3D table index.
        # For compatibility testing, --cube-mode simple instead samples the
        # Spektrafilm transform directly over 0..1 linear RGB and writes a
        # plain 3D .cube with no shaper. This is not as HDR-safe, but it avoids
        # host-specific interpretation of CUBE shaper input ranges.
        if getattr(opts, 'format', None) == 'cube' and getattr(opts, 'cube_mode', 'shaper') == 'simple':
            data = data.astype(numpy.float32)
        else:
            shaper = lambda a: pq(a, True)
            data = numpy.fromiter(map(shaper, data), dtype=numpy.float32)

        data = data.reshape(n, n, -1)
        return data

    def get_params(self, opts):
        params = photo_params(film_stocks[opts.film].value,
                              print_papers[opts.paper].value)
        params.camera.auto_exposure = False
        params.camera.auto_exposure_method = 'median'
        params.camera.exposure_compensation_ev = opts.camera_expcomp
        params.debug.deactivate_spatial_effects = True
        params.enlarger.lens_blur = 0
        params.enlarger.m_filter_shift = opts.m_shift
        params.enlarger.print_exposure = opts.print_exposure
        params.enlarger.print_exposure_compensation = True
        params.enlarger.y_filter_shift = opts.y_shift
        params.io.compute_negative = False
        params.io.crop = False
        params.io.full_image = True
        params.io.input_cctf_decoding = False
        params.io.output_cctf_encoding = False
        params.io.output_color_space = 'ACES2065-1'
        params.io.preview_resize_factor = 1.0
        params.io.upscale_factor = 1.0
        params.scanner.lens_blur = 0
        params.settings.use_camera_lut = False
        params.settings.use_enlarger_lut = False
        params.settings.use_scanner_lut = False
        if opts.input_colorspace == 'srgb':
            params.io.input_color_space = 'sRGB'
            params.settings.rgb_to_raw_method = 'mallett2019'
        elif opts.input_colorspace == 'rec2020':
            params.io.input_color_space = 'ITU-R BT.2020'
            params.settings.rgb_to_raw_method = 'hanatos2025'
        else:
            # ACES AP0 / ACES2065-1 linear RGB in, ACES AP0 linear RGB out.
            # This is the preferred mode for Resolve/ACES node workflows.
            params.io.input_color_space = 'ACES2065-1'
            params.settings.rgb_to_raw_method = 'hanatos2025'
        if spektrafilm_legacy:
            params.negative.data.tune.gamma_factor = opts.film_gamma
            params.negative.dir_couplers.active = opts.dir_couplers_amount > 0
            params.negative.dir_couplers.amount = opts.dir_couplers_amount
            params.negative.grain.active = False
            params.negative.halation.active = False
            params.negative.parametric.density_curves.active = False
            params.print_paper.data.tune.gamma_factor = opts.print_gamma
            params.print_paper.glare.active = False
            if isinstance(print_papers[opts.paper], NoPaper):
                params.io.compute_negative = True
        else:
            params.film_render.grain.active = False
            params.film_render.halation.active = False
            params.print_render.glare.active = False
            params.film_render.density_curve_gamma = opts.film_gamma
            params.film_render.dir_couplers.active = \
                opts.dir_couplers_amount > 0
            params.film_render.dir_couplers.amount = opts.dir_couplers_amount
            params.print_render.density_curve_gamma = opts.print_gamma
            params.scanner.unsharp_mask = (0.0, 0.0)
            params.debug.deactivate_stochastic_effects = True
            if isinstance(print_papers[opts.paper], NoPaper):
                params.io.scan_film = True

        apply_schema_controls_to_params(params, opts)

        if opts.auto_ym_shifts:
            key = self._key('autoshifts', opts)
            res = self.cache.get(key)
            if res is not None:
                y_shift, m_shift = res
            else:
                image = numpy.array([[
                    [0.184, 0.184, 0.184],
                ]])

                par = copy.copy(params)
                if spektrafilm_legacy:
                    par.debug.return_negative_density_cmy = True

                    photo = AgXPhoto(par)
                    density_cmy = photo.process(image)

                    def func(x):
                        y_shift, m_shift = x
                        photo.enlarger.y_filter_shift = y_shift
                        photo.enlarger.m_filter_shift = m_shift
                        log_raw = photo._expose_print(density_cmy)
                        print_cmy = photo._develop_print(log_raw)
                        out = photo._scan(print_cmy)
                        r, g, b = out.flatten()
                        return (abs(b-g), abs(r-g), abs(r-b))                    
                else:
                    photo = AgXPhoto(par)
                    density_cmy = photo.process(image)

                    def func(x):
                        y_shift, m_shift = x
                        photo = AgXPhoto(par)
                        photo.enlarger.y_filter_shift = y_shift
                        photo.enlarger.m_filter_shift = m_shift
                        out = photo.process(image)
                        r, g, b = out.flatten()
                        return (abs(b-g), abs(r-g), abs(r-b))

                start = time.time()
                res = least_squares(func, [0.0, 0.0],
                                    method='dogbox',
                                    bounds=[(-10, -10), (10, 10)],
                                    max_nfev=20)
                y_shift, m_shift = round(res.x[0], 3), round(res.x[1], 3)
                end = time.time()
                print(f'least_squares: {round(end - start, 2)}, '
                      f'y_shift: {y_shift}, m_shift: {m_shift}')

                if spektrafilm_legacy:
                    self.cache[key] = (y_shift, m_shift)
                
            params.enlarger.y_filter_shift = y_shift + opts.y_shift
            params.enlarger.m_filter_shift = m_shift + opts.m_shift
        
        return params


    def __init__(self, opts):
        self.cache = {}
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            self.image = self.get_base_image(opts)
            self.shaper = self.get_shaper()

    def _key(self, step, opts):
        keys = {
            'film' : ['film',
                      'camera_expcomp',
                      'film_gamma',
                      'dir_couplers_amount'],
            'full' : ['film',
                      'camera_expcomp',
                      'film_gamma',
                      'dir_couplers_amount',
                      'paper',
                      'print_exposure',
                      'y_shift',
                      'm_shift',
                      'print_gamma',
                      'auto_ym_shifts'],
            'autoshifts' : ['film',
                            'camera_expcomp',
                            'film_gamma',
                            'dir_couplers_amount',
                            'paper',
                            'print_exposure',
                            'print_gamma'],
        }
        d = {'step' : step}
        for k in keys[step]:
            d[k] = getattr(opts, k)
        return json.dumps(d)

    def _get(self, step, opts, photo, image):
        k = self._key(step, opts)
        res = self.cache.get(k)
        if res is None and step == 'film':
            photo.debug.return_negative_density_cmy = True
            res = photo.process(image)
            self.cache[k] = res
        return res

    def __call__(self, opts):
        start = time.time()
        params = self.get_params(opts)
        photo = AgXPhoto(params)
        def identity(rgb, *args, **kwds): return rgb
        if spektrafilm_legacy:
            photo.print_paper._apply_cctf_encoding_and_clip = identity
        # CLF keeps input_gain/output_black_offset as separate process nodes.
        # .cube output cannot store ASC CDL nodes, so bake those controls into
        # the sampled data instead.
        base_image = self.image
        if opts.format == 'cube' and opts.input_gain:
            base_image = base_image * math.pow(2, opts.input_gain)

        if spektrafilm_legacy:
            image = self._get('full', opts, photo, base_image)
            if image is None:
                image = self._get('film', opts, photo, base_image)
                log_raw = photo._expose_print(image)
                density_cmy = photo._develop_print(log_raw)
                image = photo._scan(density_cmy)
                self.cache[self._key('full', opts)] = image
        else:
            image = photo.process(base_image)

        if opts.format == 'cube' and opts.output_black_offset:
            bl = opts.output_black_offset * 2000.0 / 65535.0
            image = image + bl

        self.make_lut(opts, image)
        end = time.time()
        sys.stderr.write('total time: %.3f\n' % (end - start))

    def get_shaper(self):
        f = io.BytesIO()
        f.write(b'<LUT1D inBitDepth="32f" outBitDepth="32f" '
                b'halfDomain="true" rawHalfs="true">\n')
        f.write(b'  <Array dim="65536 1">\n')
        for i in range(65536):
            v = struct.unpack('e', struct.pack('H', i))[0]
            if math.isfinite(v) and v >= 0:
                o = pq(v, False)
            else:
                o = 0.0
            j = struct.unpack('H', struct.pack('e', o))[0]
            f.write(f'    {j}\n'.encode('utf-8'))
        f.write(b'  </Array>\n')
        f.write(b'</LUT1D>\n')            
        return f.getvalue()

    def make_lut(self, opts, data):
        if opts.format != 'clf':
            raise SystemExit('Native CUBE generation has been removed. Generate CLF, then bake CUBE with OCIO via spektrafilm_state_to_lut.py --format cube.')
        return self.make_clf_lut(opts, data)

    def make_clf_lut(self, opts, data):
        data = data.reshape(-1, 3)
        dim = int(round(math.pow(data.shape[0], 1.0/3.0)))
        fopen = open if not opts.compressed else gzip.open
        with fopen(opts.output, 'wb') as f:
            f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
            f.write(b'<ProcessList compCLFversion="3" id="1">\n')
            if opts.input_gain:
                f.write(b'<ASC_CDL inBitDepth="32f" outBitDepth="32f" '
                        b'style="FwdNoClamp">\n')
                f.write(b' <SOPNode>\n')
                g = math.pow(2, opts.input_gain)
                f.write(f'  <Slope>{g} {g} {g}</Slope>\n'.encode('utf-8'))
                f.write(b'  <Offset>0.0 0.0 0.0</Offset>\n')
                f.write(b'  <Power>1.0 1.0 1.0</Power>\n')
                f.write(b' </SOPNode>\n')
                f.write(b'</ASC_CDL>\n')
            if opts.input_colorspace == 'srgb':
                f.write(self.ap0_to_rec709)
            elif opts.input_colorspace == 'rec2020':
                f.write(self.ap0_to_rec2020)
            # For ACES AP0 input no matrix is needed.
            f.write(self.shaper)
            f.write(b'<LUT3D inBitDepth="32f" outBitDepth="32f" '
                    b'interpolation="tetrahedral">\n')
            f.write(f'  <Array dim="{dim} {dim} {dim} 3">\n'.encode('utf-8'))
            for rgb in data:
                f.write(('    %.8f  %.8f  %.8f\n' %
                         tuple(rgb)).encode('utf-8'))
            f.write(b'  </Array>\n')
            f.write(b'</LUT3D>\n')
            if opts.output_black_offset:
                f.write(b'<ASC_CDL inBitDepth="32f" outBitDepth="32f" '
                        b'style="FwdNoClamp">\n')
                f.write(b' <SOPNode>\n')
                bl = opts.output_black_offset * 2000.0 / 65535.0
                f.write(b'  <Slope>1.0 1.0 1.0</Slope>\n')
                f.write(f'  <Offset>{bl} {bl} {bl}</Offset>\n'.encode('utf-8'))
                f.write(b'  <Power>1.0 1.0 1.0</Power>\n')
                f.write(b' </SOPNode>\n')
                f.write(b'</ASC_CDL>\n')
            f.write(b'</ProcessList>\n')

    def make_cube_lut(self, opts, data):
        if getattr(opts, 'cube_mode', 'shaper') == 'simple':
            return self.make_simple_cube_lut(opts, data)

        """Write a shaper .cube.

        The 1D section maps scene-linear values from 0..cube_shaper_max into the same
        PQ-style 0..1 shaper domain used to create self.image. The 3D section
        then maps that shaper domain through Spektrafilm and outputs linear RGB.
        """
        data = data.reshape(-1, 3)
        dim = int(round(math.pow(data.shape[0], 1.0/3.0)))
        shaper_size = max(2, int(opts.cube_shaper_size))
        scene_max = float(getattr(opts, 'cube_shaper_max', 1.0))
        if not math.isfinite(scene_max) or scene_max <= 0:
            scene_max = 1.0
        film = film_stocks[opts.film].name
        paper = print_papers[opts.paper].name
        title = f'Spektrafilm {film} @ {paper} - {opts.input_colorspace} linear - cube-shaper max{scene_max:g}'

        with open(opts.output, 'w', encoding='utf-8', newline='\n') as f:
            f.write(f'TITLE "{title}"\n')
            f.write('# Generated by spektrafilm_mklut.py\n')
            f.write(f'# Input: {opts.input_colorspace} linear RGB\n')
            f.write('# Output: ACES AP0 / ACES2065-1 linear RGB\n')
            f.write(f'# 1D shaper maps scene-linear 0..{scene_max:g} through the script PQ shaper.\n')
            f.write('\n')

            # Use three identical columns
            # so each RGB channel is shaped independently into the 3D LUT domain.
            f.write(f'LUT_1D_SIZE {shaper_size}\n')
            f.write(f'LUT_1D_INPUT_RANGE 0.0 {scene_max:.8f}\n')
            for i in range(shaper_size):
                x = scene_max * i / (shaper_size - 1)
                y = float(pq(x, False))
                y = max(0.0, min(1.0, y))
                f.write(f'{y:.10f} {y:.10f} {y:.10f}\n')

            f.write('\n')
            f.write(f'LUT_3D_SIZE {dim}\n')
            f.write('DOMAIN_MIN 0.0 0.0 0.0\n')
            f.write('DOMAIN_MAX 1.0 1.0 1.0\n')
            for rgb in data:
                # Do not clamp. Resolve processes LUTs in float and this preserves
                # scene-linear/AP0 values produced by Spektrafilm.
                f.write('%.10f %.10f %.10f\n' % tuple(float(v) for v in rgb))

    def make_simple_cube_lut(self, opts, data):
        """Write a plain 3D .cube over 0..1 linear RGB.

        This mode deliberately avoids a 1D shaper. It is intended as a
        compatibility/diagnostic mode for hosts that do not interpret CUBE
        shaper input ranges the same way as CLF half-domain shapers.
        """
        data = data.reshape(-1, 3)
        dim = int(round(math.pow(data.shape[0], 1.0/3.0)))
        film = film_stocks[opts.film].name
        paper = print_papers[opts.paper].name
        title = f'Spektrafilm {film} @ {paper} - {opts.input_colorspace} linear - cube-shaper max{scene_max:g}'

        with open(opts.output, 'w', encoding='utf-8', newline='\n') as f:
            f.write(f'TITLE "{title}"\n')
            f.write('# Generated by spektrafilm_mklut.py\n')
            f.write('# CUBE mode: simple plain 3D, no 1D shaper\n')
            f.write(f'# Input: {opts.input_colorspace} linear RGB, expected domain 0..1\n')
            f.write('# Output: ACES AP0 / ACES2065-1 linear RGB\n')
            f.write('# This mode is for compatibility testing. CLF remains the recommended release format.\n')
            f.write('\n')
            f.write(f'LUT_3D_SIZE {dim}\n')
            f.write('DOMAIN_MIN 0.0 0.0 0.0\n')
            f.write('DOMAIN_MAX 1.0 1.0 1.0\n')
            for rgb in data:
                f.write('%.10f %.10f %.10f\n' % tuple(float(v) for v in rgb))

# end of class LUTCreator


def main():
    opts = getopts()
    if opts.server and opts.format != 'clf':
        raise SystemExit('--server mode is only supported for CLF/ART output')
    process = LUTCreator(opts)
    if opts.server:
        while True:
            p = sys.stdin.readline().strip()
            o = sys.stdin.readline().strip()
            with open(p) as f:
                params = json.load(f)
            update_opts(opts, params, o)
            buf = io.StringIO()
            with redirect_stdout(buf):
                with redirect_stderr(buf):
                    process(opts)
            data = buf.getvalue().splitlines()
            sys.stdout.write(f'Y {len(data)}\n')
            for line in data:
                sys.stdout.write(f'{line}\n')
            sys.stdout.flush()
    else:
        process(opts)


if __name__ == '__main__':
    main()
