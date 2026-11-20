import unittest
from unittest.mock import patch

import build_index


PKG = "com.example.app"
ORIGINAL, CURRENT, NEXT = "a" * 64, "b" * 64, "c" * 64
APK = b"test APK"


class SigningPinTest(unittest.TestCase):
    def setUp(self):
        handovers = patch.object(build_index, "HANDOVERS", [])
        self.handovers = handovers.start()
        self.addCleanup(handovers.stop)
        verifier = patch.object(build_index, "verify_rotation", return_value=[ORIGINAL, CURRENT])
        self.verify = verifier.start()
        self.addCleanup(verifier.stop)

    def resolve(self, signer, prev, blob=APK):
        return build_index.resolve_signing_pin(PKG, signer, prev, blob)

    def test_first_sighting_and_legacy_handover_have_no_root(self):
        self.assertEqual(self.resolve(ORIGINAL, {}), (ORIGINAL, []))
        self.assertIsNone(self.resolve(CURRENT, {"signer": ORIGINAL}))
        self.handovers.append((PKG, ORIGINAL, CURRENT, "listing moved"))
        self.assertEqual(self.resolve(CURRENT, {"signer": ORIGINAL}), (CURRENT, []))
        self.verify.assert_not_called()

    def test_first_rotation_requires_verified_apk_and_exact_pin(self):
        self.handovers.append((PKG, ORIGINAL, CURRENT, "rotation", ORIGINAL))
        prev = {"signer": ORIGINAL}
        self.assertIsNone(self.resolve(NEXT, prev))
        self.assertIsNone(self.resolve("", prev))
        self.assertIsNone(self.resolve(CURRENT, prev, blob=None))
        self.verify.return_value = None
        self.assertIsNone(self.resolve(CURRENT, prev))
        self.verify.return_value = [ORIGINAL, CURRENT]
        self.assertEqual(self.resolve(CURRENT, prev), (CURRENT, [ORIGINAL, CURRENT]))
        self.verify.assert_called_with(APK, CURRENT, [ORIGINAL])

    def test_root_survives_rebuilds_and_applies_to_new_downloads(self):
        history = [ORIGINAL, CURRENT]
        prev = {"signer": CURRENT, "signerRoot": ORIGINAL, "signerHistory": history}
        self.assertEqual(self.resolve(CURRENT, prev, blob=None), (CURRENT, history))
        self.verify.assert_not_called()
        self.assertEqual(self.resolve(CURRENT, prev), (CURRENT, history))
        self.verify.assert_called_once_with(APK, CURRENT, history)
        self.assertIsNone(self.resolve("", prev))
        self.assertIsNone(self.resolve(ORIGINAL, prev))

    def test_second_rotation_keeps_original_root(self):
        prev = {"signer": CURRENT, "signerRoot": ORIGINAL, "signerHistory": [ORIGINAL, CURRENT]}
        self.handovers.append((PKG, CURRENT, NEXT, "rotation", CURRENT))
        self.assertIsNone(self.resolve(NEXT, prev))
        self.handovers[:] = [(PKG, CURRENT, NEXT, "rotation", ORIGINAL)]
        self.verify.return_value = [ORIGINAL, CURRENT, NEXT]
        self.assertEqual(self.resolve(NEXT, prev), (NEXT, [ORIGINAL, CURRENT, NEXT]))
        self.verify.assert_called_once_with(APK, NEXT, [ORIGINAL, CURRENT])


class VerifyRotationTest(unittest.TestCase):
    def test_preserves_the_full_history_for_new_rotations_and_same_key_releases(self):
        for history, prior, expected in [
            ([ORIGINAL, CURRENT, NEXT], [ORIGINAL, CURRENT], [ORIGINAL, CURRENT, NEXT]),
            ([ORIGINAL, CURRENT, NEXT], [ORIGINAL, CURRENT, NEXT], [ORIGINAL, CURRENT, NEXT]),
            ([ORIGINAL, NEXT], [ORIGINAL, CURRENT], None),
            ([ORIGINAL, NEXT], [ORIGINAL, CURRENT, NEXT], None),
            ([CURRENT, NEXT], [ORIGINAL, CURRENT], None),
            ([ORIGINAL, CURRENT], [ORIGINAL, CURRENT], None),
            ([], [ORIGINAL, CURRENT], None),
        ]:
            with self.subTest(history=history):
                with patch.object(build_index.subprocess, "check_output", return_value="\n".join(history)):
                    self.assertEqual(build_index.verify_rotation(APK, NEXT, prior), expected)

    def test_rejects_when_verifier_cannot_run(self):
        with patch.object(build_index.subprocess, "check_output", side_effect=OSError):
            self.assertIsNone(build_index.verify_rotation(APK, NEXT, [ORIGINAL, CURRENT]))
