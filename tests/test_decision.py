import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "alethe" / "scripts"
spec = importlib.util.spec_from_file_location("decision_runner", SCRIPTS / "orchestrate.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
sys.path.insert(0, str(SCRIPTS))

from decision import classifier, cli, config as settings, setup  # noqa: E402
import decision  # noqa: E402
from decision.client import Client  # noqa: E402
from decision.contracts import DecisionError, validate  # noqa: E402

PLAYBOOKS = ["complete", "direct", "plan-execute", "parallel-worktrees", "best-of-n"]
STRATEGIES = ["economy", "balanced", "parallel"]


def answers(delegate=0.9, parallel=0.1, complexity="medium", risk="high", confidence=0.8):
    return {"model": "jevos-v5", "answers": {
        "delegate": {"type": "noul", "noul": delegate}, "parallel": {"type": "noul", "noul": parallel},
        "complexity": {"type": "choice", "choice": complexity, "confidence": confidence},
        "risk": {"type": "choice", "choice": risk, "confidence": confidence},
    }}


def observable_answers(
    sensitive=0.9, hard_bug=0.05, explore_only=0.1, many_similar=0.05,
    mechanical_sweep=0.1, disjoint_modules=0.05, sequential_steps=0.85,
    scope="multi", confidence=0.85,
):
    return {"model": "jevos-v5", "answers": {
        "sensitive": {"type": "noul", "noul": sensitive},
        "hard_bug": {"type": "noul", "noul": hard_bug},
        "explore_only": {"type": "noul", "noul": explore_only},
        "many_similar": {"type": "noul", "noul": many_similar},
        "mechanical_sweep": {"type": "noul", "noul": mechanical_sweep},
        "disjoint_modules": {"type": "noul", "noul": disjoint_modules},
        "sequential_steps": {"type": "noul", "noul": sequential_steps},
        "scope": {"type": "choice", "choice": scope, "confidence": confidence},
    }}


class FakeRuntime:
    """A jevos stand-in on an ephemeral local port."""

    def __init__(self, reply):
        outer = self
        self.reply, self.requests = reply, []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send({"status": "ok"})

            def do_POST(self):
                length = int(self.headers["Content-Length"])
                outer.requests.append(json.loads(self.rfile.read(length)))
                self._send(outer.reply)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ContractTest(unittest.TestCase):
    def test_unknown_playbook_is_refused(self):
        good = {
            "schema_version": 1, "classification": {"complexity": "low", "risk": "low", "delegation_recommended": False,
                                                    "parallelizable": False},
            "recommendation": {"playbook": "made-up", "tier": "cheap", "runner_strategy": None},
            "confidence": {"overall": 0.9, "by_question": {}},
        }
        with self.assertRaises(DecisionError):
            validate(good, PLAYBOOKS, STRATEGIES)
        good["recommendation"]["playbook"] = "direct"
        self.assertTrue(validate(good, PLAYBOOKS, STRATEGIES)["proposal"])

    def test_endpoint_must_be_local(self):
        for bad in ("http://example.com:8017", "https://127.0.0.1:8017", "http://127.0.0.1"):
            with self.assertRaises(DecisionError):
                settings.local_endpoint(bad)
        self.assertEqual(settings.local_endpoint("http://localhost:8017"), "http://localhost:8017")


class ClassifierTest(unittest.TestCase):
    def classify(self, reply, min_confidence=0.5, task="Migrate auth in 8 modules"):
        runtime = FakeRuntime(reply)
        self.addCleanup(runtime.close)
        return classifier.classify(Client(runtime.endpoint, 2), task, PLAYBOOKS, STRATEGIES, min_confidence), runtime

    def test_risky_work_is_not_sent_to_cheap_tier(self):
        proposal, runtime = self.classify(answers(complexity="low", risk="high"))
        self.assertEqual(proposal["recommendation"]["tier"], "standard")
        self.assertEqual(set(runtime.requests[0]["questions"]), set(classifier.QUESTIONS))

    def test_observable_questions_map_across_playbooks_and_abstain_on_vague(self):
        full_playbooks = [
            "complete", "direct", "plan-execute", "plan-big-execute-small", "scout-then-build",
            "advisor-escalation", "cascade", "parallel-worktrees", "best-of-n", "review-gate", "audit-at-scale",
        ]
        runtime = FakeRuntime(observable_answers(explore_only=0.98, sensitive=0.05, sequential_steps=0.2, scope="wide"))
        self.addCleanup(runtime.close)
        scout = classifier.classify(
            Client(runtime.endpoint, 2),
            "Read the billing module of this unfamiliar 200-file codebase and summarize its entry points.",
            full_playbooks,
            STRATEGIES,
            0.5,
        )
        self.assertEqual(scout["recommendation"]["playbook"], "scout-then-build")
        vague = classifier.classify_heuristic("Make it better.", full_playbooks, STRATEGIES, 0.5)
        self.assertTrue(vague["abstained"])
        self.assertIsNone(vague["recommendation"]["playbook"])

    def test_parallel_needs_low_enough_risk(self):
        safe, _ = self.classify(answers(parallel=0.9, risk="low", confidence=0.9))
        risky, _ = self.classify(answers(parallel=0.9, risk="high", confidence=0.9))
        self.assertEqual(safe["recommendation"]["playbook"], "parallel-worktrees")
        self.assertNotEqual(risky["recommendation"]["playbook"], "parallel-worktrees")

    def test_low_confidence_abstains(self):
        proposal, _ = self.classify(answers(confidence=0.2))
        self.assertTrue(proposal["abstained"])
        self.assertEqual(proposal["recommendation"], {"playbook": None, "tier": None, "runner_strategy": None})

    def test_partial_or_invalid_answers_are_refused(self):
        partial = answers()
        del partial["answers"]["risk"]
        invalid = answers(complexity="enormous")
        for reply in (partial, invalid, {"answers": "nope"}, {}):
            with self.assertRaises(DecisionError):
                self.classify(reply)

    def test_unreachable_runtime_is_a_decision_error(self):
        with self.assertRaises(DecisionError):
            classifier.classify(Client("http://127.0.0.1:1", 0.5), "x", PLAYBOOKS, STRATEGIES, 0.5)


class SetupTest(unittest.TestCase):
    def test_no_download_without_consent(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(setup, "install") as install:
            policy = Path(home) / "policy.json"
            with mock.patch.object(setup, "runtime_for_platform", return_value=("a.zip", "jev")):
                result = cli.do_setup(policy, yes=False, endpoint="")
            self.assertEqual(result["consent"], "required")
            install.assert_not_called()

    def test_traversal_is_refused(self):
        with tempfile.TemporaryDirectory() as home:
            bad = Path(home) / "bad.zip"
            with zipfile.ZipFile(bad, "w") as bundle:
                bundle.writestr("../escape.txt", "x")
            with self.assertRaises(DecisionError):
                setup.safe_extract(bad, Path(home) / "out")
            self.assertFalse((Path(home) / "escape.txt").exists())

    def test_checksum_mismatch_installs_nothing(self):
        def fetch(url, target, limit):
            target.write_text("deadbeef  x" if url.endswith(setup.CHECKSUMS) else "payload")

        with tempfile.TemporaryDirectory() as home, mock.patch.object(setup, "runtime_for_platform", return_value=("a.zip", "jev")):
            archive_line = "0" * 64 + "  a.zip\n"
            with mock.patch.object(setup, "expected_hash", side_effect=lambda text, name: "0" * 64):
                with self.assertRaises(DecisionError):
                    setup.install(Path(home), fetch)
            self.assertFalse((Path(home) / setup.RELEASE).exists())
            self.assertEqual(setup.expected_hash(archive_line, "a.zip"), "0" * 64)


class ShadowTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.policy = Path(self.home.name) / "policy.json"
        self.folder = Path(self.home.name) / "run"
        self.folder.mkdir()

    def shadow(self):
        return decision.shadow(self.policy, self.folder, "Migrate auth", "direct", PLAYBOOKS, STRATEGIES)

    def use(self, mode, endpoint):
        config = settings.load(self.policy)
        config.update(mode=mode, endpoint=endpoint, timeoutSeconds=1.0)
        settings.save(self.policy, config)

    def test_off_is_the_default_and_touches_nothing(self):
        self.assertIsNone(self.shadow())
        self.assertFalse((self.folder / "decision.json").exists())

    def test_shadow_records_proposal_and_agreement(self):
        runtime = FakeRuntime(answers(delegate=0.1, risk="low", confidence=0.9))
        self.addCleanup(runtime.close)
        self.use("shadow", runtime.endpoint)
        record = self.shadow()
        self.assertEqual(record["status"], "ok")
        self.assertTrue(record["agreement"]["playbook"])
        self.assertEqual(json.loads((self.folder / "decision.json").read_text())["actual"]["playbook"], "direct")

    def test_dead_runtime_falls_back(self):
        self.use("shadow", "http://127.0.0.1:1")
        with mock.patch.object(decision.manager, "start"):
            record = self.shadow()
        self.assertEqual(record["status"], "fallback")

    def test_new_prints_the_same_result_with_shadow_on(self):
        runtime = FakeRuntime(answers())
        self.addCleanup(runtime.close)
        outputs = []
        for mode in ("off", "shadow"):
            env = {"ALETHE_ORCHESTRATE_POLICY": str(self.policy), "ALETHE_ORCHESTRATE_HOME": str(Path(self.home.name) / mode)}
            with mock.patch.dict(os.environ, env):
                self.use(mode, runtime.endpoint)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(runner.main(["new", "Same title", "--playbook", "direct"]), 0)
                created = json.loads(out.getvalue())
                self.assertEqual((Path(created["folder"]) / "decision.json").exists(), mode == "shadow")
                created.pop("folder"), created.pop("createdAt")
                outputs.append(created)
        self.assertEqual(outputs[0], outputs[1])

    def test_playbook_pick_task_and_outcome_recording(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(
                runner.main([
                    "playbook", "--pick", "--json", "--policy", str(self.policy),
                    "--task", "Migrate eight independent services with no shared code to async/await.",
                ]),
                0,
            )
        picked = json.loads(out.getvalue())
        self.assertEqual(picked["taskAdvice"]["recommendation"]["playbook"], "parallel-worktrees")
        self.assertEqual(picked["taskAdvice"]["recommendation"]["tier"], "standard")

        runtime = FakeRuntime(observable_answers())
        self.addCleanup(runtime.close)
        self.use("shadow", runtime.endpoint)
        self.shadow()
        updated = decision.record_outcome(self.folder, "worker-1", "used", "clean diff")
        self.assertEqual(updated["outcomeSummary"], {"used": 1, "partial": 0, "discarded": 0})

    def test_benchmark_fixtures_are_well_formed(self):
        cases = json.loads(cli.FIXTURES.read_text(encoding="utf-8"))["cases"]
        self.assertGreaterEqual(len(cases), 9)
        self.assertTrue(all({"id", "task", "expect"} <= set(case) for case in cases))


if __name__ == "__main__":
    unittest.main()
