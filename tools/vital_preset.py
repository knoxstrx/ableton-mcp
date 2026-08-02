"""Inspect and build narrowly-scoped Vital preset files.

Vital's ``.vital`` preset format is JSON. This tool deliberately transforms a
known-good user preset instead of trying to synthesize the whole document from
scratch. It refuses to overwrite output files, validates every changed path,
and emits a machine-readable report.

The first supported recipe is the H24 Clean Sub proof. More recipes should be
added only after this one loads successfully in Vital and is approved by ear.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


REQUIRED_TOP_LEVEL_KEYS = {
    "author",
    "comments",
    "macro1",
    "macro2",
    "macro3",
    "macro4",
    "preset_name",
    "preset_style",
    "settings",
    "synth_version",
}

EFFECT_SWITCHES = (
    "chorus_on",
    "compressor_on",
    "delay_on",
    "distortion_on",
    "eq_on",
    "filter_fx_on",
    "flanger_on",
    "phaser_on",
    "reverb_on",
)

# The values below describe the touched BASS-LAB Clean Sub state read from
# Live on 2026-08-02. Quartic envelope conversion is defined by Vital's
# ValueDetails::kQuartic parameter type: the stored value is seconds ** 0.25.
CLEAN_SUB_SECONDS = {
    "env_1_attack": 0.00544013,
    "env_1_decay": 0.47518,
    "env_1_release": 0.101598,
}
CLEAN_SUB_OSC_1_AUDIBLE_LEVEL = 0.662851

BASIC_SHAPES_SINE = 0.0
BASIC_SHAPES_SATURATED_SINE = 42.0
BASIC_SHAPES_TRIANGLE = 85.0
BASIC_SHAPES_SQUARE = 128.0
BASIC_SHAPES_SAW = 214.0


class PresetError(ValueError):
    """Raised when a preset cannot be transformed safely."""


@dataclass(frozen=True)
class ModulationSpec:
    source: str
    destination: str
    amount: float


@dataclass(frozen=True)
class PresetRecipe:
    slug: str
    preset_name: str
    comments: str
    settings: dict[str, float]
    modulations: tuple[ModulationSpec, ...] = ()
    macros: tuple[str, str, str, str] = (
        "MACRO 1",
        "MACRO 2",
        "MACRO 3",
        "MACRO 4",
    )


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_preset(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PresetError(f"Could not read Vital preset {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise PresetError("Vital preset root must be a JSON object")

    missing = sorted(REQUIRED_TOP_LEVEL_KEYS - data.keys())
    if missing:
        raise PresetError(f"Vital preset is missing top-level keys: {', '.join(missing)}")

    settings = data["settings"]
    if not isinstance(settings, dict):
        raise PresetError("Vital preset 'settings' must be a JSON object")
    if not isinstance(settings.get("modulations"), list):
        raise PresetError("Vital preset settings must contain a 'modulations' list")
    if not isinstance(settings.get("wavetables"), list):
        raise PresetError("Vital preset settings must contain a 'wavetables' list")
    if not isinstance(settings.get("lfos"), list):
        raise PresetError("Vital preset settings must contain an 'lfos' list")
    if not isinstance(settings.get("sample"), dict):
        raise PresetError("Vital preset settings must contain a 'sample' object")

    return data


def quartic_control_for_seconds(seconds: float) -> float:
    """Convert displayed seconds to Vital's stored quartic control value."""
    if seconds < 0:
        raise PresetError("Envelope time cannot be negative")
    return math.sqrt(math.sqrt(seconds))


def exponential_control_for_seconds(seconds: float) -> float:
    """Convert displayed seconds to Vital's stored base-2 exponential value."""
    if seconds <= 0:
        raise PresetError("Exponential time must be greater than zero")
    return math.log2(seconds)


def level_control_for_audible_level(level: float) -> float:
    """Convert a 0..1 audible level to Vital's stored quadratic control."""
    if not 0.0 <= level <= 1.0:
        raise PresetError("Audible oscillator level must be between zero and one")
    return math.sqrt(level)


