package osm.ghrouter;

import com.graphhopper.GHRequest;
import com.graphhopper.GHResponse;
import com.graphhopper.GraphHopper;
import com.graphhopper.ResponsePath;
import com.graphhopper.config.Profile;
import com.graphhopper.util.CustomModel;
import com.graphhopper.util.PointList;

import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Keeps one loaded {@link GraphHopper} instance per graph directory, warm in the
 * JVM. The first RouteBatch for a graph pulls it from MinIO and loads it (seconds
 * + heap); every later request reuses the in-memory instance — the "read-once,
 * route-in-memory" model, just with a real GraphHopper graph instead of a tiny
 * noded network. Thread-safe: concurrent first-touches for the same graph
 * serialize on a per-graph lock.
 */
public final class GraphHopperPool implements AutoCloseable {

    private final S3Util s3;
    private final Path graphsRoot;
    private final ConcurrentHashMap<String, GraphHopper> loaded = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<String, Object> locks = new ConcurrentHashMap<>();

    public GraphHopperPool(S3Util s3) {
        this.s3 = s3;
        String scratch = S3Util.env("FW_LOCAL_SCRATCH", System.getProperty("java.io.tmpdir"));
        this.graphsRoot = Paths.get(scratch, "gh-graphs");
    }

    /** A single routed path: geometry as [lon,lat] pairs, plus distance/time. */
    public record Route(double[][] lonLat, double meters, long millis) {}

    /**
     * Routes one origin/destination pair over the given graph (an ``s3://`` graph
     * dir built by BuildAllStateGraphs), loading + caching the graph on first use.
     */
    public Route route(String graphDirS3, String profile,
                       double fromLat, double fromLon, double toLat, double toLon) throws Exception {
        GraphHopper hopper = getOrLoad(graphDirS3, profile);
        GHRequest req = new GHRequest(fromLat, fromLon, toLat, toLon).setProfile(profile);
        GHResponse rsp = hopper.route(req);
        if (rsp.hasErrors()) {
            throw new RuntimeException("route " + fromLat + "," + fromLon + " -> "
                    + toLat + "," + toLon + ": " + rsp.getErrors());
        }
        ResponsePath best = rsp.getBest();
        PointList pts = best.getPoints();
        double[][] coords = new double[pts.size()][2];
        for (int i = 0; i < pts.size(); i++) {
            coords[i][0] = pts.getLon(i);
            coords[i][1] = pts.getLat(i);
        }
        return new Route(coords, best.getDistance(), best.getTime());
    }

    private GraphHopper getOrLoad(String graphDirS3, String profile) throws Exception {
        GraphHopper existing = loaded.get(graphDirS3);
        if (existing != null) {
            return existing;
        }
        Object lock = locks.computeIfAbsent(graphDirS3, k -> new Object());
        synchronized (lock) {
            existing = loaded.get(graphDirS3);
            if (existing != null) {
                return existing;
            }
            // The prebuilt graph was created by graphhopper-web, whose encoded-value/
            // profile config hash a hand-built graphhopper-core Profile can't reproduce
            // (load fails "Profiles do not match"). Instead, import the region's PBF
            // here with OUR profile: build + route then run identical graphhopper-core
            // code, so the load always matches. Imported once per region, cached
            // in-JVM (and on local disk, so a restart re-loads rather than re-imports).
            String pbfUri = pbfUriForGraph(graphDirS3);
            Path pbf = graphsRoot.resolve("pbf").resolve(slug(pbfUri) + ".osm.pbf");
            System.out.println("[gh-router] localizing pbf " + pbfUri);
            s3.downloadFile(pbfUri, pbf);
            Path importDir = graphsRoot.resolve("imported_" + slug(graphDirS3));

            GraphHopper hopper = new GraphHopper();
            hopper.setOSMFile(pbf.toString());
            hopper.setGraphHopperLocation(importDir.toString());
            hopper.setProfiles(new Profile(profile).setVehicle(profile));
            System.out.println("[gh-router] import-or-load " + profile + " from " + pbf.getFileName() + " -> " + importDir);
            try {
                // imports if importDir is empty, else loads it (same code -> matches)
                hopper.importOrLoad();
            } catch (Throwable t) {
                System.out.println("[gh-router] IMPORT/LOAD FAILED for " + graphDirS3 + ": " + t);
                t.printStackTrace();
                throw new RuntimeException("GraphHopper import/load failed for " + graphDirS3
                        + " (profile=" + profile + "): " + t, t);
            }
            loaded.put(graphDirS3, hopper);
            System.out.println("[gh-router] ready: " + graphDirS3 + " (profile=" + profile + ")");
            return hopper;
        }
    }

    /** s3://…/graphhopper/<region>-latest/<profile> -> s3://…/pbf/<region>-latest.osm.pbf */
    private static String pbfUriForGraph(String graphDirS3) {
        String s = graphDirS3.replaceAll("/+$", "");
        int lastSlash = s.lastIndexOf('/');               // drop trailing "/<profile>"
        String withoutProfile = lastSlash > 0 ? s.substring(0, lastSlash) : s;
        return withoutProfile.replace("/graphhopper/", "/pbf/") + ".osm.pbf";
    }

    private static String slug(String s3uri) {
        return s3uri.replace("s3://", "").replaceAll("[^A-Za-z0-9._-]", "_");
    }

    @Override
    public void close() {
        loaded.values().forEach(GraphHopper::close);
        loaded.clear();
    }
}
