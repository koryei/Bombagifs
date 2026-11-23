"""Scout configuration: defaults, overrides, and validation."""

from __future__ import annotations

import unittest

from scout_config import ScoutConfig, read_scout_config


class ScoutConfigTests(unittest.TestCase):
    def test_defaults_match_the_spec(self) -> None:
        cfg = read_scout_config({})
        self.assertEqual(cfg.mode, "off")
        self.assertIsNone(cfg.owner_id)
        self.assertEqual(cfg.seed_chats, frozenset())
        self.assertEqual(cfg.judge, "auto")
        self.assertEqual((cfg.inception_api_key, cfg.llm_base_url, cfg.llm_api_key, cfg.llm_model), ("", "", "", ""))
        self.assertEqual(cfg.screen, "all")
        self.assertEqual((cfg.decide_funny_min, cfg.decide_standalone_min, cfg.decide_sensitive_max), (0.85, 0.80, 0.10))
        self.assertEqual((cfg.judge_funny_min, cfg.deleted_funny_min, cfg.deleted_sensitive_max), (8.0, 9.0, 0.05))
        self.assertEqual(cfg.clip_deleted, "on")
        self.assertEqual((cfg.chat_cooldown_s, cfg.author_cooldown_s, cfg.max_per_hour), (900, 7200, 4))
        self.assertEqual((cfg.buffer_minutes, cfg.buffer_max_per_chat), (15, 200))
        self.assertEqual((cfg.reaction_window_s, cfg.tick_s, cfg.max_calls_per_day), (45, 5, 2000))
        self.assertEqual(cfg.theme, "onyx")
        self.assertEqual((cfg.manual, cfg.review, cfg.review_per_hour, cfg.examples_per_label), (True, "dry-run", 6, 8))
        self.assertEqual((cfg.near_miss_decide_funny, cfg.near_miss_final_funny), (0.6, 6.0))
        self.assertEqual((cfg.manual_limit_per_10_min, cfg.manual_author_cooldown_s), (3, 120))

    def test_trigger_defaults(self) -> None:
        cfg = read_scout_config({})
        self.assertEqual((cfg.triggers, cfg.trigger_anyone, cfg.trigger_ai_per_day, cfg.trigger_max_age_s), ("ai", False, 300, 180))

    def test_trigger_overrides_and_validation(self) -> None:
        cfg = read_scout_config(
            {"SCOUT_TRIGGERS": " Phrases ", "SCOUT_TRIGGER_ANYONE": "on", "SCOUT_TRIGGER_AI_PER_DAY": "40", "SCOUT_TRIGGER_MAX_AGE_SECONDS": "60"}
        )
        self.assertEqual((cfg.triggers, cfg.trigger_anyone, cfg.trigger_ai_per_day, cfg.trigger_max_age_s), ("phrases", True, 40, 60))
        self.assertEqual(read_scout_config({"SCOUT_TRIGGERS": "off"}).triggers, "off")
        for name, value in (("SCOUT_TRIGGERS", "maybe"), ("SCOUT_TRIGGER_ANYONE", "yes"), ("SCOUT_TRIGGER_AI_PER_DAY", "0"),
                            ("SCOUT_TRIGGER_MAX_AGE_SECONDS", "-1"), ("SCOUT_TRIGGER_MAX_AGE_SECONDS", "abc")):
            with self.subTest(name=name, value=value):
                with self.assertRaisesRegex(RuntimeError, name):
                    read_scout_config({name: value})

    def test_v3_1_overrides(self) -> None:
        cfg = read_scout_config(
            {
                "SCOUT_MANUAL": " OFF ",
                "SCOUT_REVIEW": "always",
                "SCOUT_REVIEW_PER_HOUR": "2",
                "SCOUT_EXAMPLES": "5",
                "SCOUT_MANUAL_LIMIT_PER_10_MIN": "7",
                "SCOUT_MANUAL_AUTHOR_COOLDOWN_SECONDS": "30",
            }
        )
        self.assertEqual((cfg.manual, cfg.review, cfg.review_per_hour, cfg.examples_per_label), (False, "always", 2, 5))
        self.assertEqual((cfg.manual_limit_per_10_min, cfg.manual_author_cooldown_s), (7, 30))
        self.assertTrue(read_scout_config({"SCOUT_MANUAL": "on"}).manual)
        self.assertEqual(read_scout_config({"SCOUT_REVIEW": "OFF"}).review, "off")

    def test_manual_rejects_other_values_naming_the_variable(self) -> None:
        for value in ("yes", "true", "1", "maybe"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, "SCOUT_MANUAL"):
                    read_scout_config({"SCOUT_MANUAL": value})

    def test_review_rejects_unknown_choice(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "SCOUT_REVIEW"):
            read_scout_config({"SCOUT_REVIEW": "sometimes"})

    def test_near_miss_bars_can_be_lowered_for_a_training_phase(self) -> None:
        cfg = read_scout_config({"SCOUT_NEAR_MISS_DECIDE_FUNNY": "0", "SCOUT_NEAR_MISS_FINAL_FUNNY": "0"})
        self.assertEqual((cfg.near_miss_decide_funny, cfg.near_miss_final_funny), (0.0, 0.0))
        for name, value in (("SCOUT_NEAR_MISS_DECIDE_FUNNY", "1.5"), ("SCOUT_NEAR_MISS_DECIDE_FUNNY", "-0.1"),
                            ("SCOUT_NEAR_MISS_FINAL_FUNNY", "11"), ("SCOUT_NEAR_MISS_FINAL_FUNNY", "x")):
            with self.subTest(name=name, value=value):
                with self.assertRaisesRegex(RuntimeError, name):
                    read_scout_config({name: value})

    def test_new_integers_must_be_positive(self) -> None:
        for name in ("SCOUT_REVIEW_PER_HOUR", "SCOUT_EXAMPLES", "SCOUT_MANUAL_LIMIT_PER_10_MIN", "SCOUT_MANUAL_AUTHOR_COOLDOWN_SECONDS"):
            for value in ("0", "-1", "1.5", "x"):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(RuntimeError, name):
                        read_scout_config({name: value})

    def test_reads_every_override(self) -> None:
        cfg = read_scout_config(
            {
                "SELFBOT_SCOUT_MODE": "dry-run",
                "SELFBOT_OWNER_ID": "42",
                "SELFBOT_SCOUT_CHATS": "1, 2,3",
                "SCOUT_JUDGE": "openai",
                "INCEPTION_API_KEY": "ik",
                "SCOUT_LLM_BASE_URL": "https://llm.example/",
                "SCOUT_LLM_API_KEY": "lk",
                "SCOUT_LLM_MODEL": "m",
                "SCOUT_SCREEN": "signals",
                "SCOUT_DECIDE_FUNNY_MIN": "0.9",
                "SCOUT_DECIDE_STANDALONE_MIN": "0.7",
                "SCOUT_DECIDE_SENSITIVE_MAX": "0.2",
                "SCOUT_JUDGE_FUNNY_MIN": "7.5",
                "SCOUT_DELETED_FUNNY_MIN": "9.5",
                "SCOUT_DELETED_SENSITIVE_MAX": "0.01",
                "SELFBOT_CLIP_DELETED": "owner",
                "SCOUT_CHAT_COOLDOWN_MINUTES": "30",
                "SCOUT_AUTHOR_COOLDOWN_MINUTES": "60",
                "SCOUT_MAX_PER_HOUR": "2",
                "SCOUT_BUFFER_MINUTES": "10",
                "SCOUT_BUFFER_MAX_PER_CHAT": "50",
                "SCOUT_REACTION_WINDOW_SECONDS": "30",
                "SCOUT_TICK_SECONDS": "2",
                "SCOUT_MAX_CALLS_PER_DAY": "100",
                "CLIP_THEME": "dark",
            }
        )
        self.assertEqual((cfg.mode, cfg.owner_id, cfg.seed_chats), ("dry-run", 42, frozenset({1, 2, 3})))
        self.assertEqual((cfg.judge, cfg.inception_api_key, cfg.llm_base_url, cfg.llm_api_key, cfg.llm_model), ("openai", "ik", "https://llm.example", "lk", "m"))
        self.assertEqual(cfg.screen, "signals")
        self.assertEqual((cfg.decide_funny_min, cfg.decide_standalone_min, cfg.decide_sensitive_max), (0.9, 0.7, 0.2))
        self.assertEqual((cfg.judge_funny_min, cfg.deleted_funny_min, cfg.deleted_sensitive_max), (7.5, 9.5, 0.01))
        self.assertEqual(cfg.clip_deleted, "owner")
        self.assertEqual((cfg.chat_cooldown_s, cfg.author_cooldown_s, cfg.max_per_hour), (1800, 3600, 2))
        self.assertEqual((cfg.buffer_minutes, cfg.buffer_max_per_chat, cfg.reaction_window_s, cfg.tick_s, cfg.max_calls_per_day), (10, 50, 30, 2, 100))
        self.assertEqual(cfg.theme, "dark")

    def test_rejects_unknown_choices_naming_the_variable(self) -> None:
        for name, value in (
            ("SELFBOT_SCOUT_MODE", "maybe"),
            ("SCOUT_JUDGE", "gpt"),
            ("SCOUT_SCREEN", "some"),
            ("SELFBOT_CLIP_DELETED", "yes"),
            ("CLIP_THEME", "neon"),
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(RuntimeError, name):
                    read_scout_config({name: value})

    def test_modes_are_case_insensitive(self) -> None:
        self.assertEqual(read_scout_config({"SELFBOT_SCOUT_MODE": " DRY-RUN "}).mode, "dry-run")

    def test_probabilities_must_be_between_0_and_1(self) -> None:
        for name in ("SCOUT_DECIDE_FUNNY_MIN", "SCOUT_DECIDE_STANDALONE_MIN", "SCOUT_DECIDE_SENSITIVE_MAX", "SCOUT_DELETED_SENSITIVE_MAX"):
            for value in ("1.5", "-0.1", "abc", "nan"):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(RuntimeError, name):
                        read_scout_config({name: value})

    def test_funny_min_must_be_0_to_10(self) -> None:
        for name in ("SCOUT_JUDGE_FUNNY_MIN", "SCOUT_DELETED_FUNNY_MIN"):
            for value in ("10.5", "-1", "x"):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(RuntimeError, name):
                        read_scout_config({name: value})

    def test_integers_must_be_positive(self) -> None:
        for name in ("SCOUT_MAX_PER_HOUR", "SCOUT_BUFFER_MINUTES", "SCOUT_REACTION_WINDOW_SECONDS", "SCOUT_MAX_CALLS_PER_DAY", "SCOUT_CHAT_COOLDOWN_MINUTES"):
            for value in ("0", "-3", "1.5", "x"):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(RuntimeError, name):
                        read_scout_config({name: value})

    def test_ids_must_be_positive_integers(self) -> None:
        for env in ({"SELFBOT_OWNER_ID": "0"}, {"SELFBOT_OWNER_ID": "abc"}, {"SELFBOT_SCOUT_CHATS": "1,x"}, {"SELFBOT_SCOUT_CHATS": "1,-2"}):
            with self.subTest(env=env):
                with self.assertRaisesRegex(RuntimeError, "SELFBOT_OWNER_ID|SELFBOT_SCOUT_CHATS"):
                    read_scout_config(env)

    def test_openai_judge_requires_url_key_and_model(self) -> None:
        full = {"SCOUT_JUDGE": "openai", "SCOUT_LLM_BASE_URL": "https://x.example", "SCOUT_LLM_API_KEY": "k", "SCOUT_LLM_MODEL": "m"}
        read_scout_config(full)
        for missing in ("SCOUT_LLM_BASE_URL", "SCOUT_LLM_API_KEY", "SCOUT_LLM_MODEL"):
            env = {k: v for k, v in full.items() if k != missing}
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(RuntimeError, missing):
                    read_scout_config(env)

    def test_llm_base_url_must_be_https_unless_loopback(self) -> None:
        read_scout_config({"SCOUT_LLM_BASE_URL": "http://127.0.0.1:1234"})
        read_scout_config({"SCOUT_LLM_BASE_URL": "http://localhost:8080"})
        for bad in ("http://llm.example", "ftp://x", "https://user:pw@x.example", "https://x.example/?q=1", "not a url"):
            with self.subTest(url=bad):
                with self.assertRaisesRegex(RuntimeError, "SCOUT_LLM_BASE_URL"):
                    read_scout_config({"SCOUT_LLM_BASE_URL": bad})

    def test_repr_hides_api_keys(self) -> None:
        cfg = read_scout_config({"INCEPTION_API_KEY": "super-secret-1", "SCOUT_LLM_API_KEY": "super-secret-2"})
        self.assertNotIn("super-secret-1", repr(cfg))
        self.assertNotIn("super-secret-2", repr(cfg))
        self.assertIsInstance(cfg, ScoutConfig)


if __name__ == "__main__":
    unittest.main()
