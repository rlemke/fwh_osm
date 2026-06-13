package osm.ghrouter;

import afl.agent.Handler;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.BiConsumer;

/**
 * Handler for {@code osm.Routing.GraphHopper.RouteBatch}.
 *
 * Params:
 *   graph   — a GraphHopperCache dict (uses {@code graphDir} = s3:// graph dir,
 *             {@code profile}).
 *   pairs   — JSON array of origin/destination pairs:
 *             [{"from":{"lat":..,"lon":..,"name":".."},"to":{...}}, ...]
 *   profile — optional override (defaults to the graph's profile, else "car").
 *
 * Returns {@code result}: a BatchRouteResult dict with the routes GeoJSON path
 * (finalized to MinIO so RenderMap on any host can read it) + summary stats.
 */
public final class RouteBatchHandler implements Handler {

    private final GraphHopperPool pool;
    private final S3Util s3;
    private final ObjectMapper json = new ObjectMapper();
    private final String outputBase;

    public RouteBatchHandler(GraphHopperPool pool, S3Util s3) {
        this.pool = pool;
        this.s3 = s3;
        this.outputBase = S3Util.env("AFL_OSM_OUTPUT_BASE", "s3://afl-cache/osm-output");
    }

    @Override
    @SuppressWarnings("unchecked")
    public Map<String, Object> handle(Map<String, Object> params) throws Exception {
        BiConsumer<String, String> log = (BiConsumer<String, String>) params.get("_step_log");

        Map<String, Object> graph = (Map<String, Object>) params.get("graph");
        if (graph == null || graph.get("graphDir") == null) {
            throw new IllegalArgumentException("RouteBatch: missing graph.graphDir");
        }
        String graphDir = String.valueOf(graph.get("graphDir"));
        String profile = str(params.get("profile"),
                str(graph.get("profile"), "car"));

        String pairsJson = (String) params.get("pairs");
        if (pairsJson == null || pairsJson.isBlank()) {
            throw new IllegalArgumentException("RouteBatch: missing pairs JSON");
        }
        List<Map<String, Object>> pairs = json.readValue(pairsJson, List.class);
        stepLog(log, "RouteBatch: " + pairs.size() + " pairs over " + graphDir + " (profile=" + profile + ")", "info");

        List<Map<String, Object>> features = new ArrayList<>();
        double totalKm = 0.0;
        double totalMin = 0.0;
        int routed = 0;
        int failed = 0;

        for (Map<String, Object> pair : pairs) {
            Map<String, Object> from = (Map<String, Object>) pair.get("from");
            Map<String, Object> to = (Map<String, Object>) pair.get("to");
            double fLat = num(from.get("lat")), fLon = num(from.get("lon"));
            double tLat = num(to.get("lat")), tLon = num(to.get("lon"));
            String fName = str(from.get("name"), "");
            String tName = str(to.get("name"), "");
            try {
                GraphHopperPool.Route r = pool.route(graphDir, profile, fLat, fLon, tLat, tLon);
                double km = r.meters() / 1000.0;
                double min = r.millis() / 60000.0;
                totalKm += km;
                totalMin += min;
                routed++;

                List<List<Double>> coords = new ArrayList<>();
                for (double[] c : r.lonLat()) {
                    coords.add(List.of(c[0], c[1]));
                }
                Map<String, Object> props = new LinkedHashMap<>();
                props.put("from", fName);
                props.put("to", tName);
                props.put("distance_km", round(km, 2));
                props.put("duration_min", round(min, 1));
                Map<String, Object> geom = new LinkedHashMap<>();
                geom.put("type", "LineString");
                geom.put("coordinates", coords);
                Map<String, Object> feat = new LinkedHashMap<>();
                feat.put("type", "Feature");
                feat.put("geometry", geom);
                feat.put("properties", props);
                features.add(feat);
            } catch (Exception e) {
                failed++;
                stepLog(log, "RouteBatch: no route " + fName + " -> " + tName + ": " + e.getMessage(), "info");
            }
        }

        Map<String, Object> fc = new LinkedHashMap<>();
        fc.put("type", "FeatureCollection");
        fc.put("features", features);

        Path tmp = Files.createTempFile("gh-routes-", ".geojson");
        json.writeValue(tmp.toFile(), fc);
        String region = regionSlug(graphDir);
        String outUri = outputBase.replaceAll("/+$", "")
                + "/routes/" + region + "_" + profile + "_routes.geojson";
        s3.uploadFile(tmp, outUri);
        Files.deleteIfExists(tmp);

        stepLog(log, "RouteBatch: " + routed + " routes ("
                + failed + " unroutable), " + Math.round(totalKm) + " km -> " + outUri, "success");

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("output_path", outUri);
        result.put("route_count", (long) routed);
        result.put("failed_count", (long) failed);
        result.put("total_distance_km", round(totalKm, 1));
        result.put("total_duration_min", round(totalMin, 1));
        result.put("profile", profile);
        result.put("format", "GeoJSON");
        return Map.of("result", result);
    }

    private static void stepLog(BiConsumer<String, String> log, String msg, String level) {
        if (log != null) {
            log.accept(msg, level);
        } else {
            System.out.println("[gh-router] " + msg);
        }
    }

    private static double num(Object o) {
        return ((Number) o).doubleValue();
    }

    private static String str(Object o, String dflt) {
        return o == null ? dflt : String.valueOf(o);
    }

    private static double round(double v, int dp) {
        double f = Math.pow(10, dp);
        return Math.round(v * f) / f;
    }

    /** ".../graphhopper/north-america/us/california-latest/car" -> "california". */
    private static String regionSlug(String graphDir) {
        String s = graphDir.replaceAll("/+$", "");
        String[] parts = s.split("/");
        for (String p : parts) {
            if (p.endsWith("-latest")) {
                return p.substring(0, p.length() - "-latest".length());
            }
        }
        return parts.length >= 2 ? parts[parts.length - 2] : "region";
    }
}
