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
        verifier = patch.object(build_index, "verify_rotation", return_value=True)
        self.verify = verifier.start()
        self.addCleanup(verifier.stop)

    def resolve(self, signer, prev, blob=APK):
        return build_index.resolve_signing_pin(PKG, signer, prev, blob)

    def test_first_sighting_and_legacy_handover_have_no_root(self):
        self.assertEqual(self.resolve(ORIGINAL, {}), (ORIGINAL, ""))
        self.assertIsNone(self.resolve(CURRENT, {"signer": ORIGINAL}))
        self.handovers.append((PKG, ORIGINAL, CURRENT, "listing moved"))
        self.assertEqual(self.resolve(CURRENT, {"signer": ORIGINAL}), (CURRENT, ""))
        self.verify.assert_not_called()

    def test_first_rotation_requires_verified_apk_and_exact_pin(self):
        self.handovers.append((PKG, ORIGINAL, CURRENT, "rotation", ORIGINAL))
        prev = {"signer": ORIGINAL}
        self.assertIsNone(self.resolve(NEXT, prev))
        self.assertIsNone(self.resolve("", prev))
        self.assertIsNone(self.resolve(CURRENT, prev, blob=None))
        self.verify.return_value = False
        self.assertIsNone(self.resolve(CURRENT, prev))
        self.verify.return_value = True
        self.assertEqual(self.resolve(CURRENT, prev), (CURRENT, ORIGINAL))
        self.verify.assert_called_with(APK, CURRENT, ORIGINAL, ORIGINAL)

    def test_root_survives_rebuilds_and_applies_to_new_downloads(self):
        prev = {"signer": CURRENT, "signerRoot": ORIGINAL}
        self.assertEqual(self.resolve(CURRENT, prev, blob=None), (CURRENT, ORIGINAL))
        self.verify.assert_not_called()
        self.assertEqual(self.resolve(CURRENT, prev), (CURRENT, ORIGINAL))
        self.verify.assert_called_once_with(APK, CURRENT, ORIGINAL, CURRENT)
        self.assertIsNone(self.resolve("", prev))
        self.assertIsNone(self.resolve(ORIGINAL, prev))

    def test_second_rotation_keeps_original_root(self):
        prev = {"signer": CURRENT, "signerRoot": ORIGINAL}
        self.handovers.append((PKG, CURRENT, NEXT, "rotation", CURRENT))
        self.assertIsNone(self.resolve(NEXT, prev))
        self.handovers[:] = [(PKG, CURRENT, NEXT, "rotation", ORIGINAL)]
        self.assertEqual(self.resolve(NEXT, prev), (NEXT, ORIGINAL))
        self.verify.assert_called_once_with(APK, NEXT, ORIGINAL, CURRENT)


class VerifyRotationTest(unittest.TestCase):
    def test_requires_exact_current_signer_root_and_ancestor(self):
        verified = ("Verified using v3 scheme (APK Signature Scheme v3): true\n"
                    f"Signer #1 certificate SHA-256 digest: {NEXT}\n")
        for history, expected in [
            ([ORIGINAL, CURRENT, NEXT], True),
            ([ORIGINAL, NEXT], False),
            ([CURRENT, NEXT], False),
            ([ORIGINAL, CURRENT], False),
            ([], False),
        ]:
            with self.subTest(history=history):
                lineage = "".join(
                    f"Signer #{i} in lineage certificate SHA-256 digest: {cert}\n"
                    for i, cert in enumerate(history, 1)
                )
                with patch.object(build_index.subprocess, "check_output", side_effect=[verified, lineage]):
                    self.assertIs(build_index.verify_rotation(APK, NEXT, ORIGINAL, CURRENT), expected)

    def test_rejects_multiple_current_signers(self):
        verified = ("Verified using v3 scheme (APK Signature Scheme v3): true\n"
                    f"Signer #1 certificate SHA-256 digest: {NEXT}\n"
                    f"Signer #2 certificate SHA-256 digest: {CURRENT}\n")
        with patch.object(build_index.subprocess, "check_output", return_value=verified):
            self.assertFalse(build_index.verify_rotation(APK, NEXT, ORIGINAL, CURRENT))

    def test_rejects_a_signature_without_v3(self):
        verified = f"Signer #1 certificate SHA-256 digest: {NEXT}\n"
        with patch.object(build_index.subprocess, "check_output", return_value=verified):
            self.assertFalse(build_index.verify_rotation(APK, NEXT, ORIGINAL, CURRENT))
