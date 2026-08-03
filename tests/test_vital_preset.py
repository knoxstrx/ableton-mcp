import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from tools.vital_preset import (
    ADDITIONAL_RECIPES,
    CLEAN_SUB_OSC_1_AUDIBLE_LEVEL,
    CLEAN_SUB_SECONDS,
    EFFECT_SWITCHES,
    PresetError,
    build_clean_sub,
    build_clean_sub_files,
    build_recipe,
    build_recipe_files,
    changed_paths,
    cutoff_control_for_hz,
    exponential_control_for_seconds,
    level_control_for_audible_level,
    quartic_control_for_seconds,
    unison_detune_control_for_percent,
)


def fixture() -> dict:
    modulation_slots = 64
    settings = {
        "modulations": [
            {"source": "env_2", "destination": "filter_1_cutoff"},
            *(
                {"source": "", "destination": ""}
                for _ in range(modulation_slots - 1)
            ),
        ],
        "wavetables": [{"opaque": "wave-data"}],
        "lfos": [{"opaque": "lfo-data"}],
        "sample": {"opaque": "sample-data"},
        "osc_1_on": 1.0,
        "osc_1_level": 0.82,
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
        "osc_2_on": 1.0,
        "osc_3_on": 0.0,
        "sample_on": 0.0,
        "filter_1_on": 1.0,
        "filter_2_on": 0.0,
        "env_1_delay": 0.2,
        "env_1_attack": 0.2,
        "env_1_hold": 0.0,
        "env_1_decay": 0.8,
        "env_1_sustain": 0.8,
        "env_1_release": 0.6,
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
    settings.update({name: 0.0 for name in EFFECT_SWITCHES})
    settings.update(
        {
            f"modulation_{index}_amount": 0.15 if index == 1 else 0.0
            for index in range(1, modulation_slots + 1)
        }
    )
    for recipe in ADDITIONAL_RECIPES.values():
        for name in recipe.settings:
            settings.setdefault(name, 0.0)
    return {
        "author": "Kilian Douglas Brune",
        "comments": "",
        "macro1": "TONE",
        "macro2": "SHAPE",
        "macro3": "DIRT",
        "macro4": "CHARACTER",
        "preset_name": "KDB Bass 02 - Warm Mono",
        "preset_style": "Bass",
        "settings": settings,
        "synth_version": "1.6.4",
    }


class VitalPresetTests(unittest.TestCase):
    def test_quartic_control_round_trip(self):
        for seconds in (0.0, 0.00544013, 0.101598, 0.47518):
            control = quartic_control_for_seconds(seconds)
            self.assertTrue(math.isclose(control**4, seconds, abs_tol=1e-12))

    def test_other_parameter_conversions(self):
        self.assertTrue(math.isclose(2 ** exponential_control_for_seconds(0.08), 0.08))
        self.assertTrue(math.isclose(level_control_for_audible_level(0.38) ** 2, 0.38))
        self.assertTrue(math.isclose(unison_detune_control_for_percent(7.0) ** 2, 7.0))
        self.assertTrue(math.isclose(cutoff_control_for_hz(440.0), 69.0))

    def test_clean_sub_changes_only_allow_list(self):
        source = fixture()
        original = copy.deepcopy(source)
        result, allowed = build_clean_sub(source)

        self.assertEqual(source, original)
        self.assertFalse(changed_paths(source, result) - allowed)
        self.assertEqual(result["settings"]["wavetables"], source["settings"]["wavetables"])
        self.assertEqual(result["settings"]["sample"], source["settings"]["sample"])
        self.assertEqual(result["settings"]["lfos"], source["settings"]["lfos"])
        self.assertEqual(result["settings"]["osc_2_on"], 0.0)
        self.assertEqual(result["settings"]["filter_1_on"], 0.0)
        self.assertEqual(result["settings"]["env_1_delay"], 0.0)
        self.assertEqual(result["settings"]["env_1_sustain"], 1.0)
        self.assertTrue(
            math.isclose(
                result["settings"]["osc_1_level"] ** 2,
                CLEAN_SUB_OSC_1_AUDIBLE_LEVEL,
                abs_tol=1e-12,
            )
        )
        for key, seconds in CLEAN_SUB_SECONDS.items():
            self.assertTrue(math.isclose(result["settings"][key] ** 4, seconds, abs_tol=1e-12))
        self.assertEqual(
            result["settings"]["modulations"],
            [
                {"source": "", "destination": ""}
                for _ in source["settings"]["modulations"]
            ],
        )

    def test_file_builder_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.vital"
            output = root / "KDB Bass 01 - Clean Sub.vital"
            report = root / "report.json"
            source.write_text(json.dumps(fixture()), encoding="utf-8")

            first = build_clean_sub_files(source, output, report)
            self.assertTrue(first["invariants"]["only_allow_listed_paths_changed"])
            self.assertEqual(first["invariants"]["active_modulations"], 0)
            with self.assertRaises(PresetError):
                build_clean_sub_files(source, output, root / "second-report.json")

    def test_missing_required_setting_stops(self):
        source = fixture()
        del source["settings"]["osc_2_on"]
        with self.assertRaisesRegex(PresetError, "osc_2_on"):
            build_clean_sub(source)

    def test_every_additional_recipe_is_allow_listed_and_valid(self):
        source = fixture()
        for slug, recipe in ADDITIONAL_RECIPES.items():
            with self.subTest(recipe=slug):
                result, allowed = build_recipe(source, recipe)
                self.assertFalse(changed_paths(source, result) - allowed)
                self.assertEqual(result["preset_name"], recipe.preset_name)
                self.assertEqual(result["preset_style"], recipe.preset_style)
                self.assertEqual(
                    tuple(result[f"macro{index}"] for index in range(1, 5)),
                    recipe.macros,
                )
                self.assertEqual(result["settings"]["wavetables"], source["settings"]["wavetables"])
                self.assertEqual(result["settings"]["sample"], source["settings"]["sample"])
                self.assertEqual(result["settings"]["lfos"], source["settings"]["lfos"])
                active = [
                    route
                    for route in result["settings"]["modulations"]
                    if route["source"] and route["destination"]
                ]
                self.assertEqual(len(active), len(recipe.modulations))

    def test_warm_foundation_is_a_polyphonic_pad_with_neutral_macros(self):
        source = fixture()
        result, _ = build_recipe(source, ADDITIONAL_RECIPES["warm-foundation"])
        settings = result["settings"]

        self.assertEqual(result["preset_style"], "Pad")
        self.assertEqual(
            (result["macro1"], result["macro2"], result["macro3"], result["macro4"]),
            ("TONE", "MOTION", "SPACE", "WIDTH"),
        )
        self.assertEqual(settings["polyphony"], 8.0)
        self.assertEqual(settings["osc_1_unison_voices"], 4.0)
        self.assertEqual(settings["osc_2_unison_voices"], 3.0)
        self.assertTrue(math.isclose(settings["osc_1_unison_detune"] ** 2, 7.0))
        self.assertTrue(math.isclose(settings["osc_2_unison_detune"] ** 2, 5.0))
        self.assertTrue(math.isclose(settings["env_1_attack"] ** 4, 0.75))
        self.assertTrue(math.isclose(settings["env_1_release"] ** 4, 3.2))
        self.assertEqual(settings["chorus_on"], 1.0)
        self.assertEqual(settings["reverb_on"], 1.0)
        self.assertTrue(math.isclose(2 ** settings["reverb_decay_time"], 3.0))
        self.assertEqual(
            tuple(settings[f"macro_control_{index}"] for index in range(1, 5)),
            (0.0, 0.0, 0.0, 0.0),
        )
        active = [
            route
            for route in settings["modulations"]
            if route["source"] and route["destination"]
        ]
        self.assertEqual(len(active), 6)
        self.assertEqual(settings["modulation_2_amount"], 2.5 / 128.0)

    def test_dark_reese_macros_preserve_the_zero_position_sound(self):
        source = fixture()
        result, _ = build_recipe(source, ADDITIONAL_RECIPES["dark-reese"])
        settings = result["settings"]

        self.assertEqual(
            (result["macro1"], result["macro2"], result["macro3"], result["macro4"]),
            ("TONE", "MOTION", "DIRT", "WIDTH"),
        )
        self.assertEqual(settings["macro_control_1"], 0.0)
        self.assertEqual(settings["macro_control_2"], 0.0)
        self.assertEqual(settings["macro_control_3"], 0.0)
        self.assertEqual(settings["macro_control_4"], 0.0)
        self.assertEqual(settings["modulation_2_amount"], 0.0)
        self.assertEqual(settings["distortion_mix"], 0.0)
        self.assertEqual(settings["lfo_1_tempo"], 5.0)
        self.assertEqual(settings["lfo_1_sync_type"], 1.0)
        self.assertTrue(math.isclose(2 ** settings["portamento_time"], 0.18))
        self.assertEqual(settings["modulation_6_amount"], -0.24)
        self.assertEqual(settings["modulation_7_amount"], 0.24)

    def test_additional_recipe_file_builder_refuses_overwrite(self):
        recipe = ADDITIONAL_RECIPES["short-pluck"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.vital"
            output = root / f"{recipe.preset_name}.vital"
            report = root / "report.json"
            source.write_text(json.dumps(fixture()), encoding="utf-8")

            first = build_recipe_files(source, output, report, recipe)
            self.assertEqual(first["recipe"], "short-pluck")
            self.assertEqual(first["invariants"]["active_modulations"], 1)
            with self.assertRaises(PresetError):
                build_recipe_files(source, output, root / "second-report.json", recipe)


if __name__ == "__main__":
    unittest.main()