def cutoff_control_for_hz(frequency_hz: float) -> float:
    """Convert Hz to Vital's MIDI-note-style filter cutoff control."""
    if frequency_hz <= 0:
        raise PresetError("Filter cutoff frequency must be greater than zero")
    return 69.0 + 12.0 * math.log2(frequency_hz / 440.0)


def _require_setting_keys(settings: dict[str, Any], names: Iterable[str]) -> None:
    missing = sorted(name for name in names if name not in settings)
    if missing:
        raise PresetError(f"Source preset is missing required settings: {', '.join(missing)}")


def _empty_modulation_routes(routes: list[Any]) -> list[dict[str, str]]:
    if not routes:
        raise PresetError("Source preset has no modulation slots")
    for index, route in enumerate(routes, start=1):
        if not isinstance(route, dict):
            raise PresetError(f"Modulation slot {index} is not a JSON object")
        if "source" not in route or "destination" not in route:
            raise PresetError(f"Modulation slot {index} lacks source/destination")
    return [{"source": "", "destination": ""} for _ in routes]


def _active_modulation_count(routes: list[Any]) -> int:
    return sum(
        1
        for route in routes
        if isinstance(route, dict) and route.get("source") and route.get("destination")
    )


def clean_sub_setting_updates(source_settings: dict[str, Any]) -> dict[str, float]:
    modulation_count = len(source_settings["modulations"])
    updates: dict[str, float] = {
        "osc_1_on": 1.0,
        "osc_1_level": math.sqrt(CLEAN_SUB_OSC_1_AUDIBLE_LEVEL),
        "osc_1_pan": 0.0,
        "osc_1_destination": 0.0,
        "osc_1_transpose": 0.0,
        "osc_1_tune": 0.0,
        "osc_1_unison_voices": 1.0,
        "osc_1_phase": 0.5,
        "osc_1_random_phase": 0.0,
        "osc_1_wave_frame": 0.0,
        "osc_1_distortion_type": 0.0,
        "osc_1_spectral_morph_type": 0.0,
        "osc_2_on": 0.0,
        "osc_3_on": 0.0,
        "sample_on": 0.0,
        "filter_1_on": 0.0,
        "filter_2_on": 0.0,
        "env_1_delay": 0.0,
        "env_1_attack": quartic_control_for_seconds(CLEAN_SUB_SECONDS["env_1_attack"]),
        "env_1_hold": 0.0,
        "env_1_decay": quartic_control_for_seconds(CLEAN_SUB_SECONDS["env_1_decay"]),
        "env_1_sustain": 1.0,
        "env_1_release": quartic_control_for_seconds(CLEAN_SUB_SECONDS["env_1_release"]),
        "env_1_attack_power": 0.0,
        "env_1_decay_power": -2.0,
        "env_1_release_power": -2.0,
        "polyphony": 1.0,
        "legato": 0.0,
        "portamento_force": 0.0,
        "portamento_scale": 0.0,
        "portamento_time": -10.0,
        "macro_control_1": 0.0,
        "macro_control_2": 0.0,
        "macro_control_3": 0.0,
        "macro_control_4": 0.0,
        "mpe_enabled": 0.0,
    }
    updates.update({name: 0.0 for name in EFFECT_SWITCHES})
    updates.update({f"modulation_{index}_amount": 0.0 for index in range(1, modulation_count + 1)})
    return updates


def _oscillator_updates(
    oscillator: int,
    *,
    wave_frame: float,
    audible_level: float,
    transpose: float = 0.0,
    tune: float = 0.0,
    pan: float = 0.0,
) -> dict[str, float]:
    prefix = f"osc_{oscillator}"
    return {
        f"{prefix}_on": 1.0,
        f"{prefix}_level": level_control_for_audible_level(audible_level),
        f"{prefix}_pan": pan,
        f"{prefix}_destination": 0.0,
        f"{prefix}_transpose": transpose,
        f"{prefix}_tune": tune,
        f"{prefix}_unison_voices": 1.0,
        f"{prefix}_phase": 0.5,
        f"{prefix}_random_phase": 0.0,
        f"{prefix}_wave_frame": wave_frame,
        f"{prefix}_distortion_type": 0.0,
        f"{prefix}_spectral_morph_type": 0.0,
    }


