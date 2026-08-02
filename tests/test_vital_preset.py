import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from tools.vital_preset import (
    CLEAN_SUB_OSC_1_AUDIBLE_LEVEL,
    CLEAN_SUB_SECONDS,
    EFFECT_SWITCHES,
    PresetError,
    build_clean_sub,
    build_clean_sub_files,
    changed_paths,
    quartic_control_for_seconds,
)


def fixture() -> dict:
    settings = {
        "modulations": [
            {"source": "env_2", "destination": "filter_1_cutoff"},
            {"source": "", "destination": ""},
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
        "modulation_1_amount": 0.15,
        "modulation_2_amount": 0.0,
    }
    settings.update({name: 0.0 for name in EFFECT_SWITCHES})
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
            [{"source": "", "destination": ""}, {"source": "", "destination": ""}],
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


if __name__ == "__main__":
    unittest.main()
