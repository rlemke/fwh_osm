package osm.ghrouter;

import com.graphhopper.GHRequest;
import com.graphhopper.GHResponse;
import com.graphhopper.GraphHopper;
import com.graphhopper.ResponsePath;
import com.graphhopper.config.Profile;
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
        String scratch = S3Util.env("AFL_LOCAL_SCRATCH", System.getProperty("java.io.tmpdir"));
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
            Path local = graphsRoot.resolve(slug(graphDirS3));
            // (Re)download if the graph dir isn't already staged locally.
            if (!Files.isDirectory(local) || !Files.exists(local.resolve("nodes"))) {
                int n = s3.downloadPrefix(graphDirS3, local);
                System.out.println("[gh-router] localized " + n + " graph files for " + graphDirS3 + " -> " + local);
            }
            GraphHopper hopper = new GraphHopper();
            hopper.setGraphHopperLocation(local.toString());
            // Profile MUST match what the graph was built with (config: name=<profile>,
            // vehicle=<profile>). No OSM file is set, so importOrLoad() loads the
            // existing graph rather than re-importing.
            hopper.setProfiles(new Profile(profile).setVehicle(profile));
            hopper.setAllowWrites(false);
            hopper.importOrLoad();
            loaded.put(graphDirS3, hopper);
            System.out.println("[gh-router] loaded graph " + graphDirS3 + " (profile=" + profile + ")");
            return hopper;
        }
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