def _envelope_updates(
    envelope: int,
    *,
    attack: float,
    decay: float,
    sustain: float,
    release: float,
) -> dict[str, float]:
    prefix = f"env_{envelope}"
    return {
        f"{prefix}_delay": 0.0,
        f"{prefix}_attack": quartic_control_for_seconds(attack),
        f"{prefix}_hold": 0.0,
        f"{prefix}_decay": quartic_control_for_seconds(decay),
        f"{prefix}_sustain": sustain,
        f"{prefix}_release": quartic_control_for_seconds(release),
        f"{prefix}_attack_power": 0.0,
        f"{prefix}_decay_power": -2.0,
        f"{prefix}_release_power": -2.0,
    }


def _filter_updates(
    *,
    model: float,
    style: float,
    cutoff_hz: float,
    resonance: float,
    drive_db: float,
    keytrack: float = 0.0,
) -> dict[str, float]:
    return {
        "filter_1_on": 1.0,
        "filter_1_model": model,
        "filter_1_style": style,
        "filter_1_cutoff": cutoff_control_for_hz(cutoff_hz),
        "filter_1_resonance": resonance,
        "filter_1_drive": drive_db,
        "filter_1_mix": 1.0,
        "filter_1_keytrack": keytrack,
        "filter_1_blend": 0.0,
    }


