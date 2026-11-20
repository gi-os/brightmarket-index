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
        verified = ("Verified using v3 scheme (APK Signature Scheme v3): true\n"
                    f"Signer #1 certificate SHA-256 digest: {NEXT}\n")
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
                lineage = "".join(
                    f"Signer #{i} in lineage certificate SHA-256 digest: {cert}\n"
                    "Has installed data capability: true\nHas rollback capability      : false\n"
                    for i, cert in enumerate(history, 1)
                )
                with patch.object(build_index.subprocess, "check_output", side_effect=[verified, lineage]):
                    self.assertEqual(build_index.verify_rotation(APK, NEXT, prior), expected)

    def test_requires_update_capability_without_rollback_for_every_retired_signer(self):
        verified = ("Verified using v3 scheme (APK Signature Scheme v3): true\n"
                    f"Signer #1 certificate SHA-256 digest: {NEXT}\n")
        for installed_data, rollback in [("false", "false"), ("true", "true")]:
            with self.subTest(installed_data=installed_data, rollback=rollback):
                lineage = (
                    f"Signer #1 in lineage certificate SHA-256 digest: {ORIGINAL}\n"
                    "Has installed data capability: true\nHas rollback capability      : false\n"
                    f"Signer #2 in lineage certificate SHA-256 digest: {CURRENT}\n"
                    f"Has installed data capability: {installed_data}\nHas rollback capability      : {rollback}\n"
                    f"Signer #3 in lineage certificate SHA-256 digest: {NEXT}\n"
                )
                with patch.object(build_index.subprocess, "check_output", side_effect=[verified, lineage]):
                    self.assertIsNone(build_index.verify_rotation(APK, NEXT, [ORIGINAL, CURRENT]))

    def test_rejects_multiple_current_signers(self):
        verified = ("Verified using v3 scheme (APK Signature Scheme v3): true\n"
                    f"Signer #1 certificate SHA-256 digest: {NEXT}\n"
                    f"Signer #2 certificate SHA-256 digest: {CURRENT}\n")
        with patch.object(build_index.subprocess, "check_output", return_value=verified):
            self.assertIsNone(build_index.verify_rotation(APK, NEXT, [ORIGINAL, CURRENT]))

    def test_rejects_a_signature_without_v3(self):
        verified = f"Signer #1 certificate SHA-256 digest: {NEXT}\n"
        with patch.object(build_index.subprocess, "check_output", return_value=verified):
            self.assertIsNone(build_index.verify_rotation(APK, NEXT, [ORIGINAL, CURRENT]))
