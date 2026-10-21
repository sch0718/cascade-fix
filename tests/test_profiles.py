import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cascade import profiles  # noqa: E402


def write_toml(tmp, text):
    path = Path(tmp) / "repos.toml"
    path.write_text(text, encoding="utf-8")
    return path


class ProfilesTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name).resolve()

    def test_builtins_match_design(self):
        d, co, lo = (profiles.builtin(n) for n in ("default", "claude-only", "local"))
        self.assertEqual(set(d["roles"]), set(profiles.ROLE_KEYS))
        self.assertEqual(d["roles"]["code.a"], {"runner": "claude", "model": "claude-sonnet-5-5", "effort": "low",
                                                "fallback": {"runner": "claude", "model": "claude-sonnet-5-5", "effort": "low"}})
        self.assertEqual(d["roles"]["code.opinion"]["runner"], "auto")
        self.assertEqual(d["roles"]["code.review"]["runner"], "auto")
        self.assertFalse(d["adopt_on_both_pass"])
        self.assertEqual(co["roles"]["code.opinion"], {"runner": "claude", "model": "claude-fable-5-1", "effort": "high",
                                                       "fallback": {"runner": "claude", "model": "claude-fable-5-1", "effort": "high"}})
        self.assertTrue(co["adopt_on_both_pass"])
        self.assertEqual(lo["roles"]["code.a"]["runner"], "codex")
        self.assertEqual(lo["roles"]["code.a"]["effort"], "low")
        self.assertEqual(lo["roles"]["code.b"]["model"], "claude-haiku-5-5")
        self.assertEqual(lo["roles"]["doc.judge_a"]["model"], "claude-sonnet-5-5")
        self.assertEqual(lo["roles"]["code.review"], {"runner": "codex", "model": "", "effort": "",
                                                      "fallback": {"runner": "claude", "model": "claude-fable-5-1", "effort": "high"}})
        for key in ("code.adjudicator", "doc.adjudicator"):
            self.assertEqual(lo["roles"][key]["model"], "claude-opus-5-5")

    def test_builtins_are_valid_and_independent_copies(self):
        self.assertEqual(profiles.ROLE_KEYS[0], "code.review")
        self.assertEqual(profiles.ROLE_KEYS[-1], "doc.recheck")
        self.assertEqual(len(profiles.ROLE_KEYS), 14)
        for name in profiles.BUILTIN_NAMES:
            profiles.validate(name, profiles.builtin(name))
        first = profiles.builtin("local")
        first["roles"]["code.a"]["fallback"]["model"] = "x"
        self.assertEqual(profiles.builtin("local")["roles"]["code.a"]["fallback"]["model"], "claude-sonnet-5-5")

    def test_local_external_cells_cover_review_and_code_a_only(self):
        lo = profiles.builtin("local")["roles"]
        external = {key for key, cell in lo.items() if cell["runner"] != "claude"}
        self.assertEqual(external, {"code.review", "code.a", "code.opinion", "code.recheck",
                                    "doc.review", "doc.opinion", "doc.recheck"})

    def test_allowed_runners(self):
        self.assertEqual(profiles.allowed_runners("code.adjudicator"), ("claude",))
        self.assertEqual(profiles.allowed_runners("code.a"), ("claude", "codex", "cursor"))
        self.assertEqual(profiles.allowed_runners("doc.recheck"), ("claude", "codex", "cursor", "auto"))

    def test_reset_cell_table(self):
        me = lambda cell: (cell["model"], cell["effort"])
        self.assertEqual(me(profiles.reset_cell("code.opinion", "codex")), ("", ""))
        self.assertEqual(me(profiles.reset_cell("code.a", "codex")), ("", "low"))
        self.assertEqual(me(profiles.reset_cell("code.b", "cursor", "composer-3")), ("composer-3", ""))
        self.assertEqual(me(profiles.reset_cell("code.opinion", "claude")), ("claude-fable-5-1", "high"))
        self.assertEqual(me(profiles.reset_cell("code.opinion", "auto")), ("claude-fable-5-1", "high"))

    def test_merge_runner_only_drops_old_model(self):
        base = profiles.builtin("claude-only")["roles"]["code.a"]
        merged = profiles.merge_cell("code.a", base, {"runner": "codex"})
        self.assertEqual((merged["runner"], merged["model"], merged["effort"]), ("codex", "", "low"))
        self.assertEqual(merged["fallback"], base["fallback"])

    def test_explicit_model_wins_over_reset(self):
        base = profiles.builtin("claude-only")["roles"]["code.opinion"]
        merged = profiles.merge_cell("code.opinion", base, {"runner": "claude", "model": "claude-sonnet-5-5", "effort": "low"})
        self.assertEqual((merged["model"], merged["effort"]), ("claude-sonnet-5-5", "low"))

    def test_merge_does_not_mutate_base(self):
        base = profiles.builtin("claude-only")["roles"]["code.a"]
        snapshot = copy.deepcopy(base)
        profiles.merge_cell("code.a", base, {"runner": "cursor", "fallback": {"runner": "codex"}})
        self.assertEqual(base, snapshot)

    def test_merge_without_runner_keeps_base_values(self):
        base = profiles.builtin("local")["roles"]["code.a"]
        merged = profiles.merge_cell("code.a", base, {"effort": "medium"})
        self.assertEqual((merged["runner"], merged["model"], merged["effort"]), ("codex", "", "medium"))

    def test_validate_rejects(self):
        cases = {
            "code.adjudicator": {"runner": "codex"},
            "code.a": {"runner": "auto"},
            "code.b": {"runner": "cursor", "model": "composer-2.5", "effort": "low"},
            "code.opinion": {"runner": "codex", "model": "gpt-6.1-sol"},
            "doc.checker": {"runner": "cursor", "model": "claude-sonnet-5-5"},
            "doc.writer": {"runner": "claude", "model": "gpt-6.1-sol"},
        }
        for key, override in cases.items():
            p = profiles.builtin("default")
            p["roles"][key] = profiles.merge_cell(key, p["roles"][key], override)
            with self.subTest(key=key), self.assertRaises(profiles.ProfileError):
                profiles.validate("x", p)

    def test_validate_rejects_more(self):
        def mutated(key, **fields):
            p = profiles.builtin("default")
            p["roles"][key].update(fields)
            return p

        def mutated_fallback(key, **fields):
            p = profiles.builtin("default")
            p["roles"][key]["fallback"].update(fields)
            return p

        cases = {
            "fallback auto": mutated_fallback("code.review", runner="auto"),
            "fallback judge codex": mutated_fallback("code.adjudicator", runner="codex", model="", effort=""),
            "fallback codex review with effort": mutated_fallback("code.recheck", runner="codex", model="", effort="high"),
            "bad effort": mutated("code.a", effort="turbo"),
            "claude empty model": mutated("code.a", model=""),
            "gpt on cursor": mutated("code.b", runner="cursor", model="gpt-6.1-sol", effort=""),
            "adopt not bool": {**profiles.builtin("default"), "adopt_on_both_pass": "yes"},
        }
        for label, p in cases.items():
            with self.subTest(label), self.assertRaisesRegex(profiles.ProfileError, r"profiles\.x"):
                profiles.validate("x", p)

    def test_validate_message_names_profile_and_key(self):
        p = profiles.builtin("default")
        p["roles"]["code.adjudicator"]["runner"] = "codex"
        with self.assertRaisesRegex(profiles.ProfileError, r'profiles\.x 칸 "code\.adjudicator"'):
            profiles.validate("x", p)

    def test_load_merges_and_bases(self):
        path = write_toml(self.tmp, """
default_profile = "mine"
[profiles.local]
"code.b" = { runner = "cursor" }
[profiles.mine]
base = "local"
adopt_on_both_pass = false
"code.a" = { runner = "claude", model = "claude-haiku-5-5", effort = "low", fallback = { runner = "cursor", model = "composer-2.5" } }
""")
        found, data = profiles.load_profiles(path)
        self.assertEqual(found["local"]["roles"]["code.b"]["model"], "composer-2.5")
        self.assertEqual(found["mine"]["roles"]["code.b"]["runner"], "cursor")
        self.assertEqual(found["mine"]["roles"]["code.a"]["fallback"], {"runner": "cursor", "model": "composer-2.5", "effort": ""})
        self.assertFalse(found["mine"]["adopt_on_both_pass"])
        self.assertEqual(data["default_profile"], "mine")

    def test_load_without_file_returns_builtins(self):
        for path in (None, self.tmp / "missing.toml"):
            with self.subTest(path=path):
                found, data = profiles.load_profiles(path)
                self.assertEqual(tuple(found), profiles.BUILTIN_NAMES)
                self.assertEqual(data, {})

    def test_new_profile_starts_from_default(self):
        path = write_toml(self.tmp, '[profiles.mine]\n"code.b" = { runner = "cursor", model = "composer-2.5" }\n')
        found, _ = profiles.load_profiles(path)
        self.assertEqual(found["mine"]["roles"]["code.a"], profiles.builtin("default")["roles"]["code.a"])
        self.assertEqual(found["mine"]["roles"]["code.opinion"]["runner"], "auto")
        self.assertFalse(found["mine"]["adopt_on_both_pass"])

    def test_base_claude_only_is_allowed(self):
        path = write_toml(self.tmp, '[profiles.mine]\nbase = "claude-only"\n"code.b" = { runner = "cursor" }\n')
        found, _ = profiles.load_profiles(path)
        self.assertTrue(found["mine"]["adopt_on_both_pass"])
        self.assertEqual(found["mine"]["roles"]["code.b"]["model"], "composer-2.5")
        self.assertEqual(found["claude-only"], profiles.builtin("claude-only"))

    def test_claude_only_cannot_be_overridden(self):
        path = write_toml(self.tmp, '[profiles.claude-only]\n"code.a" = { runner = "codex" }\n')
        with self.assertRaisesRegex(profiles.ProfileError, "claude-only"):
            profiles.load_profiles(path)

    def test_base_errors(self):
        for text in ('[profiles.a]\nbase = "b"\n[profiles.b]\nbase = "a"\n', '[profiles.a]\nbase = "nope"\n',
                     '[profiles.a]\nbase = "a"\n', '[profiles.local]\nbase = "claude-only"\n'):
            with self.subTest(text=text), self.assertRaises(profiles.ProfileError):
                profiles.load_profiles(write_toml(self.tmp, text))

    def test_unknown_base_lists_known_names(self):
        path = write_toml(self.tmp, '[profiles.a]\nbase = "nope"\n')
        with self.assertRaisesRegex(profiles.ProfileError, r"profiles\.a.*default.*claude-only.*local"):
            profiles.load_profiles(path)

    def test_wrong_types_are_config_errors(self):
        for text in ('[profiles.a]\n"code.a" = "codex"\n', '[profiles.a]\n"code.a" = { runner = 1 }\n',
                     '[profiles.a]\n"code.z" = { runner = "claude" }\n', '[profiles.a]\n"code.a" = { color = "red" }\n'):
            with self.subTest(text=text), self.assertRaisesRegex(profiles.ProfileError, r"profiles\.a"):
                profiles.load_profiles(write_toml(self.tmp, text))

    def test_more_load_errors_name_profile(self):
        for text in ('[profiles.a]\nadopt_on_both_pass = "yes"\n',
                     '[profiles.a]\n"code.a" = { fallback = { runner = "auto" } }\n',
                     '[profiles.a]\n"code.a" = { fallback = { color = "red" } }\n',
                     '[profiles.a]\n"code.adjudicator" = { runner = "codex" }\n',
                     '[profiles.a]\n"code.b" = { runner = "cursor", effort = "low" }\n',
                     '[profiles.a]\ncode.a = { runner = "codex" }\n',
                     '[profiles.local]\n"code.review" = { model = "gpt-6.1-sol" }\n'):
            with self.subTest(text=text), self.assertRaisesRegex(profiles.ProfileError, r"profiles\.(a|local)"):
                profiles.load_profiles(write_toml(self.tmp, text))

    def test_toml_syntax_error_is_profile_error(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.load_profiles(write_toml(self.tmp, "[profiles.a\n"))

    def test_profile_error_is_config_error(self):
        from cascade.config import ConfigError
        self.assertTrue(issubclass(profiles.ProfileError, ConfigError))


if __name__ == "__main__":
    unittest.main()