ADDITIONAL_RECIPES: dict[str, PresetRecipe] = {
    "short-pluck": PresetRecipe(
        slug="short-pluck",
        preset_name="KDB Bass 03 - Short Pluck",
        comments="Short saw-and-sine bass pluck with a fast low-pass filter envelope.",
        settings={
            **_oscillator_updates(1, wave_frame=BASIC_SHAPES_SAW, audible_level=0.42),
            **_oscillator_updates(2, wave_frame=BASIC_SHAPES_SINE, audible_level=0.16),
            **_filter_updates(
                model=0.0, style=1.0, cutoff_hz=115.0, resonance=0.18, drive_db=3.0
            ),
            **_envelope_updates(
                1, attack=0.002, decay=0.22, sustain=0.0, release=0.08
            ),
            **_envelope_updates(
                2, attack=0.0, decay=0.13, sustain=0.0, release=0.06
            ),
            "legato": 0.0,
            "portamento_time": -10.0,
        },
        modulations=(
            ModulationSpec("env_2", "filter_1_cutoff", 32.0 / 128.0),
        ),
    ),
    "dark-reese": PresetRecipe(
        slug="dark-reese",
        preset_name="KDB Bass 04 - Dark Reese",
        comments=(
            "Two subtly detuned saws through a dark 24 dB low-pass filter. "
            "Macros add tone, slow motion, dirt and optional width."
        ),
        settings={
            **_oscillator_updates(
                1,
                wave_frame=BASIC_SHAPES_SAW,
                audible_level=0.27,
                tune=-0.07,
                pan=-0.12,
            ),
            **_oscillator_updates(
                2,
                wave_frame=BASIC_SHAPES_SAW,
                audible_level=0.27,
                tune=0.07,
                pan=0.12,
            ),
            **_filter_updates(
                model=0.0, style=1.0, cutoff_hz=650.0, resonance=0.14, drive_db=2.0
            ),
            **_envelope_updates(
                1, attack=0.005, decay=0.8, sustain=1.0, release=0.22
            ),
            "lfo_1_sync": 1.0,
            "lfo_1_sync_type": 1.0,
            "lfo_1_tempo": 5.0,
            "lfo_1_phase": 0.0,
            "distortion_on": 1.0,
            "distortion_type": 0.0,
            "distortion_drive": 5.0,
            "distortion_mix": 0.0,
            "distortion_filter_order": 0.0,
            "legato": 1.0,
            "portamento_time": exponential_control_for_seconds(0.18),
        },
        modulations=(
            ModulationSpec("macro_control_1", "filter_1_cutoff", 24.0 / 128.0),
            ModulationSpec("lfo_1", "filter_1_cutoff", 0.0),
            ModulationSpec("macro_control_2", "modulation_2_amount", 10.0 / 128.0),
            ModulationSpec("macro_control_3", "filter_1_drive", 0.25),
            ModulationSpec("macro_control_3", "distortion_mix", 0.30),
            ModulationSpec("macro_control_4", "osc_1_pan", -0.24),
            ModulationSpec("macro_control_4", "osc_2_pan", 0.24),
        ),
        macros=("TONE", "MOTION", "DIRT", "WIDTH"),
    ),
    "driven-mid": PresetRecipe(
        slug="driven-mid",
        preset_name="KDB Bass 05 - Driven Mid Bass",
        comments="Saw plus octave square through a driven filter and parallel soft clip.",
        settings={
            **_oscillator_updates(1, wave_frame=BASIC_SHAPES_SAW, audible_level=0.24),
            **_oscillator_updates(
                2,
                wave_frame=BASIC_SHAPES_SQUARE,
                audible_level=0.14,
                transpose=12.0,
                tune=0.03,
            ),
            **_filter_updates(
                model=1.0, style=0.0, cutoff_hz=1200.0, resonance=0.12, drive_db=7.0
            ),
            **_envelope_updates(
                1, attack=0.003, decay=0.45, sustain=0.82, release=0.12
            ),
            "distortion_on": 1.0,
            "distortion_type": 0.0,
            "distortion_drive": 6.0,
            "distortion_mix": 0.35,
            "distortion_filter_order": 0.0,
            "legato": 0.0,
            "portamento_time": -10.0,
        },
    ),
    "long-808": PresetRecipe(
        slug="long-808",
        preset_name="KDB Bass 06 - Long 808",
        comments="Long-decay sine 808 with a short pitch drop, subtle harmonics and legato glide.",
        settings={
            **_oscillator_updates(1, wave_frame=BASIC_SHAPES_SINE, audible_level=0.55),
            **_oscillator_updates(
                2, wave_frame=BASIC_SHAPES_SATURATED_SINE, audible_level=0.08
            ),
            **_envelope_updates(
                1, attack=0.002, decay=1.8, sustain=0.0, release=0.25
            ),
            **_envelope_updates(
                2, attack=0.0, decay=0.055, sustain=0.0, release=0.0
            ),
            "distortion_on": 1.0,
            "distortion_type": 0.0,
            "distortion_drive": 3.0,
            "distortion_mix": 0.12,
            "distortion_filter_order": 0.0,
            "legato": 1.0,
            "portamento_time": exponential_control_for_seconds(0.08),
        },
        modulations=(
            ModulationSpec("env_2", "osc_1_transpose", 12.0 / 96.0),
            ModulationSpec("env_2", "osc_2_transpose", 12.0 / 96.0),
        ),
    ),
    "acid-pulse": PresetRecipe(
        slug="acid-pulse",
        preset_name="KDB Bass 07 - Acid Pulse",
        comments="Monophonic saw through a resonant ladder filter with a fast acid envelope.",
        settings={
            **_oscillator_updates(1, wave_frame=BASIC_SHAPES_SAW, audible_level=0.38),
            **_filter_updates(
                model=2.0,
                style=1.0,
                cutoff_hz=160.0,
                resonance=0.62,
                drive_db=5.5,
                keytrack=0.25,
            ),
            **_envelope_updates(
                1, attack=0.002, decay=0.30, sustain=0.45, release=0.08
            ),
            **_envelope_updates(
                2, attack=0.0, decay=0.18, sustain=0.0, release=0.08
            ),
            "distortion_on": 1.0,
            "distortion_type": 0.0,
            "distortion_drive": 4.0,
            "distortion_mix": 0.18,
            "distortion_filter_order": 0.0,
            "legato": 1.0,
            "portamento_time": exponential_control_for_seconds(0.05),
        },
        modulations=(
            ModulationSpec("env_2", "filter_1_cutoff", 42.0 / 128.0),
        ),
    ),
}


