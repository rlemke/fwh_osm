package osm.ghrouter;

import software.amazon.awssdk.auth.credentials.AwsBasicCredentials;
import software.amazon.awssdk.auth.credentials.StaticCredentialsProvider;
import software.amazon.awssdk.core.sync.RequestBody;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.S3Configuration;
import software.amazon.awssdk.services.s3.model.GetObjectRequest;
import software.amazon.awssdk.services.s3.model.ListObjectsV2Request;
import software.amazon.awssdk.services.s3.model.ListObjectsV2Response;
import software.amazon.awssdk.services.s3.model.PutObjectRequest;
import software.amazon.awssdk.services.s3.model.S3Object;

import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;

/**
 * Minimal S3/MinIO helper for pulling a GraphHopper graph directory down to
 * local disk (GraphHopper reads a local dir) and pushing the routes GeoJSON
 * back. Mirrors the Python S3Storage: endpoint + path-style + static creds from
 * the AFL_S3_* env that the osm runners already use.
 */
public final class S3Util implements AutoCloseable {

    private final S3Client s3;

    public S3Util() {
        String endpoint = env("AFL_S3_ENDPOINT", "http://afl-minio:9000");
        String region = env("AFL_S3_REGION", "us-east-1");
        String access = env("AFL_S3_ACCESS_KEY", "minioadmin");
        String secret = env("AFL_S3_SECRET_KEY", "minioadmin");
        this.s3 = S3Client.builder()
                .endpointOverride(URI.create(endpoint))
                .region(Region.of(region))
                .credentialsProvider(StaticCredentialsProvider.create(
                        AwsBasicCredentials.create(access, secret)))
                // MinIO needs path-style (bucket in the path, not the host).
                .serviceConfiguration(S3Configuration.builder().pathStyleAccessEnabled(true).build())
                .build();
    }

    /** Splits an ``s3://bucket/key`` URI into {bucket, key}. */
    static String[] split(String s3uri) {
        if (!s3uri.startsWith("s3://")) {
            throw new IllegalArgumentException("not an s3 uri: " + s3uri);
        }
        String rest = s3uri.substring("s3://".length());
        int slash = rest.indexOf('/');
        if (slash < 0) {
            return new String[]{rest, ""};
        }
        return new String[]{rest.substring(0, slash), rest.substring(slash + 1)};
    }

    /**
     * Downloads every object under {@code s3Dir} (a prefix) into {@code localDir},
     * preserving the relative path so the GraphHopper directory layout is intact.
     * Returns the number of files fetched.
     */
    public int downloadPrefix(String s3Dir, Path localDir) throws Exception {
        String[] bk = split(s3Dir);
        String bucket = bk[0];
        String prefix = bk[1].endsWith("/") ? bk[1] : bk[1] + "/";
        Files.createDirectories(localDir);

        int count = 0;
        String token = null;
        do {
            ListObjectsV2Request.Builder req = ListObjectsV2Request.builder()
                    .bucket(bucket).prefix(prefix);
            if (token != null) {
                req.continuationToken(token);
            }
            ListObjectsV2Response resp = s3.listObjectsV2(req.build());
            for (S3Object obj : resp.contents()) {
                String key = obj.key();
                if (key.endsWith("/")) {
                    continue; // directory marker
                }
                String rel = key.substring(prefix.length());
                Path dst = localDir.resolve(rel);
                Files.createDirectories(dst.getParent());
                s3.getObject(GetObjectRequest.builder().bucket(bucket).key(key).build(), dst);
                count++;
            }
            token = Boolean.TRUE.equals(resp.isTruncated()) ? resp.nextContinuationToken() : null;
        } while (token != null);

        return count;
    }

    /** Downloads a single ``s3://`` object to a local file (size-checked cache
     *  hit: skips the download if the local file already matches the object). */
    public void downloadFile(String s3uri, Path dst) throws Exception {
        String[] bk = split(s3uri);
        Files.createDirectories(dst.getParent());
        if (Files.exists(dst)) {
            long remote = s3.headObject(b -> b.bucket(bk[0]).key(bk[1])).contentLength();
            if (Files.size(dst) == remote) {
                return; // already localized
            }
        }
        s3.getObject(GetObjectRequest.builder().bucket(bk[0]).key(bk[1]).build(), dst);
    }

    /** Uploads a local file to an ``s3://`` URI. */
    public void uploadFile(Path localFile, String s3uri) {
        String[] bk = split(s3uri);
        s3.putObject(
                PutObjectRequest.builder().bucket(bk[0]).key(bk[1]).build(),
                RequestBody.fromFile(localFile));
    }

    static String env(String key, String dflt) {
        String v = System.getenv(key);
        return (v == null || v.isEmpty()) ? dflt : v;
    }

    @Override
    public void close() {
        s3.close();
    }
}
