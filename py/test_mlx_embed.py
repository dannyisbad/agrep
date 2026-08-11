"""The Metal lane's two obligations: same vector space, polite scheduling.

These run everywhere, including boxes with no MLX and no Apple Silicon - the
gates are pure decisions over injected facts, so they are testable without the
hardware. The one test that needs a GPU says so and skips.

The bug this file exists to prevent: the library this lane originally used
defaulted to MEAN pooling while agrep's contract is CLS. Vectors still came out
normalized, still 384-wide, still finite - every shape assertion passed - and
ranking was quietly destroyed (0.878 mean cosine, 0.769 worst, on real corpus
rows). So correctness here is asserted numerically against the ONNX lane, never
structurally.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from _test_support import isolate_data_dir

isolate_data_dir()
import embedder  # noqa: E402
import mlx_embed  # noqa: E402


class CapabilityGate(unittest.TestCase):
    """available() is a fact about the machine, never a benchmark."""

    def test_non_apple_silicon_is_unavailable(self) -> None:
        with mock.patch.object(mlx_embed, "_apple_silicon", return_value=False):
            ok, reason = mlx_embed.available()
        self.assertFalse(ok)
        self.assertIn("apple silicon", reason)

    def test_env_off_disables_even_where_supported(self) -> None:
        with mock.patch.object(mlx_embed, "_apple_silicon", return_value=True), \
                mock.patch.dict("os.environ", {"AGREP_MLX": "off"}):
            ok, reason = mlx_embed.available()
        self.assertFalse(ok)
        self.assertIn("off", reason)


class CourtesyGate(unittest.TestCase):
    """should_use() decides whether taking the GPU is polite, once per lane pick."""

    def _load(self, value, cores=8):
        return mock.patch.object(
            mlx_embed.os, "getloadavg", return_value=(value, value, value)), \
            mock.patch.object(mlx_embed.os, "cpu_count", return_value=cores)

    def test_idle_machine_takes_the_gpu(self) -> None:
        load, cores = self._load(1.0, cores=8)
        with load, cores, mock.patch.dict("os.environ", {"AGREP_MLX": ""}):
            ok, _ = mlx_embed.should_use()
        self.assertTrue(ok)

    def test_busy_machine_yields_the_gpu(self) -> None:
        # The compositor shares this silicon; a backfill must not make the
        # owner's windows stutter just because it can go faster.
        load, cores = self._load(7.6, cores=8)
        with load, cores, mock.patch.dict("os.environ", {"AGREP_MLX": ""}):
            ok, reason = mlx_embed.should_use()
        self.assertFalse(ok)
        self.assertIn("busy", reason)

    def test_load_is_judged_per_core_not_absolute(self) -> None:
        # The bug this pins: a flat ceiling tuned on one box switched the lane
        # off permanently on an 18-core machine that idles around 5-11. The
        # same absolute load must read as busy on 4 cores and fine on 32.
        load_small, cores_small = self._load(8.0, cores=4)
        with load_small, cores_small, mock.patch.dict("os.environ", {"AGREP_MLX": ""}):
            busy, _ = mlx_embed.should_use()
        load_big, cores_big = self._load(8.0, cores=32)
        with load_big, cores_big, mock.patch.dict("os.environ", {"AGREP_MLX": ""}):
            roomy, _ = mlx_embed.should_use()
        self.assertFalse(busy)
        self.assertTrue(roomy)

    def test_pinned_on_ignores_load(self) -> None:
        # A foreground `agrep index` is work the owner is already waiting on.
        load, cores = self._load(99.0, cores=1)
        with load, cores, mock.patch.dict("os.environ", {"AGREP_MLX": "on"}):
            ok, reason = mlx_embed.should_use()
        self.assertTrue(ok)
        self.assertIn("pinned", reason)

    def test_unreadable_load_is_not_permission(self) -> None:
        with mock.patch.object(mlx_embed.os, "getloadavg",
                               side_effect=OSError("no")):
            ok, reason = mlx_embed.should_use()
        self.assertFalse(ok)
        self.assertIn("unreadable", reason)


class _FakeLane:
    """A Metal lane whose numerics the test controls."""

    def __init__(self, transform=None):
        self._transform = transform or (lambda v: v)
        self.calls = 0

    def encode(self, input_ids, attention_mask):
        self.calls += 1
        rows = input_ids.shape[0]
        base = np.tile(np.arange(384, dtype=np.float32), (rows, 1))
        return self._transform(base)


class ParityGate(unittest.TestCase):
    """A lane may only serve if it agrees with the index already on disk."""

    def _embedder(self):
        e = embedder.Embedder.__new__(embedder.Embedder)
        e.profile = dict(embedder.PROFILE, pooling="cls", normalize=True,
                         dim=384, native_dim=384,
                         layernorm_before_truncate=False)
        e._metal = None
        e.lane = embedder.LANE_CPU
        return e

    def test_disagreeing_lane_is_rejected(self) -> None:
        e = self._embedder()
        onnx = np.tile(np.arange(384, dtype=np.float32), (2, 1))
        onnx = onnx / np.linalg.norm(onnx, axis=1, keepdims=True)
        # A lane that reverses the vector is a different space entirely -
        # the shape is right, which is exactly why shape is not the check.
        metal = e._finalize_pooled(
            np.tile(np.arange(384, dtype=np.float32)[::-1], (2, 1)), 2)
        worst = float((onnx * metal).sum(axis=1).min())
        self.assertLess(worst, embedder._METAL_MIN_COSINE)

    def test_matching_lane_clears_the_floor(self) -> None:
        e = self._embedder()
        raw = np.tile(np.arange(384, dtype=np.float32), (2, 1))
        a = e._finalize_pooled(raw, 2)
        b = e._finalize_pooled(raw * 1.0001, 2)   # numerics, not a new space
        self.assertGreaterEqual(float((a * b).sum(axis=1).min()),
                                embedder._METAL_MIN_COSINE)

    def test_metal_lane_needs_only_mlx(self) -> None:
        # The extra costs one dependency. Importing a model library would pull
        # transformers and a web-server stack into this log-search CLI, which
        # vendoring the encoder exists to avoid.
        import mlx_embed as m
        source = Path(m.__file__).read_text()
        for banned in ("mlx_embeddings", "transformers"):
            self.assertNotIn(banned, source)

    def test_encoder_pools_cls_with_no_switch(self) -> None:
        # Not a style preference: a pooling switch is what let a default of
        # "mean" write into a CLS index unnoticed.
        source = Path(__file__).with_name("mlx_modernbert.py").read_text()
        self.assertNotIn("classifier_pooling", source)
        self.assertIn("[:, 0, :]", source)

    def test_probe_set_includes_a_long_row(self) -> None:
        # The pooling failure grows with sequence length: a probe set of short
        # strings scores ~0.999 even when pooling is wrong, so length is part
        # of the contract, not an accident of how the list was written.
        longest = max(len(p) for p in embedder._METAL_PROBES)
        self.assertGreater(longest, 200)


class LaneSelection(unittest.TestCase):
    """The lane is chosen ONCE, and the store's own identity outranks the machine.

    The bug: `should_use` used to be re-asked before every batch, so a backfill
    took the GPU while the machine was idle and dropped to CPU when it was busy,
    leaving one store holding rows from two vector spaces. On a 24-row fixture
    that flipped 3 of 45 queries - a different top-1, and a hit that became "no
    confident match" - which is why lane selection now happens up front and the
    chosen lane is written into embeddings.meta.
    """

    def _machine(self, load, cores=8):
        return mock.patch.object(
            mlx_embed.os, "getloadavg", return_value=(load, load, load)), \
            mock.patch.object(mlx_embed.os, "cpu_count", return_value=cores)

    def test_cpu_lane_identity_is_the_bare_profile_string(self) -> None:
        # Every store built before lanes existed carries this exact string; a
        # suffix here would silently re-embed every user's corpus.
        self.assertEqual(embedder.profile_string(embedder.LANE_CPU),
                         embedder.PROFILE_STRING)
        self.assertNotEqual(embedder.profile_string(embedder.LANE_METAL),
                            embedder.PROFILE_STRING)

    def test_metal_is_the_default_where_available_and_idle(self) -> None:
        # No env var required: an installed metal extra on an idle apple
        # silicon machine starts new stores on the fast lane. Existing stores
        # still conform to their recorded lane (tested below).
        load, cores = self._machine(0.1)
        with load, cores, mock.patch.dict("os.environ", {"AGREP_MLX": ""}), \
                mock.patch.object(mlx_embed, "available", return_value=(True, "ok")):
            self.assertEqual(embedder.default_lane(), embedder.LANE_METAL)

    def test_mlx_off_disables_the_default(self) -> None:
        # available() owns the off switch; the default lane honors it even on
        # a capable idle machine.
        load, cores = self._machine(0.1)
        with load, cores, mock.patch.dict("os.environ", {"AGREP_MLX": "off"}):
            self.assertEqual(embedder.default_lane(), embedder.LANE_CPU)

    def test_explicit_mlx_pin_stays_metal_even_under_load(self) -> None:
        # AGREP_MLX=on pins the lane on; should_use short-circuits to True
        # under it, so load never overrides an explicit pin (the load courtesy
        # lives on the auto path where should_use actually checks it).
        load, cores = self._machine(7.6)
        with load, cores, mock.patch.dict("os.environ", {"AGREP_MLX": "on"}), \
                mock.patch.object(mlx_embed, "available", return_value=(True, "ok")):
            self.assertEqual(embedder.default_lane(), embedder.LANE_METAL)

    def test_auto_path_defers_metal_to_a_busy_machine(self) -> None:
        # The courtesy is real on the auto path: AGREP_MLX unset, should_use
        # checks load and declines when the machine is busy.
        load, cores = self._machine(7.6)
        with load, cores, mock.patch.dict("os.environ", {"AGREP_MLX": ""}), \
                mock.patch.object(mlx_embed, "available", return_value=(True, "ok")), \
                mock.patch.object(mlx_embed, "should_use",
                                  return_value=(False, "machine busy")):
            self.assertEqual(embedder.default_lane(), embedder.LANE_CPU)

    def test_full_rebuild_re_decides_the_lane(self) -> None:
        # `reindex --full` is the one sanctioned lane move: with the fresh-lane
        # flag up, _active_lane ignores the recorded CPU identity and answers
        # the machine default; without it, the same store conforms.
        import embed
        cpu_identity = embedder.profile_string(embedder.LANE_CPU)
        with mock.patch.object(embed.common, "read_index_meta",
                               return_value=("x", cpu_identity)), \
                mock.patch.dict("os.environ", {"AGREP_MLX": ""}), \
                mock.patch.object(mlx_embed, "available",
                                  return_value=(True, "ok")), \
                mock.patch.object(mlx_embed, "should_use",
                                  return_value=(True, "idle")), \
                mock.patch.dict(embed._ACTIVE_LANE, clear=True):
            with mock.patch.object(embed, "_FRESH_LANE", False):
                self.assertEqual(embed._active_lane(), embedder.LANE_CPU)
            with mock.patch.dict(embed._ACTIVE_LANE, clear=True), \
                    mock.patch.object(embed, "_FRESH_LANE", True):
                self.assertEqual(embed._active_lane(), embedder.LANE_METAL)

    def test_a_metal_store_conforms_without_the_env_being_set(self) -> None:
        # Conform, never mix: the rows already on disk decide, not the shell.
        metal = embedder.profile_string(embedder.LANE_METAL)
        with mock.patch.dict("os.environ", {"AGREP_MLX": ""}), \
                mock.patch.object(mlx_embed, "available", return_value=(True, "ok")):
            self.assertEqual(embedder.resolve_lane(metal), embedder.LANE_METAL)

    def test_a_cpu_store_stays_cpu_under_agrep_mlx_on(self) -> None:
        # An increment must not switch an existing store to the other engine.
        with mock.patch.dict("os.environ", {"AGREP_MLX": "on"}), \
                mock.patch.object(mlx_embed, "available", return_value=(True, "ok")), \
                mock.patch.object(mlx_embed, "should_use", return_value=(True, "idle")):
            self.assertEqual(
                embedder.resolve_lane(embedder.PROFILE_STRING), embedder.LANE_CPU)

    def test_metal_store_without_metal_resolves_to_a_visible_mismatch(self) -> None:
        # Not "serve it on CPU": answering CPU here makes the identities differ,
        # which is what turns into a refusal instead of different results.
        metal = embedder.profile_string(embedder.LANE_METAL)
        with mock.patch.object(mlx_embed, "available",
                               return_value=(False, "disabled by AGREP_MLX=off")):
            self.assertEqual(embedder.resolve_lane(metal), embedder.LANE_CPU)
            self.assertNotEqual(embedder.store_profile_string(metal), metal)

    def test_an_unknown_model_id_falls_back_to_the_machine_default(self) -> None:
        # "Machine default" is default_lane's answer, whatever this machine's
        # capability makes it - pin capability both ways so the test does not
        # depend on where it runs.
        with mock.patch.dict("os.environ", {"AGREP_MLX": ""}), \
                mock.patch.object(mlx_embed, "available",
                                  return_value=(False, "mlx missing")):
            self.assertEqual(embedder.resolve_lane("some-other-model"),
                             embedder.LANE_CPU)
        with mock.patch.dict("os.environ", {"AGREP_MLX": ""}), \
                mock.patch.object(mlx_embed, "available",
                                  return_value=(True, "ok")), \
                mock.patch.object(mlx_embed, "should_use",
                                  return_value=(True, "idle")):
            self.assertEqual(embedder.resolve_lane("some-other-model"),
                             embedder.LANE_METAL)


class FallbackBehaviour(unittest.TestCase):
    """Once a lane is chosen, nothing may quietly move a store off it."""

    def _embedder(self, lane):
        e = embedder.Embedder.__new__(embedder.Embedder)
        e.profile = dict(embedder.PROFILE, pooling="cls", dim=384,
                         native_dim=384, layernorm_before_truncate=False)
        e._metal = lane
        e.lane = embedder.LANE_METAL
        return e

    def test_the_lane_is_not_re_decided_per_batch(self) -> None:
        # A busy machine used to send this batch to CPU mid-store. Courtesy is
        # now spent at lane selection; here the answer is always the metal lane.
        lane = _FakeLane()
        e = self._embedder(lane)
        with mock.patch.object(mlx_embed, "should_use",
                               return_value=(False, "machine busy")):
            out = e._metal_pooled(np.zeros((2, 4), np.int64),
                                  np.ones((2, 4), np.int64))
        self.assertEqual(out.shape, (2, 384))
        self.assertEqual(lane.calls, 1)

    def test_lane_exception_is_an_error_not_a_silent_cpu_fallback(self) -> None:
        class Boom:
            def encode(self, *_a):
                raise RuntimeError("metal fell over")
        e = self._embedder(Boom())
        with self.assertRaisesRegex(embedder.EmbedderUnavailable,
                                    "mixing vector spaces"):
            e._metal_pooled(np.zeros((2, 4), np.int64),
                            np.ones((2, 4), np.int64))
        # Still the metal lane: half a store in each space is the failure this
        # identity exists to prevent, so the pass fails instead of finishing.
        self.assertEqual(e.lane, embedder.LANE_METAL)
        self.assertEqual(e.profile_string,
                         embedder.profile_string(embedder.LANE_METAL))

    def test_pinned_provider_never_opens_the_metal_lane(self) -> None:
        e = embedder.Embedder.__new__(embedder.Embedder)
        e.profile = dict(embedder.PROFILE, provider="CoreMLExecutionProvider")
        e._metal = None
        e.lane = embedder.LANE_CPU
        with mock.patch.object(mlx_embed, "available") as avail:
            e._start_metal_lane()
        avail.assert_not_called()
        self.assertIsNone(e._metal)

    def test_a_named_lane_that_cannot_open_raises_instead_of_downgrading(self) -> None:
        # The store said metal. Producing CPU rows for it would be the defect.
        e = embedder.Embedder.__new__(embedder.Embedder)
        e.profile = dict(embedder.PROFILE)
        e._metal = None
        e.lane = embedder.LANE_CPU
        with mock.patch.object(mlx_embed, "available",
                               return_value=(False, "mlx missing: no module")), \
                self.assertRaisesRegex(embedder.EmbedderUnavailable, "mlx missing"):
            e._start_metal_lane(required=True)
        self.assertIsNone(e._metal)


class CrossLaneRefusal(unittest.TestCase):
    """A store whose lane cannot open here is refused, never served differently."""

    def test_guard_refuses_a_metal_store_when_metal_is_closed(self) -> None:
        import ask
        metal = embedder.profile_string(embedder.LANE_METAL)
        with mock.patch.object(mlx_embed, "available",
                               return_value=(False, "disabled by AGREP_MLX=off")):
            with self.assertRaises(RuntimeError) as caught:
                ask._guard_embedder(metal, "message index", "embed.py")
        message = str(caught.exception)
        self.assertIn("different vector space", message)
        # The refusal has to name the lane and the lever, or the owner is told
        # only that something is wrong with an index they cannot see.
        self.assertIn(embedder.LANE_METAL, message)
        self.assertIn("--full", message)

    def test_guard_serves_a_metal_store_when_metal_is_open(self) -> None:
        import ask
        metal = embedder.profile_string(embedder.LANE_METAL)
        with mock.patch.object(mlx_embed, "available", return_value=(True, "ok")):
            ask._guard_embedder(metal, "message index", "embed.py")

    def test_coherence_reports_profile_mismatch_for_an_unreachable_lane(self) -> None:
        import semantic
        metal = embedder.profile_string(embedder.LANE_METAL)
        with mock.patch.object(mlx_embed, "available", return_value=(True, "ok")):
            self.assertEqual(semantic._active_embedding_profile(metal)[1], metal)
        with mock.patch.object(mlx_embed, "available",
                               return_value=(False, "disabled by AGREP_MLX=off")):
            self.assertEqual(semantic._active_embedding_profile(metal)[1],
                             embedder.PROFILE_STRING)


class LaneDisclosure(unittest.TestCase):
    """Parity is not identity, and every surface that mentions the lane says so.

    The lane clears a 0.999 parity floor and STILL flipped 3 of 45 fixture
    queries at their thresholds. So "parity 0.99933" alone is the sentence a
    reader mistakes for "same results", and a store built here is approximate
    in a way only embeddings.meta used to record.
    """

    def test_open_line_states_the_divergence_beside_the_number(self) -> None:
        e = embedder.Embedder.__new__(embedder.Embedder)
        e.profile = dict(embedder.PROFILE)
        e._metal = None
        e.lane = embedder.LANE_CPU
        said: list[str] = []
        with mock.patch.object(mlx_embed, "available", return_value=(True, "ok")), \
                mock.patch.object(mlx_embed, "MLXEmbedder", return_value=object()), \
                mock.patch.object(embedder.Embedder, "_metal_parity",
                                  return_value=0.99933), \
                mock.patch.object(embedder.common, "dbg",
                                  side_effect=lambda text, *a, **k: said.append(text)):
            e._start_metal_lane()
        self.assertEqual(e.lane, embedder.LANE_METAL)
        line = " ".join(said)
        self.assertIn("0.99933", line)      # the measured number is not softened
        self.assertIn("near-threshold", line)
        self.assertIn("may differ from cpu", line)

    def test_doctor_gives_a_metal_store_its_own_row(self) -> None:
        import doctor
        metal = embedder.profile_string(embedder.LANE_METAL)
        with mock.patch.object(doctor, "_store_embedding_identity",
                               return_value=metal):
            row = doctor._store_lane_notice()
        self.assertIsNotNone(row)
        self.assertIn(embedder.LANE_METAL, row)
        self.assertIn("near-threshold", row)
        self.assertIn("cpu", row)

    def test_doctor_says_nothing_about_an_ordinary_cpu_store(self) -> None:
        # The bare profile string is every pre-lane store on disk; a row here
        # would tell millions of ordinary stores they are approximate.
        import doctor
        for identity in (embedder.PROFILE_STRING, None, ""):
            with mock.patch.object(doctor, "_store_embedding_identity",
                                   return_value=identity):
                self.assertIsNone(doctor._store_lane_notice())

    def test_doctors_suffix_parse_tracks_the_embedders_mapping(self) -> None:
        # doctor parses the suffix instead of calling embedder.lane_of, because
        # the routine tier must not import numpy to read a string. That
        # duplication is only safe while these two agree.
        import doctor
        self.assertTrue(embedder.profile_string(embedder.LANE_METAL).endswith(
            doctor._LANE_IDENTITY_SUFFIX + embedder.LANE_METAL))
        self.assertNotIn(doctor._LANE_IDENTITY_SUFFIX, embedder.PROFILE_STRING)


class LaneChangeRebuild(unittest.TestCase):
    """A lane change re-embeds EVERY row, and used to do it in total silence.

    Only a semantic-embed.log line marked it, so on a 44k-row corpus the
    interactive surfaces showed minutes of partial coverage that read exactly
    like the ordinary newest-first catch-up it is not.
    """

    def setUp(self) -> None:
        import semantic
        self.state = semantic.embed_state_path()
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self.state.unlink, True)
        self.change = {"from": embedder.LANE_METAL, "to": embedder.LANE_CPU}

    def _record(self, record: dict) -> None:
        self.state.write_text(json.dumps(record), encoding="utf-8")

    def test_an_unfinished_rebuild_reports_both_lanes(self) -> None:
        import semantic
        self._record({"state": "partial", "lane_change": self.change})
        self.assertEqual(semantic.lane_change_rebuild(), self.change)

    def test_a_finished_rebuild_stops_speaking(self) -> None:
        # "ready" is the publish that completed coverage: the wait is over.
        import semantic
        self._record({"state": "ready", "lane_change": self.change})
        self.assertIsNone(semantic.lane_change_rebuild())

    def test_an_ordinary_partial_refresh_claims_no_lane_change(self) -> None:
        import semantic
        self._record({"state": "partial", "indexed": 128, "total": 44000})
        self.assertIsNone(semantic.lane_change_rebuild())
        self._record({"state": "running", "lane_change": {"from": "x"}})
        self.assertIsNone(semantic.lane_change_rebuild())

    def test_a_caller_holding_the_state_does_not_pay_a_second_read(self) -> None:
        # The incomplete-coverage footer reads this record for its freshness
        # stamp a few lines above the attribution, and one render owes the
        # reader one read - a budget the search surface pins by call count.
        import semantic
        self._record({"state": "ready"})   # what a re-read would wrongly say
        held = {"state": "partial", "lane_change": self.change}
        with mock.patch.object(semantic, "read_embed_state",
                               side_effect=AssertionError("state re-read")):
            self.assertEqual(semantic.lane_change_rebuild(held), self.change)

    def test_the_notice_names_the_lanes_and_the_background_work(self) -> None:
        import surface_policy as surface
        line = surface.semantic_lane_change_notice(self.change)
        self.assertIn(embedder.LANE_METAL, line)
        self.assertIn(embedder.LANE_CPU, line)
        self.assertIn("re-embedding history in the background", line)
        self.assertIsNone(surface.semantic_lane_change_notice(None))

    def test_only_a_lane_change_is_recorded_never_a_model_change(self) -> None:
        # A new model already reads as a new embedder. Claiming "lane changed"
        # for one would attribute the rebuild to the wrong cause entirely.
        import embed
        metal = embedder.profile_string(embedder.LANE_METAL)
        with mock.patch.object(embed, "_LANE_CHANGE", None):
            embed._note_lane_change(metal, embedder.PROFILE_STRING)
            self.assertEqual(embed._LANE_CHANGE, self.change)
        with mock.patch.object(embed, "_LANE_CHANGE", None):
            embed._note_lane_change("other-model@0123456789ab/x:seq512",
                                    embedder.PROFILE_STRING)
            self.assertIsNone(embed._LANE_CHANGE)

    def test_running_states_carry_the_reason_and_ready_drops_it(self) -> None:
        import embed
        with mock.patch.object(embed, "_LANE_CHANGE", self.change):
            embed._publish_state({"state": "running"})
            running = json.loads(self.state.read_text(encoding="utf-8"))
            embed._publish_state({"state": "partial", "indexed": 128})
            partial = json.loads(self.state.read_text(encoding="utf-8"))
            embed._publish_state({"state": "ready", "indexed": 44000})
            done = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(running["lane_change"], self.change)
        self.assertEqual(partial["lane_change"], self.change)
        self.assertNotIn("lane_change", done)

    def test_later_passes_inherit_the_reason_they_did_not_discover(self) -> None:
        # Passes 2..n plan as ordinary increments: without inheritance the
        # attribution vanishes partway through the rebuild it describes.
        import embed
        self._record({"state": "partial", "lane_change": self.change})
        self.assertEqual(embed._inherited_lane_change(), self.change)
        self._record({"state": "ready", "lane_change": self.change})
        self.assertIsNone(embed._inherited_lane_change())
        self._record({"state": "partial"})
        self.assertIsNone(embed._inherited_lane_change())


class VectorSpaceOnRealHardware(unittest.TestCase):
    """The only test that needs a GPU; it is the one that matters most."""

    def test_metal_and_cpu_agree_on_real_text(self) -> None:
        ok, reason = mlx_embed.available()
        if not ok:
            self.skipTest(f"metal lane unavailable here: {reason}")
        try:
            e = embedder.Embedder(download=False, lane=embedder.LANE_METAL)
        except embedder.EmbedderUnavailable as exc:
            self.skipTest(f"metal lane unavailable: {exc}")
        if e._metal is None:
            self.skipTest("metal lane did not open on this box")
        texts = list(embedder._METAL_PROBES)
        metal = e.embed_texts(texts)
        e._metal = None
        cpu = e.embed_texts(texts)
        worst = float((metal * cpu).sum(axis=1).min())
        self.assertGreaterEqual(worst, embedder._METAL_MIN_COSINE)


if __name__ == "__main__":
    unittest.main()
