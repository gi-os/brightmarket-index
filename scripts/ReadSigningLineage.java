import com.android.apksig.ApkVerifier;
import java.io.File;
import java.security.MessageDigest;
import java.util.HexFormat;

class ReadSigningLineage {
    private static final int LIGHT_PHONE_API = 30;

    public static void main(String[] args) throws Exception {
        var result = new ApkVerifier.Builder(new File(args[0]))
                .setMinCheckedPlatformVersion(LIGHT_PHONE_API)
                .setMaxCheckedPlatformVersion(LIGHT_PHONE_API)
                .build()
                .verify();
        if (!result.isVerified() || !result.isVerifiedUsingV3Scheme()) {
            return;
        }
        var signers = result.getV3SchemeSigners().stream()
                .filter(s -> s.getMinSdkVersion() <= LIGHT_PHONE_API && LIGHT_PHONE_API <= s.getMaxSdkVersion())
                .toList();
        if (signers.size() != 1) {
            return;
        }
        var signer = signers.get(0);
        var lineage = signer.getSigningCertificateLineage();
        if (lineage == null || lineage.size() < 2 || !lineage.isCertificateLatestInLineage(signer.getCertificate())) {
            return;
        }
        var certs = lineage.getCertificatesInLineage();
        for (var retired : certs.subList(0, certs.size() - 1)) {
            var caps = lineage.getSignerCapabilities(retired);
            if (!caps.hasInstalledData() || caps.hasRollback()) {
                return;
            }
        }
        var sha256 = MessageDigest.getInstance("SHA-256");
        for (var cert : certs) {
            System.out.println(HexFormat.of().formatHex(sha256.digest(cert.getEncoded())));
        }
    }
}