def build_clean_sub(source: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    """Return a Clean Sub clone plus the exact path allow-list."""
    result = copy.deepcopy(source)
    result.update(
        {
            "preset_name": "KDB Bass 01 - Clean Sub",
            "preset_style": "Bass",
            "comments": "Clean sine sub; mono; no filters, modulation, glide or effects.",
            "macro1": "MACRO 1",
            "macro2": "MACRO 2",
            "macro3": "MACRO 3",
            "macro4": "MACRO 4",
        }
    )

    settings = result["settings"]
    updates = clean_sub_setting_updates(settings)
    _require_setting_keys(settings, updates)
    settings.update(updates)
    settings["modulations"] = _empty_modulation_routes(settings["modulations"])

    allowed_paths = {
        "preset_name",
        "preset_style",
        "comments",
        "macro1",
        "macro2",
        "macro3",
        "macro4",
        "settings.modulations",
    }
    allowed_paths.update(f"settings.{name}" for name in updates)

    validate_clean_sub(result)
    assert_only_allowed_changes(source, result, allowed_paths)
    return result, allowed_paths


def validate_clean_sub(preset: dict[str, Any]) -> None:
    settings = preset["settings"]
    expected = clean_sub_setting_updates(settings)
    for name, wanted in expected.items():
        actual = settings.get(name)
        if not isinstance(actual, (int, float)) or not math.isclose(actual, wanted, abs_tol=1e-12):
            raise PresetError(f"Clean Sub invariant failed: {name}={actual!r}, wanted {wanted!r}")

    active = _active_modulation_count(settings["modulations"])
    if active:
        raise PresetError(f"Clean Sub must have zero active modulations, found {active}")
    if preset["preset_name"] != "KDB Bass 01 - Clean Sub":
        raise PresetError("Clean Sub preset name invariant failed")


def recipe_setting_updates(
    source_settings: dict[str, Any], recipe: PresetRecipe
) -> dict[str, float]:
    updates = clean_sub_setting_updates(source_settings)
    updates.update(recipe.settings)
    for index, modulation in enumerate(recipe.modulations, start=1):
        updates[f"modulation_{index}_amount"] = modulation.amount
        for suffix in ("power", "bipolar", "stereo", "bypass"):
            name = f"modulation_{index}_{suffix}"
            if name in source_settings:
                updates[name] = 0.0
    return updates


def _routes_for_recipe(routes: list[Any], recipe: PresetRecipe) -> list[dict[str, str]]:
    result = _empty_modulation_routes(routes)
    if len(recipe.modulations) > len(result):
        raise PresetError(
            f"Recipe {recipe.slug} needs {len(recipe.modulations)} modulation slots, "
            f"source provides {len(result)}"
        )
    for index, modulation in enumerate(recipe.modulations):
        result[index] = {
            "source": modulation.source,
            "destination": modulation.destination,
        }
    return result


def build_recipe(
    source: dict[str, Any], recipe: PresetRecipe
) -> tuple[dict[str, Any], set[str]]:
    """Build an allow-listed preset recipe from a known-good neutral seed."""
    result = copy.deepcopy(source)
    result.update(
        {
            "preset_name": recipe.preset_name,
            "preset_style": "Bass",
            "comments": recipe.comments,
            "macro1": recipe.macros[0],
            "macro2": recipe.macros[1],
            "macro3": recipe.macros[2],
            "macro4": recipe.macros[3],
        }
    )

    settings = result["settings"]
    updates = recipe_setting_updates(settings, recipe)
    _require_setting_keys(settings, updates)
    settings.update(updates)
    settings["modulations"] = _routes_for_recipe(settings["modulations"], recipe)

    allowed_paths = {
        "preset_name",
        "preset_style",
        "comments",
        "macro1",
        "macro2",
        "macro3",
        "macro4",
        "settings.modulations",
    }
    allowed_paths.update(f"settings.{name}" for name in updates)

    validate_recipe(result, recipe)
    assert_only_allowed_changes(source, result, allowed_paths)
    return result, allowed_paths


def validate_recipe(preset: dict[str, Any], recipe: PresetRecipe) -> None:
    settings = preset["settings"]
    expected = recipe_setting_updates(settings, recipe)
    for name, wanted in expected.items():
        actual = settings.get(name)
        if not isinstance(actual, (int, float)) or not math.isclose(actual, wanted, abs_tol=1e-12):
            raise PresetError(
                f"Recipe {recipe.slug} invariant failed: {name}={actual!r}, wanted {wanted!r}"
            )

    wanted_routes = _routes_for_recipe(settings["modulations"], recipe)
    if settings["modulations"] != wanted_routes:
        raise PresetError(f"Recipe {recipe.slug} modulation routes failed validation")
    if preset["preset_name"] != recipe.preset_name:
        raise PresetError(f"Recipe {recipe.slug} preset name invariant failed")
    actual_macros = tuple(preset[f"macro{index}"] for index in range(1, 5))
    if actual_macros != recipe.macros:
        raise PresetError(
            f"Recipe {recipe.slug} macro labels failed validation: {actual_macros!r}"
        )


def changed_paths(before: Any, after: Any, prefix: str = "") -> set[str]:
    if before == after:
        return set()
    if isinstance(before, dict) and isinstance(after, dict):
        result: set[str] = set()
        for key in before.keys() | after.keys():
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                result.add(child)
            else:
                result.update(changed_paths(before[key], after[key], child))
        return result
    if isinstance(before, list) and isinstance(after, list):
        return {prefix}
    return {prefix}


def assert_only_allowed_changes(
    before: dict[str, Any], after: dict[str, Any], allowed_paths: set[str]
) -> None:
    unexpected = sorted(changed_paths(before, after) - allowed_paths)
    if unexpected:
        raise PresetError(f"Transformer changed non-allow-listed paths: {', '.join(unexpected)}")


def scalar_change_report(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for path in sorted(changed_paths(before, after)):
        if path == "settings.modulations":
            old_routes = before["settings"]["modulations"]
            new_routes = after["settings"]["modulations"]
            changes.append(
                {
                    "path": path,
                    "before": {
                        "slots": len(old_routes),
                        "active": _active_modulation_count(old_routes),
                    },
                    "after": {
                        "slots": len(new_routes),
                        "active": _active_modulation_count(new_routes),
                    },
                }
            )
            continue

        old: Any = before
        new: Any = after
        for part in path.split("."):
            old = old[part]
            new = new[part]
        changes.append({"path": path, "before": old, "after": new})
    return changes


def write_json_exclusive(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise PresetError(f"Refusing to overwrite existing file: {path}") from exc


def write_report_exclusive(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise PresetError(f"Refusing to overwrite existing report: {path}") from exc


def build_clean_sub_files(source_path: Path, output_path: Path, report_path: Path) -> dict[str, Any]:
    if output_path.exists():
        raise PresetError(f"Refusing to overwrite existing file: {output_path}")
    if report_path.exists():
        raise PresetError(f"Refusing to overwrite existing report: {report_path}")

    source = load_preset(source_path)
    source_hash = sha256_path(source_path)
    result, allowed_paths = build_clean_sub(source)

    if output_path.stem != result["preset_name"]:
        raise PresetError(
            f"Output filename must match preset_name: expected '{result['preset_name']}.vital'"
        )
    if output_path.suffix.lower() != ".vital":
        raise PresetError("Output file must use the .vital extension")

    write_json_exclusive(output_path, result)
    reloaded = load_preset(output_path)
    validate_clean_sub(reloaded)
    assert_only_allowed_changes(source, reloaded, allowed_paths)

    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "recipe": "kdb_bass_01_clean_sub_v1",
        "source": {
            "path": str(source_path.resolve()),
            "sha256": source_hash,
            "preset_name": source["preset_name"],
            "synth_version": source["synth_version"],
        },
        "output": {
            "path": str(output_path.resolve()),
            "sha256": sha256_path(output_path),
            "preset_name": reloaded["preset_name"],
            "synth_version": reloaded["synth_version"],
            "bytes": output_path.stat().st_size,
        },
        "target_display_values": {
            "osc_1_level": CLEAN_SUB_OSC_1_AUDIBLE_LEVEL,
            **CLEAN_SUB_SECONDS,
        },
        "invariants": {
            "non_overwriting": True,
            "only_allow_listed_paths_changed": True,
            "sample_unchanged": source["settings"]["sample"] == reloaded["settings"]["sample"],
            "wavetables_unchanged": source["settings"]["wavetables"]
            == reloaded["settings"]["wavetables"],
            "lfos_unchanged": source["settings"]["lfos"] == reloaded["settings"]["lfos"],
            "active_modulations": _active_modulation_count(reloaded["settings"]["modulations"]),
        },
        "changes": scalar_change_report(source, reloaded),
    }
    write_report_exclusive(report_path, report)
    return report


def build_recipe_files(
    source_path: Path,
    output_path: Path,
    report_path: Path,
    recipe: PresetRecipe,
) -> dict[str, Any]:
    if output_path.exists():
        raise PresetError(f"Refusing to overwrite existing file: {output_path}")
    if report_path.exists():
        raise PresetError(f"Refusing to overwrite existing report: {report_path}")

    source = load_preset(source_path)
    source_hash = sha256_path(source_path)
    result, allowed_paths = build_recipe(source, recipe)

    if output_path.stem != result["preset_name"]:
        raise PresetError(
            f"Output filename must match preset_name: expected '{result['preset_name']}.vital'"
        )
    if output_path.suffix.lower() != ".vital":
        raise PresetError("Output file must use the .vital extension")

    write_json_exclusive(output_path, result)
    reloaded = load_preset(output_path)
    validate_recipe(reloaded, recipe)
    assert_only_allowed_changes(source, reloaded, allowed_paths)

    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "recipe": recipe.slug,
        "source": {
            "path": str(source_path.resolve()),
            "sha256": source_hash,
            "preset_name": source["preset_name"],
            "synth_version": source["synth_version"],
        },
        "output": {
            "path": str(output_path.resolve()),
            "sha256": sha256_path(output_path),
            "preset_name": reloaded["preset_name"],
            "synth_version": reloaded["synth_version"],
            "bytes": output_path.stat().st_size,
        },
        "recipe_settings": recipe.settings,
        "recipe_modulations": [
            {
                "source": modulation.source,
                "destination": modulation.destination,
                "amount": modulation.amount,
            }
            for modulation in recipe.modulations
        ],
        "recipe_macros": list(recipe.macros),
        "invariants": {
            "non_overwriting": True,
            "only_allow_listed_paths_changed": True,
            "sample_unchanged": source["settings"]["sample"] == reloaded["settings"]["sample"],
            "wavetables_unchanged": source["settings"]["wavetables"]
            == reloaded["settings"]["wavetables"],
            "lfos_unchanged": source["settings"]["lfos"] == reloaded["settings"]["lfos"],
            "active_modulations": _active_modulation_count(reloaded["settings"]["modulations"]),
        },
        "changes": scalar_change_report(source, reloaded),
    }
    write_report_exclusive(report_path, report)
    return report


def inspect_preset(path: Path) -> dict[str, Any]:
    preset = load_preset(path)
    settings = preset["settings"]
    return {
        "path": str(path.resolve()),
        "sha256": sha256_path(path),
        "bytes": path.stat().st_size,
        "preset_name": preset["preset_name"],
        "author": preset["author"],
        "preset_style": preset["preset_style"],
        "synth_version": preset["synth_version"],
        "settings": len(settings),
        "modulation_slots": len(settings["modulations"]),
        "active_modulations": _active_modulation_count(settings["modulations"]),
        "wavetables": len(settings["wavetables"]),
        "lfos": len(settings["lfos"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect = subparsers.add_parser("inspect", help="Validate and summarize a .vital preset")
    inspect.add_argument("preset", type=Path)

    clean = subparsers.add_parser("clean-sub", help="Build the H24 Clean Sub proof preset")
    clean.add_argument("--source", required=True, type=Path)
    clean.add_argument("--output", required=True, type=Path)
    clean.add_argument("--report", required=True, type=Path)

    build = subparsers.add_parser("build", help="Build one named KDB bass preset recipe")
    build.add_argument("--recipe", required=True, choices=sorted(ADDITIONAL_RECIPES))
    build.add_argument("--source", required=True, type=Path)
    build.add_argument("--output", required=True, type=Path)
    build.add_argument("--report", required=True, type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "inspect":
            result = inspect_preset(args.preset)
        elif args.command == "clean-sub":
            result = build_clean_sub_files(args.source, args.output, args.report)
        else:
            result = build_recipe_files(
                args.source,
                args.output,
                args.report,
                ADDITIONAL_RECIPES[args.recipe],
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except PresetError as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
